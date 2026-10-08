"""diff.py のテスト（空白・日本語のパス、hunk の中の `---` / `+++` で始まる行）."""

from __future__ import annotations

from auto_review.diff import parse_diff, unquote_git_path

SPACE_PATH = """\
diff --git a/src/my file.py b/src/my file.py
index 752aa47..462633e 100644
--- a/src/my file.py\t
+++ b/src/my file.py\t
@@ -1,2 +1,2 @@
 a
-x
+y
"""

JAPANESE_PATH = """\
diff --git "a/src/\\350\\250\\255\\350\\250\\210.md" "b/src/\\350\\250\\255\\350\\250\\210.md"
index 587be6b..975fbec 100644
--- "a/src/\\350\\250\\255\\350\\250\\210.md"
+++ "b/src/\\350\\250\\255\\350\\250\\210.md"
@@ -1 +1 @@
-x
+y
"""

# 削除した SQL のコメント行（`-- …` を消すと `--- …` になる）と、`++` で始まる行の追加（`+++ …` になる）
SQL_COMMENT = """\
diff --git a/db/schema.sql b/db/schema.sql
index 1..2 100644
--- a/db/schema.sql
+++ b/db/schema.sql
@@ -1,3 +1,3 @@
--- 会員の一意制約
-ALTER TABLE member ADD CONSTRAINT uq_email UNIQUE (email);
+++counter;
 SELECT 1;
diff --git a/b.txt b/b.txt
--- a/b.txt
+++ b/b.txt
@@ -5 +5,2 @@
-old
+new
+more
"""


def test_path_with_space_is_kept_whole():
    ch = parse_diff(SPACE_PATH)
    assert [c.path for c in ch] == ["src/my file.py"]
    assert ch[0].added == [(2, "y")] and ch[0].removed == ["x"]


def test_path_with_space_from_git_line_only():
    """バイナリ・名前だけの変更など、`+++` の行が無いときも `diff --git` の行から空白入りのパスを取る."""
    ch = parse_diff(
        "diff --git a/img/my pic.png b/img/my pic.png\nindex 1..2 100644\nBinary files a/img/my pic.png and b/img/my pic.png differ\n"
    )
    assert ch[0].path == "img/my pic.png" and ch[0].binary


def test_quoted_japanese_path_is_decoded():
    ch = parse_diff(JAPANESE_PATH)
    assert [c.path for c in ch] == ["src/設計.md"]
    assert unquote_git_path('"a/\\346\\227\\245 \\"q\\"\\tx"') == 'a/日 "q"\tx'


def test_renamed_quoted_and_spaced_paths():
    text = (
        "diff --git a/src/r.sql b/src/r 2.sql\nsimilarity index 100%\nrename from src/r.sql\nrename to src/r 2.sql\n"
        'diff --git "a/\\344\\273\\225\\346\\247\\230.md" "b/docs/\\344\\273\\225\\346\\247\\230.md"\n'
        'similarity index 100%\nrename from "\\344\\273\\225\\346\\247\\230.md"\nrename to "docs/\\344\\273\\225\\346\\247\\230.md"\n'
    )
    ch = parse_diff(text)
    assert [(c.path, c.old_path, c.status) for c in ch] == [("src/r 2.sql", "src/r.sql", "renamed"), ("docs/仕様.md", "仕様.md", "renamed")]


def test_lines_starting_with_triple_dash_or_plus_inside_hunk_are_changes():
    ch = parse_diff(SQL_COMMENT)
    assert [c.path for c in ch] == ["db/schema.sql", "b.txt"]
    assert ch[0].removed == ["-- 会員の一意制約", "ALTER TABLE member ADD CONSTRAINT uq_email UNIQUE (email);"]
    assert ch[0].added == [(1, "++counter;")]
    assert ch[1].added == [(5, "new"), (6, "more")] and ch[1].removed == ["old"]


def test_deleted_file_whose_lines_start_with_dashes():
    text = "diff --git a/m.sql b/m.sql\ndeleted file mode 100644\n--- a/m.sql\n+++ /dev/null\n@@ -1,2 +0,0 @@\n--- comment\n-SELECT 1;\n"
    ch = parse_diff(text)
    assert ch[0].status == "deleted" and ch[0].path == "m.sql"
    assert ch[0].removed == ["-- comment", "SELECT 1;"]


def test_no_newline_marker_and_crlf():
    text = "diff --git a/a.txt b/a.txt\r\n--- a/a.txt\r\n+++ b/a.txt\r\n@@ -1 +1 @@\r\n-x\r\n\\ No newline at end of file\r\n+y\r\n"
    ch = parse_diff(text)
    assert ch[0].path == "a.txt" and ch[0].removed == ["x"] and ch[0].added == [(1, "y")]
