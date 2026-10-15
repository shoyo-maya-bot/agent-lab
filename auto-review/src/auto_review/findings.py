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
    reply: 呼び出し先で読めなかったとき、その呼び出しの使用量と費用（backends.Reply. 課金はされている）. 無ければ None.
    """

    reply = None


# 指摘 1 件の形. severity は別名（CRITICAL など）を受けるため文字列で受け、読み替えは norm_severity で行う
_FINDING_ITEM = {
    "type": "object",
    "required": ["severity", "title"],
    "properties": {
        "severity": {"type": "string", "description": "MUST / WANT / NITS / Q"},
        "file": {"type": "string"},
        "location": {"type": "string", "description": "file が無いとき（設計書の章・シートなど）"},
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


TITLE_SIMILARITY = 0.35  # 題名の 2 文字ずつの組の重なり（Jaccard）. これ以上で「同じ問題」とみなす


def title_similarity(a: str, b: str) -> float:
    x, y = _bigrams(a), _bigrams(b)
    return len(x & y) / max(1, len(x | y))


def similar(a: Finding, b: Finding) -> bool:
    """同じ問題を指しているか.

    ファイルが同じで、題名が似ていることを必ず求める（行が近いだけでは同じとみなさない.
    5 行以内にある別の問題を吸収して「一致」「MUST」に上げてしまうため）. 両方に行があれば、行が近いことも求める.
    """
    if not _same_file(a.file, b.file):
        return False
    if a.line and b.line and abs(a.line - b.line) > 5:
        return False
    return title_similarity(a.title, b.title) >= TITLE_SIMILARITY


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


def _keep_extra(same: Finding, e: Finding) -> None:
    """合流させた指摘（e）の題名・説明・根拠・行を、合流先（same）の説明に残す（同じ内容なら足さない）."""
    parts = []
    if e.title and e.title != same.title:
        parts.append(e.title)
    if e.detail and e.detail != same.detail and e.detail not in same.detail:
        parts.append(e.detail)
    if e.line and e.line != same.line:
        parts.append(f"{e.line} 行目")
    if e.evidence and e.evidence != same.evidence:
        parts.append(f"根拠: {e.evidence}")
    if not parts:
        return
    by = "・".join(e.models) or e.id
    note = f"（{by} の指摘: " + " — ".join(parts) + "）"
    same.detail = f"{same.detail} {note}".strip() if same.detail else note


def merge(base: list[Finding], extra: list[Finding]) -> list[Finding]:
    """extra のうち base と同じ問題は base に合流させ（モデルの一致）、新しいものだけ足す.

    同じ問題とみなすのは similar() が真のとき（ファイル・題名が似ている、行があれば近い）だけ.
    合流させても extra（上位モデル）の題名・説明・根拠は合流先の説明に残す.
    """
    out = list(base)
    for e in extra:
        # 却下された指摘には合流させない（上位モデルが新しく出した指摘を、却下の陰に隠さない）
        same = next((b for b in out if b.status != "rejected" and similar(b, e)), None)
        if same:
            for m in e.models:
                if m not in same.models:
                    same.models.append(m)
            # 合流しても後から来た（上位モデルの）指摘の中身は捨てない. 題名・説明・根拠が違えば説明に書き足す
            _keep_extra(same, e)
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
