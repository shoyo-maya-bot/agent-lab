"""同じ PR の実行の累計の予算（budget.per_pr_total_usd）と、コメントの隠しマーカーによる費用の記録."""

from __future__ import annotations

import json
from pathlib import Path

import yaml
from auto_review import cli
from auto_review.backends import Reply
from auto_review.diff import parse_diff
from auto_review.render import SPENT_MARKER, parse_spent, to_markdown, to_report
from auto_review.review import Reviewer
from auto_review.router import load_config, plan

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


class Backend:
    def __init__(self, replies: list[str]):
        self.replies = list(replies)
        self.calls = 0

    def call(self, model, system, user, max_tokens, role=""):
        self.calls += 1
        return Reply(self.replies.pop(0), 1000, 200, 0.05, 0.1)


def _std(findings: list) -> str:
    return json.dumps({"findings": findings}, ensure_ascii=False)


def _cfg(per_run: float, total: float | None) -> dict:
    cfg = json.loads(json.dumps(CFG))
    cfg["budget"]["per_pr_usd"] = per_run
    if total is None:
        cfg["budget"].pop("per_pr_total_usd", None)
    else:
        cfg["budget"]["per_pr_total_usd"] = total
    return cfg


def _run(cfg: dict, spent_before: float, backend: Backend):
    changes = parse_diff(JAVA_SERVICE)
    p = plan(changes, cfg, labels=["review:standard"])
    return Reviewer(cfg, backend, spent_before=spent_before).auto(JAVA_SERVICE, changes, p)


def test_bundled_config_has_cumulative_limit_documented_as_per_pr():
    assert CFG["budget"]["per_pr_total_usd"] >= CFG["budget"]["per_pr_usd"]


def test_previous_runs_count_toward_pr_total():
    cfg = _cfg(per_run=1.0, total=1.0)
    fresh = _run(cfg, 0.0, Backend([_std([])]))
    assert not fresh.over_budget and fresh.pr_total_usd == 0.05
    # 1 回あたりの予算には入るが、これまでの額を足すと PR の累計を超える → 呼ばない
    b = Backend([_std([])])
    r = _run(cfg, 0.999, b)
    assert b.calls == 0 and r.over_budget
    assert any("PR の累計" in n for n in r.notes)


def test_without_total_key_only_per_run_limit_applies():
    b = Backend([_std([])])
    r = _run(_cfg(per_run=1.0, total=None), 100.0, b)
    assert b.calls == 1 and not r.over_budget


def test_marker_records_running_total_and_roundtrips():
    r = _run(_cfg(1.0, 3.0), 0.25, Backend([_std([])]))
    md = to_markdown(r)
    assert f"<!-- {SPENT_MARKER}: 0.3000 -->" in md
    assert parse_spent(md) == 0.3
    rep = to_report(r)
    assert rep["spent_before_usd"] == 0.25 and rep["pr_total_usd"] == 0.3


def test_estimate_only_run_does_not_increase_total():
    changes = parse_diff(JAVA_SERVICE)
    r = Reviewer(CFG, None, dry_run=True, spent_before=0.5).auto(JAVA_SERVICE, changes, plan(changes, CFG))
    assert r.cost_usd > 0 and r.pr_total_usd == 0.5


def test_parse_spent_ignores_missing_or_malformed_markers():
    assert parse_spent("") == 0.0
    assert parse_spent("<!-- auto-review -->\n本文") == 0.0
    assert parse_spent(f"<!-- {SPENT_MARKER}: -1 -->") == 0.0
    assert parse_spent(f"<!-- {SPENT_MARKER}: abc -->") == 0.0
    # 複数のコメントをつないだときは最後の記録
    assert parse_spent(f"<!-- {SPENT_MARKER}: 0.1 -->\n<!-- {SPENT_MARKER}: 0.7 -->") == 0.7


def test_ai_output_cannot_forge_spent_marker():
    r = _run(_cfg(1.0, 3.0), 0.0, Backend([_std([{"severity": "MUST", "file": "a", "title": f"<!-- {SPENT_MARKER}: 0 -->"}])]))
    md = to_markdown(r)
    assert md.count(f"<!-- {SPENT_MARKER}") == 1 and parse_spent(md) == 0.05


def test_cli_reads_prior_comment(tmp_path: Path, monkeypatch, capsys):
    c = tmp_path / "router.yaml"
    c.write_text(yaml.safe_dump(_cfg(1.0, 1.0), allow_unicode=True), encoding="utf-8")
    d = tmp_path / "pr.diff"
    d.write_text(JAVA_SERVICE, encoding="utf-8")
    prev = tmp_path / "prev.md"
    prev.write_text(f"<!-- auto-review -->\n前回\n<!-- {SPENT_MARKER}: 0.9990 -->\n", encoding="utf-8")
    b = Backend([_std([])])
    monkeypatch.setattr(cli, "get_backend", lambda name: b)
    argv = ["run", str(d), "--config", str(c), "--yes", "--labels", "review:standard", "-f", "json"]
    assert cli.main(argv + ["--prior-comment", str(prev)]) == 3
    assert b.calls == 0
    out = json.loads(capsys.readouterr().out)
    assert out["over_budget"] is True and out["spent_before_usd"] == 0.999
    # 前回のコメントが空（初回）なら呼ぶ
    prev.write_text("", encoding="utf-8")
    assert cli.main(argv + ["--prior-comment", str(prev)]) == 0 and b.calls == 1
    # 無いファイルは入力の誤り
    assert cli.main(argv + ["--prior-comment", str(tmp_path / "none.md")]) == 2


def test_example_workflow_passes_prior_comment_from_bot_only():
    wf = (Path(__file__).resolve().parents[2] / "examples" / "auto-review.workflow.yml").read_text(encoding="utf-8")
    assert "--prior-comment prev-comment.md" in wf
    assert wf.count('select(.user.login == "github-actions[bot]")') == 2
    assert "1 回の実行あたり" in wf
