"""auto-review の単体テスト（モデルは呼ばない. 呼び出し先は差し替える）."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from auto_review import cli
from auto_review.backends import Reply
from auto_review.diff import parse_diff
from auto_review.findings import Finding, merge, parse_findings, parse_verdicts
from auto_review.render import MARKER, to_markdown
from auto_review.review import Reviewer
from auto_review.router import glob_match, load_config, plan

CFG = load_config()

JAVA_SERVICE = """\
diff --git a/app/src/main/java/x/MemberService.java b/app/src/main/java/x/MemberService.java
index 1..2 100644
--- a/app/src/main/java/x/MemberService.java
+++ b/app/src/main/java/x/MemberService.java
@@ -10,3 +10,4 @@ class MemberService {
     void a() {}
-    void b() {}
+    @Transactional
+    void b() { lock(); }
 }
"""

DOCS_ONLY = """\
diff --git a/README.md b/README.md
--- a/README.md
+++ b/README.md
@@ -1 +1 @@
-old
+new
"""

DELETED_TEST = """\
diff --git a/app/src/test/java/x/GuardTest.java b/app/src/test/java/x/GuardTest.java
deleted file mode 100644
--- a/app/src/test/java/x/GuardTest.java
+++ /dev/null
@@ -1,3 +0,0 @@
-class GuardTest {
-    @Test void a() {}
-}
"""


def test_parse_diff_tracks_status_and_line_numbers():
    ch = parse_diff(JAVA_SERVICE + DELETED_TEST)
    assert [c.path for c in ch] == ["app/src/main/java/x/MemberService.java", "app/src/test/java/x/GuardTest.java"]
    assert ch[0].added == [(11, "    @Transactional"), (12, "    void b() { lock(); }")]
    assert ch[0].removed == ["    void b() {}"]
    assert ch[1].status == "deleted" and ch[1].is_test


@pytest.mark.parametrize(
    ("path", "glob", "ok"),
    [
        ("a/b/MemberService.java", "**/*Service.java", True),
        ("MemberService.java", "**/*Service.java", True),
        ("a/b/MemberService.java.bak", "**/*Service.java", False),
        (".github/workflows/ci.yml", ".github/workflows/**", True),
        ("docs/x/y.md", "docs/**", True),
        ("src/docs.java", "docs/**", False),
    ],
)
def test_glob_match(path, glob, ok):
    assert glob_match(path, glob) is ok


def test_plan_scores_paths_and_patterns():
    p = plan(parse_diff(JAVA_SERVICE), CFG)
    assert p.tier == "standard"
    assert p.score == 4  # Service（2）+ @Transactional（2）
    assert p.risky_files == ["app/src/main/java/x/MemberService.java"]


def test_plan_skip_and_labels():
    assert plan(parse_diff(DOCS_ONLY), CFG).tier == "skip"
    assert plan(parse_diff(JAVA_SERVICE), CFG, author="dependabot[bot]").tier == "skip"
    assert plan(parse_diff(JAVA_SERVICE), CFG, labels=["review:deep"]).tier == "deep"
    assert plan(parse_diff(JAVA_SERVICE), CFG, labels=["review:skip"]).tier == "skip"


def test_deleted_tests_push_to_deep():
    p = plan(parse_diff(JAVA_SERVICE + DELETED_TEST), CFG)
    assert p.score == 7 and p.tier == "deep"
    assert any("テストの削除" in r for r in p.reasons)


def test_parse_findings_accepts_fenced_json_and_normalizes():
    text = 'まえおき\n```json\n{"findings": [{"severity": "nit", "file": "A.java", "line": "3", "title": "t"}, {"severity": "MUST"}]}\n```'
    fs = parse_findings(text, "m", "s")
    assert len(fs) == 1  # 題名の無いものは捨てる
    assert (fs[0].severity, fs[0].line, fs[0].id) == ("NITS", 3, "s1")


def test_merge_joins_same_problem_and_marks_agreement():
    a = Finding("WANT", "x/MemberService.java", "同時登録で 500 になる", line=30, models=["sonnet"], id="s1")
    b = Finding("MUST", "MemberService.java", "同時登録で 500", line=33, models=["opus"], id="d1")
    c = Finding("WANT", "OrderJob.java", "別の問題", models=["opus"], id="d2")
    out = merge([a], [b, c])
    assert len(out) == 2
    assert out[0].models == ["sonnet", "opus"] and out[0].status == "confirmed" and out[0].severity == "MUST"


class FakeBackend:
    """呼ばれた順に用意した返答を返す."""

    def __init__(self, replies: list[str]):
        self.replies = list(replies)
        self.models: list[str] = []

    def call(self, model, system, user, max_tokens):
        self.models.append(model["id"])
        return Reply(self.replies.pop(0), 1000, 200, 0.01, 0.1)


def _std(findings):
    return "```json\n" + json.dumps({"findings": findings}, ensure_ascii=False) + "\n```"


def test_standard_only_when_no_must():
    fb = FakeBackend([_std([{"severity": "WANT", "file": "MemberService.java", "title": "t"}])])
    changes = parse_diff(JAVA_SERVICE)
    r = Reviewer(CFG, fb).auto(JAVA_SERVICE, changes, plan(changes, CFG))
    assert fb.models == [CFG["models"]["standard"]["id"]]
    assert r.open_must == 0 and len(r.findings) == 1


def test_must_is_verified_by_deep_model_and_rejections_are_hidden():
    std = _std(
        [
            {"severity": "MUST", "file": "MemberService.java", "line": 12, "title": "本物"},
            {"severity": "MUST", "file": "MemberService.java", "line": 40, "title": "勘違い"},
        ]
    )
    deep = json.dumps(
        {
            "verdicts": [{"id": "s1", "verdict": "confirm", "reason": "ok"}, {"id": "s2", "verdict": "reject", "reason": "起きない"}],
            "findings": [],
        }
    )
    fb = FakeBackend([std, deep])
    changes = parse_diff(JAVA_SERVICE)
    r = Reviewer(CFG, fb).auto(JAVA_SERVICE, changes, plan(changes, CFG))
    assert fb.models == [CFG["models"]["standard"]["id"], CFG["models"]["deep"]["id"]]
    assert [(f.title, f.status) for f in r.findings] == [("本物", "confirmed"), ("勘違い", "rejected")]
    assert r.open_must == 1
    md = to_markdown(r)
    assert md.startswith(MARKER) and "検証で却下した指摘（1 件）" in md and "MUST 2 件を deep" in md


def test_budget_skips_deep_when_too_expensive():
    cfg = json.loads(json.dumps(CFG))
    cfg["budget"]["per_pr_usd"] = 0.0001
    fb = FakeBackend([_std([])])
    changes = parse_diff(JAVA_SERVICE + DELETED_TEST)
    r = Reviewer(cfg, fb).auto(JAVA_SERVICE, changes, plan(changes, cfg))
    assert len(fb.models) == 1
    assert any("予算" in n for n in r.notes)


def test_parse_verdicts():
    assert parse_verdicts('{"verdicts": [{"id": "s1", "verdict": "uncertain"}]}')["s1"]["verdict"] == "uncertain"


def test_cli_plan_and_dry_run(tmp_path: Path, capsys):
    d = tmp_path / "pr.diff"
    d.write_text(JAVA_SERVICE + DELETED_TEST, encoding="utf-8")
    assert cli.main(["plan", str(d), "-f", "json"]) == 0
    assert json.loads(capsys.readouterr().out)["tier"] == "deep"
    out = tmp_path / "r.json"
    assert cli.main(["run", str(d), "--out-json", str(out)]) == 0  # --yes なし = 見積もりだけ
    rep = json.loads(out.read_text(encoding="utf-8"))
    assert [c["step"] for c in rep["calls"]] == ["standard", "deep"]
    assert all(c["estimated"] for c in rep["calls"]) and rep["cost_usd"] > 0


def test_cli_bad_input_returns_2(tmp_path: Path):
    assert cli.main(["plan", str(tmp_path / "missing.diff")]) == 2


def test_uncertain_verdict_is_not_upgraded_by_merge():
    """検証役が「決められない」とした指摘は、同じ問題を新しい指摘として出しても「検証済み」にしない."""
    std = _std([{"severity": "MUST", "file": "MemberService.java", "line": 33, "title": "一意制約が無いと重複登録が通る"}])
    deep = json.dumps(
        {
            "verdicts": [{"id": "s1", "verdict": "uncertain", "reason": "DDL が見えないので決められない"}],
            "findings": [{"severity": "MUST", "file": "MemberService.java", "line": 34, "title": "一意制約が無いと重複登録"}],
        }
    )
    fb = FakeBackend([std, deep])
    changes = parse_diff(JAVA_SERVICE)
    r = Reviewer(CFG, fb).auto(JAVA_SERVICE, changes, plan(changes, CFG))
    assert [f.status for f in r.findings] == ["uncertain"]
    assert "要確認" in to_markdown(r)
