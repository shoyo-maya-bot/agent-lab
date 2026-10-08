"""auto-review: PR の差分を危なさで振り分け、必要なときだけ上位モデルを足してレビューする.

    git diff origin/main...HEAD > pr.diff
    auto-review plan pr.diff                                  # 振り分けだけ（モデルは呼ばない）
    auto-review run pr.diff --repo .                          # 見積もりだけ（外部に送らない）
    auto-review run pr.diff --repo . --yes --out-md c.md      # 実際にレビュー（ANTHROPIC_API_KEY）
    auto-review run pr.diff --repo . --yes --only standard    # 比較実験: 1 つのモデルだけ

    auto-review run pr.diff --repo . --yes --prior-comment prev.md  # PR の累計の予算（前回のコメントの記録）も見る

終了コード: 0 MUST なし / 1 MUST あり（却下されたものを除く）/ 2 入力・設定の誤り（router.yaml が読めない・形が違う）/
3 実行時エラー（モデルを呼べない・出力を読めない・--yes なのに予算を超える見込みで呼ばなかった・想定外のエラー）
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from pathlib import Path

from .backends import BackendError, OutputTruncated, get_backend
from .diff import parse_diff
from .render import parse_spent, to_markdown, to_report
from .review import Reviewer, UnreadableOutput
from .router import ConfigError, load_config, plan


def _read(path: str) -> str:
    return sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8")


def _out(text: str) -> None:
    """標準出力に書く. 読む側が先に閉じた（`| head -1` など）ときは黙って捨てる（終了コードは結果どおりに返す）."""
    try:
        print(text, flush=True)
    except BrokenPipeError:
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())  # 終了時の flush で再び BrokenPipeError を出さない


def _labels(s: str | None) -> list[str]:
    return [x.strip() for x in (s or "").split(",") if x.strip()]


def _input_error(diff_text: str, changes: list) -> str | None:
    """中身があるのに 1 ファイルも読めない差分は入力の誤り（黙って「変更なし」で skip・終了コード 0 にしない）.

    空の差分（本当に変更が無い）は誤りにしない.
    """
    if changes or not diff_text.strip():
        return None
    if not any(line.startswith("diff --git ") for line in diff_text.split("\n")):
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
    run.add_argument(
        "--prior-comment",
        type=Path,
        help="同じ PR の前回の auto-review のコメント（本文のファイル）. 隠しマーカーの累計額を読み、budget.per_pr_total_usd で判定する",
    )
    args = ap.parse_args(argv)
    try:
        return _main(args)
    except Exception as e:  # 想定外のエラー. 「MUST あり」（1）と取り違えないよう 3 にする
        traceback.print_exc(file=sys.stderr)
        print(f"想定外のエラー: {type(e).__name__}: {e}", file=sys.stderr)
        return 3


def _main(args: argparse.Namespace) -> int:
    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        print(f"設定の誤り: {e}", file=sys.stderr)
        return 2
    except OSError as e:
        print(f"設定を読めません: {e}", file=sys.stderr)
        return 2
    if args.cmd == "run" and args.only and args.only not in cfg["models"]:
        print(f"設定の誤り: --only {args.only} のモデルが router.yaml の models にありません", file=sys.stderr)
        return 2
    try:
        diff_text = _read(args.diff)
        spent_before = parse_spent(args.prior_comment.read_text(encoding="utf-8")) if args.cmd == "run" and args.prior_comment else 0.0
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
            _out(json.dumps(p.to_dict(), ensure_ascii=False, indent=1))
        else:
            _out("\n".join([f"tier={p.tier} score={p.score} forced_by={p.forced_by or '-'}"] + [f"  {r}" for r in p.reasons]))
        return 0

    try:
        backend = get_backend(args.backend) if args.yes else None
        reviewer = Reviewer(cfg, backend, repo=args.repo, dry_run=not args.yes, max_findings=args.max_findings, spent_before=spent_before)
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
    _out(json.dumps(report, ensure_ascii=False, indent=1) if args.format == "json" else md)
    if result.over_budget and args.yes:  # レビューを頼まれたのにしていない. 「指摘 0 件」（終了コード 0）と区別する
        print(
            "予算を超える見込みのため、モデルを呼びませんでした"
            "（router.yaml の budget.per_pr_usd: 1 回の実行あたり / budget.per_pr_total_usd: PR の累計）",
            file=sys.stderr,
        )
        return 3
    return 1 if result.open_must else 0


if __name__ == "__main__":
    sys.exit(main())
