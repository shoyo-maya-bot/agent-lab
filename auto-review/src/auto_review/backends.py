"""モデルの呼び出し先.

- anthropic : devtools-common の `devtools_common.llm.LLMClient` で Messages API を呼ぶ
  （ANTHROPIC_API_KEY。ANTHROPIC_BASE_URL で社内の中継・閉域の窓口に向けられる）。
  429 / 5xx（529 を含む）は `retry-after` に従って再試行し、出力が max_tokens で切れたら OutputTruncated
- claude-cli: Claude Code の `claude -p` を道具なしで呼ぶ（ローカルで試すとき）
- ReplayBackend: 記録しておいた生の返答を返すだけ（モデルは呼ばない. CI で配線の退行を見るため）
- 外部に送るのは `--yes` を付けたときだけ。付けなければ見積もりだけ（cli.py）
- 外部に送る前に、差分・プロンプトの中のメールアドレスと電話番号を `devtools_common.llm.Masker` で伏せる
  （anthropic・claude-cli の両方。環境変数 AUTO_REVIEW_MASK_PII=0 で止められる）
- 費用は共通の料金表（`devtools_common.llm.price_for`）で計算する。表に無いモデルだけ router.yaml の単価を使う
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from devtools_common.llm import LLMClient, LLMError, Masker, MaxTokensError, price_for


@dataclass
class Reply:
    text: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    seconds: float


class BackendError(RuntimeError):
    pass


class OutputTruncated(BackendError):
    """出力が max_tokens で途中で切れた. 途中までの JSON は使わない（cli は「結果を読めません」で終了コード 3）."""


def price(model: dict, input_tokens: int, output_tokens: int) -> float:
    """費用の見積もり（USD）. モデル ID が共通の料金表にあればそれを、無ければ router.yaml の単価を使う."""
    p = price_for(str(model.get("id", "")))
    if p is not None:
        return (input_tokens * p.input + output_tokens * p.output) / 1_000_000
    if "input_per_mtok" not in model or "output_per_mtok" not in model:
        raise BackendError(f"モデル {model.get('id')!r} の単価がわかりません（共通の料金表にも router.yaml にもありません）")
    return (input_tokens * model["input_per_mtok"] + output_tokens * model["output_per_mtok"]) / 1_000_000


def mask_enabled() -> bool:
    """外部に送る前に個人情報（メールアドレス・電話番号）を伏せるか. 既定は伏せる."""
    return os.environ.get("AUTO_REVIEW_MASK_PII", "1").strip().lower() not in ("0", "false", "no", "off")


def default_masker() -> Masker | None:
    return Masker() if mask_enabled() else None


class AnthropicBackend:
    name = "anthropic"

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: int = 600,
        max_retries: int = 3,
        masker: Masker | None = None,
        client_factory: Callable[[str], LLMClient] | None = None,
    ):
        """client_factory: モデル ID から LLMClient を作る関数（テストで FakeLLM を渡す）."""
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        self.base_url = base_url
        self.timeout = timeout
        self.max_retries = max_retries
        self.masker = masker if masker is not None else default_masker()
        self._factory = client_factory or self._make_client
        self._clients: dict[str, LLMClient] = {}
        if not (self.api_key or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
            raise BackendError("ANTHROPIC_API_KEY が設定されていません")

    def _make_client(self, model_id: str) -> LLMClient:
        return LLMClient(
            model_id,
            api_key=self.api_key or None,
            base_url=self.base_url,
            timeout=self.timeout,
            max_retries=self.max_retries,
            masker=self.masker,
        )

    def _client(self, model_id: str) -> LLMClient:
        if model_id not in self._clients:
            self._clients[model_id] = self._factory(model_id)
        return self._clients[model_id]

    def call(self, model: dict, system: str, user: str, max_tokens: int, role: str = "") -> Reply:
        t0 = time.monotonic()
        try:
            comp = self._client(model["id"]).complete(user, system=system, max_tokens=max_tokens)
        except MaxTokensError as e:
            raise OutputTruncated(f"出力が上限で切れました（max_tokens={max_tokens}）. 結果は使えません") from e
        except LLMError as e:
            raise BackendError(f"API エラー: {e}") from e
        u = comp.usage
        inp = u.input_tokens + u.cache_read_input_tokens + u.cache_creation_input_tokens
        out = u.output_tokens
        cost = comp.cost_usd if comp.cost_usd is not None else price(model, inp, out)
        return Reply(comp.text, inp, out, cost, time.monotonic() - t0)


class ClaudeCliBackend:
    name = "claude-cli"

    def __init__(self, exe: str = "claude", timeout: int = 900, masker: Masker | None = None):
        self.exe = shutil.which(exe) or exe
        self.timeout = timeout
        self.masker = masker if masker is not None else default_masker()

    def call(self, model: dict, system: str, user: str, max_tokens: int, role: str = "") -> Reply:
        if self.masker:
            system, user = self.masker.text(system), self.masker.text(user)
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
