"""git の unified diff を、ファイルごとの変更に分ける."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


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
        p = self.path.lower()
        return "/test/" in p or "/tests/" in p or p.endswith(("test.java", "_test.py", ".feature")) or "/test_" in p


def _strip_prefix(p: str) -> str:
    p = p.strip()
    if p.startswith('"') and p.endswith('"'):
        p = p[1:-1]
    return p[2:] if p[:2] in ("a/", "b/") else p


def parse_diff(text: str) -> list[FileChange]:
    files: list[FileChange] = []
    cur: FileChange | None = None
    new_line = 0
    for raw in text.splitlines():
        if raw.startswith("diff --git "):
            m = re.match(r"diff --git (\S+) (\S+)$", raw)
            path = _strip_prefix(m.group(2)) if m else raw.split()[-1]
            cur = FileChange(path=path)
            files.append(cur)
            continue
        if cur is None:
            continue
        if raw.startswith("new file mode"):
            cur.status = "added"
        elif raw.startswith("deleted file mode"):
            cur.status = "deleted"
        elif raw.startswith("rename from "):
            cur.status, cur.old_path = "renamed", raw[len("rename from ") :]
        elif raw.startswith("rename to "):
            cur.path = raw[len("rename to ") :]
        elif raw.startswith("Binary files "):
            cur.binary = True
        elif raw.startswith("--- ") or raw.startswith("+++ "):
            if raw.startswith("--- ") and raw[4:].strip() != "/dev/null" and cur.status == "deleted":
                cur.path = _strip_prefix(raw[4:])
        elif raw.startswith("@@"):
            m = _HUNK.match(raw)
            new_line = int(m.group(1)) if m else 0
        elif raw.startswith("+"):
            cur.added.append((new_line, raw[1:]))
            new_line += 1
        elif raw.startswith("-"):
            cur.removed.append(raw[1:])
        elif raw.startswith(" ") or raw == "":
            new_line += 1
    return files
