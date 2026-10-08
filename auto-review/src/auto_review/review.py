"""振り分けの結果に沿ってモデルを呼び、指摘をまとめる.

流れ:
    1. standard モデルが差分をレビュー（skip 以外は必ず 1 回）
    2. deep に上げるか決める
       - 計画が deep            → deep モデルが「全指摘の検証 + 危ないファイルの追加レビュー」
       - standard が MUST を出した → deep モデルが「MUST の検証」だけ（範囲を絞るので安い）
    3. 見積もりが予算を超えるなら、deep の範囲を危ないファイルに絞る。それでも超えるなら deep を飛ばして理由を残す
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .backends import BackendError, Reply, price
from .diff import FileChange
from .findings import (
    FINDINGS_SCHEMA,
    SEVERITY_ORDER,
    VERDICTS_SCHEMA,
    Finding,
    UnreadableOutput,
    apply_verdicts,
    merge,
    ordered,
    parse_findings,
    parse_verification,
)
from .router import Plan
from .tokens import DEFAULT_CHARS_PER_TOKEN, estimate_tokens

PROMPTS = Path(__file__).resolve().parent / "prompts"


@dataclass
class Call:
    step: str  # standard / deep / verify / single
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    seconds: float
    estimated: bool = False

    def to_dict(self) -> dict:
        return self.__dict__.copy()


@dataclass
class Result:
    plan: Plan
    findings: list[Finding] = field(default_factory=list)
    calls: list[Call] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def cost_usd(self) -> float:
        return round(sum(c.cost_usd for c in self.calls), 4)

    @property
    def open_must(self) -> int:
        return sum(1 for f in self.findings if f.severity == "MUST" and f.status != "rejected")


def _tokens(text: str, cfg: dict) -> int:
    """見積もり用のトークン数. 日本語など ASCII 以外は 1 文字 1 トークンで数える（tokens.estimate_tokens）."""
    return estimate_tokens(text, cfg["budget"].get("chars_per_token", DEFAULT_CHARS_PER_TOKEN))


MAX_DIFF_CHARS = 400_000  # 差分そのものの上限. 超えた分は省く（大きな PR で予算の確認が効かなくなるのを防ぐ）


# 信用しないデータを区切るタグ. 中身にこれらの閉じタグ（大文字小文字・空白・属性の違いも含む）があれば無害化する
UNTRUSTED_TAGS = ("diff", "file", "findings", "focus")
_CLOSING = re.compile(r"<\s*/\s*(" + "|".join(UNTRUSTED_TAGS) + r")\b[^>]*>", re.I)


def _neutralize(text: str) -> str:
    """`</diff>`・`</DIFF >`・`< /diff>`・`</file\n>` などを `<\\/diff>` に変える（区切りを中から閉じさせない）."""
    return _CLOSING.sub(lambda m: f"<\\/{m.group(1).lower()}>", text)


def _untrusted(tag: str, text: str, attrs: str = "") -> str:
    """信用しないデータとして区切る. 閉じタグが中身にあれば無害化する."""
    assert tag in UNTRUSTED_TAGS
    return f"<{tag}{(' ' + attrs) if attrs else ''}>\n{_neutralize(text)}\n</{tag}>\n"


def _safe_file(repo: Path, path: str) -> Path | None:
    """リポジトリの中にある普通のファイルだけを返す（シンボリックリンク・外を指すパスは読まない）."""
    cur = repo
    for part in Path(path).parts:
        if part in ("..", "/") or part.startswith("/"):
            return None
        cur = cur / part
        if cur.is_symlink():
            return None
    try:
        rp = cur.resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    if repo.resolve() not in rp.parents or not rp.is_file():
        return None
    return rp


def _add_note(notes: list[str] | None, msg: str) -> None:
    if notes is not None and msg not in notes:
        notes.append(msg)


def build_context(
    diff_text: str,
    changes: list[FileChange],
    repo: Path | None,
    cfg: dict,
    focus: list[str] | None,
    max_tokens: int,
    notes: list[str] | None = None,
) -> str:
    """差分の全体と、変更ファイルの全文（focus → 危ない順、上限まで）. どちらも信用しないデータとして区切る.

    差分を切り詰めたとき・ファイルの全文を省いたときは、notes（出力の notes）にもその旨を書く
    （モデルが見ていない部分があることを、結果を読む人が分かるように）.
    """
    note = ""
    if len(diff_text) > MAX_DIFF_CHARS:
        note = f"（差分が長いため先頭の {MAX_DIFF_CHARS:,} 文字だけを渡す）\n"
        _add_note(
            notes,
            f"差分が長いため、先頭の {MAX_DIFF_CHARS:,} 文字（全 {len(diff_text):,} 文字）だけをモデルに渡しました。"
            "残りはレビューしていません",
        )
        diff_text = diff_text[:MAX_DIFF_CHARS]
    parts = ["## 差分（データ）\n\n" + note + _untrusted("diff", diff_text.strip())]
    used = _tokens(parts[0], cfg)
    if repo is None:
        return parts[0]
    order = list(dict.fromkeys((focus or []) + [c.path for c in changes]))
    by_path = {c.path: c for c in changes}
    skipped = []
    for path in order:
        ch = by_path.get(path)
        if ch is None or ch.status == "deleted" or ch.binary:
            continue
        p = _safe_file(repo, path)
        if p is None:
            continue
        text = p.read_text(encoding="utf-8", errors="replace")
        safe_path = re.sub(r'["<>]', "", path)
        block = f"## ファイル: {safe_path}（変更後の全文。データ）\n\n" + _untrusted("file", text, f'path="{safe_path}"')
        t = _tokens(block, cfg)
        if used + t > max_tokens:
            skipped.append(path)
            continue
        parts.append(block)
        used += t
    if skipped:
        parts.append("（上限のため全文を省いたファイル: " + ", ".join(skipped) + "。差分だけで判断する）\n")
        _add_note(notes, "上限のため、次のファイルは全文を渡さず差分だけでレビューしました: " + ", ".join(skipped))
    return "\n".join(parts)


def _prompt(name: str, max_findings: int) -> str:
    return (PROMPTS / f"{name}.md").read_text(encoding="utf-8").replace("{MAX_FINDINGS}", str(max_findings))


def _findings_block(findings: list[Finding]) -> str:
    """一次レビューの指摘. 信用しない差分の影響を受けたモデルの出力なので、差分と同じくデータとして区切る."""
    rows = [f"- id={f.id} [{f.severity}] {f.location}: {f.title} — {f.detail}（根拠: {f.evidence}）" for f in findings]
    return "## 一次レビューの指摘（データ）\n\n" + _untrusted("findings", "\n".join(rows) if rows else "（なし）")


def _focus_block(paths: list[str]) -> str:
    """重点ファイル. パスは差分や一次レビューの出力から来るので、これもデータとして区切る."""
    return "## 重点ファイル（データ）\n\n" + _untrusted("focus", "\n".join(f"- {p}" for p in paths))


class Reviewer:
    def __init__(self, cfg: dict, backend, repo: Path | None = None, dry_run: bool = False, max_findings: int = 15):
        self.cfg = cfg
        self.backend = backend
        self.repo = repo
        self.dry_run = dry_run
        self.max_findings = max_findings

    # ---- 呼び出し（dry-run なら見積もりだけ）
    def _call(self, step: str, model_key: str, system: str, user: str, result: Result) -> str:
        """モデルを呼び、生の出力を返す. 呼び出し先が構造化出力に対応していれば（accepts_schema）、
        スキーマを渡してツールの形で返させる. どちらの場合も、読む側（findings.load_output）でスキーマを検証する."""
        model = self.cfg["models"][model_key]
        out_key = "verify" if step == "verify" else ("deep" if step == "deep" else "standard")
        max_out = self.cfg["budget"]["output_tokens"][out_key]
        if self.dry_run:
            inp = _tokens(system + user, self.cfg)
            result.calls.append(Call(step, model["id"], inp, max_out, round(price(model, inp, max_out), 4), 0.0, estimated=True))
            return ""
        kw = (
            {"schema": VERDICTS_SCHEMA if step in ("verify", "deep") else FINDINGS_SCHEMA}
            if getattr(self.backend, "accepts_schema", False)
            else {}
        )
        reply: Reply = self.backend.call(model, system, user, max_out, role=step, **kw)
        result.calls.append(
            Call(step, model["id"], reply.input_tokens, reply.output_tokens, round(reply.cost_usd, 4), round(reply.seconds, 1))
        )
        return reply.text

    def _estimate(self, model_key: str, text: str, out_key: str) -> float:
        model = self.cfg["models"][model_key]
        return price(model, _tokens(text, self.cfg), self.cfg["budget"]["output_tokens"][out_key])

    # ---- 1 つのモデルだけで（比較実験用）
    def single(self, model_key: str, diff_text: str, changes: list[FileChange], plan: Plan) -> Result:
        result = Result(plan)
        ctx = build_context(
            diff_text, changes, self.repo, self.cfg, plan.risky_files, self.cfg["budget"]["context_max_tokens"], result.notes
        )
        text = self._call("single", model_key, _prompt("review", self.max_findings), ctx, result)
        if not self.dry_run:  # 読めない・スキーマに合わない出力は UnreadableOutput（指摘 0 件にしない）
            result.findings = ordered(parse_findings(text, self.cfg["models"][model_key]["id"], "s"))
        return result

    # ---- 自動（振り分けに沿って）
    def auto(self, diff_text: str, changes: list[FileChange], plan: Plan) -> Result:
        result = Result(plan)
        if plan.tier == "skip":
            result.notes.append(f"AI レビューは省略しました（{plan.forced_by or '対象なし'}）")
            return result
        budget = self.cfg["budget"]
        ctx = build_context(diff_text, changes, self.repo, self.cfg, plan.risky_files, budget["context_max_tokens"], result.notes)
        std_model = self.cfg["models"]["standard"]["id"]
        text = self._call("standard", "standard", _prompt("review", self.max_findings), ctx, result)
        # 読めない・壊れた JSON・スキーマに合わない出力は UnreadableOutput（cli は終了コード 3）. 黙って 0 件にしない
        std = [] if self.dry_run else parse_findings(text, std_model, "s")

        esc = self.cfg["escalation"]
        musts = [f for f in std if f.severity == "MUST"]
        if plan.tier == "deep":
            step, targets, focus = "deep", std, plan.risky_files
        elif esc.get("verify_must") and musts:
            step, targets = "verify", musts
            focus = list(dict.fromkeys(f.file for f in musts))
            result.notes.append(f"standard の MUST {len(musts)} 件を deep モデルで検証しました")
        elif self.dry_run and esc.get("verify_must"):
            result.notes.append("standard が MUST を出したときだけ、deep モデルの検証が加わります（見積もりに含めていません）")
            result.findings = ordered(std)
            return result
        else:
            result.findings = ordered(std)
            return result

        targets = sorted(targets, key=lambda f: SEVERITY_ORDER[f.severity])[: esc.get("verify_max_findings", 8)]
        max_new = self.max_findings if step == "deep" else 3
        system = _prompt("deep", max_new)
        deep_ctx = (
            ctx
            if step == "deep"
            else build_context(diff_text, changes, self.repo, self.cfg, focus, budget["context_max_tokens"] // 2, result.notes)
        )
        user = deep_ctx + "\n" + _focus_block(focus) + "\n" + _findings_block(targets)

        spent = result.cost_usd
        out_key = "deep" if step == "deep" else "verify"
        if spent + self._estimate("deep", system + user, out_key) > budget["per_pr_usd"]:
            narrow_notes: list[str] = []  # 絞った文脈を実際に使うときだけ notes に移す
            narrow = build_context(diff_text, changes, self.repo, self.cfg, focus[:3], budget["context_max_tokens"] // 3, narrow_notes)
            user = narrow + "\n" + _focus_block(focus[:3]) + "\n" + _findings_block(targets)
            if spent + self._estimate("deep", system + user, out_key) > budget["per_pr_usd"]:
                result.notes.append(f"予算 ${budget['per_pr_usd']:.2f} を超える見込みのため、deep モデルの検証を省きました")
                result.findings = ordered(std)
                return result
            result.notes.append("予算のため、deep モデルの範囲を危ないファイル 3 つに絞りました")
            for n in narrow_notes:
                _add_note(result.notes, n)

        try:
            text = self._call(step, "deep", system, user, result)
        except BackendError as e:
            result.notes.append(f"deep モデルの呼び出しに失敗したため、standard の結果だけを出します（{e}）")
            result.findings = ordered(std)
            return result
        if self.dry_run:
            result.findings = ordered(std)
            return result
        deep_model = self.cfg["models"]["deep"]["id"]
        try:
            verdicts, new = parse_verification(text, deep_model, "d")
        except UnreadableOutput as e:
            # 検証役の出力が読めないときは、検証なしの standard の結果を出し、その旨を必ず残す（黙って「検証済み」にしない）
            result.notes.append(f"deep モデルの出力を読めなかったため、standard の結果だけを出します（{e}）")
            result.findings = ordered(std)
            return result
        apply_verdicts(std, verdicts, deep_model)
        result.findings = ordered(merge(std, new))
        return result
