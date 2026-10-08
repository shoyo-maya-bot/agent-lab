"""変更の危なさを点数にして、レビューの段（skip / standard / deep）を決める.

ここはモデルを呼ばない（規則だけ・無料・毎回同じ結果）. 振り分けにまで LLM を使うと、
費用が増えるうえに「なぜ Opus に回ったか」を人が説明できなくなるため.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import yaml

from .diff import FileChange

_TEST_ANNOTATION = re.compile(r"^\s*@(Test|ParameterizedTest|RepeatedTest)\b|^\s*def test_|^\s*Scenario:")

DEFAULT_CONFIG = Path(__file__).resolve().parent / "router.yaml"


def load_config(path: Path | None = None) -> dict:
    return yaml.safe_load((path or DEFAULT_CONFIG).read_text(encoding="utf-8"))


@lru_cache(maxsize=256)
def _glob_re(glob: str) -> re.Pattern[str]:
    """`**` を含む glob を正規表現に（** は 0 個以上のディレクトリ、* は / を含まない）.

    大文字小文字は区別しない（`AGENTS.MD`・`Docs/` なども同じ規則に当てる. Windows・macOS では同じファイルのため）.
    """
    out, i = "", 0
    while i < len(glob):
        c = glob[i]
        if glob.startswith("**/", i):
            out += "(?:.*/)?"
            i += 3
            continue
        if glob.startswith("**", i):
            out += ".*"
            i += 2
            continue
        out += "[^/]*" if c == "*" else "[^/]" if c == "?" else re.escape(c)
        i += 1
    return re.compile(f"^{out}$", re.IGNORECASE)


def glob_match(path: str, glob: str) -> bool:
    return bool(_glob_re(glob).match(path))


@dataclass
class Plan:
    tier: str  # skip / standard / deep
    score: int
    reasons: list[str] = field(default_factory=list)
    file_scores: dict[str, int] = field(default_factory=dict)
    forced_by: str | None = None  # ラベル・作成者で決まったとき

    @property
    def risky_files(self) -> list[str]:
        return [p for p, s in sorted(self.file_scores.items(), key=lambda kv: -kv[1]) if s > 0]

    def to_dict(self) -> dict:
        return {
            "tier": self.tier,
            "score": self.score,
            "reasons": self.reasons,
            "risky_files": self.risky_files,
            "forced_by": self.forced_by,
        }


def _step(value: int, steps: list[list[int]]) -> int:
    add = 0
    for threshold, pts in steps:
        if value >= threshold:
            add = pts
    return add


def score_changes(changes: list[FileChange], cfg: dict) -> tuple[int, list[str], dict[str, int]]:
    risk = cfg["risk"]
    total = 0
    reasons: list[str] = []
    per_file: dict[str, int] = {}
    for ch in changes:
        s = 0
        if ch.is_test:
            # テストの変更そのものは危なくない。消したとき・減ったときだけ数える（守りが減るため）
            removed = sum(1 for t in ch.removed if _TEST_ANNOTATION.match(t))
            added = sum(1 for _, t in ch.added if _TEST_ANNOTATION.match(t))
            if ch.status == "deleted" or removed > added:
                w = risk.get("deleted_tests_weight", 0)
                s += w
                what = "テストの削除" if ch.status == "deleted" else f"テストが {removed - added} 件減った"
                reasons.append(f"+{w} {ch.path}: {what}")
            # 秘密情報らしき値は、テストのファイルでも数える
            body = "\n".join([t for _, t in ch.added] + ch.removed)
            for rule in risk.get("patterns", []):
                if "秘密" in rule.get("reason", "") and re.search(rule["regex"], body):
                    s += rule["weight"]
                    reasons.append(f"+{rule['weight']} {ch.path}: {rule['reason']}")
            per_file[ch.path] = s
            total += s
            continue
        for rule in risk.get("paths", []):
            if glob_match(ch.path, rule["glob"]):
                s += rule["weight"]
                reasons.append(f"+{rule['weight']} {ch.path}: {rule['reason']}")
                break  # パスの規則は 1 ファイル 1 つ（いちばん上に書いたものが優先）
        body = "\n".join([t for _, t in ch.added] + ch.removed)
        for rule in risk.get("patterns", []):
            if rule.get("files") and not glob_match(ch.path, rule["files"]):
                continue
            if re.search(rule["regex"], body):
                s += rule["weight"]
                reasons.append(f"+{rule['weight']} {ch.path}: {rule['reason']}")
        per_file[ch.path] = s
        total += s
    lines = sum(c.lines_changed for c in changes)
    for name, value in (("lines", lines), ("files", len(changes))):
        add = _step(value, risk.get("size", {}).get(name, []))
        if add:
            total += add
            reasons.append(f"+{add} 変更の大きさ（{'行' if name == 'lines' else 'ファイル'}: {value}）")
    return total, reasons, per_file


def plan(changes: list[FileChange], cfg: dict, labels: list[str] | None = None, author: str | None = None) -> Plan:
    labels = labels or []
    lab = cfg.get("labels", {})
    skip = cfg.get("skip", {})
    score, reasons, per_file = score_changes(changes, cfg)
    if lab.get("skip") in labels:
        return Plan("skip", score, reasons, per_file, forced_by=f"ラベル {lab['skip']}")
    if author and author in skip.get("authors", []):
        return Plan("skip", score, reasons, per_file, forced_by=f"作成者 {author}")
    if not changes:
        return Plan("skip", 0, ["変更なし"], {}, forced_by="変更なし")
    only = skip.get("only_paths", [])
    never = skip.get("never_skip_paths", [])
    if all(any(glob_match(c.path, g) for g in only) and not any(glob_match(c.path, g) for g in never) for c in changes):
        return Plan("skip", score, reasons, per_file, forced_by="文書・ロックファイルだけの変更")
    if lab.get("deep") in labels:
        return Plan("deep", score, reasons, per_file, forced_by=f"ラベル {lab['deep']}")
    if lab.get("standard") in labels:
        return Plan("standard", score, reasons, per_file, forced_by=f"ラベル {lab['standard']}")
    tier = "deep" if score >= cfg["escalation"]["deep_score"] else "standard"
    return Plan(tier, score, reasons, per_file)
