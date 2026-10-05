"""振り分けの結果に沿ってモデルを呼び、指摘をまとめる.

流れ:
    1. standard モデルが差分をレビュー（skip 以外は必ず 1 回）
    2. deep に上げるか決める
       - 計画が deep            → deep モデルが「全指摘の検証 + 危ないファイルの追加レビュー」
       - standard が MUST を出した → deep モデルが「MUST の検証」だけ（範囲を絞るので安い）
    3. 見積もりが予算を超えるなら、deep の範囲を危ないファイルに絞る。それでも超えるなら deep を飛ばして理由を残す
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .backends import BackendError, Reply, price
from .diff import FileChange
from .findings import SEVERITY_ORDER, Finding, apply_verdicts, merge, ordered, parse_findings, parse_verdicts
from .router import Plan

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


def _tokens(chars: int, cfg: dict) -> int:
    return int(chars / cfg["budget"].get("chars_per_token", 3.0)) + 1


MAX_DIFF_CHARS = 400_000  # 差分そのものの上限. 超えた分は省く（大きな PR で予算の確認が効かなくなるのを防ぐ）


def _untrusted(tag: str, text: str, attrs: str = "") -> str:
    """信用しないデータとして区切る. 閉じタグが中身にあれば無害化する."""
    body = text.replace(f"</{tag}>", f"<\\/{tag}>")
    return f"<{tag}{(' ' + attrs) if attrs else ''}>\n{body}\n</{tag}>\n"


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


def build_context(diff_text: str, changes: list[FileChange], repo: Path | None, cfg: dict, focus: list[str] | None, max_tokens: int) -> str:
    """差分の全体と、変更ファイルの全文（focus → 危ない順、上限まで）. どちらも信用しないデータとして区切る."""
    note = ""
    if len(diff_text) > MAX_DIFF_CHARS:
        note = f"（差分が長いため先頭の {MAX_DIFF_CHARS:,} 文字だけを渡す）\n"
        diff_text = diff_text[:MAX_DIFF_CHARS]
    parts = ["## 差分（データ）\n\n" + note + _untrusted("diff", diff_text.strip())]
    used = _tokens(len(parts[0]), cfg)
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
        block = f"## ファイル: {path}（変更後の全文。データ）\n\n" + _untrusted("file", text, f'path="{path.replace(chr(34), "")}"')
        t = _tokens(len(block), cfg)
        if used + t > max_tokens:
            skipped.append(path)
            continue
        parts.append(block)
        used += t
    if skipped:
        parts.append("（上限のため全文を省いたファイル: " + ", ".join(skipped) + "。差分だけで判断する）\n")
    return "\n".join(parts)


class UnreadableOutput(RuntimeError):
    """モデルの出力から JSON を読めなかった（指摘 0 件と区別する）."""


def _prompt(name: str, max_findings: int) -> str:
    return (PROMPTS / f"{name}.md").read_text(encoding="utf-8").replace("{MAX_FINDINGS}", str(max_findings))


def _findings_block(findings: list[Finding]) -> str:
    rows = [f"- id={f.id} [{f.severity}] {f.location}: {f.title} — {f.detail}（根拠: {f.evidence}）" for f in findings]
    return "## 一次レビューの指摘\n\n" + ("\n".join(rows) if rows else "（なし）") + "\n"


class Reviewer:
    def __init__(self, cfg: dict, backend, repo: Path | None = None, dry_run: bool = False, max_findings: int = 15):
        self.cfg = cfg
        self.backend = backend
        self.repo = repo
        self.dry_run = dry_run
        self.max_findings = max_findings

    # ---- 呼び出し（dry-run なら見積もりだけ）
    def _call(self, step: str, model_key: str, system: str, user: str, result: Result) -> str:
        model = self.cfg["models"][model_key]
        out_key = "verify" if step == "verify" else ("deep" if step == "deep" else "standard")
        max_out = self.cfg["budget"]["output_tokens"][out_key]
        if self.dry_run:
            inp = _tokens(len(system) + len(user), self.cfg)
            result.calls.append(Call(step, model["id"], inp, max_out, round(price(model, inp, max_out), 4), 0.0, estimated=True))
            return ""
        reply: Reply = self.backend.call(model, system, user, max_out)
        result.calls.append(
            Call(step, model["id"], reply.input_tokens, reply.output_tokens, round(reply.cost_usd, 4), round(reply.seconds, 1))
        )
        return reply.text

    def _estimate(self, model_key: str, chars: int, out_key: str) -> float:
        model = self.cfg["models"][model_key]
        return price(model, _tokens(chars, self.cfg), self.cfg["budget"]["output_tokens"][out_key])

    # ---- 1 つのモデルだけで（比較実験用）
    def single(self, model_key: str, diff_text: str, changes: list[FileChange], plan: Plan) -> Result:
        result = Result(plan)
        ctx = build_context(diff_text, changes, self.repo, self.cfg, plan.risky_files, self.cfg["budget"]["context_max_tokens"])
        text = self._call("single", model_key, _prompt("review", self.max_findings), ctx, result)
        result.findings = ordered(parse_findings(text, self.cfg["models"][model_key]["id"], "s"))
        return result

    # ---- 自動（振り分けに沿って）
    def auto(self, diff_text: str, changes: list[FileChange], plan: Plan) -> Result:
        result = Result(plan)
        if plan.tier == "skip":
            result.notes.append(f"AI レビューは省略しました（{plan.forced_by or '対象なし'}）")
            return result
        budget = self.cfg["budget"]
        ctx = build_context(diff_text, changes, self.repo, self.cfg, plan.risky_files, budget["context_max_tokens"])
        std_model = self.cfg["models"]["standard"]["id"]
        text = self._call("standard", "standard", _prompt("review", self.max_findings), ctx, result)
        std = parse_findings(text, std_model, "s")
        if not self.dry_run and not std and "findings" not in text:
            raise UnreadableOutput("standard モデルの出力を読めませんでした（JSON がありません）")

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
            ctx if step == "deep" else build_context(diff_text, changes, self.repo, self.cfg, focus, budget["context_max_tokens"] // 2)
        )
        user = deep_ctx + "\n## 重点ファイル\n\n" + "\n".join(f"- {p}" for p in focus) + "\n\n" + _findings_block(targets)

        spent = result.cost_usd
        out_key = "deep" if step == "deep" else "verify"
        if spent + self._estimate("deep", len(system) + len(user), out_key) > budget["per_pr_usd"]:
            narrow = build_context(diff_text, changes, self.repo, self.cfg, focus[:3], budget["context_max_tokens"] // 3)
            user = narrow + "\n## 重点ファイル\n\n" + "\n".join(f"- {p}" for p in focus[:3]) + "\n\n" + _findings_block(targets)
            if spent + self._estimate("deep", len(system) + len(user), out_key) > budget["per_pr_usd"]:
                result.notes.append(f"予算 ${budget['per_pr_usd']:.2f} を超える見込みのため、deep モデルの検証を省きました")
                result.findings = ordered(std)
                return result
            result.notes.append("予算のため、deep モデルの範囲を危ないファイル 3 つに絞りました")

        try:
            text = self._call(step, "deep", system, user, result)
        except BackendError as e:
            result.notes.append(f"deep モデルの呼び出しに失敗したため、standard の結果だけを出します（{e}）")
            result.findings = ordered(std)
            return result
        deep_model = self.cfg["models"]["deep"]["id"]
        apply_verdicts(std, parse_verdicts(text), deep_model)
        new = parse_findings(text, deep_model, "d")
        result.findings = ordered(merge(std, new))
        return result
