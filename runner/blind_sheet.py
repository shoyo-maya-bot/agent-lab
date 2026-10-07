#!/usr/bin/env python3
"""実行結果の指摘から、人が目隠しで採点するための表を作る（どのモデル・構成の指摘かを伏せる）.

    python runner/blind_sheet.py results/<日付>-v2 --bench bench/prs_v2 \\
        --sheet scoring/sheet.csv --key scoring/key.json [--xlsx scoring/sheet.xlsx] [--seed 12345]

入力: runner/pr_bench.py の結果（<out>/raw/*.json か、そのフォルダ、または 1 行 1 件の JSONL）. 複数を並べてよい
出力:
  - 採点表（CSV. --xlsx で Excel も）: 指摘を 1 行ずつ、順番を混ぜて並べる。モデル名・構成・何回目か・検証の状態は書かない
    同じ PR に同じ中身の指摘が複数の実行から出たときは 1 行にまとめる（どの構成から出たかは鍵にだけ残す）
  - 鍵（JSON）: 採点表の行 → 構成・実行・元の指摘. **採点が終わるまで採点する人に渡さない**
    指摘 0 件の実行も分母に入れるため、すべての実行（PR・構成・回・費用）も鍵に残す

採点の列（人が埋める）: verdict（correct / partial / incorrect）、hits_key（yes / no）、severity_fit（ok / high / low）、note
採点の基準は docs/EXPERIMENT_V2.md の「目隠しの採点」を参照。採点が終わったら runner/blind_score.py で集計する.
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import sys
from datetime import date
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
SCORE_COLUMNS = ["verdict", "hits_key", "severity_fit", "note"]
SHEET_COLUMNS = ["item", "pr", "pr_title", "pr_kind", "answer", "file", "line", "severity", "title", "detail", "evidence", *SCORE_COLUMNS]
# 採点表に出してはいけない項目（モデル・構成が分かる）. テストでも確かめる
HIDDEN_FIELDS = ("models", "status", "verdict_note", "config", "run", "id", "calls", "plan", "notes")


def load_reports(sources: list[Path]) -> list[dict]:
    """pr_bench の結果を読む. フォルダ（raw/ を含むものでもよい）・.json・.jsonl を受け付ける."""
    out: list[dict] = []
    for src in sources:
        if src.is_dir():
            raw = src / "raw" if (src / "raw").is_dir() else src
            files = sorted(raw.glob("*.json"))
            if not files:
                raise SystemExit(f"{src}: 結果の JSON がありません")
            out += [json.loads(p.read_text(encoding="utf-8")) for p in files]
        elif src.suffix == ".jsonl":
            out += [json.loads(ln) for ln in src.read_text(encoding="utf-8").splitlines() if ln.strip() and not ln.startswith("#")]
        else:
            out.append(json.loads(src.read_text(encoding="utf-8")))
    for r in out:
        for k in ("case", "config", "findings"):
            if k not in r:
                raise SystemExit(f"結果に {k} がありません（pr_bench の raw の形式ではない）")
        r.setdefault("run", 1)
    seen = set()
    for r in out:
        k = (r["case"], r["config"], r["run"])
        if k in seen:
            raise SystemExit(f"同じ実行が 2 回あります: {k}")
        seen.add(k)
    return out


def _cell(v) -> str:
    """表計算ソフトで式として実行されないようにする（CSV インジェクション対策）. 改行はそのまま残す."""
    s = "" if v is None else str(v)
    return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") else s


def _content(case: str, f: dict) -> tuple:
    return (case, f.get("file", ""), f.get("line"), f.get("severity", ""), f.get("title", ""), f.get("detail", ""), f.get("evidence", ""))


def build(
    reports: list[dict], cases: dict[str, dict], answers: dict[str, dict] | None, seed: int, dedupe: bool = True
) -> tuple[list[dict], dict]:
    """（採点表の行, 鍵）."""
    groups: dict[tuple, list[dict]] = {}
    order: list[tuple] = []
    runs = []
    for r in sorted(reports, key=lambda r: (r["case"], r["config"], r["run"])):
        runs.append(
            {
                "case": r["case"],
                "config": r["config"],
                "run": r["run"],
                "cost_usd": r.get("cost_usd", 0.0),
                "shown": len(r["findings"]),
                "models": sorted({c.get("model", "") for c in r.get("calls", [])}),
            }
        )
        for i, f in enumerate(r["findings"]):
            k = _content(r["case"], f) if dedupe else (r["case"], r["config"], r["run"], i)
            if k not in groups:
                groups[k] = []
                order.append(k)
            groups[k].append(
                {
                    "case": r["case"],
                    "config": r["config"],
                    "run": r["run"],
                    "index": i,
                    "severity": f.get("severity", ""),
                    "status": f.get("status", ""),
                    "models": f.get("models", []),
                    "content": f,
                }
            )
    rng = random.Random(seed)
    rng.shuffle(order)
    ids = rng.sample(range(10000, 100000), len(order))  # 連番にしない（並び順から何も分からないように）
    rows, items = [], {}
    for item_no, k in zip(ids, order, strict=True):
        src = groups[k]
        f = src[0]["content"]
        case = src[0]["case"]
        item = f"F{item_no}"
        ans = (answers or {}).get(case, {})
        rows.append(
            {
                "item": item,
                "pr": case,
                "pr_title": cases.get(case, {}).get("title", ""),
                "pr_kind": ans.get("kind", "") if answers else "",
                "answer": (ans.get("summary") or ans.get("why_correct") or "") if answers else "",
                "file": f.get("file", ""),
                "line": f.get("line") if f.get("line") is not None else "",
                "severity": f.get("severity", ""),
                "title": f.get("title", ""),
                "detail": f.get("detail", ""),
                "evidence": f.get("evidence", ""),
                **dict.fromkeys(SCORE_COLUMNS, ""),
            }
        )
        items[item] = [{k2: v for k2, v in s.items() if k2 != "content"} for s in src]
    key = {
        "created": date.today().isoformat(),
        "seed": seed,
        "dedupe": dedupe,
        "note": "採点が終わるまで採点する人に渡さない",
        "runs": runs,
        "items": items,
    }
    return rows, key


def write_csv(rows: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as fh:  # Excel で文字化けしないよう BOM 付き
        w = csv.DictWriter(fh, fieldnames=SHEET_COLUMNS)
        w.writeheader()
        for r in rows:
            w.writerow({k: _cell(r[k]) for k in SHEET_COLUMNS})


def write_xlsx(rows: list[dict], path: Path) -> None:
    try:
        from openpyxl import Workbook
        from openpyxl.worksheet.datavalidation import DataValidation
    except ImportError as e:  # pragma: no cover - 環境による
        raise SystemExit("--xlsx には openpyxl が要ります（pip install openpyxl）") from e
    wb = Workbook()
    ws = wb.active
    ws.title = "採点"
    ws.append(SHEET_COLUMNS)
    for r in rows:
        ws.append([_cell(r[k]) for k in SHEET_COLUMNS])
    n = len(rows) + 1
    for col, values in (("verdict", "correct,partial,incorrect"), ("hits_key", "yes,no"), ("severity_fit", "ok,high,low")):
        letter = chr(ord("A") + SHEET_COLUMNS.index(col))
        dv = DataValidation(type="list", formula1=f'"{values}"', allow_blank=True)
        ws.add_data_validation(dv)
        dv.add(f"{letter}2:{letter}{n}")
    ws.freeze_panes = "B2"
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("sources", type=Path, nargs="+", help="pr_bench の結果（フォルダ・.json・.jsonl）")
    ap.add_argument("--bench", type=Path, default=ROOT / "bench/prs_v2", help="cases.yaml（題名）と answers.yaml（答え）のフォルダ")
    ap.add_argument("--sheet", type=Path, required=True, help="採点表（CSV）")
    ap.add_argument("--key", type=Path, required=True, help="鍵（JSON）. 採点表とは別の場所に置く")
    ap.add_argument("--xlsx", type=Path, help="採点表を Excel でも書く（openpyxl が要る）")
    ap.add_argument("--seed", type=int, help="並べ替えの乱数の種（省略すると毎回変わる. 鍵に残る）")
    ap.add_argument(
        "--no-answers", action="store_true", help="答え（PR の種類と説明）の列を空にする（答えを見ずに 1 回目の採点をするとき）"
    )
    ap.add_argument("--no-dedupe", action="store_true", help="同じ中身の指摘もまとめずに 1 行ずつ出す")
    args = ap.parse_args(argv)
    if args.key.resolve().parent == args.sheet.resolve().parent:
        print("注意: 鍵を採点表と同じフォルダに置いています。採点する人に渡さないよう、別の場所に移してください", file=sys.stderr)
    reports = load_reports(args.sources)
    cases = {c["id"]: c for c in yaml.safe_load((args.bench / "cases.yaml").read_text(encoding="utf-8"))["cases"]}
    answers = None
    if not args.no_answers:
        answers = {a["id"]: a for a in yaml.safe_load((args.bench / "answers.yaml").read_text(encoding="utf-8"))["answers"]}
    unknown = sorted({r["case"] for r in reports} - set(cases))
    if unknown:
        raise SystemExit(f"課題に無い PR の結果があります: {unknown}（--bench を確かめる）")
    seed = args.seed if args.seed is not None else random.SystemRandom().randrange(1, 2**31)
    rows, key = build(reports, cases, answers, seed, dedupe=not args.no_dedupe)
    key["sources"] = [str(s) for s in args.sources]
    write_csv(rows, args.sheet)
    if args.xlsx:
        write_xlsx(rows, args.xlsx)
    args.key.parent.mkdir(parents=True, exist_ok=True)
    args.key.write_text(json.dumps(key, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"採点表 {len(rows)} 行（実行 {len(key['runs'])} 件の指摘から）: {args.sheet}　鍵: {args.key}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
