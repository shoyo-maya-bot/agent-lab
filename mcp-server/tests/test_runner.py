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


# -- 第 7 回: DEVTOOLS_MCP_TIMEOUT の誤りで起動に失敗しない、同じ秒の 2 回目でも出力先を作れる ----------------
@pytest.mark.parametrize("value", ["5m", "", "0", "-3", "nan", "inf", "abc"])
def test_bad_timeout_falls_back_to_default(value, capsys):
    assert runner.timeout_from_env(value) == runner.DEFAULT_TIMEOUT
    if value.strip():
        assert "DEVTOOLS_MCP_TIMEOUT" in capsys.readouterr().err


@pytest.mark.parametrize("value, expected", [("60", 60.0), ("1.5", 1.5), (" 120 ", 120.0), (None, 300.0)])
def test_good_timeout_is_used(value, expected):
    assert runner.timeout_from_env(value) == expected


def test_out_dir_in_the_same_second_gets_a_new_folder(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(runner.time, "strftime", lambda fmt: "20261023-080000")
    ws = runner.Workspace(tmp_path)
    dirs = [ws.out_dir("doc2md") for _ in range(3)]
    assert [d.name for d in dirs] == ["20261023-080000", "20261023-080000-2", "20261023-080000-3"]
    assert all(d.is_dir() for d in dirs)


def test_out_dir_does_not_fail_when_another_call_creates_it_first(tmp_path: Path, monkeypatch):
    # exists() と mkdir() の間に別の呼び出しが同じ名前を作っても FileExistsError にならない
    monkeypatch.setattr(runner.time, "strftime", lambda fmt: "20261023-080000")
    ws = runner.Workspace(tmp_path)
    (tmp_path / ".mcp-out" / "doc2md" / "20261023-080000").mkdir(parents=True)
    d = ws.out_dir("doc2md")
    assert d.name == "20261023-080000-2"
