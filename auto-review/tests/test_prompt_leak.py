"""評価の答えがプロンプトに漏れていないかを確かめる.

プロンプト（auto-review の prompts/、reviewers/、examples/vscode-prompts/）の例や説明に、
bench の正解（bench/bugs/*.yaml と bench/prs/cases.yaml）のファイル名・題名・特有の語が入ると、
モデルが「答えを見て」当てられてしまい、評価が測れなくなる.

答えのファイルを足したり書き換えたりしても、ここは自動で追従する（答えの側から禁止語を作る）.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
PROMPT_DIRS = [
    ROOT / "auto-review/src/auto_review/prompts",
    ROOT / "reviewers",
    ROOT / "examples/vscode-prompts",
]
SIMILARITY = 0.5  # 題名と、プロンプトの 1 行の文字 2-gram の一致率（Jaccard）がこれ以上なら漏れとみなす
CHUNK = 10  # 答えの文（題名・説明）と同じ 10 文字の並びがプロンプトにあれば漏れとみなす


def _prompt_files() -> list[Path]:
    files = [p for d in PROMPT_DIRS if d.exists() for p in sorted(d.rglob("*.md"))]
    assert files, "プロンプトのファイルが見つかりません（場所が変わったら PROMPT_DIRS を直す）"
    return files


def _distinctive(name: str) -> bool:
    """README・build のような一般的な名前は除き、そのプロジェクト特有の名前だけを禁止語にする."""
    return bool(re.search(r"[a-z][A-Z]|[-_.{,]|\d", name)) and len(name) >= 5


def _answers() -> tuple[set[str], list[str]]:
    """（禁止する語, 似ていてはいけない文）を答えのファイル全部から作る."""
    words: set[str] = set()
    sentences: list[str] = []

    def add_file(path: str) -> None:
        base = path.rsplit("/", 1)[-1]
        stem = base.rsplit(".", 1)[0]
        if _distinctive(stem):
            words.update({base, stem})

    for f in sorted((ROOT / "bench/bugs").glob("*.yaml")):
        for b in yaml.safe_load(f.read_text(encoding="utf-8"))["bugs"]:
            for x in b["files"]:
                add_file(x)
            words.update(k for k in b["keywords"] if re.fullmatch(r"[\x20-\x7e]+", k) and _distinctive(k))
            sentences += [b["title"], b.get("why", "")]
    for f in sorted((ROOT / "bench/prs").glob("*.yaml")):
        for c in yaml.safe_load(f.read_text(encoding="utf-8"))["cases"]:
            for e in c.get("edits", []):
                add_file(e["file"])
            sentences += [c["title"], c.get("description", ""), c.get("why_correct", "")]
    return words, [s for s in sentences if s]


def _squash(s: str) -> str:
    return re.sub(r"[\s、。，．,.（）()「」`*|:：]", "", s.lower())


def _bigrams(s: str) -> set[str]:
    s = _squash(s)
    return {s[i : i + 2] for i in range(len(s) - 1)}


def leaks(text: str) -> list[str]:
    words, sentences = _answers()
    low = text.lower()
    found = [f"語: {w}" for w in sorted(words) if re.search(rf"(?<![A-Za-z0-9]){re.escape(w.lower())}(?![A-Za-z0-9])", low)]
    lines = [ln for ln in text.splitlines() if ln.strip()]
    lines += re.findall(r'"([^"\n]{4,})"', text)  # JSON の例の値（"title": "…"）も 1 つずつ比べる
    squashed = _squash(text)
    for s in sentences:
        b = _bigrams(s)
        for ln in lines:
            lb = _bigrams(ln)
            if b and lb and len(b & lb) / len(b | lb) >= SIMILARITY:
                found.append(f"似た文: {s!r} ≒ {ln.strip()!r}")
        q = _squash(s)
        for i in range(len(q) - CHUNK + 1):
            if q[i : i + CHUNK] in squashed:
                found.append(f"同じ並び: {q[i : i + CHUNK]!r}（{s!r}）")
                break
    return found


def test_answers_are_loaded():
    words, sentences = _answers()
    assert {"MemberService.java", "MemberService", "OrderImportService"} <= words
    assert any("同時登録" in s for s in sentences)


@pytest.mark.parametrize("path", _prompt_files(), ids=lambda p: str(p.relative_to(ROOT)))
def test_prompt_does_not_contain_bench_answers(path: Path):
    found = leaks(path.read_text(encoding="utf-8"))
    assert not found, f"{path.relative_to(ROOT)} に bench の答えに近い記述があります:\n" + "\n".join(found)


def test_checker_catches_the_old_leak():
    """以前の review.md の出力例（B01 とほぼ同じ）を、この検査が見つけられること."""
    old = '{"severity": "MUST", "file": "src/main/java/.../MemberService.java", "line": 42,\n "title": "同時登録で 500 になる"}'
    assert any("MemberService" in x for x in leaks(old))
    assert any("似た文" in x for x in leaks('"title": "同じメールアドレスの同時登録で 500"'))
    assert leaks("例: PageCursor.java で最終ページの末尾の 1 行が返らない") == []
