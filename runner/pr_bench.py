#!/usr/bin/env python3
"""PR 単位の課題（bench/prs）を、構成ごとにレビューさせて比べる.

    python runner/pr_bench.py ../dev-environment results/2026-10-04-auto-review --backend claude-cli --yes
    python runner/pr_bench.py ../dev-environment results/... --configs auto,standard   # 構成を絞る
    python runner/pr_bench.py --summarize results/2026-10-04-auto-review              # 集計だけやり直す

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

from auto_review.backends import get_backend  # noqa: E402
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


def run_one(repo: Path, base: str, case: dict, config: str, cfg: dict, backend, dry: bool, out: Path) -> dict:
    diff = (ROOT / "bench" / "prs" / f"{case['id']}.diff").read_text(encoding="utf-8")
    note = f"# PR: {case['title']}\n\n{case.get('description', '')}\n\n"
    with tempfile.TemporaryDirectory() as tmp:
        wt = Path(tmp) / "head"
        with _GIT_LOCK:
            head_tree(repo, base, diff, wt)
        try:
            changes = parse_diff(diff)
            p = plan(changes, cfg)
            r = Reviewer(cfg, backend, repo=wt, dry_run=dry)
            result = r.auto(note + diff, changes, p) if config == "auto" else r.single(config, note + diff, changes, p)
        finally:
            with _GIT_LOCK:
                git(repo, "worktree", "remove", "--force", str(wt))
    report = to_report(result)
    report["case"], report["config"] = case["id"], config
    (out / "raw" / f"{case['id']}-{config}.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(
        f"{case['id']:>4} {config:<8} tier={p.tier:<8} findings={report['summary']['shown']:>2} cost=${report['cost_usd']:.3f}", flush=True
    )
    return report


def summarize(out: Path) -> str:
    cases = {c["id"]: c for c in yaml.safe_load((ROOT / "bench/prs/cases.yaml").read_text(encoding="utf-8"))["cases"]}
    bugs = {b["id"]: b for b in yaml.safe_load((ROOT / "bench/bugs/sample-app-known.yaml").read_text(encoding="utf-8"))["bugs"]}
    reports = [json.loads(p.read_text(encoding="utf-8")) for p in sorted((out / "raw").glob("*.json"))]
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
    md = [
        "| 構成 | 検出（P01〜P08） | 1 PR あたりの指摘 | 正解以外の MUST（P） | 誤警報 MUST・WANT（N01〜N03） | Opus の呼び出し | 費用の合計 | 1 PR あたり | 1 PR あたりの時間 |",
        "|---|---|---|---|---|---|---|---|---|",
        *rows,
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
    ap.add_argument("--summarize", type=Path, help="集計だけやり直す（結果のフォルダ）")
    args = ap.parse_args(argv)
    if args.summarize:
        print(summarize(args.summarize))
        return 0
    if not (args.repo and args.out):
        ap.error("repo と out が必要です")
    spec = yaml.safe_load((ROOT / "bench/prs/cases.yaml").read_text(encoding="utf-8"))
    cases = [c for c in spec["cases"] if not args.cases or c["id"] in args.cases.split(",")]
    cfg = load_config()
    backend = get_backend(args.backend) if args.yes else None
    (args.out / "raw").mkdir(parents=True, exist_ok=True)
    jobs = [(c, conf) for c in cases for conf in args.configs.split(",")]
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        list(ex.map(lambda j: run_one(args.repo.resolve(), spec["base_commit"], j[0], j[1], cfg, backend, not args.yes, args.out), jobs))
    print(summarize(args.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
