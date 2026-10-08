"""runner のテスト（MCP の SDK を使わない部分. どの OS でも流す）."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from devtools_mcp import runner


def test_windows_paths_keep_backslashes():
    cmd = r"C:\Users\a\.venv\Scripts\python.exe -m doc_to_markdown"
    assert runner.split_command(cmd, windows=True) == [r"C:\Users\a\.venv\Scripts\python.exe", "-m", "doc_to_markdown"]


def test_windows_quoted_path_with_spaces():
    cmd = r'"C:\Program Files\Python312\python.exe" -m design_diff'
    assert runner.split_command(cmd, windows=True) == [r"C:\Program Files\Python312\python.exe", "-m", "design_diff"]


def test_posix_split_is_unchanged():
    assert runner.split_command("python -m 'x y'", windows=False) == ["python", "-m", "x y"]


def test_json_array_works_on_any_os():
    value = json.dumps([r"C:\Users\a\.venv\Scripts\python.exe", "-m", "doc_to_markdown"])
    for win in (True, False):
        assert runner.split_command(value, windows=win) == [r"C:\Users\a\.venv\Scripts\python.exe", "-m", "doc_to_markdown"]


@pytest.mark.parametrize("bad", ["[", "[]", '["a", 1]', '[""]'])
def test_bad_json_array_is_an_error(bad):
    with pytest.raises(runner.ToolError):
        runner.split_command(bad)


def test_resolve_command_reads_json_from_env(monkeypatch):
    monkeypatch.setenv("DEVTOOLS_MCP_DOC2MD", '["py", "-m", "doc_to_markdown"]')
    assert runner.resolve_command("doc2md") == ["py", "-m", "doc_to_markdown"]


@pytest.mark.parametrize("name", [".ENV", ".Env", ".GIT/config", ".Git/HEAD", ".ENV.local"])
def test_secret_places_are_blocked_regardless_of_case(tmp_path: Path, monkeypatch, name):
    monkeypatch.setenv("DEVTOOLS_MCP_ROOT", str(tmp_path))
    w = runner.Workspace.from_env()
    with pytest.raises(runner.ToolError, match="秘密情報"):
        w.path(name, must_exist=False)


def test_cli_writes_japanese_as_utf8(tmp_path: Path, monkeypatch):
    """日本語を出す CLI でも（Windows の既定の文字コードに関係なく）文字化け・失敗にならない."""
    import sys

    monkeypatch.setenv("DEVTOOLS_MCP_ROOT", str(tmp_path))
    monkeypatch.setenv("PYTHONIOENCODING", "ascii")  # 既定の文字コードが日本語を書けない環境のまね
    monkeypatch.setenv(
        "DEVTOOLS_MCP_FIELDCHECK",
        json.dumps([sys.executable, "-c", "import json; print(json.dumps({'表': '申請一覧'}, ensure_ascii=False))"]),
    )
    r = runner.run("fieldcheck", [], runner.Workspace.from_env())
    assert r["exit_code"] == 0 and r["report"] == {"表": "申請一覧"}
