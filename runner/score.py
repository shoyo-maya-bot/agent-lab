#!/usr/bin/env python3
"""レビュー結果を正解（bench/bugs/*.yaml）と突き合わせて、検出率とノイズ率を出す.

    python runner/score.py bench/bugs/sample-app-known.yaml review.md [review2.md ...] [-f json]

レビュー結果の形式:
- Markdown: office-review の表（No / 重要度 / 箇所 / 指摘 …）か、「- [MUST] ファイル: 本文」の箇条書き
- JSON: [{"severity", "location", "message"}] か、devtools の共通レポート（findings[]）

指標:
- 検出率 = 見つけた正解の数 / 正解の数
- ノイズ率 = どの正解にも当たらない指摘の数 / 指摘の数（正解に無い本物の問題も含むので、目視で確かめる前提の目安）
- 重要度の一致 = 見つけた正解のうち、重要度が正解と同じ割合
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import yaml

SEVERITIES = ("MUST", "WANT", "NITS", "Q")


@dataclass
class Finding:
    severity: str
    location: str
    message: str

    @property
    def text(self) -> str:
        return f"{self.location} {self.message}".lower()


def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def parse_markdown(text: str) -> list[Finding]:
    out: list[Finding] = []
    header: list[str] | None = None
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("|"):
            cells = _cells(s)
            if header is None:
                if any(c in ("指摘", "message") for c in cells):
                    header = cells
                continue
            if set(s.replace("|", "").strip()) <= set("-: "):
                continue
            row = dict(zip(header, cells, strict=False))
            sev = (row.get("重要度") or row.get("severity") or "").upper()
            loc = " ".join(row.get(k, "") for k in ("箇所", "location", "ファイル"))
            out.append(Finding(sev, loc, row.get("指摘") or row.get("message") or ""))
            continue
        header = None if not s else header
        m = re.match(r"^[-*]\s*\[?(MUST|WANT|NITS|NIT|Q)\]?\s*[:：]?\s*(.+)$", s, re.I)
        if m:
            sev = m.group(1).upper().replace("NIT", "NITS").replace("NITSS", "NITS")
            body = m.group(2)
            loc, _, msg = body.partition(":") if ":" in body.split(" ")[0] else ("", "", body)
            out.append(Finding(sev, loc, msg or body))
    return out


def parse_json(data) -> list[Finding]:
    items = data.get("findings", []) if isinstance(data, dict) else data
    return [Finding(str(i.get("severity", "")).upper(), str(i.get("location", "")), str(i.get("message", ""))) for i in items]


def load_findings(path: Path) -> list[Finding]:
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".json":
        return parse_json(json.loads(text))
    return parse_markdown(text)


def matches(bug: dict, f: Finding) -> bool:
    t = f.text
    # ファイルは拡張子なしの名前でも一致とする（「MemberService.register」のように書かれることが多いため）
    stems = {x.lower() for x in bug["files"]} | {x.rsplit(".", 1)[0].lower() for x in bug["files"]}
    return any(x in t for x in stems) and any(k.lower() in t for k in bug["keywords"])


def score(bugs: list[dict], findings: list[Finding]) -> dict:
    found: dict[str, Finding] = {}
    used: set[int] = set()
    for b in bugs:
        for i, f in enumerate(findings):
            if matches(b, f):
                found.setdefault(b["id"], f)
                used.add(i)
    noise = [f for i, f in enumerate(findings) if i not in used]
    sev_ok = sum(1 for b in bugs if b["id"] in found and found[b["id"]].severity == b["severity"])
    return {
        "bugs": len(bugs),
        "findings": len(findings),
        "detected": sorted(found),
        "missed": [b["id"] for b in bugs if b["id"] not in found],
        "detection_rate": round(len(found) / len(bugs), 3) if bugs else 0.0,
        "noise_rate": round(len(noise) / len(findings), 3) if findings else 0.0,
        "severity_match": round(sev_ok / len(found), 3) if found else 0.0,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("answers", type=Path)
    ap.add_argument("reviews", type=Path, nargs="+")
    ap.add_argument("-f", "--format", choices=["markdown", "json"], default="markdown")
    args = ap.parse_args(argv)
    bugs = yaml.safe_load(args.answers.read_text(encoding="utf-8"))["bugs"]
    results = {str(p): score(bugs, load_findings(p)) for p in args.reviews}
    if args.format == "json":
        print(json.dumps(results, ensure_ascii=False, indent=1))
        return 0
    print("| レビュー | 指摘 | 検出率 | ノイズ率 | 重要度の一致 | 見逃し |\n|---|---|---|---|---|---|")
    for name, r in results.items():
        print(
            f"| {Path(name).name} | {r['findings']} | {r['detection_rate']:.0%} | {r['noise_rate']:.0%} | {r['severity_match']:.0%} | {', '.join(r['missed']) or '-'} |"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
