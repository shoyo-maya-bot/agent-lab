"""MCP サーバー本体（stdio）. ツールは読み取り中心で、元のファイルを書き換えない.

devtools-mcp            # VS Code / Claude などの MCP クライアントから起動される
"""

from __future__ import annotations

import functools
import re
from typing import Any

from mcp.server.fastmcp import FastMCP

from .runner import COMMANDS, ToolError, Workspace, resolve_command, run

mcp = FastMCP(
    "devtools",
    instructions=(
        "devtools の CLI を呼び出すツール群です。パスは作業フォルダからの相対パスで渡します。"
        "結果の exit_code が 2 のときはデータではなく前提（引数・設定）の誤りです。"
        "report.findings に出てこない項目は、そのツールの観点では問題が無かったという意味です（確認できなかった、ではない）。"
        "doc2md の結果に loss.* の warning があれば、変換で内容が欠けた可能性を利用者に伝えてください。"
    ),
)


def _guard(fn):
    """ToolError を、例外ではなくエージェントが読める結果にする."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs) -> dict[str, Any]:
        try:
            return fn(*args, **kwargs)
        except ToolError as e:
            return {"error": str(e)}

    return wrapper


@mcp.tool()
@_guard
def tools_status() -> dict[str, Any]:
    """使える CLI と作業フォルダを返す。最初に 1 回呼んで、使えるツールを確かめる。"""
    ws = Workspace.from_env()
    return {"workspace": str(ws.root), "tools": {name: bool(resolve_command(name)) for name in COMMANDS}}


@mcp.tool()
@_guard
def doc2md_convert(path: str, recursive: bool = False) -> dict[str, Any]:
    """Word / Excel / PowerPoint / PDF / draw.io を Markdown に変換する。

    path: 作業フォルダからの相対パス（ファイルかフォルダ）。結果の outputs に作られた Markdown のパスが入る。
    """
    ws = Workspace.from_env()
    src = ws.path(path)
    out = ws.out_dir("doc2md")
    args = [str(src), "-d", str(out), "-f", "json", "--continue-on-error"]
    if recursive:
        args.append("-r")
    result = run("doc2md", args, ws)
    result["outputs"] = sorted(ws.rel(p) for p in out.rglob("*.md"))
    return result


@mcp.tool()
@_guard
def design_diff_excel(doc_type: str, before: str, after: str, config_dir: str = "config") -> dict[str, Any]:
    """Excel 設計書の新旧の差分を取る。doc_type は設定の種類（config/<doc_type>.yaml。例: api / batch / if）。"""
    if not re.fullmatch(r"[a-z0-9_-]{1,40}", doc_type or ""):
        raise ToolError("doc_type は英小文字・数字・_ - だけで指定してください（例: api）")
    ws = Workspace.from_env()
    args = [
        "--config-dir", str(ws.path(config_dir)),
        "excel", "--type", doc_type, "--before", str(ws.path(before)), "--after", str(ws.path(after)), "-f", "json",
    ]  # fmt: skip
    return run("design-diff", args, ws)


@mcp.tool()
@_guard
def design_diff_spec(excel: str, oas: str, config: str = "") -> dict[str, Any]:
    """API 定義書（Excel）と OpenAPI（YAML / JSON）の整合を調べる。config は設定ファイル（既定: config/spec.yaml）。"""
    ws = Workspace.from_env()
    args = ["spec", "--excel", str(ws.path(excel)), "--oas", str(ws.path(oas)), "-f", "json"]
    if config:
        args += ["--config", str(ws.path(config))]
    return run("design-diff", args, ws)


@mcp.tool()
@_guard
def fieldcheck_validate(data: str, rules: str, table: str = "") -> dict[str, Any]:
    """表データ（CSV / JSON / YAML / Markdown の表）を、ルールファイルで項目間検証する。

    table: Markdown のとき、どの表を使うか（番号か直前の見出し名）。
    """
    ws = Workspace.from_env()
    args = ["validate", str(ws.path(data)), "-r", str(ws.path(rules)), "-f", "json"]
    if table:
        args += ["--table", table]
    return run("fieldcheck", args, ws)


@mcp.tool()
@_guard
def spec_trace_check(traceability: str = "traceability", root: str = ".", require: str = "") -> dict[str, Any]:
    """要件と実装・テストの対応表を検査する。require に implemented / proven を渡すと、その状態に達していない要件を指摘にする。"""
    ws = Workspace.from_env()
    args = ["check", str(ws.path(traceability)), "--root", str(ws.path(root)), "-f", "json"]
    if require:
        if require not in ("implemented", "proven"):
            return {"error": "require は implemented か proven です"}
        args += ["--require", require]
    return run("spec-trace", args, ws)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
