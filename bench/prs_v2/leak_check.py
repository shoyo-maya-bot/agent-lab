#!/usr/bin/env python3
"""PR の課題 v2 に、答えが漏れていないかを確かめる.

    python bench/prs_v2/leak_check.py            # 問題があれば一覧を出して終了コード 1

確かめること（どれか 1 つでも当たれば失敗）:
  1. 形: 説明のコメントの行を消していない・テストのファイルの行を消していない・ファイルを消していない
     （v1 では、消したコメントやテスト名にバグの説明が書いてあり、差分から答えが分かった）
  2. 語: 答えの leak_terms（不具合を説明する語）が、差分の追加・削除の行、PR の題名・説明に無い
  3. 文: 答えの説明（summary・detail・why_correct）と同じ 10 文字の並び（日本語を 3 文字以上含むもの）が、
     差分の追加・削除の行、PR の題名・説明に無い。題名・説明が答えの文と似ていない（文字 2-gram の一致率）
  4. 公開する情報: ID が種類を表さない（T + 数字）、題名・説明に「おとり」「不具合」などの語が無い
  5. プロンプト: auto-review・reviewers・examples のプロンプトに、答えの文と同じ並び・似た文・leak_terms が無い
     （考え方は auto-review/tests/test_prompt_leak.py と同じ）

差分の文脈の行（先頭が空白）は元のコードそのままなので、ここでは見ない.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
PROMPT_DIRS = [
    ROOT / "auto-review/src/auto_review/prompts",
    ROOT / "reviewers",
    ROOT / "examples/vscode-prompts",
]
CHUNK = 10  # 答えの文と同じ 10 文字の並びがあれば漏れ
MIN_NON_ASCII = 3  # ただし、コードの断片（差分にあって当然）を除くため、日本語などを 3 文字以上含む並びだけを見る
SIMILARITY = 0.5  # 題名・説明・プロンプトの 1 行と答えの文の、文字 2-gram の一致率（Jaccard）
CODE_SUFFIXES = (".java", ".xml", ".yml", ".yaml", ".properties", ".gradle", ".sql", ".js", ".feature", ".tf", ".sh")
PUBLIC_FORBIDDEN = ["おとり", "decoy", "不具合", "バグ", "bug", "正解", "答え", "仕込"]


def _squash(s: str) -> str:
    return re.sub(r"[\s、。，．,.（）()「」`*|:：\"']", "", s.lower())


def _bigrams(s: str) -> set[str]:
    s = _squash(s)
    return {s[i : i + 2] for i in range(len(s) - 1)}


def similar(a: str, b: str) -> bool:
    x, y = _bigrams(a), _bigrams(b)
    return bool(x and y) and len(x & y) / len(x | y) >= SIMILARITY


def chunks(sentence: str) -> list[str]:
    q = _squash(sentence)
    out = []
    for i in range(len(q) - CHUNK + 1):
        c = q[i : i + CHUNK]
        if sum(1 for ch in c if ord(ch) > 0x7F) >= MIN_NON_ASCII:
            out.append(c)
    return out


def term_hits(term: str, text: str) -> bool:
    """英数字だけの語は単語の境界で（StandardCharsets の中の charset には当てない）、それ以外は部分一致で探す."""
    t, low = term.lower(), text.lower()
    if re.fullmatch(r"[\x20-\x7e]+", term):
        return re.search(rf"(?<![A-Za-z0-9_]){re.escape(t)}(?![A-Za-z0-9_])", low) is not None
    return t in low


def changed_lines(diff: str) -> list[tuple[str, str, str]]:
    """（ファイル, '+' か '-', 行の中身）. ヘッダーの行は除く."""
    out, cur = [], ""
    for ln in diff.splitlines():
        if ln.startswith("diff --git "):
            cur = ln.split(" b/", 1)[-1]
        elif ln.startswith(("+++", "---")):
            continue
        elif ln.startswith(("+", "-")):
            out.append((cur, ln[0], ln[1:]))
    return out


def _is_comment(path: str, text: str) -> bool:
    s = text.strip()
    if not s:
        return False
    if path.endswith((".java", ".gradle", ".js", ".tf")):
        return s.startswith(("//", "/*", "*"))
    if path.endswith((".xml",)):
        return s.startswith("<!--")
    if path.endswith((".yml", ".yaml", ".properties", ".sh")):
        return s.startswith("#")
    if path.endswith(".sql"):
        return s.startswith("--")
    return False


def check_shape(diff: str) -> list[str]:
    """コメント・テストを消していないか、ファイルを消していないか."""
    errs = []
    if "deleted file mode" in diff:
        errs.append("形: ファイルを消している")
    for path, sign, text in changed_lines(diff):
        if sign != "-":
            continue
        if "/src/test/" in f"/{path}":
            errs.append(f"形: テストの行を消している {path}: {text.strip()[:60]}")
        elif path.endswith(CODE_SUFFIXES) and _is_comment(path, text):
            errs.append(f"形: コメントの行を消している {path}: {text.strip()[:60]}")
    return errs


def _answer_sentences(ans: dict) -> list[str]:
    return [str(ans[k]) for k in ("summary", "detail", "why_correct", "looks_risky") if ans.get(k)]


def check_case(case: dict, diff: str, ans: dict) -> list[str]:
    """1 つの PR（公開する題名・説明・差分）と、その答え."""
    errs = [f"{case['id']}: {e}" for e in check_shape(diff)]
    body = "\n".join(t for _, _, t in changed_lines(diff))
    public = f"{case.get('title', '')}\n{case.get('description', '')}"
    for term in ans.get("leak_terms", []):
        if term_hits(term, body):
            errs.append(f"{case['id']}: 語: 差分に {term!r}")
        if term_hits(term, public):
            errs.append(f"{case['id']}: 語: 題名・説明に {term!r}")
    squashed_body, squashed_public = _squash(body), _squash(public)
    for s in _answer_sentences(ans):
        for c in chunks(s):
            if c in squashed_body or c in squashed_public:
                errs.append(f"{case['id']}: 文: 答えと同じ並び {c!r}")
                break
        for line in (case.get("title", ""), case.get("description", "")):
            if line and similar(s, line):
                errs.append(f"{case['id']}: 文: 題名・説明が答えに似ている {line!r}")
    return errs


def check_public(cases: list[dict]) -> list[str]:
    errs = []
    for c in cases:
        if not re.fullmatch(r"T\d{2,3}", c["id"]):
            errs.append(f"{c['id']}: 公開: ID が T + 数字ではない")
        for w in PUBLIC_FORBIDDEN:
            if term_hits(w, f"{c.get('title', '')}\n{c.get('description', '')}"):
                errs.append(f"{c['id']}: 公開: 題名・説明に {w!r}")
        extra = set(c) - {"id", "title", "description"}
        if extra:
            errs.append(f"{c['id']}: 公開: 題名・説明以外の項目 {sorted(extra)}（種類が分かるおそれ）")
    return errs


def prompt_files() -> list[Path]:
    return [p for d in PROMPT_DIRS if d.exists() for p in sorted(d.rglob("*.md"))]


def check_prompt(text: str, answers: list[dict], name: str = "prompt") -> list[str]:
    errs = []
    squashed = _squash(text)
    lines = [ln for ln in text.splitlines() if ln.strip()] + re.findall(r'"([^"\n]{4,})"', text)
    for a in answers:
        for term in a.get("leak_terms", []):
            if term_hits(term, text):
                errs.append(f"{name}: 語 {term!r}（{a['id']} の答え）")
        for s in _answer_sentences(a):
            for c in chunks(s):
                if c in squashed:
                    errs.append(f"{name}: 答え（{a['id']}）と同じ並び {c!r}")
                    break
            for ln in lines:
                if similar(s, ln):
                    errs.append(f"{name}: 答え（{a['id']}）に似た行 {ln.strip()[:60]!r}")
    return errs


def load(bench: Path = HERE) -> tuple[dict, list[dict], dict[str, str]]:
    cases = yaml.safe_load((bench / "cases.yaml").read_text(encoding="utf-8"))
    answers = yaml.safe_load((bench / "answers.yaml").read_text(encoding="utf-8"))["answers"]
    diff_dir = bench / cases.get("diff_dir", ".")
    diffs = {c["id"]: (diff_dir / f"{c['id']}.diff").read_text(encoding="utf-8") for c in cases["cases"]}
    return cases, answers, diffs


def check_all(bench: Path = HERE, prompts: list[Path] | None = None) -> list[str]:
    cases, answers, diffs = load(bench)
    by_id = {a["id"]: a for a in answers}
    errs = check_public(cases["cases"])
    for c in cases["cases"]:
        errs += check_case(c, diffs[c["id"]], by_id.get(c["id"], {}))
    for p in prompt_files() if prompts is None else prompts:
        errs += check_prompt(p.read_text(encoding="utf-8"), answers, str(p.relative_to(ROOT)) if p.is_relative_to(ROOT) else p.name)
    return errs


def main() -> int:
    errs = check_all()
    for e in errs:
        print(e)
    print("漏れは見つかりませんでした" if not errs else f"{len(errs)} 件の問題があります", file=sys.stderr)
    return 1 if errs else 0


if __name__ == "__main__":
    sys.exit(main())
