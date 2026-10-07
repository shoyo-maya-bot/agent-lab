#!/usr/bin/env python3
"""PR 単位の課題（bench/prs）を、構成ごとにレビューさせて比べる.

    python runner/pr_bench.py ../dev-environment results/2026-10-04-auto-review --backend claude-cli --yes
    python runner/pr_bench.py ../dev-environment results/... --configs auto,standard   # 構成を絞る
    python runner/pr_bench.py --summarize results/2026-10-04-auto-review              # 集計だけやり直す

    # v2 の課題（bench/prs_v2. 採点は人が目隠しで行う: runner/blind_sheet.py → runner/blind_score.py）
    python runner/pr_bench.py ../dev-environment results/<日付>-v2 --bench bench/prs_v2 --configs standard,deep,auto \
        --repeat 3 --backend anthropic --yes
    # 費用の見積もりだけ（モデルは呼ばない. API で直接呼んだときの額）
    python runner/pr_bench.py ../dev-environment --bench bench/prs_v2 --configs standard,deep,auto --repeat 3 --estimate-only

    # 記録した返答で流す（モデルは呼ばない・費用ゼロ. 配線の退行を見るだけで、モデルの良し悪しは測れない）
    python runner/pr_bench.py --diff-only out/replay --configs auto --cases P01,P05,P06,P08,N01,N02,N03,D01 \
        --replay bench/replay/auto-synthetic.jsonl --check bench/replay/thresholds.yaml

構成:
  light / standard / deep : そのモデル 1 回だけ（振り分けなし）
  auto                    : auto-review の振り分け（ふだん standard、危ない PR と MUST のときだけ deep を足す）

結果: <out>/raw/<case>-<config>.json（指摘と費用。生のログは残さない）と <out>/summary.md
"""

from __future__ import annotations

import argparse
import io
import json
import subprocess
import sys
import tarfile
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "auto-review" / "src"))
sys.path.insert(0, str(ROOT / "runner"))

from auto_review.backends import ReplayBackend, get_backend, price  # noqa: E402
from auto_review.diff import parse_diff  # noqa: E402
from auto_review.render import to_report  # noqa: E402
from auto_review.review import Call, Reviewer, _tokens  # noqa: E402
from auto_review.router import load_config, plan  # noqa: E402
from score import Finding as ScoreFinding  # noqa: E402
from score import matches  # noqa: E402

CONFIGS = ["light", "standard", "deep", "auto"]
DEFAULT_BENCH = ROOT / "bench" / "prs"
_GIT_LOCK = threading.Lock()  # 同じリポジトリから同時に取り出さない（念のため）


def load_bench(bench: Path = DEFAULT_BENCH) -> tuple[dict, Path]:
    """（cases.yaml の中身, 差分のフォルダ）. v2 は cases.yaml の diff_dir（tasks/）に差分がある."""
    spec = yaml.safe_load((bench / "cases.yaml").read_text(encoding="utf-8"))
    return spec, bench / spec.get("diff_dir", ".")


def git(repo: Path, *args: str, stdin: str | None = None) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], input=stdin, check=True, capture_output=True, text=True).stdout


def head_tree(repo: Path, base: str, diff: str, dest: Path) -> None:
    """base_commit に PR の差分を当てた「変更後」のファイル一式を dest に作る.

    元のリポジトリには書き込まない（worktree を作らず `git archive` で取り出す）.
    """
    data = subprocess.run(["git", "-C", str(repo), "archive", "--format=tar", base], check=True, capture_output=True).stdout
    dest.mkdir(parents=True)
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        tar.extractall(dest, filter="data")
    git(dest, "init", "-q")  # 差分のパスを dest からの相対で当てるため（外側のリポジトリに当てない）
    if diff.strip():
        git(dest, "apply", "-", stdin=diff)


def _review(case: dict, config: str, cfg: dict, backend, dry: bool, diff: str, wt: Path | None):
    note = f"# PR: {case['title']}\n\n{case.get('description', '')}\n\n"
    changes = parse_diff(diff)
    p = plan(changes, cfg)
    if isinstance(backend, ReplayBackend):
        backend = backend.for_case(case["id"])
    r = Reviewer(cfg, backend, repo=wt, dry_run=dry)
    result = r.auto(note + diff, changes, p) if config == "auto" else r.single(config, note + diff, changes, p)
    return p, result


def run_one(
    repo: Path | None,
    base: str,
    case: dict,
    config: str,
    cfg: dict,
    backend,
    dry: bool,
    out: Path,
    run: int = 1,
    diff_dir: Path | None = None,
) -> dict:
    """repo が None なら変更後のファイルを渡さず、差分だけでレビューさせる."""
    diff = ((diff_dir or DEFAULT_BENCH) / f"{case['id']}.diff").read_text(encoding="utf-8")
    if repo is None:
        p, result = _review(case, config, cfg, backend, dry, diff, None)
    else:
        with tempfile.TemporaryDirectory() as tmp:
            wt = Path(tmp) / "head"
            with _GIT_LOCK:
                head_tree(repo, base, diff, wt)
            p, result = _review(case, config, cfg, backend, dry, diff, wt)
    report = to_report(result)
    report["case"], report["config"], report["run"] = case["id"], config, run
    suffix = "" if run == 1 else f"-r{run}"
    (out / "raw" / f"{case['id']}-{config}{suffix}.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(
        f"{case['id']:>4} {config:<8} r{run} tier={p.tier:<8} findings={report['summary']['shown']:>2} cost=${report['cost_usd']:.3f}",
        flush=True,
    )
    return report


def _load(out: Path) -> tuple[dict, dict, list[dict]]:
    cases = {c["id"]: c for c in load_bench()[0]["cases"]}
    bugs = {b["id"]: b for b in yaml.safe_load((ROOT / "bench/bugs/sample-app-known.yaml").read_text(encoding="utf-8"))["bugs"]}
    reports = [json.loads(p.read_text(encoding="utf-8")) for p in sorted((out / "raw").glob("*.json"))]
    return cases, bugs, reports


def metrics(out: Path) -> dict[str, dict]:
    """構成ごとの採点（しきい値の確認に使う）. 数え方は summarize の表と同じ."""
    cases, bugs, reports = _load(out)
    res: dict[str, dict] = {}
    for r in reports:
        m = res.setdefault(r["config"], {"runs": 0, "positives": 0, "detected": 0, "must_noise": 0, "false_alarm": 0, "decoy_must": 0})
        m["runs"] += 1
        case = cases[r["case"]]
        fs = [ScoreFinding(f["severity"], f["location"], f["message"]) for f in r["findings"]]
        if case.get("decoy"):
            m["decoy_must"] += sum(1 for f in fs if f.severity == "MUST")
        elif case.get("expect"):
            m["positives"] += 1
            hit = [f for f in fs if matches(bugs[case["expect"]], f)]
            m["detected"] += bool(hit)
            m["must_noise"] += sum(1 for f in fs if f.severity == "MUST" and f not in hit)
        else:
            m["false_alarm"] += sum(1 for f in fs if f.severity in ("MUST", "WANT"))
    return res


def check(out: Path, thresholds: Path) -> list[str]:
    """しきい値（min_* は以上、max_* は以下）を外れた項目を返す. 空なら合格."""
    spec = yaml.safe_load(thresholds.read_text(encoding="utf-8"))
    got = metrics(out)
    errors = []
    for conf, limits in spec.items():
        m = got.get(conf)
        if m is None:
            errors.append(f"{conf}: 結果がありません")
            continue
        for key, want in limits.items():
            kind, name = key.split("_", 1)
            v = m[name]
            if (kind == "min" and v < want) or (kind == "max" and v > want):
                errors.append(f"{conf}: {name}={v}（{key}={want}）")
    return errors


def summarize_cost(out: Path) -> str:
    """種類（正解）を持たない課題（v2）の集計. 費用と件数だけを出す（採点は人が目隠しで行う）."""
    reports = [json.loads(p.read_text(encoding="utf-8")) for p in sorted((out / "raw").glob("*.json"))]
    rows = []
    for conf in [c for c in CONFIGS if any(r["config"] == c for r in reports)]:
        rs = [r for r in reports if r["config"] == conf]
        n = len(rs)
        cost = sum(r["cost_usd"] for r in rs)
        shown = sum(len(r["findings"]) for r in rs)
        must = sum(1 for r in rs for f in r["findings"] if f["severity"] == "MUST")
        deep_calls = sum(1 for r in rs for c in r["calls"] if "opus" in c["model"])
        rows.append(f"| {conf} | {n} | {shown / n:.1f} | {must / n:.2f} | {deep_calls} | ${cost:.2f} | ${cost / n:.3f} |")
    md = [
        "| 構成 | 実行数 | 1 PR あたりの指摘 | 1 PR あたりの MUST | Opus の呼び出し | 費用の合計 | 1 PR あたり |",
        "|---|---|---|---|---|---|---|",
        *rows,
        "",
        "検出率・誤った MUST は、runner/blind_sheet.py で目隠しの採点表を作り、人が採点してから runner/blind_score.py で出す。",
    ]
    text = "\n".join(md) + "\n"
    (out / "summary.md").write_text(text, encoding="utf-8")
    return text


def summarize(out: Path, bench: Path = DEFAULT_BENCH) -> str:
    if bench.resolve() != DEFAULT_BENCH.resolve():
        return summarize_cost(out)
    cases, bugs, reports = _load(out)
    configs = [c for c in CONFIGS if any(r["config"] == c for r in reports)]
    rows, detail = [], []
    for conf in configs:
        rs = [r for r in reports if r["config"] == conf]
        det, n_pos, false_alarm, shown, must_noise, deep_calls = 0, 0, 0, 0, 0, 0
        for r in rs:
            case = cases[r["case"]]
            fs = [ScoreFinding(f["severity"], f["location"], f["message"]) for f in r["findings"]]
            shown += len(fs)
            deep_calls += sum(1 for c in r["calls"] if "opus" in c["model"])
            if case.get("decoy"):
                continue
            if case.get("expect"):
                n_pos += 1
                hit = [f for f in fs if matches(bugs[case["expect"]], f)]
                det += bool(hit)
                must_noise += sum(1 for f in fs if f.severity == "MUST" and f not in hit)
                detail.append((r["case"], conf, "○" if hit else "×", hit[0].severity if hit else "-", len(fs)))
            else:
                false_alarm += sum(1 for f in fs if f.severity in ("MUST", "WANT"))
                detail.append((r["case"], conf, "-", "-", len(fs)))
        cost = sum(r["cost_usd"] for r in rs)
        secs = sum(c["seconds"] for r in rs for c in r["calls"])
        n = max(1, len(rs))
        rows.append(
            f"| {conf} | {det} / {n_pos} | {shown / n:.1f} | {must_noise} | {false_alarm} | {deep_calls} "
            f"| ${cost:.2f} | ${cost / n:.3f} | {secs / n:.0f} 秒 |"
        )
    decoy_rows = []
    for conf in configs:
        rs = [r for r in reports if r["config"] == conf and cases[r["case"]].get("decoy")]
        if not rs:
            continue
        shown_must = [f for r in rs for f in r["findings"] if f["severity"] == "MUST"]
        by = {k: sum(1 for f in shown_must if f.get("status") == k) for k in ("confirmed", "uncertain", "unverified")}
        rejected = sum(1 for r in rs for f in r.get("rejected", []) if f["severity"] == "MUST")
        pr_with_must = sum(1 for r in rs if any(f["severity"] == "MUST" for f in r["findings"]))
        decoy_rows.append(
            f"| {conf} | {len(rs)} | {pr_with_must} | {len(shown_must)} | {by['confirmed']} | {by['uncertain']} | {by['unverified']} "
            f"| {rejected} | ${sum(r['cost_usd'] for r in rs) / len(rs):.3f} |"
        )
    md = [
        "| 構成 | 検出（P01〜P08） | 1 PR あたりの指摘 | 正解以外の MUST（P） | 誤警報 MUST・WANT（N01〜N03） | Opus の呼び出し | 費用の合計 | 1 PR あたり | 1 PR あたりの時間 |",
        "|---|---|---|---|---|---|---|---|---|",
        *rows,
        "",
        "おとりの PR（D01〜。正しい変更なので MUST は誤り）:",
        "",
        "| 構成 | 実行数 | MUST を見せた PR | 見せた MUST | うち検証済み | うち要確認 | うち未検証 | 検証で却下した MUST | 1 PR あたり |",
        "|---|---|---|---|---|---|---|---|---|",
        *decoy_rows,
        "",
        "| PR | 構成 | 検出 | 重要度 | 指摘数 |",
        "|---|---|---|---|---|",
        *[f"| {a} | {b} | {c} | {d} | {e} |" for a, b, c, d, e in sorted(detail)],
    ]
    text = "\n".join(md) + "\n"
    (out / "summary.md").write_text(text, encoding="utf-8")
    return text


# ---------------------------------------------------------------- 費用の見積もり（モデルは呼ばない）

# 出力トークンの見込み（1 回あたり）. --observed の結果フォルダから平均を取る. 無ければこの値（2026-10-04 の実測）
DEFAULT_OBSERVED = ROOT / "results" / "2026-10-04-auto-review-repeat"
FALLBACK_OUTPUT = {
    ("standard", "sonnet"): 1008,
    ("single", "sonnet"): 953,
    ("single", "opus"): 1559,
    ("verify", "opus"): 909,
    ("deep", "opus"): 1490,
}
FALLBACK_P_MUST = 20 / 39  # auto で、Sonnet の一次レビューが MUST を出して Opus の検証に回った割合（同上）


def _family(model: str) -> str:
    return next((k for k in ("haiku", "sonnet", "opus") if k in model), model)


def observed_stats(folder: Path | None) -> tuple[dict[tuple[str, str], float], float, str]:
    """（(段階, モデルの系統) → 出力トークンの平均, auto で検証に回る割合, 出どころ）."""
    files = sorted((folder / "raw").glob("*.json")) if folder and (folder / "raw").exists() else []
    if not files:
        return dict(FALLBACK_OUTPUT), FALLBACK_P_MUST, "既定値（2026-10-04 の実測）"
    outs: dict[tuple[str, str], list[int]] = {}
    n_std, n_verify = 0, 0
    for p in files:
        r = json.loads(p.read_text(encoding="utf-8"))
        for c in r.get("calls", []):
            if not c.get("estimated"):
                outs.setdefault((c["step"], _family(c["model"])), []).append(c["output_tokens"])
        if r.get("config") == "auto" and r.get("plan", {}).get("tier") == "standard":
            n_std += 1
            n_verify += any(c["step"] == "verify" for c in r.get("calls", []))
    table = dict(FALLBACK_OUTPUT)
    table.update({k: sum(v) / len(v) for k, v in outs.items()})
    return table, (n_verify / n_std if n_std else FALLBACK_P_MUST), str(folder.relative_to(ROOT) if folder.is_relative_to(ROOT) else folder)


class EstimatingReviewer(Reviewer):
    """モデルを呼ばずに、実際と同じ道筋（振り分け・検証・予算の絞り込み）をたどって呼び出しを数える.

    一次レビューの返答の代わりに、決まった形の指摘（must=True なら MUST を 1 件含む）を返す。
    入力トークンは文字数から（auto-review の見積もりと同じ換算）、出力トークンは実測の平均（expected）と上限（max_tokens）の 2 通り。
    """

    FAKE_FINDINGS = 5  # 一次レビューが返す指摘の数（実測の 1 PR あたりの指摘に近い値）
    FAKE_DETAIL = "あ" * 300  # 1 件の説明の長さ（検証に渡す文字数を実際に近づける）

    def __init__(self, cfg: dict, repo: Path | None, outputs: dict, must: bool, upper: bool):
        super().__init__(cfg, backend=None, repo=repo, dry_run=False)
        self.outputs, self.must, self.upper = outputs, must, upper
        self.files: list[str] = []

    def _call(self, step: str, model_key: str, system: str, user: str, result) -> str:
        model = self.cfg["models"][model_key]
        out_key = "verify" if step == "verify" else ("deep" if step == "deep" else "standard")
        max_out = self.cfg["budget"]["output_tokens"][out_key]
        inp = _tokens(len(system) + len(user), self.cfg)
        out = max_out if self.upper else min(max_out, int(self.outputs.get((step, _family(model["id"])), max_out)))
        result.calls.append(Call(step, model["id"], inp, out, round(price(model, inp, out), 6), 0.0, estimated=True))
        if step in ("standard", "single"):
            f = self.files[0] if self.files else "x"
            items = [
                {
                    "severity": "MUST" if (self.must and i == 0) else "WANT",
                    "file": f,
                    "line": i + 1,
                    "title": f"t{i}",
                    "detail": self.FAKE_DETAIL,
                }
                for i in range(self.FAKE_FINDINGS)
            ]
            return json.dumps({"findings": items}, ensure_ascii=False)
        return json.dumps({"verdicts": [], "findings": []})


def estimate_case(case: dict, diff: str, config: str, cfg: dict, wt: Path | None, outputs: dict, upper: bool) -> dict[str, float]:
    """1 PR・1 構成の見積もり. auto は「MUST なし（検証なし）」と「MUST あり（検証あり）」の両方を返す."""
    note = f"# PR: {case['title']}\n\n{case.get('description', '')}\n\n"
    changes = parse_diff(diff)
    p = plan(changes, cfg)
    res = {}
    for must in (False, True) if config == "auto" else (False,):
        r = EstimatingReviewer(cfg, wt, outputs, must, upper)
        r.files = [c.path for c in changes]
        result = r.auto(note + diff, changes, p) if config == "auto" else r.single(config, note + diff, changes, p)
        res["must" if must else "base"] = sum(c.cost_usd for c in result.calls)
        res[("must" if must else "base") + "_in"] = sum(c.input_tokens for c in result.calls)
        res[("must" if must else "base") + "_out"] = sum(c.output_tokens for c in result.calls)
    res["tier"] = p.tier
    return res


def estimate(
    repo: Path | None, bench: Path, case_ids: list[str] | None, configs: list[str], repeat: int, observed: Path | None
) -> tuple[str, dict]:
    spec, diff_dir = load_bench(bench)
    cases = [c for c in spec["cases"] if not case_ids or c["id"] in case_ids]
    cfg = load_config()
    outputs, p_must, src = observed_stats(observed)
    per: dict[str, dict[str, float]] = {}
    tiers: dict[str, int] = {}
    for case in cases:
        diff = (diff_dir / f"{case['id']}.diff").read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as tmp:
            wt = None
            if repo is not None:
                wt = Path(tmp) / "head"
                head_tree(repo, spec["base_commit"], diff, wt)
            for conf in configs:
                exp = estimate_case(case, diff, conf, cfg, wt, outputs, upper=False)
                up = estimate_case(case, diff, conf, cfg, wt, outputs, upper=True)
                m = per.setdefault(conf, {"runs": 0, "in": 0, "out": 0, "expected": 0.0, "low": 0.0, "high": 0.0, "upper": 0.0})
                m["runs"] += repeat
                if conf == "auto":
                    tiers[exp["tier"]] = tiers.get(exp["tier"], 0) + 1
                    if exp["tier"] == "standard":  # 検証に回るかは一次レビューの結果しだい → 割合で重み付け
                        e = exp["base"] + p_must * (exp["must"] - exp["base"])
                        lo, hi, u = exp["base"], exp["must"], up["must"]
                        tin = exp["base_in"] + p_must * (exp["must_in"] - exp["base_in"])
                        tout = exp["base_out"] + p_must * (exp["must_out"] - exp["base_out"])
                    else:  # deep・skip は振り分けで決まる
                        e = lo = hi = exp["base"]
                        u = up["base"]
                        tin, tout = exp["base_in"], exp["base_out"]
                else:
                    e = lo = hi = exp["base"]
                    u = up["base"]
                    tin, tout = exp["base_in"], exp["base_out"]
                m["expected"] += e * repeat
                m["low"] += lo * repeat
                m["high"] += hi * repeat
                m["upper"] += u * repeat
                m["in"] += tin * repeat
                m["out"] += tout * repeat
    rows = [
        f"| {conf} | {m['runs']} | {m['in'] / 1e6:.2f} M | {m['out'] / 1e6:.3f} M | **${m['expected']:.2f}** | ${m['low']:.2f}〜${m['high']:.2f} "
        f"| ${m['upper']:.2f} | ${m['expected'] / max(1, m['runs']):.4f} |"
        for conf, m in per.items()
    ]
    total = sum(m["expected"] for m in per.values())
    total_upper = sum(m["upper"] for m in per.values())
    md = [
        f"見積もり（モデルは呼んでいない）: 課題 {len(cases)} 件 × {repeat} 回、{'変更後のファイルも渡す' if repo else '差分だけ'}、API で直接呼ぶ場合",
        "",
        "| 構成 | 実行数 | 入力トークン | 出力トークン（見込み） | 費用の見込み | 幅（auto: 検証 0〜全部） | 上限（出力が max_tokens） | 1 PR あたり |",
        "|---|---|---|---|---|---|---|---|",
        *rows,
        "",
        f"- 合計の見込み **${total:.2f}**、上限 ${total_upper:.2f}。単価は devtools_common.llm.PRICES（auto-review の price）",
        f"- 出力トークンと、auto で検証に回る割合（{p_must:.0%}）の出どころ: {src}",
        f"- auto の振り分け: {', '.join(f'{k} {v} 件' for k, v in sorted(tiers.items())) or '-'}",
        "- 入力は文字数 ÷ chars_per_token（router.yaml）。プロンプトのキャッシュは使わない前提（使えば入力はもっと安い）。再試行の分は含まない",
    ]
    return "\n".join(md) + "\n", per


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("repo", type=Path, nargs="?", help="dev-environment のパス")
    ap.add_argument("out", type=Path, nargs="?")
    ap.add_argument("--configs", default=",".join(CONFIGS))
    ap.add_argument("--cases", help="カンマ区切りで絞る（例: P01,N01）")
    ap.add_argument("--backend", default="claude-cli", choices=["anthropic", "claude-cli"])
    ap.add_argument("--yes", action="store_true", help="実際にモデルを呼ぶ（付けなければ見積もりだけ）")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--repeat", type=int, default=1, help="同じ PR・構成を何回流すか（ばらつきを見る）")
    ap.add_argument("--summarize", type=Path, help="集計だけやり直す（結果のフォルダ）")
    ap.add_argument("--diff-only", action="store_true", help="変更後のファイルを渡さず差分だけで（repo は不要. 引数は out だけ）")
    ap.add_argument("--replay", type=Path, help="記録した返答（JSONL）を返す偽の呼び出し先を使う（モデルは呼ばない）")
    ap.add_argument("--check", type=Path, help="しきい値（YAML）. 外れたら終了コード 1. --replay では、使われなかった記録も失敗にする")
    ap.add_argument(
        "--bench", type=Path, default=DEFAULT_BENCH, help="課題のフォルダ（cases.yaml と差分. 既定は bench/prs、v2 は bench/prs_v2）"
    )
    ap.add_argument("--estimate-only", action="store_true", help="費用の見積もりだけを出す（モデルは呼ばない. out は不要）")
    ap.add_argument("--observed", type=Path, default=DEFAULT_OBSERVED, help="見積もりで出力トークンの平均を取る、過去の結果のフォルダ")
    args = ap.parse_args(argv)
    if args.summarize:
        print(summarize(args.summarize, args.bench))
        return 0
    if args.estimate_only:
        if args.yes or args.replay:
            ap.error("--estimate-only は --yes・--replay と一緒に使えません")
        repo = None if args.diff_only else args.repo
        if repo is None and not args.diff_only:
            ap.error("repo が必要です（差分だけで見積もるなら --diff-only）")
        text, _ = estimate(
            repo.resolve() if repo else None,
            args.bench,
            args.cases.split(",") if args.cases else None,
            args.configs.split(","),
            args.repeat,
            args.observed,
        )
        print(text)
        return 0
    if args.diff_only and args.out is None:
        args.repo, args.out = None, args.repo
    if not args.out or not (args.repo or args.diff_only):
        ap.error("repo と out が必要です（--diff-only なら out だけ）")
    spec, diff_dir = load_bench(args.bench)
    cases = [c for c in spec["cases"] if not args.cases or c["id"] in args.cases.split(",")]
    cfg = load_config()
    if args.replay:
        backend = ReplayBackend(args.replay)
        args.yes = True  # 記録を返すだけなので「実行」として扱う（外部には送らない）
    else:
        backend = get_backend(args.backend) if args.yes else None
    (args.out / "raw").mkdir(parents=True, exist_ok=True)
    jobs = [(c, conf, k) for k in range(1, args.repeat + 1) for c in cases for conf in args.configs.split(",")]
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        list(
            ex.map(
                lambda j: run_one(
                    args.repo.resolve() if args.repo else None,
                    spec["base_commit"],
                    j[0],
                    j[1],
                    cfg,
                    backend,
                    not args.yes,
                    args.out,
                    j[2],
                    diff_dir,
                ),
                jobs,
            )
        )
    print(summarize(args.out, args.bench))
    if args.check:
        errors = check(args.out, args.check)
        if args.replay:
            ids = {c["id"] for c in cases}
            unused = sorted(k for k in set(backend.records) - set(backend.used) if k[0] in ids)
            errors += [f"使われなかった記録: case={c} role={r} model={m}" for c, r, m in unused]
            errors += [f"記録の呼び出しに失敗: {e}" for e in backend.errors]
        for e in errors:
            print(f"しきい値を外れました: {e}", file=sys.stderr)
        print(json.dumps(metrics(args.out), ensure_ascii=False))
        return 1 if errors else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
