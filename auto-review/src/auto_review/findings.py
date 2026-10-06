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
    status: str = "unverified"  # unverified（検証なし）/ confirmed / uncertain（検証したが決められない）/ rejected
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


# モデルがよく使う別名. ここに無い値は「読めない重要度」として扱う（下の norm_severity）
SEVERITY_ALIASES = {
    "MUST": ("MUST", "CRITICAL", "BLOCKER", "BLOCKING", "HIGH", "SEVERE", "MAJOR", "ERROR", "P0", "P1", "必須", "重大", "致命的", "高"),
    "WANT": ("WANT", "SHOULD", "MEDIUM", "MODERATE", "WARNING", "WARN", "P2", "推奨", "中"),
    "NITS": ("NITS", "NIT", "NITPICK", "MINOR", "LOW", "TRIVIAL", "STYLE", "INFO", "SUGGESTION", "P3", "軽微", "低"),
    "Q": ("Q", "QUESTION", "QUESTIONS", "ASK", "質問", "確認"),
}
_ALIAS = {a: sev for sev, names in SEVERITY_ALIASES.items() for a in names}


def norm_severity(s) -> tuple[str, bool]:
    """重要度をそろえる. 戻り値は（重要度, 読めたか）.

    読めない値（空・未知の語）は WANT に下げず、MUST の候補として返す（呼び出し側で「要確認」を付ける）.
    「CRITICAL」を WANT に下げて見落とすより、人が 1 件多く確かめるほうが安全なため.
    """
    key = re.sub(r"[\s\[\]【】()（）:：!！._-]", "", str(s or "")).upper()
    if key in _ALIAS:
        return _ALIAS[key], True
    return "MUST", False


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
        sev, known = norm_severity(x.get("severity"))
        out.append(
            Finding(
                severity=sev,
                file=str(x.get("file") or x.get("location") or "").strip(),
                line=_to_int(x.get("line")),
                title=str(x.get("title") or x.get("message") or "").strip(),
                detail=str(x.get("detail") or "").strip(),
                evidence=str(x.get("evidence") or "").strip(),
                models=[model],
                id=f"{prefix}{i}",
            )
        )
        if not known:
            # 下げずに MUST の候補として残し、人が確かめる印を付ける（検証役が判定すればそちらで上書きされる）
            out[-1].status = "uncertain"
            out[-1].verdict_note = f"重要度「{str(x.get('severity') or '')[:30]}」を読めないため、MUST として扱っています（要確認）"
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


def _same_file(a: str, b: str) -> bool:
    a, b = a.lower().split(":")[0], b.lower().split(":")[0]
    if "/" in a and "/" in b:
        return a == b or a.endswith("/" + b) or b.endswith("/" + a)
    return bool(a) and a.rsplit("/", 1)[-1] == b.rsplit("/", 1)[-1]


def similar(a: Finding, b: Finding) -> bool:
    """同じ問題を指しているか（ファイルが同じで、行が近いか題名が似ている）."""
    if not _same_file(a.file, b.file):
        return False
    if a.line and b.line:
        return abs(a.line - b.line) <= 5
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
            sev, known = norm_severity(v.get("severity"))
            if known:  # 検証役の重要度が読めないときは、元の重要度を残す
                f.severity = sev
        elif kind.startswith("reject"):
            f.status = "rejected"
        else:
            # uncertain など: 検証役が決められなかった. 人が確かめる印を付け、あとの合流で「一致」に上げない
            f.status = "uncertain"


def merge(base: list[Finding], extra: list[Finding]) -> list[Finding]:
    """extra のうち base と同じ問題は base に合流させ（モデルの一致）、新しいものだけ足す."""
    out = list(base)
    for e in extra:
        same = next((b for b in out if similar(b, e)), None)
        if same:
            for m in e.models:
                if m not in same.models:
                    same.models.append(m)
            # 検証の判定（認める・決められない・却下）が付いたものは、合流で上書きしない
            if same.status == "unverified":
                same.status = "confirmed"
                if SEVERITY_ORDER[e.severity] < SEVERITY_ORDER[same.severity]:
                    same.severity = e.severity
        else:
            out.append(e)
    return out


def ordered(findings: list[Finding]) -> list[Finding]:
    status_order = {"confirmed": 0, "uncertain": 1, "unverified": 2, "rejected": 3}
    return sorted(findings, key=lambda f: (status_order[f.status], SEVERITY_ORDER[f.severity], -len(f.models)))
