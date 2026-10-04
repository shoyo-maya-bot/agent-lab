"""claude -p の stream-json の記録から、MCP ツールの呼び出しと最終報告をまとめる.

python runner/mcp-e2e/summarize.py run.jsonl
"""

from __future__ import annotations

import json
import sys


def _text(content) -> str:
    if isinstance(content, list):
        return "".join(x.get("text", "") for x in content if isinstance(x, dict))
    return str(content)


def _short(result: str) -> str:
    try:
        j = json.loads(result)
    except json.JSONDecodeError:
        return result[:150].replace("\n", " ")
    report = j.get("report") if isinstance(j.get("report"), dict) else {}
    return f"exit={j.get('exit_code')} error={str(j.get('error', ''))[:70]} findings={len(report.get('findings', []))}"


def main(path: str) -> None:
    calls: list[tuple[str, str, dict]] = []
    results: dict[str, str] = {}
    final: dict = {}
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            msg = d.get("message") or {}
            content = msg.get("content") if isinstance(msg.get("content"), list) else []
            if d.get("type") == "assistant":
                calls += [(c["id"], c["name"], c["input"]) for c in content if c.get("type") == "tool_use"]
            elif d.get("type") == "user":
                for c in content:
                    if isinstance(c, dict) and c.get("type") == "tool_result":
                        results[c["tool_use_id"]] = _text(c.get("content"))
            elif d.get("type") == "result":
                final = d
    for i, (cid, name, args) in enumerate(calls, start=1):
        print(i, name.replace("mcp__devtools__", ""), json.dumps(args, ensure_ascii=False)[:110], "->", _short(results.get(cid, "")))
    print("turns", final.get("num_turns"), "cost_usd", final.get("total_cost_usd"), "seconds", round(final.get("duration_ms", 0) / 1000))
    print(final.get("result"))


if __name__ == "__main__":
    main(sys.argv[1])
