#!/usr/bin/env python3
"""PR 単位の課題（bench/prs）を、構成ごとにレビューさせて比べる.

    python runner/pr_bench.py ../dev-environment results/2026-10-04-auto-review --backend claude-cli --yes
    python runner/pr_bench.py ../dev-environment results/... --configs auto,standard   # 構成を絞る
    python runner/pr_bench.py --summarize results/2026-10-04-auto-review              # 集計だけやり直す

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
import json
import subprocess
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "auto-review" / "src"))
sys.path.insert(0, str(ROOT / "runner"))

from auto_review.backends import ReplayBackend, get_backend  # noqa: E402
from auto_review.diff import parse_diff  # noqa: E402
from auto_review.render import to_report  # noqa: E402
from auto_review.review import Reviewer  # noqa: E402
from auto_review.router import load_config, plan  # noqa: E402
from score import Finding as ScoreFinding  # noqa: E402
from score import matches  # noqa: E402

CONFIGS = ["light", "standard", "deep", "auto"]
_GIT_LOCK = threading.Lock()  # 同じリポジトリへの worktree の追加・削除は同時に行わない


def git(repo: Path, *args: str, stdin: str | None = None) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], input=stdin, check=True, capture_output=True, text=True).stdout


def head_tree(repo: Path, base: str, diff: str, dest: Path) -> None:
    """base_commit に PR の差分を当てた「変更後」のファイル一式を dest に作る."""
    git(repo, "worktree", "add", "-q", "--detach", str(dest), base)
    if diff.strip():
        git(dest, "apply", "--index", "-", stdin=diff)


def _review(case: dict, config: str, cfg: dict, backend, dry: bool, diff: str, wt: Path | None):
    note = f"# PR: {case['title']}\n\n{case.get('description', '')}\n\n"
    changes = parse_diff(diff)
    p = plan(changes, cfg)
    if isinstance(backend, ReplayBackend):
        backend = backend.for_case(case["id"])
    r = Reviewer(cfg, backend, repo=wt, dry_run=dry)
    result = r.auto(note + diff, changes, p) if config == "auto" else r.single(config, note + diff, changes, p)
    return p, result


def run_one(repo: Path | None, base: str, case: dict, config: str, cfg: dict, backend, dry: bool, out: Path, run: int = 1) -> dict:
    """repo が None なら変更後のファイルを渡さず、差分だけでレビューさせる."""
    diff = (ROOT / "bench" / "prs" / f"{case['id']}.diff").read_text(encoding="utf-8")
    if repo is None:
        p, result = _review(case, config, cfg, backend, dry, diff, None)
    else:
        with tempfile.TemporaryDirectory() as tmp:
            wt = Path(tmp) / "head"
            with _GIT_LOCK:
                head_tree(repo, base, diff, wt)
            try:
                p, result = _review(case, config, cfg, backend, dry, diff, wt)
            finally:
                with _GIT_LOCK:
                    git(repo, "worktree", "remove", "--force", str(wt))
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
    cases = {c["id"]: c for c in yaml.safe_load((ROOT / "bench/prs/cases.yaml").read_text(encoding="utf-8"))["cases"]}
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


def summarize(out: Path) -> str:
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
    args = ap.parse_args(argv)
    if args.summarize:
        print(summarize(args.summarize))
        return 0
    if args.diff_only and args.out is None:
        args.repo, args.out = None, args.repo
    if not args.out or not (args.repo or args.diff_only):
        ap.error("repo と out が必要です（--diff-only なら out だけ）")
    spec = yaml.safe_load((ROOT / "bench/prs/cases.yaml").read_text(encoding="utf-8"))
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
                    args.repo.resolve() if args.repo else None, spec["base_commit"], j[0], j[1], cfg, backend, not args.yes, args.out, j[2]
                ),
                jobs,
            )
        )
    print(summarize(args.out))
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
