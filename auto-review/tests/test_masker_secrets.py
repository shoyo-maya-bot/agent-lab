"""既定の伏せ字（default_masker）は、メール・電話に加えて API キー・トークンらしい文字列も伏せる.

キーの形の文字列は、秘密情報スキャンに引っかからないよう実行時に組み立てる（すべて架空）。本物の API は呼ばない.
"""

from __future__ import annotations

import json

import pytest
from auto_review.backends import AnthropicBackend, default_masker
from devtools_common.llm import FakeLLM

SECRETS = [
    "sk-" + "ant-api03-" + "Ab3_" * 10,
    "sk-" + "proj-" + "Zx9" * 10,
    "AK" + "IA" + "ABCDEFGHIJ234567",
    "gh" + "p_" + "a1B2" * 9,
    "github" + "_pat_" + "11ABCDEFG0" + "x" * 30,
]


@pytest.mark.parametrize("secret", SECRETS)
def test_default_masker_masks_secrets(monkeypatch, secret):
    monkeypatch.delenv("AUTO_REVIEW_MASK_PII", raising=False)
    m = default_masker()
    assert m is not None
    assert secret not in m.text(f'+  String key = "{secret}";')


def test_secrets_in_diff_are_not_sent(monkeypatch):
    monkeypatch.delenv("AUTO_REVIEW_MASK_PII", raising=False)
    fakes: list[FakeLLM] = []
    holder: list[AnthropicBackend] = []

    def factory(model_id: str) -> FakeLLM:
        resp = {
            "id": "m",
            "model": model_id,
            "content": [{"type": "text", "text": "{}"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }
        fakes.append(FakeLLM([resp], model=model_id, masker=holder[0].masker))
        return fakes[-1]

    holder.append(AnthropicBackend(api_key="test-key", client_factory=factory))
    holder[0].call({"id": "claude-sonnet-5-5"}, "system", "diff:\n" + "\n".join(SECRETS), 100)
    sent = json.dumps(fakes[0].requests[0], ensure_ascii=False)
    for s in SECRETS:
        assert s not in sent


def test_masking_can_still_be_turned_off(monkeypatch):
    monkeypatch.setenv("AUTO_REVIEW_MASK_PII", "0")
    assert default_masker() is None
