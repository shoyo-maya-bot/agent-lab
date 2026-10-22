"""auto-review の README と router.yaml・例の workflow・コードの値のずれを機械で見つける."""

from __future__ import annotations

import re
from pathlib import Path

import yaml
from auto_review.diff import TEST_DIRS, TEST_JAVA_SUFFIXES
from auto_review.router import glob_match, load_config

ROOT = Path(__file__).resolve().parents[2]
README = (ROOT / "auto-review" / "README.md").read_text(encoding="utf-8")
WORKFLOW = ROOT / "examples" / "auto-review.workflow.yml"
CFG = load_config()


def test_budget_defaults_in_readme_match_router_yaml():
    b = CFG["budget"]
    assert f"`budget.per_pr_usd`（既定 ${b['per_pr_usd']:g}）" in README
    assert f"`budget.per_pr_total_usd`（既定 ${b['per_pr_total_usd']:g}）" in README
    assert f"`chars_per_token`（既定 {b['chars_per_token']:g}）" in README


def test_deep_score_in_flowchart_matches_router_yaml():
    s = CFG["escalation"]["deep_score"]
    assert f"点数 < {s}" in README and f"点数 >= {s}" in README


def _path_weight(path: str) -> int:
    return next(r["weight"] for r in CFG["risk"]["paths"] if glob_match(path, r["glob"]))


def _pattern_weight(text: str) -> int:
    return next(r["weight"] for r in CFG["risk"]["patterns"] if re.search(r["regex"], text))


def _size_points(lines: int) -> int:
    return max([p for t, p in CFG["risk"]["size"]["lines"] if lines >= t], default=0)


def test_score_examples_in_readme_match_router_yaml():
    line = next(ln for ln in README.splitlines() if "点数の例:" in ln)
    examples = {
        "SQL の Mapper": _path_weight("src/main/resources/x/MemberMapper.xml"),
        "Service": _path_weight("src/main/java/x/MemberService.java"),
        "例外ハンドラ": _path_weight("src/main/java/x/GlobalExceptionHandler.java"),
        "`@Transactional` の変更": _pattern_weight("@Transactional"),
        "テストを消した・減らした": CFG["risk"]["deleted_tests_weight"],
        "変更が 400 行以上": _size_points(400),
    }
    for label, weight in examples.items():
        assert f"{label} +{weight}" in line, label


def test_test_file_rules_in_readme_match_code():
    for d in sorted(TEST_DIRS):
        assert f"`{d}`" in README
    for suffix in TEST_JAVA_SUFFIXES:
        assert f"`*{suffix}`" in README
    flagged = [r["reason"] for r in CFG["risk"]["patterns"] if r.get("in_tests")]
    assert flagged and all(reason in README for reason in flagged)


def test_example_workflow_does_not_cancel_running_reviews():
    # README は「例の workflow は実行中の実行を取り消さない」と書いている（取り消すと課金の後の費用が累計から漏れる）
    wf = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert wf["concurrency"]["cancel-in-progress"] is False
    assert "`cancel-in-progress: false`" in README


def test_masking_env_vars_are_documented():
    src = (ROOT / "auto-review" / "src" / "auto_review" / "backends.py").read_text(encoding="utf-8")
    for name in set(re.findall(r'"(AUTO_REVIEW_[A-Z_]+)"', src)):
        assert name in README, name
