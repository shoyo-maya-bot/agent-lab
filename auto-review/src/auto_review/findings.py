"""モデルの出力から指摘を取り出し、検証の結果を当てはめて、人が見る順に並べる."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field

SEVERITY_ORDER = {"MUST": 0, "WANT": 1, "Q": 2, "NITS": 3}


@dataclass
class Finding:
    severity: str
    file: str
    title: str
    detail: str = ""
    line: int | None = None
    evidence: str = ""
    models: list[str] = field(default_factory=list)  # 指摘した・認めたモデル
    status: str = "unverified"  # unverified / confirmed / rejected
    verdict_note: str = ""
    id: str = ""

    @property
    def location(self) -> str:
        return f"{self.file}:{self.line}" if self.line else self.file

    def to_report(self) -> dict:
        d = asdict(self)
        d["location"] = self.location
        d["message"] = f"{self.title} {self.detail}".strip()
        return d


def _json_blocks(text: str) -> list:
    """```json ... ``` の中身（無ければ本文全体）を JSON として読む."""
    blocks = re.findall(r"```(?:json)?\s*\n(.*?)```", text, re.S) or [text]
    out = []
    for b in blocks:
        try:
            out.append(json.loads(b))
        except json.JSONDecodeError:
            m = re.search(r"(\{.*\}|\[.*\])", b, re.S)
            if m:
                try:
                    out.append(json.loads(m.group(1)))
                except json.JSONDecodeError:
                    continue
    return out


def _norm_sev(s: str) -> str:
    s = str(s or "").upper().strip()
    return {"NIT": "NITS", "QUESTION": "Q"}.get(s, s if s in SEVERITY_ORDER else "WANT")


def _to_int(v) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def parse_findings(text: str, model: str, prefix: str) -> list[Finding]:
    items: list[dict] = []
    for data in _json_blocks(text):
        if isinstance(data, dict):
            data = data.get("findings", [])
        if isinstance(data, list):
            items += [x for x in data if isinstance(x, dict)]
    out = []
    for i, x in enumerate(items, start=1):
        out.append(
            Finding(
                severity=_norm_sev(x.get("severity")),
                file=str(x.get("file") or x.get("location") or "").strip(),
                line=_to_int(x.get("line")),
                title=str(x.get("title") or x.get("message") or "").strip(),
                detail=str(x.get("detail") or "").strip(),
                evidence=str(x.get("evidence") or "").strip(),
                models=[model],
                id=f"{prefix}{i}",
            )
        )
    return [f for f in out if f.title]


def parse_verdicts(text: str) -> dict[str, dict]:
    """検証の結果 {"verdicts": [{"id", "verdict": confirm|reject|uncertain, "severity", "reason"}]}."""
    out: dict[str, dict] = {}
    for data in _json_blocks(text):
        if isinstance(data, dict):
            for v in data.get("verdicts", []):
                if isinstance(v, dict) and v.get("id"):
                    out[str(v["id"])] = v
    return out


def _bigrams(s: str) -> set[str]:
    s = re.sub(r"\s+", "", s.lower())
    return {s[i : i + 2] for i in range(len(s) - 1)} or {s}


def similar(a: Finding, b: Finding) -> bool:
    """同じ問題を指しているか（ファイルが同じで、行が近いか題名が似ている）."""
    fa, fb = a.file.rsplit("/", 1)[-1].lower(), b.file.rsplit("/", 1)[-1].lower()
    if not fa or fa != fb:
        return False
    if a.line and b.line and abs(a.line - b.line) <= 5:
        return True
    x, y = _bigrams(a.title), _bigrams(b.title)
    return len(x & y) / max(1, len(x | y)) >= 0.35


def apply_verdicts(findings: list[Finding], verdicts: dict[str, dict], model: str) -> None:
    for f in findings:
        v = verdicts.get(f.id)
        if not v:
            continue
        kind = str(v.get("verdict", "")).lower()
        f.verdict_note = str(v.get("reason", "")).strip()
        if kind.startswith("confirm"):
            f.status = "confirmed"
            f.models.append(model)
            if v.get("severity"):
                f.severity = _norm_sev(v["severity"])
        elif kind.startswith("reject"):
            f.status = "rejected"


def merge(base: list[Finding], extra: list[Finding]) -> list[Finding]:
    """extra のうち base と同じ問題は base に合流させ（モデルの一致）、新しいものだけ足す."""
    out = list(base)
    for e in extra:
        same = next((b for b in out if similar(b, e)), None)
        if same:
            for m in e.models:
                if m not in same.models:
                    same.models.append(m)
            if same.status == "unverified":
                same.status = "confirmed"
            if SEVERITY_ORDER[e.severity] < SEVERITY_ORDER[same.severity]:
                same.severity = e.severity
        else:
            out.append(e)
    return out


def ordered(findings: list[Finding]) -> list[Finding]:
    status_order = {"confirmed": 0, "unverified": 1, "rejected": 2}
    return sorted(findings, key=lambda f: (status_order[f.status], SEVERITY_ORDER[f.severity], -len(f.models)))
