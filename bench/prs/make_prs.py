#!/usr/bin/env python3
"""cases.yaml から PR の差分（bench/prs/<id>.diff）を作る.

    python bench/prs/make_prs.py ../dev-environment

dev-environment の base_commit を一時的な worktree に取り出し、ケースごとに編集して git diff を保存する.
編集の書き方: old/new（old はちょうど 1 か所に一致すること）、delete_between [始まり, 終わり)（終わりは残す）、delete_file
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout


def apply_edit(root: Path, e: dict, case_id: str) -> None:
    p = root / e["file"]
    if e.get("delete_file"):
        git(root, "rm", "-q", e["file"])
        return
    s = p.read_text(encoding="utf-8")
    if "old" in e:
        n = s.count(e["old"])
        if n != 1:
            raise SystemExit(f"{case_id}: {e['file']} で old が {n} か所に一致しました（1 か所であること）:\n{e['old']}")
        s = s.replace(e["old"], e["new"])
    elif "delete_between" in e:
        start, end = e["delete_between"]
        i = s.find(start)
        j = s.find(end, i + len(start)) if i >= 0 else -1
        if i < 0 or j < 0:
            raise SystemExit(f"{case_id}: {e['file']} で delete_between の目印が見つかりません: {start!r} → {end!r}")
        s = s[:i] + s[j:]
    p.write_text(s, encoding="utf-8")


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print(__doc__)
        return 2
    repo = Path(argv[0]).resolve()
    spec = yaml.safe_load((HERE / "cases.yaml").read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory() as tmp:
        wt = Path(tmp) / "wt"
        git(repo, "worktree", "add", "-q", "--detach", str(wt), spec["base_commit"])
        try:
            for case in spec["cases"]:
                for e in case["edits"]:
                    apply_edit(wt, e, case["id"])
                diff = git(wt, "diff", "HEAD")
                (HERE / f"{case['id']}.diff").write_text(diff, encoding="utf-8")
                print(
                    f"{case['id']}: {sum(1 for x in diff.splitlines() if x.startswith(('+', '-')) and not x.startswith(('+++', '---')))} 行"
                )
                git(wt, "reset", "-q", "--hard", "HEAD")
        finally:
            git(repo, "worktree", "remove", "--force", str(wt))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
