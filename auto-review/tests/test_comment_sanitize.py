"""PR のコメントに載せる文字列の無害化（メンション・Markdown のリンクと画像・箇所のバッククォート）."""

from __future__ import annotations

import re

from auto_review.diff import parse_diff
from auto_review.findings import Finding
from auto_review.render import _cell, to_markdown
from auto_review.review import Result
from auto_review.router import load_config, plan

CFG = load_config()

DIFF = """\
diff --git a/app/x/A.java b/app/x/A.java
--- a/app/x/A.java
+++ b/app/x/A.java
@@ -1 +1 @@
-a
+b
"""

# GitHub が通知・リンクにする形（無害化の後に残っていてはいけない）
_LIVE_MENTION = re.compile(r"(?<![\w`])[@＠][A-Za-z0-9]")
_LIVE_LINK = re.compile(r"(?<!\\)\[[^\]\\]*(?<!\\)\]\s*[(\[]")


def _md(**kw) -> str:
    f = Finding(
        id="s1",
        severity="MUST",
        file=kw.get("file", "A.java"),
        line=None,
        title=kw.get("title", "t"),
        detail=kw.get("detail", "d"),
        models=["claude-sonnet-5-5"],
    )
    changes = parse_diff(DIFF)
    return to_markdown(Result(plan(changes, CFG), findings=[f]))


def test_user_and_team_mentions_are_neutralized():
    md = _md(title="@octocat 見て", detail="@my-org/security-team と ＠someone に連絡")
    assert not _LIVE_MENTION.search(md)
    assert "@​octocat" in md and "@​my-org/security-team" in md


def test_markdown_links_and_images_are_neutralized():
    for s in ("[ここ](https://evil.example/x)", "![図](https://evil.example/p.png)", "[ref][1]", "[x]: https://evil.example"):
        out = _cell(s)
        assert not _LIVE_LINK.search(out), out
        assert "https://" not in out  # 自動リンクにもさせない
    assert "ここ" in _cell("[ここ](https://evil.example/x)")  # 文字は残す


def test_escaped_bracket_in_input_does_not_reopen_link():
    out = _cell("\\[a\\](https://evil.example)")
    assert not _LIVE_LINK.search(out.replace("\\\\", ""))


def test_location_backticks_cannot_break_out_of_code():
    md = _md(file="A.java`](https://evil.example) @octocat `")
    row = next(line for line in md.splitlines() if line.startswith("| 未検証"))
    cell = row.split(" | ")[2]
    assert cell.startswith("`") and cell.endswith("`") and cell.count("`") == 2


def test_notes_and_reasons_are_sanitized():
    changes = parse_diff(DIFF.replace("app/x/A.java", "app/x/@octocat[x](https:/e).java"))
    r = Result(plan(changes, CFG), notes=["上限のため省いたファイル: @octocat/[a](https://e.example)"])
    md = to_markdown(r)
    assert not _LIVE_MENTION.search(md) and not _LIVE_LINK.search(md)
