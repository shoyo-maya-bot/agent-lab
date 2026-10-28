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
from mcp.server.mcpserver.exceptions import ToolError as MCPToolError

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
    # 利用者が直せる誤りは MCP のツールのエラー（isError）になる（プロトコルのエラーやクラッシュにしない）
    with pytest.raises(MCPToolError, match="作業フォルダの外"):
        server.fieldcheck_validate("../x.csv", "docs/b.xlsx")
    with pytest.raises(MCPToolError, match="implemented か proven"):
        server.spec_trace_check(require="done")


def test_doc2md_writes_only_under_mcp_out(ws: Path):
    r = server.doc2md_convert("docs/a.xlsx")
    assert r["outputs"] and all(p.startswith(".mcp-out/doc2md/") for p in r["outputs"])
    assert "--continue-on-error" in r["report"]["args"]


def test_missing_command(ws: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("DEVTOOLS_MCP_SPEC_TRACE")
    monkeypatch.setenv("PATH", "")
    with pytest.raises(MCPToolError, match="見つかりません"):
        server.spec_trace_check()
    assert server.tools_status()["tools"]["spec-trace"] is False


def _call(env: dict[str, str], calls: list[tuple[str, dict]]):
    async def go():
        params = StdioServerParameters(command=sys.executable, args=["-m", "devtools_mcp.server"], env={**os.environ, **env})
        async with stdio_client(params) as (r, w), ClientSession(r, w) as s:
            init = await s.initialize()
            tools = {t.name: t for t in (await s.list_tools()).tools}
            results = [await s.call_tool(name, arguments) for name, arguments in calls]
            return init, tools, results

    return asyncio.run(go())


def test_over_stdio_protocol(ws: Path):
    init, tools, (ok, bad) = _call(
        {},
        [
            ("design_diff_spec", {"excel": "docs/a.xlsx", "oas": "docs/b.xlsx"}),
            ("design_diff_spec", {"excel": "../outside.xlsx", "oas": "docs/b.xlsx"}),
        ],
    )
    assert set(tools) == {
        "tools_status",
        "doc2md_convert",
        "design_diff_excel",
        "design_diff_spec",
        "fieldcheck_validate",
        "spec_trace_check",
    }
    assert "データです" in (init.instructions or "")  # 結果の中の「指示」に従わないよう伝えている
    # 読み取りだけの道具と、.mcp-out/ に書く道具を区別して伝える
    assert tools["design_diff_spec"].annotations.read_only_hint is True
    assert tools["doc2md_convert"].annotations.read_only_hint is False
    assert tools["doc2md_convert"].annotations.destructive_hint is False
    assert all(t.annotations.open_world_hint is False for t in tools.values())

    assert not ok.is_error
    result = ok.structured_content or json.loads(ok.content[0].text)
    assert result["report"]["args"][0] == "spec"

    assert bad.is_error  # 引数の誤りはツールのエラー. 理由の文章がエージェントに届く
    assert "作業フォルダの外" in bad.content[0].text


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

    with pytest.raises(MCPToolError, match="doc_type"):
        server.design_diff_excel("../../etc/passwd", "a.xlsx", "b.xlsx")


def test_doc2md_recursive_skips_git_and_previous_outputs(ws: Path):
    """recursive: .git・前回までの出力（.mcp-out/）・隠しフォルダは探さない（doc2md -r に任せると全部変換していた）."""
    for d in (".git/objects", ".mcp-out/doc2md/20260101-000000", ".venv/lib", "docs/sub/deep"):
        (ws / d).mkdir(parents=True)
    (ws / ".mcp-out/doc2md/20260101-000000/old.docx").write_bytes(b"x")
    r = server.doc2md_convert(".", recursive=True)
    args = r["report"]["args"]
    inputs = args[: args.index("-d")]
    assert inputs == [str(ws), *(str(ws / d) for d in ("config", "docs", "docs/sub", "docs/sub/deep"))]
    assert "-r" not in args
    assert not any("/." in Path(p).as_posix().replace(ws.as_posix(), "") for p in inputs)
    # recursive なし・ファイルの指定は今までどおり 1 つだけ渡す
    assert server.doc2md_convert("docs")["report"]["args"][0] == str(ws / "docs")
