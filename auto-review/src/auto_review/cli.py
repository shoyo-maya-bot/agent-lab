"""auto-review: PR の差分を危なさで振り分け、必要なときだけ上位モデルを足してレビューする.

    git diff origin/main...HEAD > pr.diff
    auto-review plan pr.diff                                  # 振り分けだけ（モデルは呼ばない）
    auto-review run pr.diff --repo .                          # 見積もりだけ（外部に送らない）
    auto-review run pr.diff --repo . --yes --out-md c.md      # 実際にレビュー（ANTHROPIC_API_KEY）
    auto-review run pr.diff --repo . --yes --only standard    # 比較実験: 1 つのモデルだけ

終了コード: 0 MUST なし / 1 MUST あり（却下されたものを除く）/ 2 入力の誤り / 3 実行時エラー
（モデルを呼べない・出力を読めない・--yes なのに予算 budget.per_pr_usd を超える見込みで呼ばなかった）
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .backends import BackendError, OutputTruncated, get_backend
from .diff import parse_diff
from .render import to_markdown, to_report
from .review import Reviewer, UnreadableOutput
from .router import load_config, plan


def _read(path: str) -> str:
    return sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8")


def _labels(s: str | None) -> list[str]:
    return [x.strip() for x in (s or "").split(",") if x.strip()]


def _input_error(diff_text: str, changes: list) -> str | None:
    """中身があるのに 1 ファイルも読めない差分は入力の誤り（黙って「変更なし」で skip・終了コード 0 にしない）.

    空の差分（本当に変更が無い）は誤りにしない.
    """
    if changes or not diff_text.strip():
        return None
    if not any(line.startswith("diff --git ") for line in diff_text.splitlines()):
        return (
            "git の差分（`diff --git` で始まる行）がありません。`diff -u` などの形式には対応していません。"
            "`git diff origin/main...HEAD > pr.diff` のように git diff で作ってください"
        )
    return "差分からファイルの変更を 1 件も読めません（形式を確かめてください）"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="auto-review", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("plan", "run"):
        s = sub.add_parser(name)
        s.add_argument("diff", help="unified diff のファイル（- で標準入力）")
        s.add_argument("--config", type=Path, help="router.yaml（既定: 同梱のもの）")
        s.add_argument("--labels", help="PR のラベル（カンマ区切り）")
        s.add_argument("--author", help="PR の作成者（Dependabot などを省くため）")
        s.add_argument("-f", "--format", choices=["markdown", "json"], default="markdown")
    run = sub.choices["run"]
    run.add_argument("--repo", type=Path, help="変更後のファイルを読むリポジトリのルート（省略時は差分だけを渡す）")
    run.add_argument("--backend", choices=["anthropic", "claude-cli"], default="anthropic")
    run.add_argument("--yes", action="store_true", help="実際にモデルを呼ぶ（付けなければ見積もりだけ）")
    run.add_argument("--only", choices=["light", "standard", "deep"], help="比較実験: 振り分けをせず 1 つのモデルだけで")
    run.add_argument("--max-findings", type=int, default=15)
    run.add_argument("--out-md", type=Path)
    run.add_argument("--out-json", type=Path)
    args = ap.parse_args(argv)

    try:
        cfg = load_config(args.config)
        diff_text = _read(args.diff)
    except (OSError, ValueError) as e:
        print(f"入力を読めません: {e}", file=sys.stderr)
        return 2
    changes = parse_diff(diff_text)
    err = _input_error(diff_text, changes)
    if err:
        print(f"入力の誤り: {err}", file=sys.stderr)
        return 2
    p = plan(changes, cfg, _labels(args.labels), args.author)

    if args.cmd == "plan":
        if args.format == "json":
            print(json.dumps(p.to_dict(), ensure_ascii=False, indent=1))
        else:
            print(f"tier={p.tier} score={p.score} forced_by={p.forced_by or '-'}")
            for r in p.reasons:
                print(" ", r)
        return 0

    try:
        backend = get_backend(args.backend) if args.yes else None
        reviewer = Reviewer(cfg, backend, repo=args.repo, dry_run=not args.yes, max_findings=args.max_findings)
        result = reviewer.single(args.only, diff_text, changes, p) if args.only else reviewer.auto(diff_text, changes, p)
    except OutputTruncated as e:  # 途中で切れた出力は「指摘 0 件」と区別する
        print(f"結果を読めません: {e}", file=sys.stderr)
        return 3
    except BackendError as e:
        print(f"モデルを呼べません: {e}", file=sys.stderr)
        return 3
    except UnreadableOutput as e:
        print(f"結果を読めません: {e}", file=sys.stderr)
        return 3
    md, report = to_markdown(result), to_report(result)
    if args.out_md:
        args.out_md.write_text(md, encoding="utf-8")
    if args.out_json:
        args.out_json.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=1) if args.format == "json" else md)
    if result.over_budget and args.yes:  # レビューを頼まれたのにしていない. 「指摘 0 件」（終了コード 0）と区別する
        print("予算を超える見込みのため、モデルを呼びませんでした（router.yaml の budget.per_pr_usd）", file=sys.stderr)
        return 3
    return 1 if result.open_must else 0


if __name__ == "__main__":
    sys.exit(main())
