"""devtools-mcp のテスト. 偽の CLI と、（入っていれば）本物の design-diff で、MCP クライアントから呼んで確かめる."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from devtools_mcp import runner, server

HERE = Path(__file__).resolve().parent
FAKE = f"{sys.executable} {HERE / 'fake_cli.py'}"


@pytest.fixture()
def ws(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("DEVTOOLS_MCP_ROOT", str(tmp_path))
    for _, env in runner.COMMANDS.values():
        monkeypatch.setenv(env, FAKE)
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs/a.xlsx").write_bytes(b"x")
    (tmp_path / "docs/b.xlsx").write_bytes(b"x")
    (tmp_path / "config").mkdir()
    return tmp_path


def test_path_guard(ws: Path):
    w = runner.Workspace.from_env()
    assert w.path("docs/a.xlsx") == ws / "docs/a.xlsx"
    with pytest.raises(runner.ToolError, match="作業フォルダの外"):
        w.path("../secret.txt", must_exist=False)
    with pytest.raises(runner.ToolError, match="作業フォルダの外"):
        w.path("/etc/passwd")
    with pytest.raises(runner.ToolError, match="見つかりません"):
        w.path("docs/none.xlsx")


def test_design_diff_excel_builds_args(ws: Path):
    r = server.design_diff_excel("api", "docs/a.xlsx", "docs/b.xlsx")
    assert r["exit_code"] == 0 and r["meaning"] == "問題なし"
    args = r["report"]["args"]
    assert args[:2] == ["--config-dir", str(ws / "config")]
    assert args[2:4] == ["excel", "--type"] and args[-2:] == ["-f", "json"]
    assert "log line" in r["stderr"]


def test_exit_code_meaning_and_errors(ws: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("FAKE_EXIT", "2")
    r = server.fieldcheck_validate("docs/a.xlsx", "docs/b.xlsx", table="申請一覧")
    assert r["exit_code"] == 2 and "前提の誤り" in r["meaning"]
    assert r["report"]["args"][-2:] == ["--table", "申請一覧"]
    assert "error" in server.fieldcheck_validate("../x.csv", "docs/b.xlsx")  # 外のパスは結果として返す（例外にしない）
    assert "error" in server.spec_trace_check(require="done")


def test_doc2md_writes_only_under_mcp_out(ws: Path):
    r = server.doc2md_convert("docs/a.xlsx")
    assert r["outputs"] and all(p.startswith(".mcp-out/doc2md/") for p in r["outputs"])
    assert "--continue-on-error" in r["report"]["args"]


def test_missing_command(ws: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("DEVTOOLS_MCP_SPEC_TRACE")
    monkeypatch.setenv("PATH", "")
    assert "見つかりません" in server.spec_trace_check()["error"]
    assert server.tools_status()["tools"]["spec-trace"] is False


def _call(env: dict[str, str], tool: str, arguments: dict) -> tuple[list[str], dict]:
    async def go():
        params = StdioServerParameters(command=sys.executable, args=["-m", "devtools_mcp.server"], env={**os.environ, **env})
        async with stdio_client(params) as (r, w), ClientSession(r, w) as s:
            await s.initialize()
            names = [t.name for t in (await s.list_tools()).tools]
            res = await s.call_tool(tool, arguments)
            return names, json.loads(res.content[0].text)

    return asyncio.run(go())


def test_over_stdio_protocol(ws: Path):
    names, result = _call({}, "design_diff_spec", {"excel": "docs/a.xlsx", "oas": "docs/b.xlsx"})
    assert set(names) == {
        "tools_status",
        "doc2md_convert",
        "design_diff_excel",
        "design_diff_spec",
        "fieldcheck_validate",
        "spec_trace_check",
    }
    assert result["report"]["args"][0] == "spec"


def test_real_design_diff_if_available(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """design-diff のリポジトリが隣にあれば、本物のサンプルで差分を取る."""
    repo = Path(os.environ.get("DESIGN_DIFF_REPO", HERE.parents[2] / "design-diff"))
    if not (repo / "samples/before").is_dir():
        pytest.skip("design-diff のリポジトリが無い")
    try:
        import design_diff  # noqa: F401
    except ImportError:
        pytest.skip("design-diff が入っていない")
    monkeypatch.setenv("DEVTOOLS_MCP_ROOT", str(repo))
    monkeypatch.setenv("DEVTOOLS_MCP_DESIGN_DIFF", f"{sys.executable} -m design_diff")
    r = server.design_diff_excel("batch", "samples/before/JOB0001_注文取込.xlsx", "samples/after/JOB0001_注文取込.xlsx")
    assert r["exit_code"] == 0
    rules = {f["rule_id"] for f in r["report"]["findings"]}
    assert {"value_changed", "row_added", "cell_changed"} <= rules


def test_workspace_requires_root_and_blocks_secrets(tmp_path, monkeypatch):
    from devtools_mcp.runner import ToolError, Workspace

    monkeypatch.delenv("DEVTOOLS_MCP_ROOT", raising=False)
    with pytest.raises(ToolError):
        Workspace.from_env()
    monkeypatch.setenv("DEVTOOLS_MCP_ROOT", str(tmp_path))
    (tmp_path / ".env").write_text("KEY=1", encoding="utf-8")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("x", encoding="utf-8")
    ws = Workspace.from_env()
    for bad in (".env", ".git/config"):
        with pytest.raises(ToolError):
            ws.path(bad)


def test_design_diff_rejects_bad_doc_type(tmp_path, monkeypatch):
    monkeypatch.setenv("DEVTOOLS_MCP_ROOT", str(tmp_path))
    from devtools_mcp import server

    res = server.design_diff_excel("../../etc/passwd", "a.xlsx", "b.xlsx")
    assert res.get("error") or res.get("ok") is False or "doc_type" in json.dumps(res, ensure_ascii=False)
