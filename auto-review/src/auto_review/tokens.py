"""入力トークン数の見積もり（モデルを呼ぶ前の予算の確認に使う）.

英数字・記号（ASCII）は 3〜4 文字で 1 トークン程度だが、日本語は 1〜1.5 文字で 1 トークン程度になる.
全体を「3 文字で 1 トークン」で数えると、日本語の多い入力では 2〜3 倍少なく見積もり、予算の確認が甘くなる.
そこで ASCII は chars_per_token 文字で 1 トークン、ASCII 以外（日本語など）は 1 文字 1 トークンで数える（多めに見る側）.
"""

from __future__ import annotations

DEFAULT_CHARS_PER_TOKEN = 3.0


def estimate_tokens(text: str, chars_per_token: float = DEFAULT_CHARS_PER_TOKEN) -> int:
    """text の入力トークン数の見積もり. ASCII は chars_per_token 文字で 1、ASCII 以外は 1 文字で 1."""
    if chars_per_token <= 0:
        raise ValueError(f"chars_per_token は正の数にしてください: {chars_per_token}")
    non_ascii = sum(1 for ch in text if ord(ch) > 0x7F)
    ascii_chars = len(text) - non_ascii
    return int(ascii_chars / chars_per_token) + non_ascii + 1


__all__ = ["DEFAULT_CHARS_PER_TOKEN", "estimate_tokens"]
