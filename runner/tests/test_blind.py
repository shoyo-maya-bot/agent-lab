"""目隠しの採点（blind_sheet.py → 人が採点 → blind_score.py）を、合成のデータで確かめる（モデルは呼ばない）."""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "runner"))

import blind_score  # noqa: E402
import blind_sheet  # noqa: E402

STYLE = {"standard": "甲", "deep": "乙", "auto": "丙"}  # 構成ごとに説明の書き方が違う（名前は出さない）
MODELS = {"standard": ["claude-sonnet-5-5"], "deep": ["claude-opus-5-5"], "auto": ["claude-sonnet-5-5", "claude-opus-5-5"]}


def _bench(tmp: Path) -> Path:
    b = tmp / "bench"
    b.mkdir(parents=True)
    cases = [{"id": f"T0{i}", "title": f"題名 {i}", "description": "説明"} for i in range(1, 7)]
    answers = [
        {"id": "T01", "kind": "bug", "severity": "MUST", "severity_ok": ["MUST"], "summary": "答え 1"},
        {"id": "T02", "kind": "bug", "severity": "WANT", "severity_ok": ["WANT", "MUST"], "summary": "答え 2"},
        {"id": "T03", "kind": "bug", "severity": "MUST", "severity_ok": ["MUST"], "summary": "答え 3"},
        {"id": "T04", "kind": "clean"},
        {"id": "T05", "kind": "decoy", "why_correct": "正しい理由 5"},
        {"id": "T06", "kind": "decoy", "why_correct": "正しい理由 6"},
    ]
    (b / "cases.yaml").write_text(yaml.safe_dump({"base_commit": "x", "cases": cases}, allow_unicode=True), encoding="utf-8")
    (b / "answers.yaml").write_text(yaml.safe_dump({"answers": answers}, allow_unicode=True), encoding="utf-8")
    return b


def _finding(case: str, n: int, sev: str, config: str, run: int, tag: str) -> dict:
    # 中身に「本当は何か」（tag）を埋めておき、合成の採点者がそれを見て採点する（実際の採点者はこれを持たない）
    return {
        "severity": sev,
        "file": f"src/F{case}.java",
        "line": n,
        "title": f"{case} の指摘 {tag} {n}",
        "detail": f"説明 {STYLE[config]} {run} {n}",
        "evidence": "根拠",
        "models": MODELS[config],
        "status": "confirmed" if config == "auto" else "unverified",
        "verdict_note": "Opus が認めた" if config == "auto" else "",
    }


def _raw(tmp: Path) -> Path:
    """deep はバグ 3/3 を見つけ、おとりで誤った MUST 0。standard は 1/3 で、おとりに誤った MUST。auto はその間."""
    raw = tmp / "out" / "raw"
    raw.mkdir(parents=True)
    plan = {
        "deep": {
            "T01": [("MUST", "hit")],
            "T02": [("WANT", "hit")],
            "T03": [("MUST", "hit"), ("NITS", "ok")],
            "T04": [],
            "T05": [],
            "T06": [("WANT", "bad")],
        },
        "standard": {
            "T01": [("MUST", "hit")],
            "T02": [("WANT", "ok")],
            "T03": [],
            "T04": [("WANT", "bad")],
            "T05": [("MUST", "bad")],
            "T06": [("MUST", "bad")],
        },
        "auto": {"T01": [("MUST", "hit")], "T02": [("NITS", "partialhit")], "T03": [], "T04": [], "T05": [("MUST", "bad")], "T06": []},
    }
    cost = {"standard": 0.015, "deep": 0.04, "auto": 0.03}
    for config, per_case in plan.items():
        for case, fs in per_case.items():
            for run in (1, 2):
                rep = {
                    "case": case,
                    "config": config,
                    "run": run,
                    "cost_usd": cost[config],
                    "calls": [{"step": "single", "model": MODELS[config][0]}],
                    "plan": {"tier": "standard"},
                    "notes": [f"{config} のメモ"],
                    "findings": [_finding(case, i, sev, config, run, tag) for i, (sev, tag) in enumerate(fs, start=1)],
                }
                suffix = "" if run == 1 else f"-r{run}"
                (raw / f"{case}-{config}{suffix}.json").write_text(json.dumps(rep, ensure_ascii=False), encoding="utf-8")
    return raw.parent


def _make(tmp: Path, seed: int = 7, extra: list[str] | None = None) -> tuple[Path, Path, Path]:
    bench, out = _bench(tmp), _raw(tmp)
    sheet, key = tmp / "share" / "sheet.csv", tmp / "secret" / "key.json"
    assert (
        blind_sheet.main([str(out), "--bench", str(bench), "--sheet", str(sheet), "--key", str(key), "--seed", str(seed), *(extra or [])])
        == 0
    )
    return bench, sheet, key


def _rows(sheet: Path) -> list[dict]:
    with sheet.open(encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def _fill(sheet: Path) -> None:
    """合成の採点者: 題名の tag から採点する."""
    rows = _rows(sheet)
    for r in rows:
        tag = r["title"].split()[2]
        r["verdict"] = {"hit": "correct", "partialhit": "partial", "ok": "correct", "bad": "incorrect"}[tag]
        r["hits_key"] = "yes" if tag in ("hit", "partialhit") else "no"
        r["severity_fit"] = "high" if tag == "bad" else "ok"
    with sheet.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)


def test_sheet_hides_models_and_configs(tmp_path):
    _, sheet, key = _make(tmp_path)
    text = sheet.read_text(encoding="utf-8-sig").lower()
    for word in ("opus", "sonnet", "haiku", "claude", "standard", "deep", "auto", "confirmed", "unverified", "メモ", "認めた"):
        assert word not in text, word
    rows = _rows(sheet)
    assert list(rows[0]) == blind_sheet.SHEET_COLUMNS
    assert all(not r[c] for r in rows for c in blind_sheet.SCORE_COLUMNS)
    assert len({r["item"] for r in rows}) == len(rows)
    k = json.loads(key.read_text(encoding="utf-8"))
    assert set(k["items"]) == {r["item"] for r in rows}
    assert len(k["runs"]) == 3 * 6 * 2  # 指摘 0 件の実行も分母に残る
    assert sum(1 for r in k["runs"] if r["shown"] == 0) > 0


def test_sheet_order_is_shuffled_and_reproducible(tmp_path):
    _, sheet, _ = _make(tmp_path / "a", seed=7)
    _, sheet2, _ = _make(tmp_path / "b", seed=7)
    _, sheet3, _ = _make(tmp_path / "c", seed=8)
    rows = _rows(sheet)
    assert [r["title"] for r in rows] == [r["title"] for r in _rows(sheet2)]
    assert [r["title"] for r in rows] != [r["title"] for r in _rows(sheet3)]
    assert [r["pr"] for r in rows] != sorted(r["pr"] for r in rows)  # PR の順（≒ 構成ごとの塊）のままではない


def test_identical_findings_are_merged_but_kept_in_key(tmp_path):
    bench, out = _bench(tmp_path), _raw(tmp_path)
    reports = blind_sheet.load_reports([out])
    # 別の構成の同じ PR に、まったく同じ指摘を足す
    src = next(r for r in reports if r["case"] == "T01" and r["config"] == "deep" and r["run"] == 1)
    dst = next(r for r in reports if r["case"] == "T01" and r["config"] == "standard" and r["run"] == 1)
    dst["findings"].append(dict(src["findings"][0]))
    cases = {c["id"]: c for c in yaml.safe_load((bench / "cases.yaml").read_text(encoding="utf-8"))["cases"]}
    rows, key = blind_sheet.build(reports, cases, None, seed=1)
    merged = [v for v in key["items"].values() if {s["config"] for s in v} == {"deep", "standard"}]
    assert merged and len(rows) == len(key["items"])
    rows2, key2 = blind_sheet.build(reports, cases, None, seed=1, dedupe=False)
    assert len(rows2) == sum(len(r["findings"]) for r in reports) > len(rows)


def test_csv_cells_cannot_become_formulas(tmp_path):
    rows = [dict.fromkeys(blind_sheet.SHEET_COLUMNS, "") | {"item": "F1", "title": '=HYPERLINK("x")', "detail": "+1", "evidence": "@a"}]
    p = tmp_path / "s.csv"
    blind_sheet.write_csv(rows, p)
    r = _rows(p)[0]
    assert r["title"].startswith("'=") and r["detail"] == "'+1" and r["evidence"] == "'@a"


def test_no_answers_option_blanks_answer_columns(tmp_path):
    _, sheet, _ = _make(tmp_path, extra=["--no-answers"])
    assert all(r["pr_kind"] == "" and r["answer"] == "" for r in _rows(sheet))
    _, sheet2, _ = _make(tmp_path / "with")
    assert {r["pr_kind"] for r in _rows(sheet2)} <= {"bug", "clean", "decoy"}


def test_score_metrics_and_intervals(tmp_path):
    bench, sheet, key = _make(tmp_path)
    _fill(sheet)
    text, data = blind_score.score(sheet, key, bench / "answers.yaml", n_boot=300, seed=3)
    p = data["point"]
    assert p["deep"]["detect_strict"] == pytest.approx(1.0)
    assert p["standard"]["detect_strict"] == pytest.approx(1 / 3)
    assert p["auto"]["detect_strict"] == pytest.approx(1 / 3)  # T02 は partial なので厳しめでは数えない
    assert p["auto"]["detect_lenient"] == pytest.approx(2 / 3)
    # 誤った MUST: standard は T05・T06 で 1 件ずつ × 2 回 = 4 件 / 12 実行、deep は 0（T06 の誤りは WANT）
    assert p["standard"]["false_must_per_pr"] == pytest.approx(4 / 12)
    assert p["deep"]["false_must_per_pr"] == 0
    assert p["standard"]["negative_runs_with_false_must"] == pytest.approx(4 / 6)
    assert p["auto"]["negative_runs_with_false_must"] == pytest.approx(2 / 6)
    assert p["deep"]["cost_per_pr"] == pytest.approx(0.04)
    assert p["auto"]["hit_severity_ok"] == pytest.approx(0.5)  # T01 の MUST は合い、T02 の NITS は答え（WANT・MUST）に合わない
    assert p["deep"]["hit_severity_ok"] == pytest.approx(1.0)
    for conf, ms in data["ci"].items():
        for m, ci in ms.items():
            v = p[conf][m]
            if ci is not None and v is not None:
                assert ci[0] - 1e-9 <= v <= ci[1] + 1e-9, (conf, m, ci, v)
    d = data["diff"]["auto - deep"]["detect_lenient"]
    assert d["point"] == pytest.approx(-1 / 3)
    assert "検出率（厳しめ）" in text and "auto - deep" in text


def test_score_rejects_incomplete_or_inconsistent_sheet(tmp_path):
    bench, sheet, key = _make(tmp_path)
    with pytest.raises(blind_score.ScoreError, match="verdict"):
        blind_score.score(sheet, key, bench / "answers.yaml", n_boot=10)
    _fill(sheet)
    rows = _rows(sheet)
    rows[0]["verdict"], rows[0]["hits_key"] = "incorrect", "yes"
    with sheet.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows[:-1])
    with pytest.raises(blind_score.ScoreError) as e:
        blind_score.score(sheet, key, bench / "answers.yaml", n_boot=10)
    assert "hits_key=yes" in str(e.value) and "採点表に無い行" in str(e.value)
    assert blind_score.main([str(sheet), str(key), "--bench", str(bench), "--boot", "10"]) == 2


def test_japanese_score_values_are_accepted(tmp_path):
    bench, sheet, key = _make(tmp_path)
    _fill(sheet)
    rows = _rows(sheet)
    to_ja = {"correct": "○", "partial": "△", "incorrect": "×"}
    for r in rows:
        r["verdict"] = to_ja[r["verdict"]]
        r["hits_key"] = "はい" if r["hits_key"] == "yes" else "いいえ"
        r["severity_fit"] = "適切" if r["severity_fit"] == "ok" else "重すぎ"
    with sheet.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    _, data = blind_score.score(sheet, key, bench / "answers.yaml", n_boot=50)
    assert data["point"]["deep"]["detect_strict"] == pytest.approx(1.0)


def test_xlsx_round_trip(tmp_path):
    pytest.importorskip("openpyxl")
    bench, sheet, key = _make(tmp_path)
    _fill(sheet)
    xlsx = tmp_path / "s.xlsx"
    blind_sheet.write_xlsx(_rows(sheet), xlsx)
    _, a = blind_score.score(sheet, key, bench / "answers.yaml", n_boot=50)
    _, b = blind_score.score(xlsx, key, bench / "answers.yaml", n_boot=50)
    assert a["point"] == b["point"]


def test_key_next_to_sheet_warns(tmp_path, capsys):
    bench, out = _bench(tmp_path), _raw(tmp_path)
    blind_sheet.main(
        [str(out), "--bench", str(bench), "--sheet", str(tmp_path / "s.csv"), "--key", str(tmp_path / "k.json"), "--seed", "1"]
    )
    assert "別の場所" in capsys.readouterr().err
