r"""CLI の呼び出しと、ファイルの置き場所の制限.

- 呼び出すのは決まった CLI だけ（シェルを通さない。引数はリストで渡す）
- 引数に渡すパスは、作業フォルダ（環境変数 DEVTOOLS_MCP_ROOT。**必須**. 未指定・ドライブの直下・ホームフォルダはエラー）の中だけ.
  `.git`・`.env`（`.env.*` を含む. 大文字小文字を区別しない）は扱わない
- CLI が書き出すファイルは作業フォルダの .mcp-out/ の下だけ
- 結果は CLI の JSON レポートをそのまま返す（終了コードの意味を添える）
- CLI の実行の時間の上限は DEVTOOLS_MCP_TIMEOUT（秒。既定 300）. 数でない・0 以下・無限大の値は既定に戻して警告する
  （起動に失敗させない）
- コマンドの上書き（DEVTOOLS_MCP_*）は JSON の配列（`["C:\\py\\python.exe", "-m", "x"]`）でも書ける.
  文字列で書いた場合、Windows では `\` を消さない分け方（posix=False）をする
"""

from __future__ import annotations

import json
import math
import os
import shlex
import shutil
import subprocess
import sys
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
DEFAULT_TIMEOUT = 300.0


def timeout_from_env(value: str | None) -> float:
    """DEVTOOLS_MCP_TIMEOUT の値（秒）. 空・数でない・0 以下・nan・無限大は既定（300 秒）にして標準エラーに警告する.

    import のときに int() で読むと、`5m` などの誤りでサーバーが起動しなくなる（エージェントからは理由が見えない）.
    """
    if value is None or not value.strip():
        return DEFAULT_TIMEOUT
    try:
        seconds = float(value)
    except ValueError:
        seconds = math.nan
    if not math.isfinite(seconds) or seconds <= 0:
        print(
            f"devtools-mcp: DEVTOOLS_MCP_TIMEOUT={value!r} は秒数（0 より大きい数）ではないので、既定の {DEFAULT_TIMEOUT:g} 秒を使います",
            file=sys.stderr,
        )
        return DEFAULT_TIMEOUT
    return seconds


TIMEOUT = timeout_from_env(os.environ.get("DEVTOOLS_MCP_TIMEOUT"))
STDERR_LIMIT = 4000


class ToolError(ValueError):
    """エージェントに返す、利用者が直せる誤り."""


@dataclass
class Workspace:
    root: Path

    @classmethod
    def from_env(cls) -> Workspace:
        raw = os.environ.get("DEVTOOLS_MCP_ROOT", "").strip()
        if not raw:
            raise ToolError("環境変数 DEVTOOLS_MCP_ROOT に作業フォルダを指定してください（未指定で PC 全体を扱わないため）")
        root = Path(raw).resolve()
        if root == Path(root.anchor) or root == Path.home().resolve():
            raise ToolError(f"作業フォルダにドライブの直下やホームフォルダは指定できません: {root}")
        return cls(root)

    def path(self, value: str, *, must_exist: bool = True) -> Path:
        """作業フォルダからの相対パス（または中の絶対パス）を検証して返す."""
        if not value:
            raise ToolError("パスが空です")
        p = Path(value)
        p = (p if p.is_absolute() else self.root / p).resolve()
        if p != self.root and self.root not in p.parents:
            raise ToolError(f"作業フォルダの外は扱えません: {value}（作業フォルダ: {self.root}）")
        rel_parts = p.relative_to(self.root).parts if p != self.root else ()
        # Windows は大文字小文字を区別しないので、`.ENV`・`.Git` でも抜けられないよう casefold で比べる
        folded = [part.casefold() for part in rel_parts]
        if any(part == ".git" or part == ".env" or part.startswith(".env.") for part in folded):
            raise ToolError(f"秘密情報を含みうる場所は扱えません: {value}")
        if must_exist and not p.exists():
            raise ToolError(f"見つかりません: {value}")
        return p

    def out_dir(self, tool: str) -> Path:
        """`.mcp-out/<tool>/<日時>`（同じ秒に作られていれば `-2`, `-3` …）を新しく作って返す.

        「無ければ作る」を 1 回の mkdir（exist_ok=False）で行うので、同じ秒に 2 つの呼び出しが来ても
        FileExistsError にならず、別のフォルダを使う（既存のフォルダに書き込まない）.
        """
        base = self.root / ".mcp-out" / tool / time.strftime("%Y%m%d-%H%M%S")
        base.parent.mkdir(parents=True, exist_ok=True)
        for n in range(1, 10_000):
            d = base if n == 1 else base.with_name(f"{base.name}-{n}")
            try:
                d.mkdir()
            except FileExistsError:
                continue
            return d
        raise ToolError(f"出力先のフォルダを作れません（同じ時刻のフォルダが多すぎます）: {self.rel(base)}")

    def rel(self, p: Path) -> str:
        try:
            return p.resolve().relative_to(self.root).as_posix()
        except ValueError:
            return str(p)


def split_command(value: str, *, windows: bool | None = None) -> list[str]:
    r"""上書き用の環境変数の値をコマンドの配列にする.

    - `[` で始まれば JSON の配列として読む（どの OS でもそのまま。パスの `\` は JSON の書き方で `\\`）
    - Windows では shlex.split(posix=False) で分け、`"` で囲んだ語の引用符だけを外す（`C:\Users\a\.venv\…` の `\` を消さない）
    - それ以外は shlex.split（POSIX）
    """
    v = value.strip()
    if v.startswith("["):
        try:
            data = json.loads(v)
        except json.JSONDecodeError as e:
            raise ToolError(f"コマンドの指定（JSON の配列）を読めません: {e.msg}") from None
        if not data or not all(isinstance(x, str) and x for x in data):
            raise ToolError("コマンドの指定（JSON の配列）は、空でない文字列の配列にしてください")
        return list(data)
    if windows is None:
        windows = os.name == "nt"
    if windows:
        parts = shlex.split(v, posix=False)
        return [x[1:-1] if len(x) >= 2 and x[0] == x[-1] == '"' else x for x in parts]
    return shlex.split(v)


def resolve_command(tool: str) -> list[str] | None:
    name, env = COMMANDS[tool]
    override = os.environ.get(env)
    if override and override.strip():
        return split_command(override)
    found = shutil.which(name)
    return [found] if found else None


def child_env() -> dict[str, str]:
    """CLI に渡す環境変数. 出力を UTF-8 で読むので、Python 製の CLI にも UTF-8 で書かせる.

    Windows ではパイプへの出力が既定で cp932 / cp1252 になり、日本語を書くと UnicodeEncodeError で落ちる（終了コード 1）ため.
    """
    return {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}


REPORT_LIMIT = 200_000  # エージェントに返すレポートの上限（文字）


def run(tool: str, args: list[str], ws: Workspace) -> dict[str, Any]:
    cmd = resolve_command(tool)
    if cmd is None:
        raise ToolError(f"{tool} が見つかりません。インストールするか、環境変数 {COMMANDS[tool][1]} でコマンドを指定してください")
    started = time.monotonic()
    try:
        proc = subprocess.run(
            [*cmd, *args],
            cwd=ws.root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=TIMEOUT,
            check=False,
            env=child_env(),
        )
    except subprocess.TimeoutExpired:
        raise ToolError(f"{tool} が {TIMEOUT:g} 秒以内に終わりませんでした") from None
    report: Any = None
    if len(proc.stdout) > REPORT_LIMIT:
        return {
            "tool": tool,
            "exit_code": proc.returncode,
            "meaning": EXIT_MEANING.get(proc.returncode, "不明な終了コード"),
            "report": None,
            "truncated": True,
            "message": f"レポートが大きすぎます（{len(proc.stdout):,} 文字）. 対象を絞るか、CLI で直接 -o に書き出してください",
            "stderr": proc.stderr[-STDERR_LIMIT:],
            "seconds": round(time.monotonic() - started, 2),
        }
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
