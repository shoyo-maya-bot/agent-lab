"""「MUST を deep モデルで検証しました」の注記は、予算の確認・呼び出し・読み取りが済んでから書く.

予算で省いた・呼び出しに失敗した・出力を読めなかったときに「検証しました」と残さない.
"""

from __future__ import annotations

import json

from auto_review.backends import BackendError, Reply
from auto_review.diff import parse_diff
from auto_review.findings import UnreadableOutput
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
MUST = [{"severity": "MUST", "file": "MemberService.java", "line": 12, "title": "排他が外れる"}]
STD = json.dumps({"findings": MUST}, ensure_ascii=False)
VERIFIED = "を deep モデルで検証しました"


class Backend:
    def __init__(self, replies):
        self.replies = list(replies)
        self.roles: list[str] = []

    def call(self, model, system, user, max_tokens, role=""):
        self.roles.append(role)
        r = self.replies.pop(0)
        if isinstance(r, BaseException):
            raise r
        return Reply(r, 1000, 200, 0.01, 0.1)


def _auto(backend, cfg=CFG):
    changes = parse_diff(JAVA_SERVICE)
    return Reviewer(cfg, backend).auto(JAVA_SERVICE, changes, plan(changes, cfg))


def _has_verified(r) -> bool:
    return any(VERIFIED in n for n in r.notes)


def test_note_is_written_after_successful_verification():
    verdicts = json.dumps({"verdicts": [{"id": "s1", "verdict": "confirmed", "note": "確かに外れる"}], "new_findings": []})
    r = _auto(Backend([STD, verdicts]))
    assert _has_verified(r)


def test_no_verified_note_when_skipped_for_budget():
    cfg = json.loads(json.dumps(CFG))
    b = Backend([STD])
    # standard の 1 回分（0.01）は通り、deep の見積もりは超える予算
    cfg["budget"]["per_pr_usd"] = 0.0
    cfg["budget"].pop("per_pr_total_usd", None)
    changes = parse_diff(JAVA_SERVICE)
    rv = Reviewer(cfg, b)
    rv._remaining = lambda spent: (0.5 - spent, "テストの予算") if spent == 0 else (0.0, "テストの予算")
    r = rv.auto(JAVA_SERVICE, changes, plan(changes, cfg))
    assert b.roles == ["standard"]
    assert any("省きました" in n for n in r.notes)
    assert not _has_verified(r)


def test_no_verified_note_when_deep_call_fails():
    r = _auto(Backend([STD, BackendError("API エラー")]))
    assert any("呼び出しに失敗" in n for n in r.notes) and not _has_verified(r)


def test_no_verified_note_when_deep_output_unreadable():
    r = _auto(Backend([STD, UnreadableOutput("x")]))
    assert any("読めなかった" in n for n in r.notes) and not _has_verified(r)
    r = _auto(Backend([STD, "これは JSON ではない"]))
    assert any("読めなかった" in n for n in r.notes) and not _has_verified(r)
