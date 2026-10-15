"""課金された後に失敗した実行（出力が切れた・読めない）の費用も、コメントの隠しマーカーの累計に入れる.

入れないと次の実行の --prior-comment で累計に数えられず、budget.per_pr_total_usd が効かない. 本物の API は呼ばない.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from auto_review import cli
from auto_review.backends import AnthropicBackend, OutputTruncated, Reply
from auto_review.findings import UnreadableOutput
from auto_review.render import SPENT_MARKER, parse_spent
from auto_review.router import load_config
from devtools_common.llm import FakeLLM

CFG = load_config()

JAVA_SERVICE = """\
diff --git a/app/src/main/java/x/MemberService.java b/app/src/main/java/x/MemberService.java
--- a/app/src/main/java/x/MemberService.java
+++ b/app/src/main/java/x/MemberService.java
@@ -10,3 +10,4 @@ class MemberService {
     void a() {}
-    void b() {}
+    @Transactional
+    void b() { lock(); }
 }
"""


def _resp(content: list[dict], stop: str = "end_turn") -> dict:
    return {
        "id": "msg_1",
        "model": "claude-sonnet-5-5",
        "content": content,
        "stop_reason": stop,
        "usage": {"input_tokens": 100_000, "output_tokens": 4_000},
    }


def _anthropic(responses: list[dict]) -> AnthropicBackend:
    return AnthropicBackend(api_key="test-key", client_factory=lambda m: FakeLLM(responses, model=m, sleep=lambda s: None))


def _paths(tmp_path: Path, total: float = 3.0) -> tuple[Path, Path]:
    cfg = json.loads(json.dumps(CFG))
    cfg["budget"]["per_pr_usd"], cfg["budget"]["per_pr_total_usd"] = 5.0, total
    c = tmp_path / "router.yaml"
    c.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
    d = tmp_path / "pr.diff"
    d.write_text(JAVA_SERVICE, encoding="utf-8")
    return c, d


@pytest.mark.parametrize(
    "response",
    [
        _resp([{"type": "text", "text": '{"findings": ['}], stop="max_tokens"),  # 切れた
        _resp([{"type": "text", "text": "指摘はありません"}]),  # 結果の道具を使わなかった
    ],
    ids=["truncated", "no-tool"],
)
def test_failed_run_after_billing_records_spent_in_comment(tmp_path, monkeypatch, capsys, response):
    c, d = _paths(tmp_path)
    md, js = tmp_path / "comment.md", tmp_path / "report.json"
    monkeypatch.setattr(cli, "get_backend", lambda name: _anthropic([response]))
    prev = tmp_path / "prev.md"
    prev.write_text(f"<!-- auto-review -->\n<!-- {SPENT_MARKER}: 0.5000 -->\n", encoding="utf-8")
    argv = ["run", str(d), "--config", str(c), "--yes", "--labels", "review:standard"]
    assert cli.main(argv + ["--prior-comment", str(prev), "--out-md", str(md), "--out-json", str(js)]) == 3
    err = capsys.readouterr().err
    assert "結果を読めません" in err and "記録しました" in err
    text = md.read_text(encoding="utf-8")
    assert "最後までできませんでした" in text and "指摘はありません。" not in text
    spent = parse_spent(text)
    assert spent > 0.5  # 前回の 0.5 + 今回課金された額
    rep = json.loads(js.read_text(encoding="utf-8"))
    assert rep["error"] and rep["pr_total_usd"] == spent and rep["summary"]["findings"] == 0
    assert rep["calls"] and not rep["calls"][0]["estimated"]


def test_recorded_failure_counts_toward_next_run_total(tmp_path, monkeypatch):
    """失敗した実行の記録を次の実行に渡すと、累計の予算で止まる（以前は 0 のままで止まらなかった）."""
    c, d = _paths(tmp_path, total=0.25)
    md = tmp_path / "comment.md"
    truncated = _resp([{"type": "text", "text": "{"}], stop="max_tokens")
    monkeypatch.setattr(cli, "get_backend", lambda name: _anthropic([truncated]))
    argv = ["run", str(d), "--config", str(c), "--yes", "--labels", "review:standard"]
    assert cli.main(argv + ["--out-md", str(md)]) == 3
    assert parse_spent(md.read_text(encoding="utf-8")) > 0.0

    calls: list[str] = []

    class Never:
        masker = None

        def call(self, *a, **k):
            calls.append("x")
            return Reply(json.dumps({"findings": []}), 1, 1, 0.0, 0.0)

    monkeypatch.setattr(cli, "get_backend", lambda name: Never())
    assert cli.main(argv + ["--prior-comment", str(md), "-f", "json"]) == 3  # 累計の予算を超える見込みで呼ばない
    assert calls == []


def test_failure_without_billing_writes_nothing(tmp_path, monkeypatch):
    """課金された呼び出しが無い失敗（API エラー）では、前回のコメントを置き換えないよう何も書かない."""
    c, d = _paths(tmp_path)
    md = tmp_path / "comment.md"
    bad = {"status": 400, "headers": {}, "body": {"type": "error", "error": {"type": "invalid_request_error", "message": "x"}}}
    monkeypatch.setattr(cli, "get_backend", lambda name: _anthropic([bad]))
    assert cli.main(["run", str(d), "--config", str(c), "--yes", "--out-md", str(md)]) == 3
    assert not md.exists()


def test_backend_attaches_spent_reply_to_errors():
    be = _anthropic([_resp([{"type": "text", "text": "{"}], stop="max_tokens")])
    model = {"id": "claude-sonnet-5-5"}
    with pytest.raises(OutputTruncated) as ei:
        be.call(model, "s", "u", 100)
    assert ei.value.reply is not None and ei.value.reply.cost_usd > 0
    from auto_review.findings import FINDINGS_SCHEMA

    be = _anthropic([_resp([{"type": "text", "text": "なし"}])])
    with pytest.raises(UnreadableOutput) as ei2:
        be.call(model, "s", "u", 100, schema=FINDINGS_SCHEMA)
    assert ei2.value.reply is not None and ei2.value.reply.output_tokens == 4_000


def test_example_workflow_posts_comment_when_billed_run_failed():
    wf = (Path(__file__).resolve().parents[2] / "examples" / "auto-review.workflow.yml").read_text(encoding="utf-8")
    assert "pr_total_usd > .spent_before_usd" in wf
    assert 'exit "$code"' in wf  # コメントを書いた後で、終了コードどおりに失敗させる
