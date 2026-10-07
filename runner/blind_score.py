#!/usr/bin/env python3
"""目隠しで採点した表を鍵と合わせ、構成ごとの指標と信頼区間（ブートストラップ）を出す.

    python runner/blind_score.py scoring/sheet.csv scoring/key.json --bench bench/prs_v2 [--out scoring/metrics.md] [--json m.json]

採点の値（大文字小文字は問わない. 日本語でもよい）:
  verdict      : correct（正しい問題を正しく説明）/ partial（場所や問題は合うが説明が不正確・あいまい）/ incorrect（問題ではない・誤り）
  hits_key     : yes（答えの不具合を指している）/ no
  severity_fit : ok / high（重すぎる）/ low（軽すぎる）

指標（構成ごと. 分母は実行の数で、指摘 0 件の実行も含む）:
  - 検出率（厳しめ）: バグの PR の実行のうち、hits_key=yes かつ verdict=correct の指摘がある割合
  - 検出率（ゆるめ）: 同じく verdict が correct か partial
  - 誤った MUST: 1 実行あたりの「MUST で verdict=incorrect」の件数。おとり・問題なしの PR で 1 件でも出した実行の割合も出す
  - 正しい指摘の割合: 見せた指摘のうち verdict=correct（partial は 0.5）
  - 重要度が適切: 答えに当たった指摘のうち、重要度が答えの severity_ok に入る割合（自動）と、採点者の severity_fit=ok の割合
  - 1 PR あたりの費用

信頼区間: PR を単位に（同じ PR の 3 回はまとめて）、種類ごとに復元抽出するブートストラップの 95% 区間。
構成の差（例: deep − standard）も、同じ抽出で対にして区間を出す（区間が 0 をまたがなければ差があると言える目安）.
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import sys
from itertools import combinations
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
CONFIG_ORDER = ["light", "standard", "deep", "auto"]

VERDICT = {
    "correct": "correct",
    "正しい": "correct",
    "○": "correct",
    "partial": "partial",
    "一部": "partial",
    "△": "partial",
    "incorrect": "incorrect",
    "誤り": "incorrect",
    "×": "incorrect",
}
YESNO = {"yes": True, "y": True, "はい": True, "○": True, "no": False, "n": False, "いいえ": False, "×": False, "": False}
FIT = {"ok": "ok", "適切": "ok", "high": "high", "重すぎ": "high", "高すぎ": "high", "low": "low", "軽すぎ": "low", "低すぎ": "low", "": ""}


class ScoreError(ValueError):
    pass


def read_sheet(path: Path) -> list[dict]:
    if path.suffix == ".xlsx":
        from openpyxl import load_workbook

        ws = load_workbook(path, read_only=True).active
        rows = list(ws.iter_rows(values_only=True))
        head = [str(h) for h in rows[0]]
        return [
            {h: ("" if v is None else str(v)) for h, v in zip(head, r, strict=False)} for r in rows[1:] if any(v is not None for v in r)
        ]
    with path.open(encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def parse_scores(rows: list[dict], key: dict) -> dict[str, dict]:
    """採点表 → {item: {verdict, hits_key, severity_fit}}. 抜け・読めない値・鍵に無い行は ScoreError."""
    out, errors = {}, []
    for r in rows:
        item = (r.get("item") or "").strip()
        if item not in key["items"]:
            errors.append(f"鍵に無い行: {item!r}")
            continue
        v = VERDICT.get((r.get("verdict") or "").strip().lower())
        h = YESNO.get((r.get("hits_key") or "").strip().lower())
        s = FIT.get((r.get("severity_fit") or "").strip().lower())
        if v is None:
            errors.append(f"{item}: verdict が未記入か読めません（{r.get('verdict')!r}）")
        if h is None:
            errors.append(f"{item}: hits_key が読めません（{r.get('hits_key')!r}）")
        if s is None:
            errors.append(f"{item}: severity_fit が読めません（{r.get('severity_fit')!r}）")
        if v == "incorrect" and h:
            errors.append(f"{item}: verdict=incorrect なのに hits_key=yes（答えに当たっているなら partial 以上）")
        out[item] = {"verdict": v, "hits_key": h, "severity_fit": s}
    missing = sorted(set(key["items"]) - set(out))
    if missing:
        errors.append(f"採点表に無い行（{len(missing)} 件）: {', '.join(missing[:10])}")
    if errors:
        raise ScoreError("\n".join(errors))
    return out


def per_run(key: dict, scores: dict[str, dict], answers: dict[str, dict]) -> list[dict]:
    """実行ごとの集計（指摘 0 件の実行も含む）."""
    runs = {(r["case"], r["config"], r["run"]): dict(r, findings=[]) for r in key["runs"]}
    for item, sources in key["items"].items():
        sc = scores[item]
        for s in sources:
            runs[(s["case"], s["config"], s["run"])]["findings"].append({"severity": s["severity"], **sc})
    out = []
    for (case, config, run), r in sorted(runs.items()):
        a = answers.get(case)
        if a is None:
            raise ScoreError(f"答えに無い PR: {case}")
        fs = r["findings"]
        hits = [f for f in fs if f["hits_key"] and f["verdict"] in ("correct", "partial")]
        out.append(
            {
                "case": case,
                "config": config,
                "run": run,
                "kind": a["kind"],
                "cost": float(r.get("cost_usd") or 0.0),
                "shown": len(fs),
                "correct": sum(1 for f in fs if f["verdict"] == "correct"),
                "partial": sum(1 for f in fs if f["verdict"] == "partial"),
                "detect_strict": int(any(f["verdict"] == "correct" for f in hits)) if a["kind"] == "bug" else 0,
                "detect_lenient": int(bool(hits)) if a["kind"] == "bug" else 0,
                "false_must": sum(1 for f in fs if f["severity"] == "MUST" and f["verdict"] == "incorrect"),
                "hit_items": len(hits),
                "hit_sev_ok": sum(1 for f in hits if f["severity"] in a.get("severity_ok", [])),
                "fit_scored": sum(1 for f in fs if f["severity_fit"]),
                "fit_ok": sum(1 for f in fs if f["severity_fit"] == "ok"),
            }
        )
    return out


SUM_FIELDS = (
    "n",
    "cost",
    "shown",
    "correct",
    "partial",
    "detect_strict",
    "detect_lenient",
    "false_must",
    "hit_items",
    "hit_sev_ok",
    "fit_scored",
    "fit_ok",
)


def aggregate(runs: list[dict]) -> dict[str, dict[str, dict[str, float]]]:
    """{構成: {PR: 合計}}. 抽出は PR 単位なので、PR ごとに先に足しておく."""
    agg: dict[str, dict[str, dict[str, float]]] = {}
    for r in runs:
        a = agg.setdefault(r["config"], {}).setdefault(r["case"], dict.fromkeys(SUM_FIELDS, 0.0) | {"kind": r["kind"]})
        a["n"] += 1
        for k in SUM_FIELDS[1:]:
            a[k] += r[k]
    return agg


def _ratio(a: float, b: float) -> float | None:
    return a / b if b else None


def metrics_for(per_case: dict[str, dict], cases: list[str]) -> dict[str, float | None]:
    """選んだ PR（重複あり）での指標."""
    tot = {k: 0.0 for k in SUM_FIELDS}
    bug = {k: 0.0 for k in SUM_FIELDS}
    neg = {k: 0.0 for k in SUM_FIELDS}  # おとり・問題なし
    decoy = {k: 0.0 for k in SUM_FIELDS}
    neg_runs_with_fm = 0.0
    for c in cases:
        a = per_case.get(c)
        if a is None:
            continue
        for k in SUM_FIELDS:
            tot[k] += a[k]
            if a["kind"] == "bug":
                bug[k] += a[k]
            else:
                neg[k] += a[k]
                if a["kind"] == "decoy":
                    decoy[k] += a[k]
        if a["kind"] != "bug":
            neg_runs_with_fm += a.get("runs_with_fm", 0.0)
    return {
        "detect_strict": _ratio(bug["detect_strict"], bug["n"]),
        "detect_lenient": _ratio(bug["detect_lenient"], bug["n"]),
        "false_must_per_pr": _ratio(tot["false_must"], tot["n"]),
        "false_must_per_pr_negative": _ratio(neg["false_must"], neg["n"]),
        "negative_runs_with_false_must": _ratio(neg_runs_with_fm, neg["n"]),
        "false_must_per_pr_decoy": _ratio(decoy["false_must"], decoy["n"]),
        "precision": _ratio(tot["correct"] + 0.5 * tot["partial"], tot["shown"]),
        "shown_per_pr": _ratio(tot["shown"], tot["n"]),
        "hit_severity_ok": _ratio(tot["hit_sev_ok"], tot["hit_items"]),
        "severity_fit_ok": _ratio(tot["fit_ok"], tot["fit_scored"]),
        "cost_per_pr": _ratio(tot["cost"], tot["n"]),
    }


METRICS = [
    ("detect_strict", "検出率（厳しめ）", "pct"),
    ("detect_lenient", "検出率（ゆるめ）", "pct"),
    ("false_must_per_pr", "誤った MUST / PR（全体）", "num"),
    ("negative_runs_with_false_must", "誤った MUST を出した割合（おとり・問題なし）", "pct"),
    ("false_must_per_pr_decoy", "誤った MUST / PR（おとり）", "num"),
    ("precision", "正しい指摘の割合", "pct"),
    ("shown_per_pr", "指摘 / PR", "num"),
    ("hit_severity_ok", "重要度が答えに合う（当たった指摘）", "pct"),
    ("severity_fit_ok", "重要度が適切（採点者）", "pct"),
    ("cost_per_pr", "費用 / PR（$）", "usd"),
]
DIFF_METRICS = ["detect_lenient", "detect_strict", "false_must_per_pr", "negative_runs_with_false_must", "precision", "cost_per_pr"]


def _with_fm_runs(runs: list[dict], agg: dict) -> None:
    """「誤った MUST を 1 件でも出した実行」の数も PR ごとに持っておく."""
    for r in runs:
        a = agg[r["config"]][r["case"]]
        a["runs_with_fm"] = a.get("runs_with_fm", 0.0) + (1 if r["false_must"] > 0 else 0)


def bootstrap(
    agg: dict[str, dict[str, dict]], kinds: dict[str, str], n_boot: int = 2000, seed: int = 1, alpha: float = 0.05
) -> tuple[dict, dict, dict]:
    """（点推定, 区間, 構成の差の区間）. PR を種類ごとに復元抽出する（層別のクラスター・ブートストラップ）."""
    configs = [c for c in CONFIG_ORDER if c in agg] + sorted(c for c in agg if c not in CONFIG_ORDER)
    all_cases = sorted(kinds)
    strata: dict[str, list[str]] = {}
    for c in all_cases:
        strata.setdefault(kinds[c], []).append(c)
    point = {conf: metrics_for(agg[conf], all_cases) for conf in configs}
    rng = random.Random(seed)
    samples: dict[str, dict[str, list[float]]] = {conf: {m: [] for m, _, _ in METRICS} for conf in configs}
    diffs: dict[tuple[str, str], dict[str, list[float]]] = {pair: {m: [] for m in DIFF_METRICS} for pair in combinations(configs, 2)}
    for _ in range(n_boot):
        pick = [c for members in strata.values() for c in rng.choices(members, k=len(members))]
        ms = {conf: metrics_for(agg[conf], pick) for conf in configs}
        for conf in configs:
            for m, _, _ in METRICS:
                if ms[conf][m] is not None:
                    samples[conf][m].append(ms[conf][m])
        for a, b in diffs:
            for m in DIFF_METRICS:
                if ms[a][m] is not None and ms[b][m] is not None:
                    diffs[(a, b)][m].append(ms[b][m] - ms[a][m])

    def ci(xs: list[float]) -> tuple[float, float] | None:
        if not xs:
            return None
        xs = sorted(xs)
        lo = xs[int((alpha / 2) * (len(xs) - 1))]
        hi = xs[int((1 - alpha / 2) * (len(xs) - 1) + 0.5)]
        return (lo, hi)

    intervals = {conf: {m: ci(samples[conf][m]) for m, _, _ in METRICS} for conf in configs}
    diff_ci = {
        f"{b} - {a}": {
            m: {"point": (point[b][m] - point[a][m]) if point[a][m] is not None and point[b][m] is not None else None, "ci": ci(v)}
            for m, v in d.items()
        }
        for (a, b), d in diffs.items()
    }
    return point, intervals, diff_ci


def _fmt(v: float | None, kind: str) -> str:
    if v is None:
        return "-"
    if kind == "pct":
        return f"{v:.0%}"
    if kind == "usd":
        return f"{v:.4f}"
    return f"{v:.2f}"


def report(point: dict, intervals: dict, diff_ci: dict, counts: dict) -> str:
    configs = list(point)
    md = [
        f"PR {counts['cases']} 件（bug {counts.get('bug', 0)}・clean {counts.get('clean', 0)}・decoy {counts.get('decoy', 0)}）、"
        f"実行 {counts['runs']} 件、採点した指摘 {counts['items']} 行。括弧は 95% 区間（PR 単位のブートストラップ）",
        "",
        "| 指標 | " + " | ".join(configs) + " |",
        "|---|" + "---|" * len(configs),
    ]
    for m, label, kind in METRICS:
        cells = []
        for conf in configs:
            ci = intervals[conf][m]
            rng = f"（{_fmt(ci[0], kind)}〜{_fmt(ci[1], kind)}）" if ci else ""
            cells.append(_fmt(point[conf][m], kind) + rng)
        md.append(f"| {label} | " + " | ".join(cells) + " |")
    if diff_ci:
        md += ["", "構成の差（右 − 左. 区間が 0 をまたがなければ差がある目安）:", "", "| 比べるもの | " + " | ".join(DIFF_METRICS) + " |"]
        md.append("|---|" + "---|" * len(DIFF_METRICS))
        kinds = dict((m, k) for m, _, k in METRICS)
        for name, d in diff_ci.items():
            cells = []
            for m in DIFF_METRICS:
                ci = d[m]["ci"]
                p = d[m]["point"]
                k = "num" if kinds[m] == "pct" else kinds[m]
                cells.append(f"{_fmt(p, k)}" + (f"（{_fmt(ci[0], k)}〜{_fmt(ci[1], k)}）" if ci else ""))
            md.append(f"| {name} | " + " | ".join(cells) + " |")
    return "\n".join(md) + "\n"


def score(sheet: Path, key_path: Path, answers_path: Path, n_boot: int = 2000, seed: int = 1) -> tuple[str, dict]:
    key = json.loads(key_path.read_text(encoding="utf-8"))
    answers = {a["id"]: a for a in yaml.safe_load(answers_path.read_text(encoding="utf-8"))["answers"]}
    scores = parse_scores(read_sheet(sheet), key)
    runs = per_run(key, scores, answers)
    agg = aggregate(runs)
    _with_fm_runs(runs, agg)
    kinds = {r["case"]: r["kind"] for r in runs}
    point, intervals, diff_ci = bootstrap(agg, kinds, n_boot=n_boot, seed=seed)
    counts = {"cases": len(kinds), "runs": len(runs), "items": len(scores)}
    for k in kinds.values():
        counts[k] = counts.get(k, 0) + 1
    text = report(point, intervals, diff_ci, counts)
    data = {"counts": counts, "point": point, "ci": intervals, "diff": diff_ci, "n_boot": n_boot, "seed": seed}
    return text, data


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("sheet", type=Path, help="採点を終えた表（.csv か .xlsx）")
    ap.add_argument("key", type=Path, help="blind_sheet.py が書いた鍵（JSON）")
    ap.add_argument("--bench", type=Path, default=ROOT / "bench/prs_v2", help="answers.yaml のフォルダ")
    ap.add_argument("--boot", type=int, default=2000, help="ブートストラップの回数")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--out", type=Path, help="結果の Markdown")
    ap.add_argument("--json", type=Path, help="結果の JSON")
    args = ap.parse_args(argv)
    try:
        text, data = score(args.sheet, args.key, args.bench / "answers.yaml", args.boot, args.seed)
    except ScoreError as e:
        print(f"採点表に問題があります:\n{e}", file=sys.stderr)
        return 2
    print(text)
    if args.out:
        args.out.write_text(text, encoding="utf-8")
    if args.json:
        args.json.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
