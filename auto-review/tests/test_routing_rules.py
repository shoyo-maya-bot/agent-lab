"""テストファイルの判定・deep 段の読めない出力・`review:standard` ラベル・文書だけの変更の振り分け."""

from __future__ import annotations

import json

import pytest
from auto_review.backends import Reply
from auto_review.diff import FileChange, parse_diff
from auto_review.findings import UnreadableOutput
from auto_review.review import Reviewer
from auto_review.router import load_config, plan

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


def _docs_diff(path: str, line: str) -> str:
    return f"""\
diff --git a/{path} b/{path}
--- a/{path}
+++ b/{path}
@@ -1 +1 @@
-old
+{line}
"""


def _std(findings) -> str:
    return json.dumps({"findings": findings}, ensure_ascii=False)


class Backend:
    """呼ばれた順に返答を返す. 例外を入れておくとそれを投げる."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.roles: list[str] = []

    def call(self, model, system, user, max_tokens, role=""):
        self.roles.append(role)
        r = self.replies.pop(0)
        if isinstance(r, BaseException):
            raise r
        return Reply(r, 1000, 200, 0.01, 0.1)


MUST = [{"severity": "MUST", "file": "MemberService.java", "line": 12, "title": "排他が外れる"}]


# -- 1. テストのファイルの判定 ---------------------------------------------------
@pytest.mark.parametrize(
    "path, expected",
    [
        ("app/src/main/java/x/Contest.java", False),
        ("app/src/main/java/x/Latest.java", False),
        ("app/src/main/java/x/Manifest.java", False),
        ("app/src/main/java/x/Greatest.java", False),
        ("app/src/main/java/x/MemberServiceTest.java", True),
        ("app/src/main/java/x/MemberServiceTests.java", True),
        ("app/src/main/java/x/MemberApiIT.java", True),
        ("app/src/test/java/x/Helper.java", True),  # src/test/ 配下は従来どおり
        ("app/src/TEST/java/x/Helper.java", True),
        ("tools/tests/helper.py", True),
        ("tools/x/test_member.py", True),
        ("tools/x/member_test.py", True),
        ("app/src/test/resources/member.feature", True),
        ("app/src/main/java/x/MemberServicetest.java", False),
    ],
)
def test_is_test(path, expected):
    assert FileChange(path).is_test is expected


def test_contest_is_scored_as_production_code():
    diff = """\
diff --git a/app/src/main/java/x/Contest.java b/app/src/main/java/x/Contest.java
--- a/app/src/main/java/x/Contest.java
+++ b/app/src/main/java/x/Contest.java
@@ -1 +1,2 @@
 class Contest {
+    @Transactional void a() {}
"""
    p = plan(parse_diff(diff), CFG)
    assert p.score == 2  # @Transactional（2）. テスト扱いだと 0 になる


# -- 2. deep 段の UnreadableOutput ------------------------------------------------
def test_deep_step_unreadable_output_falls_back_to_standard_with_note():
    fb = Backend([_std(MUST), UnreadableOutput("モデルが結果の道具を使いませんでした")])
    changes = parse_diff(JAVA_SERVICE)
    r = Reviewer(CFG, fb).auto(JAVA_SERVICE, changes, plan(changes, CFG))
    assert fb.roles == ["standard", "verify"]
    assert [(f.title, f.status) for f in r.findings] == [("排他が外れる", "unverified")]
    assert any("deep モデルの出力を読めなかった" in n for n in r.notes)


def test_deep_tier_unreadable_output_falls_back_too():
    fb = Backend([_std(MUST), UnreadableOutput("x")])
    changes = parse_diff(JAVA_SERVICE)
    r = Reviewer(CFG, fb).auto(JAVA_SERVICE, changes, plan(changes, CFG, labels=["review:deep"]))
    assert fb.roles == ["standard", "deep"]
    assert len(r.findings) == 1 and r.open_must == 1


def test_standard_step_unreadable_output_is_still_an_error():
    fb = Backend([UnreadableOutput("x")])
    changes = parse_diff(JAVA_SERVICE)
    with pytest.raises(UnreadableOutput):
        Reviewer(CFG, fb).auto(JAVA_SERVICE, changes, plan(changes, CFG))


# -- 3. review:standard ラベルは verify 段に上げない -------------------------------
def test_standard_label_does_not_verify_must_with_deep_model():
    fb = Backend([_std(MUST)])
    changes = parse_diff(JAVA_SERVICE)
    p = plan(changes, CFG, labels=["review:standard"])
    assert p.tier == "standard" and not p.escalate
    r = Reviewer(CFG, fb).auto(JAVA_SERVICE, changes, p)
    assert fb.roles == ["standard"]
    assert r.open_must == 1
    assert any("ラベルで standard に固定" in n for n in r.notes)


def test_standard_label_dry_run_estimates_standard_only():
    changes = parse_diff(JAVA_SERVICE)
    r = Reviewer(CFG, None, dry_run=True).auto(JAVA_SERVICE, changes, plan(changes, CFG, labels=["review:standard"]))
    assert [c.step for c in r.calls] == ["standard"]
    assert not any("deep モデルの検証が加わります" in n for n in r.notes)


def test_without_label_must_is_still_verified():
    fb = Backend([_std(MUST), json.dumps({"verdicts": [{"id": "s1", "verdict": "confirm"}], "findings": []})])
    changes = parse_diff(JAVA_SERVICE)
    Reviewer(CFG, fb).auto(JAVA_SERVICE, changes, plan(changes, CFG))
    assert fb.roles == ["standard", "verify"]


# -- 4. 文書だけの変更でも危険度の点数があれば skip しない ------------------------------
def test_docs_only_with_secret_is_not_skipped():
    p = plan(parse_diff(_docs_diff("docs/setup.md", "api_key = sk-live-0000example")), CFG)
    assert p.tier != "skip"
    assert p.forced_by is None
    assert any("秘密情報" in r for r in p.reasons)


def test_docs_only_without_risk_is_still_skipped():
    p = plan(parse_diff(_docs_diff("docs/setup.md", "手順を直した")), CFG)
    assert p.tier == "skip"


def test_deep_label_wins_over_docs_only_skip():
    """review:deep は「必ず Opus まで」（README）. 後退: 文書だけの変更の省略を先に見ていて、ラベルが効かなかった."""
    changes = parse_diff(_docs_diff("docs/setup.md", "手順を直した"))
    p = plan(changes, CFG, labels=["review:deep"])
    assert p.tier == "deep" and p.forced_by == "ラベル review:deep"
    # review:skip はそれより強い. review:standard は上限なので、文書だけなら今までどおり省く
    assert plan(changes, CFG, labels=["review:deep", "review:skip"]).tier == "skip"
    assert plan(changes, CFG, labels=["review:standard"]).tier == "skip"
