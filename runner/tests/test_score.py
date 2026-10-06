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


# ---- 記録した返答での PR の課題（モデルは呼ばない. 配線の退行を見る）

REPLAY = ROOT / "bench/replay/auto-synthetic.jsonl"
REPLAY_CASES = "P01,P05,P06,P08,N01,N02,N03,D01"  # CI（.github/workflows/ci.yml）と同じ
THRESHOLDS = ROOT / "bench/replay/thresholds.yaml"


def _pr_bench():
    sys.path.insert(0, str(ROOT / "runner"))
    import pr_bench

    return pr_bench


def test_replay_bench_meets_thresholds(tmp_path):
    pb = _pr_bench()
    args = [
        "--diff-only",
        str(tmp_path / "out"),
        "--configs",
        "auto",
        "--cases",
        REPLAY_CASES,
        "--workers",
        "1",
        "--replay",
        str(REPLAY),
        "--check",
        str(THRESHOLDS),
    ]
    assert pb.main(args) == 0
    m = pb.metrics(tmp_path / "out")["auto"]
    assert (m["detected"], m["must_noise"], m["false_alarm"], m["decoy_must"]) == (4, 0, 0, 0)


def test_replay_bench_fails_when_a_recording_is_missing(tmp_path):
    """検証役の記録が無いと（deep の失敗は本体では握りつぶされる）、確認で失敗になる."""
    pb = _pr_bench()
    lines = [x for x in REPLAY.read_text(encoding="utf-8").splitlines() if '"role": "verify"' not in x or '"D01"' not in x]
    rec = tmp_path / "rec.jsonl"
    rec.write_text("\n".join(lines) + "\n", encoding="utf-8")
    args = ["--diff-only", str(tmp_path / "out"), "--configs", "auto", "--cases", "D01", "--replay", str(rec), "--check", str(THRESHOLDS)]
    assert pb.main(args) == 1


def test_replay_fixture_is_labeled_synthetic():
    head = REPLAY.read_text(encoding="utf-8").splitlines()[0]
    assert head.startswith("#") and "合成" in head and "実際のモデルの出力ではありません" in head
