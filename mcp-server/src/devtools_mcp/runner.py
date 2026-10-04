"""CLI の呼び出しと、ファイルの置き場所の制限.

- 呼び出すのは決まった CLI だけ（シェルを通さない。引数はリストで渡す）
- 引数に渡すパスは、作業フォルダ（DEVTOOLS_MCP_ROOT。既定はカレントディレクトリ）の中だけ
- CLI が書き出すファイルは作業フォルダの .mcp-out/ の下だけ
- 結果は CLI の JSON レポートをそのまま返す（終了コードの意味を添える）
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# ツール名 → (既定のコマンド名, 上書き用の環境変数)
COMMANDS: dict[str, tuple[str, str]] = {
    "doc2md": ("doc2md", "DEVTOOLS_MCP_DOC2MD"),
    "design-diff": ("design-diff", "DEVTOOLS_MCP_DESIGN_DIFF"),
    "fieldcheck": ("fieldcheck", "DEVTOOLS_MCP_FIELDCHECK"),
    "spec-trace": ("spec-trace", "DEVTOOLS_MCP_SPEC_TRACE"),
}
EXIT_MEANING = {
    0: "問題なし",
    1: "指摘あり（データ・成果物の問題）",
    2: "前提の誤り（引数・設定・入力の形式）。データではなく前提を確認する",
    3: "実行時エラー",
}
TIMEOUT = int(os.environ.get("DEVTOOLS_MCP_TIMEOUT", "300"))
STDERR_LIMIT = 4000


class ToolError(ValueError):
    """エージェントに返す、利用者が直せる誤り."""


@dataclass
class Workspace:
    root: Path

    @classmethod
    def from_env(cls) -> Workspace:
        return cls(Path(os.environ.get("DEVTOOLS_MCP_ROOT") or os.getcwd()).resolve())

    def path(self, value: str, *, must_exist: bool = True) -> Path:
        """作業フォルダからの相対パス（または中の絶対パス）を検証して返す."""
        if not value:
            raise ToolError("パスが空です")
        p = Path(value)
        p = (p if p.is_absolute() else self.root / p).resolve()
        if p != self.root and self.root not in p.parents:
            raise ToolError(f"作業フォルダの外は扱えません: {value}（作業フォルダ: {self.root}）")
        if must_exist and not p.exists():
            raise ToolError(f"見つかりません: {value}")
        return p

    def out_dir(self, tool: str) -> Path:
        d = self.root / ".mcp-out" / tool / time.strftime("%Y%m%d-%H%M%S")
        n = 1
        base = d
        while d.exists():
            n += 1
            d = base.with_name(f"{base.name}-{n}")
        d.mkdir(parents=True)
        return d

    def rel(self, p: Path) -> str:
        try:
            return p.resolve().relative_to(self.root).as_posix()
        except ValueError:
            return str(p)


def resolve_command(tool: str) -> list[str] | None:
    name, env = COMMANDS[tool]
    override = os.environ.get(env)
    if override:
        return shlex.split(override)
    found = shutil.which(name)
    return [found] if found else None


def run(tool: str, args: list[str], ws: Workspace) -> dict[str, Any]:
    cmd = resolve_command(tool)
    if cmd is None:
        raise ToolError(f"{tool} が見つかりません。インストールするか、環境変数 {COMMANDS[tool][1]} でコマンドを指定してください")
    started = time.monotonic()
    try:
        proc = subprocess.run([*cmd, *args], cwd=ws.root, capture_output=True, text=True, encoding="utf-8", timeout=TIMEOUT, check=False)
    except subprocess.TimeoutExpired:
        raise ToolError(f"{tool} が {TIMEOUT} 秒以内に終わりませんでした") from None
    report: Any = None
    if proc.stdout.strip():
        try:
            report = json.loads(proc.stdout)
        except json.JSONDecodeError:
            report = {"text": proc.stdout[-STDERR_LIMIT:]}
    return {
        "tool": tool,
        "exit_code": proc.returncode,
        "meaning": EXIT_MEANING.get(proc.returncode, "不明な終了コード"),
        "report": report,
        "stderr": proc.stderr[-STDERR_LIMIT:],
        "seconds": round(time.monotonic() - started, 2),
    }
