"""PR のコメント（Markdown）と、機械可読のレポート（JSON）."""

from __future__ import annotations

import math
import re

from .review import Result

MARKER = "<!-- auto-review -->"
# 同じ PR で使った額の累計（USD）を、コメントの末尾に隠して残す. 次の実行が --prior-comment で読み、PR の累計の予算を判定する
SPENT_MARKER = "auto-review-spent-usd"
_SPENT_RE = re.compile(r"<!--\s*" + SPENT_MARKER + r":\s*([0-9]+(?:\.[0-9]+)?)\s*-->")
STATUS_LABEL = {"confirmed": "検証済み", "uncertain": "要確認", "unverified": "未検証", "rejected": "却下"}


def _short(model: str) -> str:
    for k in ("haiku", "sonnet", "opus"):
        if k in model:
            return k.capitalize()
    return model


def parse_spent(text: str) -> float:
    """前回のコメントの隠しマーカーから、同じ PR でこれまでに使った額（USD）を読む. 無ければ 0.

    マーカーが複数あれば最後のものを使う. 負の値・数でない値は受け付けない（正規表現で数字だけを読む）.
    """
    found = _SPENT_RE.findall(text or "")
    if not found:
        return 0.0
    v = float(found[-1])
    return v if math.isfinite(v) else 0.0


# GitHub のメンション（@user・@org/team・@channel など）. 全角の ＠ も. @ の後ろに幅 0 の空白を入れて通知させない
_MENTION = re.compile(r"([@＠])(?=[A-Za-z0-9_-])")
# 自動でリンクになる URL（https://…・www.…）. 区切りに幅 0 の空白を入れてリンクにさせない
_AUTOLINK = re.compile(r"(?i)\b(https?|ftp)(://)|\b(www)(\.)")


def _cell(s: str) -> str:
    """AI の出力（と PR の差分から来る文字列）を PR のコメントに載せる前に無害化する.

    HTML・メンション（@user・@org/team・@channel）・Markdown のリンクと画像（`[..](..)`・`![..](..)`・`[..][ref]`・
    自動リンクの URL）・表の区切り・改行. 文字は消さず、記号をエスケープして文字として見せる.
    """
    s = (s or "")[:2000].replace("\\", "\\\\")
    s = s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    s = s.replace("[", "\\[").replace("]", "\\]")
    s = _MENTION.sub(lambda m: m.group(1) + "\u200b", s)
    s = _AUTOLINK.sub(lambda m: (m.group(1) + "\u200b" + m.group(2)) if m.group(1) else (m.group(3) + "\u200b" + m.group(4)), s)
    return s.replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def _code(s: str, table: bool = True) -> str:
    """コードの形（`…`）で載せる文字列. 中のバッククォートで囲みを閉じさせない（似た文字 ˋ に置き換える）.

    コードの中では HTML・メンション・リンクは効かないので、エスケープしない（すると &amp; などがそのまま見える）.
    表の中なら | だけはエスケープする（GFM の表は、コードの中の | も列の区切りにするため）.
    """
    s = (s or "")[:500].replace("`", "\u02cb").replace("\r", " ").replace("\n", " ").strip() or "-"
    return f"`{s.replace('|', chr(92) + '|') if table else s}`"


def to_markdown(r: Result, max_rows: int = 20) -> str:
    p = r.plan
    lines = [MARKER, "### AI レビュー（下書き）", ""]
    why = f"（{p.forced_by}）" if p.forced_by else f"（点数 {p.score}）"
    used = " → ".join(dict.fromkeys(_short(c.model) for c in r.calls)) or "なし"
    lines.append(f"振り分け: **{p.tier}** {why}　使ったモデル: {used}")
    for n in r.notes:  # notes にはファイルのパス（PR の差分から来る）も入るので無害化する
        lines.append(f"> {_cell(n)}")
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
            lines.append(f"| {STATUS_LABEL[f.status]} | {f.severity} | {_code(f.location)} | {body} | {models} |")
        if len(shown) > max_rows:
            lines.append(f"\n（ほか {len(shown) - max_rows} 件はレポートの JSON を参照）")
    elif r.over_budget:  # 予算超過で呼ばなかった（「指摘なし」と書かない）
        lines.append("予算を超える見込みのため、AI レビューをしていません。")
    elif p.tier != "skip":
        lines.append("指摘はありません。")
    if rejected:
        lines += ["", f"<details><summary>検証で却下した指摘（{len(rejected)} 件）</summary>", ""]
        lines += [f"- \\[{f.severity}\\] {_code(f.location, table=False)} {_cell(f.title)} — {_cell(f.verdict_note)}" for f in rejected]
        lines += ["", "</details>"]
    if p.reasons:
        lines += ["", "<details><summary>振り分けの理由</summary>", ""] + [f"- {_cell(x)}" for x in p.reasons[:15]] + ["", "</details>"]
    if r.calls:
        est = "（見積もり）" if any(c.estimated for c in r.calls) else ""
        per = " / ".join(f"{_short(c.model)} ${c.cost_usd:.3f}" for c in r.calls)
        tin, tout = sum(c.input_tokens for c in r.calls), sum(c.output_tokens for c in r.calls)
        lines += ["", f"費用{est}: **${r.cost_usd:.3f}**（{per}）・入力 {tin:,} / 出力 {tout:,} トークン"]
    lines += ["", "AI の指摘は下書きです。採否は人が決めてください。"]
    # 同じ PR で使った額の累計（次の実行が PR の累計の予算を判定するのに使う. 見積もりだけの実行は増やさない）
    lines += ["", f"<!-- {SPENT_MARKER}: {r.pr_total_usd:.4f} -->"]
    return "\n".join(lines) + "\n"


def to_report(r: Result) -> dict:
    return {
        "tool": "auto-review",
        "plan": r.plan.to_dict(),
        "calls": [c.to_dict() for c in r.calls],
        "cost_usd": r.cost_usd,
        "notes": r.notes,
        "over_budget": r.over_budget,
        "spent_before_usd": round(r.spent_before, 4),
        "pr_total_usd": r.pr_total_usd,
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
