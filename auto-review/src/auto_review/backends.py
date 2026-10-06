"""モデルの呼び出し先.

- anthropic : Messages API を直接呼ぶ（ANTHROPIC_API_KEY。ANTHROPIC_BASE_URL で社内の中継・閉域の窓口に向けられる）
- claude-cli: Claude Code の `claude -p` を道具なしで呼ぶ（ローカルで試すとき）
- ReplayBackend: 記録しておいた生の返答を返すだけ（モデルは呼ばない. CI で配線の退行を見るため）
- 外部に送るのは `--yes` を付けたときだけ。付けなければ見積もりだけ（cli.py）
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Reply:
    text: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    seconds: float


class BackendError(RuntimeError):
    pass


def price(model: dict, input_tokens: int, output_tokens: int) -> float:
    return (input_tokens * model["input_per_mtok"] + output_tokens * model["output_per_mtok"]) / 1_000_000


class AnthropicBackend:
    name = "anthropic"

    def __init__(self, api_key: str | None = None, base_url: str | None = None, timeout: int = 600):
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        self.base_url = (base_url or os.environ.get("ANTHROPIC_BASE_URL") or "https://api.anthropic.com").rstrip("/")
        self.timeout = timeout
        if not self.api_key:
            raise BackendError("ANTHROPIC_API_KEY が設定されていません")

    def call(self, model: dict, system: str, user: str, max_tokens: int, role: str = "") -> Reply:
        body = json.dumps(
            {
                "model": model["id"],
                "max_tokens": max_tokens,
                "system": system,
                "messages": [{"role": "user", "content": user}],
            }
        ).encode()
        req = urllib.request.Request(
            f"{self.base_url}/v1/messages",
            data=body,
            headers={"x-api-key": self.api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
        )
        t0 = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as res:
                data = json.loads(res.read())
        except urllib.error.HTTPError as e:
            raise BackendError(f"API エラー {e.code}: {e.read()[:300]!r}") from e
        except urllib.error.URLError as e:
            raise BackendError(f"API に接続できません: {e.reason}") from e
        if data.get("stop_reason") == "max_tokens":
            raise BackendError("出力が上限で切れました（max_tokens）. 結果は使えません")
        text = "".join(c.get("text", "") for c in data.get("content", []) if c.get("type") == "text")
        u = data.get("usage", {})
        inp = u.get("input_tokens", 0) + u.get("cache_read_input_tokens", 0) + u.get("cache_creation_input_tokens", 0)
        out = u.get("output_tokens", 0)
        return Reply(text, inp, out, price(model, inp, out), time.monotonic() - t0)


class ClaudeCliBackend:
    name = "claude-cli"

    def __init__(self, exe: str = "claude", timeout: int = 900):
        self.exe = shutil.which(exe) or exe
        self.timeout = timeout

    def call(self, model: dict, system: str, user: str, max_tokens: int, role: str = "") -> Reply:
        cmd = [
            self.exe,
            "-p",
            "--model",
            model["id"],
            "--tools",
            "",
            "--strict-mcp-config",
            "--system-prompt",
            system,
            "--output-format",
            "json",
        ]
        t0 = time.monotonic()
        try:
            res = subprocess.run(cmd, input=user, capture_output=True, text=True, timeout=self.timeout, check=False)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise BackendError(f"claude -p を実行できません: {e}") from e
        try:
            data = json.loads(res.stdout)
        except json.JSONDecodeError as e:
            raise BackendError(f"claude -p の出力を読めません: {res.stdout[:200]} {res.stderr[:200]}") from e
        if data.get("is_error"):
            raise BackendError(f"claude -p がエラーを返しました: {str(data.get('result'))[:300]}")
        u = data.get("usage", {})
        inp = u.get("input_tokens", 0) + u.get("cache_read_input_tokens", 0) + u.get("cache_creation_input_tokens", 0)
        out = u.get("output_tokens", 0)
        cost = data.get("total_cost_usd")
        return Reply(data.get("result", ""), inp, out, float(cost) if cost is not None else price(model, inp, out), time.monotonic() - t0)


class ReplayBackend:
    """記録した生の返答（JSONL）を、（ケース ID, 役割, モデル ID）で引いて返す. 外部には何も送らない.

    1 行 1 件: {"case": "P01", "role": "standard", "model": "claude-sonnet-5-5", "text": "<モデルの生の返答>",
               "input_tokens": 1000, "output_tokens": 200, "expect_in_user": ["<findings>"]}
    - role は Reviewer の段階（standard / verify / deep / single）
    - expect_in_user（任意）: 渡されたメッセージに含まれているべき文字列. 無ければ失敗にする（プロンプトの配線の退行を見る）
    - `#` で始まる行と空行は読み飛ばす（ファイルの説明を書くため）
    - 記録が無い呼び出しは BackendError（黙って 0 件にしない）
    """

    name = "replay"

    def __init__(self, path: Path | str, case: str = ""):
        self.path = Path(path)
        self.case = case
        self.records: dict[tuple[str, str, str], dict] = {}
        for n, line in enumerate(self.path.read_text(encoding="utf-8").splitlines(), start=1):
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            try:
                r = json.loads(line)
                key = (str(r["case"]), str(r["role"]), str(r["model"]))
                str(r["text"])
            except (json.JSONDecodeError, KeyError, TypeError) as e:
                raise BackendError(f"{self.path}:{n} を読めません: {e}") from e
            if key in self.records:
                raise BackendError(f"{self.path}:{n} 同じ組み合わせの記録が 2 つあります: {key}")
            self.records[key] = r
        self.used: list[tuple[str, str, str]] = []
        self.errors: list[str] = []  # 呼び出し側が BackendError を握りつぶしても（deep の失敗など）、あとで確かめられるように残す

    def for_case(self, case: str) -> ReplayBackend:
        """同じ記録（と使った記録・失敗の一覧）を共有し、ケース ID だけ違うものを返す."""
        other = object.__new__(ReplayBackend)
        other.path, other.case, other.records, other.used, other.errors = self.path, case, self.records, self.used, self.errors
        return other

    def _fail(self, msg: str) -> BackendError:
        self.errors.append(msg)
        return BackendError(msg)

    def call(self, model: dict, system: str, user: str, max_tokens: int, role: str = "") -> Reply:
        key = (self.case, role, model["id"])
        r = self.records.get(key)
        if r is None:
            raise self._fail(f"記録がありません: case={key[0]} role={key[1]} model={key[2]}（{self.path}）")
        missing = [x for x in r.get("expect_in_user", []) if x not in user]
        if missing:
            raise self._fail(f"{key} のメッセージに、あるべき文字列がありません: {missing}")
        self.used.append(key)
        inp, out = int(r.get("input_tokens", 0)), int(r.get("output_tokens", 0))
        return Reply(r["text"], inp, out, price(model, inp, out), 0.0)


def get_backend(name: str):
    if name == "anthropic":
        return AnthropicBackend()
    if name == "claude-cli":
        return ClaudeCliBackend()
    raise BackendError(f"未対応の呼び出し先: {name}")
