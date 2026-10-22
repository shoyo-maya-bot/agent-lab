"""mcp-server の README・リポジトリの README と、登録した道具・コマンド・既定値のずれを機械で見つける."""

from __future__ import annotations

import re
from pathlib import Path

from devtools_mcp import runner

ROOT = Path(__file__).resolve().parents[2]
MCP_README = (ROOT / "mcp-server" / "README.md").read_text(encoding="utf-8")
TOP_README = (ROOT / "README.md").read_text(encoding="utf-8")
SERVER = (ROOT / "mcp-server" / "src" / "devtools_mcp" / "server.py").read_text(encoding="utf-8")


def _registered_tools() -> set[str]:
    return set(re.findall(r"@mcp\.tool\([^\n]*\)\n(?:@\w+\n)*def (\w+)\(", SERVER))


def test_readme_tool_table_matches_registered_tools():
    table = MCP_README.split("| ツール | 中身 |", 1)[1].split("\n\n", 1)[0]
    documented = set(re.findall(r"`([a-z0-9_]+)`", table))
    tools = _registered_tools()
    assert len(tools) >= 6
    # `design_diff_excel` / `design_diff_spec` は 1 行にまとめて書いている
    assert documented == tools


def test_top_readme_lists_the_wrapped_clis():
    row = next(ln for ln in TOP_README.splitlines() if ln.startswith("| MCP |"))
    listed = set(re.search(r"自作ツール（([^）]+)）", row).group(1).split("・"))
    assert listed == set(runner.COMMANDS)


def test_timeout_default_is_documented():
    assert f"`DEVTOOLS_MCP_TIMEOUT`（秒。既定 {runner.DEFAULT_TIMEOUT:g}）" in MCP_README


def test_exit_meanings_cover_the_common_exit_codes():
    assert set(runner.EXIT_MEANING) == {0, 1, 2, 3}
