"""変更の危なさを点数にして、レビューの段（skip / standard / deep）を決める.

ここはモデルを呼ばない（規則だけ・無料・毎回同じ結果）. 振り分けにまで LLM を使うと、
費用が増えるうえに「なぜ Opus に回ったか」を人が説明できなくなるため.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import yaml

from .diff import FileChange

_TEST_ANNOTATION = re.compile(r"^\s*@(Test|ParameterizedTest|RepeatedTest)\b|^\s*def test_|^\s*Scenario:")

DEFAULT_CONFIG = Path(__file__).resolve().parent / "router.yaml"


class ConfigError(ValueError):
    """router.yaml の誤り（YAML として読めない・必要なキーが無い・型が違う）. cli は終了コード 2（入力の誤り）にする."""


def load_config(path: Path | None = None) -> dict:
    """router.yaml を読み、形を確かめる. ファイルを読めなければ OSError、中身の誤りは ConfigError."""
    src = path or DEFAULT_CONFIG
    try:
        cfg = yaml.safe_load(src.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise ConfigError(f"{src}: YAML として読めません: {e}") from e
    except UnicodeDecodeError as e:
        raise ConfigError(f"{src}: UTF-8 として読めません: {e}") from e
    try:
        validate_config(cfg)
    except ConfigError as e:
        raise ConfigError(f"{src}: {e}") from e
    return cfg


def _is_num(v: object) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _is_int(v: object) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _need(cond: bool, where: str, what: str) -> None:
    if not cond:
        raise ConfigError(f"{where} は{what}にしてください")


def _str_list(v: object, where: str) -> None:
    _need(isinstance(v, list) and all(isinstance(x, str) for x in v), where, "文字列の配列")


def validate_config(cfg: object) -> None:
    """振り分け・予算で使うキーと型を確かめる（壊れた設定をトレースバックや「MUST あり」と取り違えないように）."""
    _need(isinstance(cfg, dict), "設定の全体", "マッピング（キー: 値）")
    assert isinstance(cfg, dict)
    models = cfg.get("models")
    _need(isinstance(models, dict), "models", "マッピング")
    for key in ("standard", "deep"):
        _need(key in models, f"models.{key}", "必ず書く項目")
    for key, m in models.items():
        _need(
            isinstance(m, dict) and isinstance(m.get("id"), str) and m["id"].strip() != "", f"models.{key}", "id（文字列）を持つマッピング"
        )
        for k in ("input_per_mtok", "output_per_mtok"):
            if k in m:
                _need(_is_num(m[k]) and m[k] >= 0, f"models.{key}.{k}", "0 以上の数")
    budget = cfg.get("budget")
    _need(isinstance(budget, dict), "budget", "マッピング")
    _need(_is_num(budget.get("per_pr_usd")) and budget["per_pr_usd"] >= 0, "budget.per_pr_usd", "0 以上の有限の数")
    total = budget.get("per_pr_total_usd")
    _need(total is None or (_is_num(total) and total >= 0), "budget.per_pr_total_usd", "0 以上の有限の数（または書かない）")
    _need(_is_int(budget.get("context_max_tokens")) and budget["context_max_tokens"] > 0, "budget.context_max_tokens", "1 以上の整数")
    out = budget.get("output_tokens")
    _need(isinstance(out, dict), "budget.output_tokens", "マッピング（standard・deep・verify）")
    for k in ("standard", "deep", "verify"):
        _need(_is_int(out.get(k)) and out[k] > 0, f"budget.output_tokens.{k}", "1 以上の整数")
    if "chars_per_token" in budget:
        _need(_is_num(budget["chars_per_token"]) and budget["chars_per_token"] > 0, "budget.chars_per_token", "0 より大きい数")
    esc = cfg.get("escalation")
    _need(isinstance(esc, dict), "escalation", "マッピング")
    _need(_is_num(esc.get("deep_score")), "escalation.deep_score", "数")
    if "verify_max_findings" in esc:
        _need(_is_int(esc["verify_max_findings"]) and esc["verify_max_findings"] >= 0, "escalation.verify_max_findings", "0 以上の整数")
    labels = cfg.get("labels", {})
    _need(isinstance(labels, dict) and all(isinstance(v, str) for v in labels.values()), "labels", "文字列の値を持つマッピング")
    skip = cfg.get("skip", {})
    _need(isinstance(skip, dict), "skip", "マッピング")
    for k in ("authors", "only_paths", "never_skip_paths"):
        if k in skip:
            _str_list(skip[k], f"skip.{k}")
    risk = cfg.get("risk", {})
    _need(isinstance(risk, dict), "risk", "マッピング")
    paths = risk.get("paths", [])
    _need(isinstance(paths, list), "risk.paths", "配列")
    for i, rule in enumerate(paths):
        where = f"risk.paths[{i}]"
        _need(isinstance(rule, dict) and isinstance(rule.get("glob"), str), where, "glob（文字列）を持つマッピング")
        _need(_is_num(rule.get("weight")), f"{where}.weight", "数")
        _need(isinstance(rule.get("reason", ""), str), f"{where}.reason", "文字列")
    patterns = risk.get("patterns", [])
    _need(isinstance(patterns, list), "risk.patterns", "配列")
    for i, rule in enumerate(patterns):
        where = f"risk.patterns[{i}]"
        _need(isinstance(rule, dict) and isinstance(rule.get("regex"), str), where, "regex（文字列）を持つマッピング")
        _need(_is_num(rule.get("weight")), f"{where}.weight", "数")
        _need(isinstance(rule.get("reason", ""), str), f"{where}.reason", "文字列")
        _need(isinstance(rule.get("files", ""), str), f"{where}.files", "文字列（glob）")
        _need(isinstance(rule.get("in_tests", False), bool), f"{where}.in_tests", "真偽値（true / false）")
        try:
            re.compile(rule["regex"])
        except re.error as e:
            raise ConfigError(f"{where}.regex を正規表現として読めません: {e}") from e
    size = risk.get("size", {})
    _need(isinstance(size, dict), "risk.size", "マッピング")
    for k, steps in size.items():
        _need(
            isinstance(steps, list) and all(isinstance(x, list) and len(x) == 2 and all(_is_num(y) for y in x) for x in steps),
            f"risk.size.{k}",
            "[しきい値, 点数] の配列",
        )
    if "deleted_tests_weight" in risk:
        _need(_is_num(risk["deleted_tests_weight"]), "risk.deleted_tests_weight", "数")


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
    escalate: bool = True  # False なら standard の MUST があっても deep モデルに上げない（`review:standard` ラベル）

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


def pattern_applies_to_tests(rule: dict) -> bool:
    """risk.patterns の規則をテストのファイルにも当てるか.

    `in_tests: true` / `false` で決める. 書いていない規則は、互換のため理由（reason）に「秘密」を含むときだけ当てる
    （これまでの動き. 新しく書く規則は in_tests を明示する）.
    """
    if "in_tests" in rule:
        return bool(rule["in_tests"])
    return "秘密" in str(rule.get("reason", ""))


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
            # in_tests: true の規則（秘密情報らしき値など）は、テストのファイルでも数える
            body = "\n".join([t for _, t in ch.added] + ch.removed)
            for rule in risk.get("patterns", []):
                if rule.get("files") and not glob_match(ch.path, rule["files"]):
                    continue
                if pattern_applies_to_tests(rule) and re.search(rule["regex"], body):
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
    # review:deep は「必ず Opus まで」（README）. 文書だけの変更の省略より先に見る（以前は文書だけだと省かれ、ラベルが効かなかった）
    if lab.get("deep") in labels:
        return Plan("deep", score, reasons, per_file, forced_by=f"ラベル {lab['deep']}")
    only = skip.get("only_paths", [])
    never = skip.get("never_skip_paths", [])
    docs_only = all(any(glob_match(c.path, g) for g in only) and not any(glob_match(c.path, g) for g in never) for c in changes)
    # 文書だけの変更でも、秘密情報らしき値などファイルの危険度の点数があれば skip しない（変更の大きさの点数は除く）
    # review:standard は「Opus に上げない」上限なので、文書だけの変更はこれまでどおり省く
    if docs_only and not any(s > 0 for s in per_file.values()):
        return Plan("skip", score, reasons, per_file, forced_by="文書・ロックファイルだけの変更")
    if lab.get("standard") in labels:
        return Plan("standard", score, reasons, per_file, forced_by=f"ラベル {lab['standard']}", escalate=False)
    tier = "deep" if score >= cfg["escalation"]["deep_score"] else "standard"
    return Plan(tier, score, reasons, per_file)
