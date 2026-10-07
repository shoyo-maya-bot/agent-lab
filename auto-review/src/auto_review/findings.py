"""モデルの出力から指摘を取り出し、検証の結果を当てはめて、人が見る順に並べる."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field

from devtools_common.llm import SchemaValidationError, validate

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


class UnreadableOutput(RuntimeError):
    """モデルの出力を読めなかった（JSON が無い・壊れている・スキーマに合わない）. 指摘 0 件とは区別する.

    cli は「結果を読めません」で終了コード 3 にする.
    """


# 指摘 1 件の形. severity は別名（CRITICAL など）を受けるため文字列で受け、読み替えは norm_severity で行う
_FINDING_ITEM = {
    "type": "object",
    "required": ["severity", "title"],
    "properties": {
        "severity": {"type": "string", "description": "MUST / WANT / NITS / Q"},
        "file": {"type": "string"},
        "line": {"type": ["integer", "string", "null"]},
        "title": {"type": "string"},
        "detail": {"type": "string"},
        "evidence": {"type": "string"},
    },
}
# 一次レビューの出力: {"findings": [...]}
FINDINGS_SCHEMA: dict = {
    "type": "object",
    "required": ["findings"],
    "properties": {"findings": {"type": "array", "items": _FINDING_ITEM}},
}
# 検証役の出力: {"verdicts": [...], "findings": [...]}（findings は省略可）
VERDICTS_SCHEMA: dict = {
    "type": "object",
    "required": ["verdicts"],
    "properties": {
        "verdicts": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["id", "verdict"],
                "properties": {
                    "id": {"type": "string"},
                    "verdict": {"type": "string", "description": "confirm / reject / uncertain"},
                    "severity": {"type": "string"},
                    "reason": {"type": "string"},
                },
            },
        },
        "findings": {"type": "array", "items": _FINDING_ITEM},
    },
}

_FENCED = re.compile(r"```(?:json)?[ \t]*\n(.*?)```", re.S)


def _validate(data, schema: dict) -> None:
    try:
        validate(data, schema)
    except SchemaValidationError as e:
        raise UnreadableOutput(f"出力が決まった形（スキーマ）に合いません: {e}") from e


def load_output(text: str, schema: dict) -> dict:
    """モデルの出力（```json のコードブロック、または本文全体）を JSON として読み、スキーマで検証して返す.

    読めない・壊れている（末尾のカンマなど）・スキーマに合わない場合は UnreadableOutput（黙って 0 件にしない）.
    コードブロックが複数あるときは、すべてが読めてスキーマに合う場合に限り、配列をつなげて 1 つにする.
    """
    blocks = _FENCED.findall(text or "")
    candidates = blocks or [text or ""]
    merged: dict | None = None
    for raw in candidates:
        body = raw.strip()
        if not body:
            raise UnreadableOutput("出力に JSON がありません（空）")
        try:
            data = json.loads(body)
        except json.JSONDecodeError as e:
            # 前置きの文章だけが付いた場合に限り、最初の { から最後の } までを読み直す
            m = re.search(r"\{.*\}", body, re.S) if not blocks else None
            try:
                data = json.loads(m.group(0)) if m else None
            except json.JSONDecodeError:
                data = None
            if data is None:
                raise UnreadableOutput(f"出力を JSON として読めません（{e.msg}, {e.lineno} 行 {e.colno} 文字目）") from e
        _validate(data, schema)
        if merged is None:
            merged = data
        else:
            for k, v in data.items():
                if isinstance(v, list) and isinstance(merged.get(k), list):
                    merged[k] = merged[k] + v
                else:
                    merged.setdefault(k, v)
    assert merged is not None
    return merged


# モデルがよく使う別名. ここに無い値は「読めない重要度」として扱う（下の norm_severity）.
# 読み替えの表はここ 1 か所だけに置く（review-platform もこの表と norm_severity を使う）
SEVERITY_ALIASES = {
    "MUST": ("MUST", "CRITICAL", "BLOCKER", "BLOCKING", "HIGH", "SEVERE", "MAJOR", "ERROR", "P0", "P1", "必須", "重大", "致命的", "高"),
    "WANT": ("WANT", "SHOULD", "MEDIUM", "MODERATE", "WARNING", "WARN", "P2", "推奨", "中"),
    "NITS": ("NITS", "NIT", "NITPICK", "MINOR", "LOW", "TRIVIAL", "STYLE", "INFO", "SUGGESTION", "P3", "軽微", "低"),
    "Q": ("Q", "QUESTION", "QUESTIONS", "QUERY", "ASK", "質問", "確認"),
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


def findings_from_data(items: list, model: str, prefix: str) -> list[Finding]:
    """スキーマで検証済みの指摘の配列を Finding にする. 読めない重要度は MUST の候補として残し、要確認の印を付ける."""
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
    empty = [f.id for f in out if not f.title]
    if empty:
        raise UnreadableOutput(f"題名が空の指摘があります: {', '.join(empty)}")
    return out


def verdicts_from_data(items: list) -> dict[str, dict]:
    """スキーマで検証済みの判定の配列を {id: 判定} にする."""
    return {str(v["id"]): v for v in items if isinstance(v, dict) and v.get("id")}


def parse_findings(text: str, model: str, prefix: str) -> list[Finding]:
    """一次レビューの出力 {"findings": [...]} を読む. 読めなければ UnreadableOutput."""
    return findings_from_data(load_output(text, FINDINGS_SCHEMA)["findings"], model, prefix)


def parse_verdicts(text: str) -> dict[str, dict]:
    """検証の結果 {"verdicts": [{"id", "verdict": confirm|reject|uncertain, "severity", "reason"}]}. 読めなければ UnreadableOutput."""
    return verdicts_from_data(load_output(text, VERDICTS_SCHEMA)["verdicts"])


def parse_verification(text: str, model: str, prefix: str) -> tuple[dict[str, dict], list[Finding]]:
    """検証役の出力を 1 回だけ読み、（判定, 新しい指摘）を返す. 読めなければ UnreadableOutput."""
    data = load_output(text, VERDICTS_SCHEMA)
    return verdicts_from_data(data["verdicts"]), findings_from_data(data.get("findings", []), model, prefix)


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
