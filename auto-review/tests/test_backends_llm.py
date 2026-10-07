"""anthropic の呼び出し先（devtools_common.llm.LLMClient）を FakeLLM で確かめる. 本物の API は呼ばない."""

from __future__ import annotations

import json

import pytest
from auto_review import backends, cli
from auto_review.backends import AnthropicBackend, BackendError, OutputTruncated, price
from devtools_common.llm import FakeLLM, price_for

SONNET = {"id": "claude-sonnet-5-5", "input_per_mtok": 99.0, "output_per_mtok": 99.0}

DIFF = """diff --git a/src/main/java/com/example/MemberService.java b/src/main/java/com/example/MemberService.java
--- a/src/main/java/com/example/MemberService.java
+++ b/src/main/java/com/example/MemberService.java
@@ -1,3 +1,4 @@
 class MemberService {
+  String admin = "taro.yamada@example.co.jp"; // 090-1234-5678
 }
"""


def _ok(text: str, *, stop: str = "end_turn", inp: int = 1000, out: int = 200, **usage) -> dict:
    return {
        "id": "msg_1",
        "model": "claude-sonnet-5-5",
        "content": [{"type": "text", "text": text}],
        "stop_reason": stop,
        "usage": {"input_tokens": inp, "output_tokens": out, **usage},
    }


def _overloaded(retry_after: str) -> dict:
    return {
        "status": 529,
        "headers": {"retry-after": retry_after, "request-id": "req_x"},
        "body": {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}},
    }


def _backend(responses: list[dict], sleeps: list[float] | None = None, **kw) -> tuple[AnthropicBackend, dict[str, FakeLLM]]:
    """FakeLLM を使う AnthropicBackend. 伏せ字は本番と同じく AnthropicBackend が決めたもの（be.masker）を渡す."""
    fakes: dict[str, FakeLLM] = {}
    holder: list[AnthropicBackend] = []

    def factory(model_id: str) -> FakeLLM:
        fake = FakeLLM(
            responses,
            model=model_id,
            sleep=(sleeps.append if sleeps is not None else (lambda s: None)),
            masker=holder[0].masker,
        )
        fakes[model_id] = fake
        return fake

    holder.append(AnthropicBackend(api_key="test-key", client_factory=factory, **kw))
    return holder[0], fakes


def test_retries_529_honoring_retry_after():
    sleeps: list[float] = []
    be, fakes = _backend([_overloaded("2"), _ok('```json\n{"findings": []}\n```')], sleeps)
    reply = be.call(SONNET, "system", "user", 4000, role="standard")
    assert reply.text.startswith("```json")
    assert len(sleeps) == 1 and 2.0 <= sleeps[0] <= 2.5  # retry-after の 2 秒 + 小さなゆらぎ
    assert len(fakes["claude-sonnet-5-5"].requests) == 2


def test_gives_up_after_retries_as_backend_error():
    be, _ = _backend([_overloaded("1")] * 4)
    with pytest.raises(BackendError, match="529"):
        be.call(SONNET, "s", "u", 100)


def test_non_retryable_error_is_backend_error_without_retry():
    bad = {"status": 400, "headers": {}, "body": {"type": "error", "error": {"type": "invalid_request_error", "message": "bad"}}}
    be, fakes = _backend([bad])
    with pytest.raises(BackendError, match="invalid_request_error"):
        be.call(SONNET, "s", "u", 100)
    assert len(fakes["claude-sonnet-5-5"].requests) == 1


def test_max_tokens_is_output_truncated():
    be, _ = _backend([_ok('{"findings": [', stop="max_tokens")])
    with pytest.raises(OutputTruncated, match="max_tokens"):
        be.call(SONNET, "s", "u", 100)


def test_cli_max_tokens_exits_3_as_unreadable(tmp_path, monkeypatch, capsys):
    d = tmp_path / "pr.diff"
    d.write_text(DIFF, encoding="utf-8")
    be, _ = _backend([_ok('{"findings": [', stop="max_tokens")])
    monkeypatch.setattr(cli, "get_backend", lambda name: be)
    assert cli.main(["run", str(d), "--yes"]) == 3
    assert "結果を読めません" in capsys.readouterr().err


def test_cost_uses_shared_price_table_and_cache_tokens():
    be, _ = _backend([_ok("x", inp=1000, out=200, cache_read_input_tokens=10_000)])
    reply = be.call(SONNET, "s", "u", 100)
    p = price_for("claude-sonnet-5-5")
    want = (1000 * p.input + 200 * p.output + 10_000 * p.cache_read) / 1_000_000
    assert reply.cost_usd == pytest.approx(want)
    assert reply.input_tokens == 11_000 and reply.output_tokens == 200


def test_price_prefers_shared_table_and_falls_back_to_router_yaml():
    p = price_for("claude-haiku-4-5-20251001")
    assert price({"id": "claude-haiku-4-5-20251001", "input_per_mtok": 99, "output_per_mtok": 99}, 1_000_000, 0) == p.input
    assert price({"id": "in-house-model", "input_per_mtok": 3.0, "output_per_mtok": 7.0}, 1_000_000, 1_000_000) == 10.0
    with pytest.raises(BackendError, match="単価"):
        price({"id": "in-house-model"}, 1, 1)


def test_emails_and_phones_in_diff_are_masked_before_sending(monkeypatch):
    monkeypatch.delenv("AUTO_REVIEW_MASK_PII", raising=False)
    be, fakes = _backend([_ok("x")])
    be.call(SONNET, "連絡先 hanako@example.com", "<diff>\n" + DIFF + "</diff>", 100)
    sent = json.dumps(fakes["claude-sonnet-5-5"].requests[0], ensure_ascii=False)
    assert "taro.yamada@example.co.jp" not in sent and "090-1234-5678" not in sent and "hanako@example.com" not in sent
    assert "＊＊＊" in sent and "MemberService" in sent


def test_masking_can_be_turned_off(monkeypatch):
    monkeypatch.setenv("AUTO_REVIEW_MASK_PII", "0")
    assert backends.default_masker() is None
    be, fakes = _backend([_ok("x")])
    be.call(SONNET, "s", DIFF, 100)
    assert "taro.yamada@example.co.jp" in json.dumps(fakes["claude-sonnet-5-5"].requests[0], ensure_ascii=False)


def test_claude_cli_masks_before_sending(monkeypatch):
    monkeypatch.delenv("AUTO_REVIEW_MASK_PII", raising=False)
    seen = {}

    def fake_run(cmd, input, **kw):  # noqa: A002 — subprocess.run と同じ引数名
        seen["cmd"], seen["input"] = cmd, input
        out = json.dumps({"result": "ok", "usage": {"input_tokens": 1, "output_tokens": 1}, "total_cost_usd": 0.001})
        return type("R", (), {"stdout": out, "stderr": "", "returncode": 0})()

    monkeypatch.setattr(backends.subprocess, "run", fake_run)
    reply = backends.ClaudeCliBackend(exe="claude").call(SONNET, "mail a@b.co", DIFF, 100, role="standard")
    assert reply.text == "ok"
    assert "taro.yamada@example.co.jp" not in seen["input"] and "a@b.co" not in " ".join(seen["cmd"])


def test_missing_api_key_is_backend_error(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    with pytest.raises(BackendError, match="ANTHROPIC_API_KEY"):
        AnthropicBackend()


def test_real_client_is_built_with_masker_and_base_url(monkeypatch):
    monkeypatch.delenv("AUTO_REVIEW_MASK_PII", raising=False)
    be = AnthropicBackend(api_key="k", base_url="https://gateway.example.internal", max_retries=5)
    client = be._client("claude-sonnet-5-5")
    assert client is be._client("claude-sonnet-5-5")  # モデルごとに 1 つ
    assert client.masker is be.masker and client.masker is not None
    assert client.base_url == "https://gateway.example.internal" and client.max_retries == 5 and client.timeout == 600


# ---- 構造化出力（道具の強制）


def _tool(data, *, name: str = "submit_review") -> dict:
    return {
        "id": "msg_t",
        "model": "claude-sonnet-5-5",
        "content": [{"type": "tool_use", "id": "toolu_1", "name": name, "input": data}],
        "stop_reason": "tool_use",
        "usage": {"input_tokens": 100, "output_tokens": 20},
    }


def test_schema_forces_the_result_tool_and_returns_its_input_as_json():
    from auto_review.findings import FINDINGS_SCHEMA

    be, fakes = _backend([_tool({"findings": [{"severity": "MUST", "title": "t"}]})])
    reply = be.call(SONNET, "s", "u", 100, role="standard", schema=FINDINGS_SCHEMA)
    assert json.loads(reply.text) == {"findings": [{"severity": "MUST", "title": "t"}]}
    req = fakes["claude-sonnet-5-5"].requests[0]
    assert req["tool_choice"] == {"type": "tool", "name": "submit_review"}
    assert req["tools"][0]["input_schema"] == FINDINGS_SCHEMA


def test_schema_reply_without_tool_is_unreadable():
    from auto_review.findings import FINDINGS_SCHEMA, UnreadableOutput

    be, _ = _backend([_ok("findings はありません")])
    with pytest.raises(UnreadableOutput):
        be.call(SONNET, "s", "u", 100, schema=FINDINGS_SCHEMA)


def test_reviewer_uses_structured_output_and_rejects_off_schema_tool_input(tmp_path, monkeypatch, capsys):
    """道具の入力がスキーマに合わない（findings が文字列）なら、cli は終了コード 3."""
    d = tmp_path / "pr.diff"
    d.write_text(DIFF, encoding="utf-8")
    be, _ = _backend([_tool({"findings": "なし"})])
    monkeypatch.setattr(cli, "get_backend", lambda name: be)
    assert cli.main(["run", str(d), "--yes"]) == 3
    assert "スキーマ" in capsys.readouterr().err
