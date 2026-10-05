"""PR のコメント（Markdown）と、機械可読のレポート（JSON）."""

from __future__ import annotations

import re

from .review import Result

MARKER = "<!-- auto-review -->"
STATUS_LABEL = {"confirmed": "検証済み", "uncertain": "要確認", "unverified": "未検証", "rejected": "却下"}


def _short(model: str) -> str:
    for k in ("haiku", "sonnet", "opus"):
        if k in model:
            return k.capitalize()
    return model


_MENTION = re.compile(r"[@＠](channel|here|everyone|all)\b", re.I)


def _cell(s: str) -> str:
    """AI の出力を PR のコメントに載せる前に無害化する（HTML・画像・全員へのメンション・表の区切り・改行）."""
    s = (s or "")[:2000].replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("![", "!\\[")
    s = _MENTION.sub(lambda m: "@\u200b" + m.group(1), s)
    return s.replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def to_markdown(r: Result, max_rows: int = 20) -> str:
    p = r.plan
    lines = [MARKER, "### AI レビュー（下書き）", ""]
    why = f"（{p.forced_by}）" if p.forced_by else f"（点数 {p.score}）"
    used = " → ".join(dict.fromkeys(_short(c.model) for c in r.calls)) or "なし"
    lines.append(f"振り分け: **{p.tier}** {why}　使ったモデル: {used}")
    for n in r.notes:
        lines.append(f"> {n}")
    lines.append("")
    shown = [f for f in r.findings if f.status != "rejected"]
    rejected = [f for f in r.findings if f.status == "rejected"]
    if shown:
        lines += ["| 状態 | 重要度 | 箇所 | 指摘 | モデル |", "|---|---|---|---|---|"]
        for f in shown[:max_rows]:
            body = f"**{_cell(f.title)}** {_cell(f.detail)}"
            if f.verdict_note:
                body += f"<br>検証: {_cell(f.verdict_note)}"
            models = "・".join(_short(m) for m in f.models)
            lines.append(f"| {STATUS_LABEL[f.status]} | {f.severity} | `{_cell(f.location)}` | {body} | {models} |")
        if len(shown) > max_rows:
            lines.append(f"\n（ほか {len(shown) - max_rows} 件はレポートの JSON を参照）")
    elif p.tier != "skip":
        lines.append("指摘はありません。")
    if rejected:
        lines += ["", f"<details><summary>検証で却下した指摘（{len(rejected)} 件）</summary>", ""]
        lines += [f"- [{f.severity}] `{_cell(f.location)}` {_cell(f.title)} — {_cell(f.verdict_note)}" for f in rejected]
        lines += ["", "</details>"]
    if p.reasons:
        lines += ["", "<details><summary>振り分けの理由</summary>", ""] + [f"- {x}" for x in p.reasons[:15]] + ["", "</details>"]
    if r.calls:
        est = "（見積もり）" if any(c.estimated for c in r.calls) else ""
        per = " / ".join(f"{_short(c.model)} ${c.cost_usd:.3f}" for c in r.calls)
        tin, tout = sum(c.input_tokens for c in r.calls), sum(c.output_tokens for c in r.calls)
        lines += ["", f"費用{est}: **${r.cost_usd:.3f}**（{per}）・入力 {tin:,} / 出力 {tout:,} トークン"]
    lines += ["", "AI の指摘は下書きです。採否は人が決めてください。"]
    return "\n".join(lines) + "\n"


def to_report(r: Result) -> dict:
    return {
        "tool": "auto-review",
        "plan": r.plan.to_dict(),
        "calls": [c.to_dict() for c in r.calls],
        "cost_usd": r.cost_usd,
        "notes": r.notes,
        "summary": {
            "findings": len(r.findings),
            "shown": sum(1 for f in r.findings if f.status != "rejected"),
            "confirmed": sum(1 for f in r.findings if f.status == "confirmed"),
            "uncertain": sum(1 for f in r.findings if f.status == "uncertain"),
            "rejected": sum(1 for f in r.findings if f.status == "rejected"),
            "open_must": r.open_must,
        },
        "findings": [f.to_report() for f in r.findings if f.status != "rejected"],
        "rejected": [f.to_report() for f in r.findings if f.status == "rejected"],
    }
