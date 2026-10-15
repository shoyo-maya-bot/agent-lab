"""差分を "\\n" だけで行に分ける（U+2028 などで hunk の行数をずらさない）."""

from __future__ import annotations

import pytest
from auto_review.diff import parse_diff


@pytest.mark.parametrize("sep", [" ", " ", "\u0085", "\x0b", "\x0c", "\x1c", "\x1d", "\x1e"])
def test_unicode_line_separators_do_not_shift_hunk(sep: str):
    diff = f"diff --git a/a.js b/a.js\n--- a/a.js\n+++ b/a.js\n@@ -1,1 +1,3 @@\n+const s = 'x{sep}y';\n+password = 'leak';\n keep\n"
    (fc,) = parse_diff(diff)
    assert fc.added == [(1, f"const s = 'x{sep}y';"), (2, "password = 'leak';")]


def test_crlf_lines_are_still_read():
    diff = "diff --git a/a.txt b/a.txt\r\n--- a/a.txt\r\n+++ b/a.txt\r\n@@ -1 +1 @@\r\n-a\r\n+b\r\n"
    (fc,) = parse_diff(diff)
    assert fc.path == "a.txt" and fc.added == [(1, "b")] and fc.removed == ["a"]
