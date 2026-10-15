"""git の unified diff を、ファイルごとの変更に分ける.

- hunk の見出し（`@@ -a,b +c,d @@`）の行数に沿って読む. hunk の中の `--- …`（削除した SQL のコメント行など）や
  `+++ …`（`++` で始まる行の追加）は、ファイルの見出しではなく変更の行として扱う
- パスは `+++ b/…`（削除なら `--- a/…`）→ `rename to` → `diff --git` の順で決める.
  空白を含むパス（`src/my file.py`）も、git の引用符付きのパス（`"b/src/\\350\\250\\255.md"`）も読む
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_ESCAPES = {"a": 7, "b": 8, "t": 9, "n": 10, "v": 11, "f": 12, "r": 13, '"': 34, "\\": 92}


@dataclass
class FileChange:
    path: str
    status: str = "modified"  # added / modified / deleted / renamed
    added: list[tuple[int, str]] = field(default_factory=list)  # (新しい行番号, 内容)
    removed: list[str] = field(default_factory=list)
    old_path: str | None = None
    binary: bool = False

    @property
    def lines_changed(self) -> int:
        return len(self.added) + len(self.removed)

    @property
    def is_test(self) -> bool:
        """テストのファイルか. Java のファイル名の末尾（`Test.java`・`Tests.java`・`IT.java`）は大文字小文字を区別して見る
        （`Contest.java`・`Latest.java` はテストではない）. `src/test/` などのフォルダは従来どおり大文字小文字を区別しない."""
        p = self.path.lower()
        name = self.path.rsplit("/", 1)[-1]
        return (
            "/test/" in p
            or "/tests/" in p
            or "/test_" in p
            or name.endswith(("Test.java", "Tests.java", "IT.java"))
            or p.endswith(("_test.py", ".feature"))
        )


def unquote_git_path(p: str) -> str:
    """git の引用符付きのパス（C の文字列の形. 日本語などは `\\350\\250\\255` の 8 進のバイト）を元に戻す.

    引用符が無ければそのまま返す. バイト列は UTF-8 として読む（読めないバイトは置き換える）.
    """
    if len(p) < 2 or not (p.startswith('"') and p.endswith('"')):
        return p
    body, out, i = p[1:-1], bytearray(), 0
    while i < len(body):
        ch = body[i]
        if ch != "\\" or i + 1 >= len(body):
            out += ch.encode("utf-8")
            i += 1
            continue
        nxt = body[i + 1]
        if nxt in "01234567":
            m = re.match(r"[0-7]{1,3}", body[i + 1 :])
            assert m is not None
            out.append(int(m.group(0), 8) & 0xFF)
            i += 1 + len(m.group(0))
        else:
            out.append(_ESCAPES.get(nxt, ord(nxt) if ord(nxt) < 128 else 63))
            i += 2
    return out.decode("utf-8", errors="replace")


def _strip_prefix(p: str) -> str:
    p = unquote_git_path(p.strip())
    return p[2:] if p[:2] in ("a/", "b/") else p


def _header_path(p: str) -> str | None:
    """`--- a/x` / `+++ b/x` のパス. 空白を含むパスに git が付ける末尾のタブは除く. /dev/null は None."""
    p = p.split("\t", 1)[0] if not p.startswith('"') else p.rstrip("\t")
    p = p.rstrip("\r")
    if p.strip() == "/dev/null":
        return None
    return _strip_prefix(p)


def _symmetric(s: str) -> bool:
    """`a/P b/P` の形か（P は同じ文字列）."""
    half = len(s) // 2
    return len(s) % 2 == 1 and s.startswith("a/") and s[half] == " " and s[half + 1 :] == "b" + s[1:half]


def _git_line_paths(rest: str) -> tuple[str, str]:
    """`diff --git` の後ろ（`a/x b/y`）から 2 つのパスを取り出す. 引用符付き・空白入りにも対応する."""
    rest = rest.rstrip("\r")
    tokens: list[str] = []
    s = rest
    while s and len(tokens) < 2:
        s = s.lstrip(" ")
        if s.startswith('"'):
            m = re.match(r'"(?:[^"\\]|\\.)*"', s)
            if m is None:
                break
            tokens.append(m.group(0))
            s = s[m.end() :]
        else:
            if tokens:  # 2 つ目（引用符なし）は残り全部
                tokens.append(s)
                s = ""
            elif _symmetric(s):
                # 名前の変わらないファイル: `a/P b/P`（P に空白があっても真ん中で分けられる）
                half = len(s) // 2
                tokens += [s[:half], s[half + 1 :]]
                s = ""
            else:
                i = s.find(" b/") if s.startswith("a/") else -1
                if i < 0:
                    i = s.find(" ")
                if i < 0:
                    tokens.append(s)
                    s = ""
                else:
                    tokens.append(s[:i])
                    s = s[i + 1 :]
    if not tokens:
        return rest, rest
    if len(tokens) == 1:
        tokens.append(tokens[0])
    return _strip_prefix(tokens[0]), _strip_prefix(tokens[1])


def parse_diff(text: str) -> list[FileChange]:
    files: list[FileChange] = []
    cur: FileChange | None = None
    old_left = new_left = 0  # 今の hunk で残っている行数（古い側・新しい側）
    new_line = 0
    # 行は "\n" だけで分ける（末尾の "\r" は除く）. str.splitlines() は U+2028・U+0085・\x0c なども行の区切りにするため、
    # それらを含む行で hunk の行数がずれ、後ろの追加行を落としてしまう
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    for raw in lines:
        raw = raw[:-1] if raw.endswith("\r") else raw
        if cur is not None and (old_left > 0 or new_left > 0):
            tag = raw[:1]
            if tag == "+" and new_left > 0:
                cur.added.append((new_line, raw[1:]))
                new_line += 1
                new_left -= 1
                continue
            if tag == "-" and old_left > 0:
                cur.removed.append(raw[1:])
                old_left -= 1
                continue
            if tag == " " or raw == "":  # 空行は、末尾の空白を消すツールを通った文脈の行として扱う
                new_line += 1
                old_left, new_left = max(0, old_left - 1), max(0, new_left - 1)
                continue
            if tag == "\\":  # "\ No newline at end of file"
                continue
            # 行数が見出しと合わない（手で編集した差分など）. ここで hunk を終え、見出しの行として読み直す
            old_left = new_left = 0
        if raw.startswith("\\"):
            continue
        if raw.startswith("diff --git "):
            _, new = _git_line_paths(raw[len("diff --git ") :])
            cur = FileChange(path=new)
            files.append(cur)
            continue
        if cur is None:
            continue
        if raw.startswith("new file mode"):
            cur.status = "added"
        elif raw.startswith("deleted file mode"):
            cur.status = "deleted"
        elif raw.startswith("rename from "):
            cur.status, cur.old_path = "renamed", unquote_git_path(raw[len("rename from ") :].rstrip("\r"))
        elif raw.startswith("rename to "):
            cur.path = unquote_git_path(raw[len("rename to ") :].rstrip("\r"))
        elif raw.startswith("Binary files "):
            cur.binary = True
        elif raw.startswith("--- "):
            p = _header_path(raw[4:])
            if p is not None and cur.status == "deleted":
                cur.path = p
        elif raw.startswith("+++ "):
            p = _header_path(raw[4:])
            if p is not None:
                cur.path = p
        elif raw.startswith("@@"):
            m = _HUNK.match(raw)
            if m:
                old_left = int(m.group(2)) if m.group(2) is not None else 1
                new_left = int(m.group(4)) if m.group(4) is not None else 1
                new_line = int(m.group(3))
    return files
