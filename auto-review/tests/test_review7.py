"""第 7 回の指摘: テストのファイルの判定（部分一致をやめる）、テストに当てる規則の明示、claude-cli の出力の上限."""

from __future__ import annotations

import json
import subprocess

import pytest
from auto_review import backends
from auto_review.backends import OutputTruncated
from auto_review.diff import FileChange, is_test_path
from auto_review.router import load_config, pattern_applies_to_tests, score_changes

SONNET = {"id": "claude-sonnet-5-5", "input_per_mtok": 1.0, "output_per_mtok": 1.0}


@pytest.mark.parametrize(
    "path, expected",
    [
        ("src/test_support/security/AuthFilter.java", False),  # テストの語を含むだけのフォルダ
        ("app/testing/Helper.java", False),
        ("app/contest/Rules.java", False),
        ("test_member.py", True),  # ルートの test_*.py
        ("tests/helper.py", True),  # ルートの tests/
        ("test/java/x/Helper.java", True),
        ("pkg/tests/data/input.json", True),
        ("pkg/sub/test_member.py", True),
        ("pkg/sub/member_test.py", True),
        ("Tests/Helper.cs", True),
    ],
)
def test_is_test_path_uses_folder_and_file_names(path, expected):
    assert is_test_path(path) is expected
    assert FileChange(path).is_test is expected


def test_test_support_security_is_scored():
    cfg = load_config()
    ch = FileChange("src/test_support/security/AuthFilter.java", added=[(1, "  return true;")])
    total, reasons, _ = score_changes([ch], cfg)
    assert total >= 4 and any("認証" in r for r in reasons)


def test_root_test_file_counts_deleted_tests():
    cfg = load_config()
    ch = FileChange("test_member.py", removed=["def test_a():", "def test_b():"], added=[(1, "def test_a():")])
    total, reasons, _ = score_changes([ch], cfg)
    assert total == cfg["risk"]["deleted_tests_weight"]
    assert any("テストが 1 件減った" in r for r in reasons)


def test_in_tests_flag_decides_which_patterns_apply_to_tests():
    assert pattern_applies_to_tests({"reason": "x", "in_tests": True}) is True
    assert pattern_applies_to_tests({"reason": "秘密情報", "in_tests": False}) is False
    assert pattern_applies_to_tests({"reason": "秘密情報らしき値"}) is True  # 書いていない規則は従来どおり
    assert pattern_applies_to_tests({"reason": "同時実行"}) is False
    rules = load_config()["risk"]["patterns"]
    assert [r["reason"] for r in rules if pattern_applies_to_tests(r)] == ["秘密情報らしき値"]


def test_secret_in_test_file_is_scored_by_flag():
    cfg = load_config()
    cfg["risk"]["patterns"] = [{"regex": "PASSWORD_X", "weight": 5, "reason": "独自の規則", "in_tests": True}]
    ch = FileChange("tests/test_login.py", added=[(1, "PASSWORD_X = 1")])
    total, _, _ = score_changes([ch], cfg)
    assert total == 5


def _fake_cli(monkeypatch, payload: dict):
    seen: dict = {}

    def fake_run(cmd, input, **kw):  # noqa: A002 — subprocess.run と同じ引数名
        seen["cmd"], seen["env"] = cmd, kw.get("env")
        return subprocess.CompletedProcess(cmd, 0, json.dumps(payload), "")

    monkeypatch.setattr(backends.subprocess, "run", fake_run)
    return seen


def test_claude_cli_passes_max_tokens(monkeypatch):
    seen = _fake_cli(monkeypatch, {"result": "ok", "usage": {"input_tokens": 1, "output_tokens": 1}})
    backends.ClaudeCliBackend(exe="claude").call(SONNET, "s", "u", 1234, role="standard")
    assert seen["env"]["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] == "1234"


def test_claude_cli_truncated_output_is_reported_with_cost(monkeypatch):
    payload = {"result": "{", "stop_reason": "max_tokens", "usage": {"input_tokens": 5, "output_tokens": 9}, "total_cost_usd": 0.01}
    _fake_cli(monkeypatch, payload)
    with pytest.raises(OutputTruncated) as e:
        backends.ClaudeCliBackend(exe="claude").call(SONNET, "s", "u", 9, role="standard")
    assert e.value.reply is not None and e.value.reply.cost_usd == 0.01
