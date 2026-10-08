"""壊れた router.yaml は設定の誤り（終了コード 2）、想定外のエラーは 3（MUST ありの 1 と取り違えない）."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from auto_review import cli
from auto_review.router import ConfigError, load_config

DIFF = """\
diff --git a/app/x/MemberService.java b/app/x/MemberService.java
--- a/app/x/MemberService.java
+++ b/app/x/MemberService.java
@@ -1 +1 @@
-a
+b
"""


def _base() -> dict:
    return json.loads(json.dumps(load_config()))


def _write(tmp_path: Path, text: str) -> tuple[Path, Path]:
    c = tmp_path / "router.yaml"
    c.write_text(text, encoding="utf-8")
    d = tmp_path / "pr.diff"
    d.write_text(DIFF, encoding="utf-8")
    return c, d


def _drop(path: str):
    def f(cfg: dict) -> None:
        *head, last = path.split(".")
        for k in head:
            cfg = cfg[k]
        del cfg[last]

    return f


def _set(path: str, value):
    def f(cfg: dict) -> None:
        *head, last = path.split(".")
        for k in head:
            cfg = cfg[k]
        cfg[last] = value

    return f


@pytest.mark.parametrize(
    "broken",
    [
        _drop("escalation"),
        _drop("budget.per_pr_usd"),
        _drop("models.standard"),
        _drop("budget.output_tokens.verify"),
        _set("budget.per_pr_usd", "1 ドル"),
        _set("budget.per_pr_usd", float("inf")),
        _set("budget.per_pr_total_usd", -1),
        _set("models.deep", {"input_per_mtok": 1}),
        _set("risk.patterns", [{"regex": "(", "weight": 1, "reason": "x"}]),
        _set("risk.paths", [{"weight": 1}]),
        _set("skip.authors", "dependabot[bot]"),
    ],
)
@pytest.mark.parametrize("cmd", ["plan", "run"])
def test_broken_config_is_exit_2(tmp_path: Path, capsys, broken, cmd):
    cfg = _base()
    broken(cfg)
    c, d = _write(tmp_path, yaml.safe_dump(cfg, allow_unicode=True))
    assert cli.main([cmd, str(d), "--config", str(c)]) == 2
    err = capsys.readouterr().err
    assert "設定の誤り" in err and "Traceback" not in err


@pytest.mark.parametrize("text", ["budget: [1, 2\n", "- just\n- a list\n", "", "models: {standard: {id: x}}\n"])
def test_yaml_error_or_wrong_shape_is_exit_2(tmp_path: Path, capsys, text):
    c, d = _write(tmp_path, text)
    assert cli.main(["run", str(d), "--config", str(c)]) == 2
    assert "設定の誤り" in capsys.readouterr().err


def test_load_config_raises_config_error():
    with pytest.raises(ConfigError):
        from auto_review.router import validate_config

        validate_config({"models": {}})


def test_only_with_unknown_model_is_exit_2(tmp_path: Path, capsys):
    cfg = _base()
    del cfg["models"]["light"]
    c, d = _write(tmp_path, yaml.safe_dump(cfg, allow_unicode=True))
    assert cli.main(["run", str(d), "--config", str(c), "--only", "light"]) == 2


def test_unexpected_error_is_exit_3_not_1(tmp_path: Path, monkeypatch, capsys):
    c, d = _write(tmp_path, yaml.safe_dump(_base(), allow_unicode=True))

    def boom(*a, **k):
        raise KeyError("bug")

    monkeypatch.setattr(cli, "plan", boom)
    assert cli.main(["run", str(d), "--config", str(c)]) == 3
    assert "想定外のエラー" in capsys.readouterr().err


def test_bundled_config_is_valid(tmp_path: Path):
    c, d = _write(tmp_path, yaml.safe_dump(_base(), allow_unicode=True))
    assert cli.main(["plan", str(d), "--config", str(c)]) == 0
