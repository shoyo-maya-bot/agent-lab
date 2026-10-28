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


def test_pr_case_count_in_readmes_matches_cases_yaml():
    """README の「課題は N PR」が bench/prs/cases.yaml の件数と同じ（後退: 16 件あるのに 11 PR と書いていた）."""
    cases = yaml.safe_load((ROOT / "bench" / "prs" / "cases.yaml").read_text(encoding="utf-8"))["cases"]
    ids = [c["id"] for c in cases]
    n = len(ids)
    kinds = {k: sum(1 for i in ids if i.startswith(k)) for k in ("P", "N", "D")}
    assert f"課題は {n} PR" in README
    assert f"弱いところを 1 つ戻した PR {kinds['P']}・問題の無い PR {kinds['N']}・おとり {kinds['D']}" in README
    top = (ROOT / "README.md").read_text(encoding="utf-8")
    assert f"PR 単位の課題 {n} 件（弱いところを 1 つ戻した PR {kinds['P']} 件 + 問題の無い PR {kinds['N']} 件" in top
    assert f"「おとり」{kinds['D']} 件" in top


def test_agents_md_install_uses_constraints_like_ci():
    """AGENTS.md のテストの手順は CI と同じく constraints.txt で版をそろえる（後退: -c constraints.txt が無かった）."""
    agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    ci_pkgs = re.search(r'pip install \$C (-e "mcp-server\[dev\]" -e "auto-review\[dev\]" pyyaml)', ci).group(1)
    assert f"pip install -c constraints.txt {ci_pkgs}" in agents
    for line in agents.splitlines():
        if line.startswith("pip install"):
            assert "-c constraints.txt" in line, line


def test_label_and_retry_notes_match_code():
    import inspect

    from auto_review.backends import AnthropicBackend

    default_retries = inspect.signature(AnthropicBackend.__init__).parameters["max_retries"].default
    assert f"最大 {default_retries} 回（`max_retries`）" in README
    assert f"`{CFG['labels']['deep']}`（必ず Opus まで。文書だけの変更でも省かない）" in README
