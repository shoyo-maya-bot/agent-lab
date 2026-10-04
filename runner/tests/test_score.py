from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("score", ROOT / "runner/score.py")
score = importlib.util.module_from_spec(spec)
sys.modules["score"] = score  # dataclass がモジュールを参照するため
spec.loader.exec_module(score)

import yaml  # noqa: E402

BUGS = yaml.safe_load((ROOT / "bench/bugs/sample-app-known.yaml").read_text(encoding="utf-8"))["bugs"]


def test_answer_file_is_well_formed():
    ids = [b["id"] for b in BUGS]
    assert len(ids) == len(set(ids)) == 8
    for b in BUGS:
        assert b["severity"] in score.SEVERITIES and b["files"] and b["keywords"] and b["why"]


def test_example_review():
    r = score.score(BUGS, score.load_findings(ROOT / "bench/samples/review-example.md"))
    assert r["detected"] == ["B01", "B02", "B04"]
    assert r["detection_rate"] == 0.375
    assert r["noise_rate"] == 0.25  # 4 件中 1 件（変数名）
    assert r["severity_match"] == round(2 / 3, 3)  # B02 は正解 MUST に対して WANT


def test_needs_both_file_and_keyword():
    f = score.Finding("MUST", "", "同時に登録されると壊れる")  # ファイルが無い
    assert not score.matches(BUGS[0], f)


def test_bullets_and_json(tmp_path: Path):
    md = tmp_path / "r.md"
    md.write_text("- [MUST] OrderImportJob.java: ジョブ ID を誤っても終了コード 0 で何もしない\n- [Q] 何か\n", encoding="utf-8")
    assert score.score(BUGS, score.load_findings(md))["detected"] == ["B05"]
    js = tmp_path / "r.json"
    js.write_text(
        json.dumps(
            {"findings": [{"severity": "NITS", "location": "OrderImportJob.java:57", "message": "log.error にメッセージ ID が無い"}]}
        ),
        encoding="utf-8",
    )
    assert score.score(BUGS, score.load_findings(js))["detected"] == ["B08"]
