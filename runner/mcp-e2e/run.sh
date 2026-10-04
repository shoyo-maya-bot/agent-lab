#!/usr/bin/env bash
# MCP の実地検証: エージェント（Claude Code のヘッドレス実行）に devtools の MCP ツールだけを使わせて、
# 設計書の差分・整合・データ検証・対応表の検査・文書の変換を行わせる。
#
#   runner/mcp-e2e/run.sh <作業用の空フォルダ>
#
# 前提: 兄弟フォルダに design-diff / office-review / ai-coding / doc-to-markdown / devtools-common があり、
#       各 CLI が `python -m <module>` で動くこと（各リポジトリの setup 済み）。claude コマンドがログイン済みであること。
# 仕込み: 対応表の 1 つのテスト（wrongHeaderIsRejected）からアサーションを消し、エージェントが気づくかを見る。
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
REPOS="$(cd "$HERE/../../.." && pwd)"
OUT="$(mkdir -p "$1" && cd "$1" && pwd)"
PY="${PYTHON:-python3}"
WS="$OUT/ws"
rm -rf "$WS" && mkdir -p "$WS/fieldcheck"
cp -r "$REPOS/design-diff/samples" "$WS/design"
cp -r "$REPOS/design-diff/config" "$WS/config"
cp -r "$REPOS/office-review/fieldcheck/samples/." "$WS/fieldcheck/"
cp -r "$REPOS/ai-coding/template" "$WS/template"
cp -r "$REPOS/ai-coding/samples" "$WS/samples"
cp -r "$REPOS/ai-coding/traceability" "$WS/traceability"
cp "$REPOS/doc-to-markdown/samples/expense-claim.docx" "$WS/design/"
"$PY" - "$WS" <<'PY'
import re, sys
from pathlib import Path
p = Path(sys.argv[1]) / "template/src/test/java/com/example/app/order/OrderImportServiceTest.java"
s = p.read_text(encoding="utf-8")
i = s.index("wrongHeaderIsRejected"); j = s.index("\n    }\n", i)
p.write_text(s[:i] + re.sub(r"\n\s*assert\w*\(.*?;", "\n        // (assertion removed for the experiment)", s[i:j], flags=re.S) + s[j:], encoding="utf-8")
PY
PP="$REPOS/agent-lab/mcp-server/src:$REPOS/design-diff/src:$REPOS/office-review/src:$REPOS/ai-coding/tools/spec-trace/src:$REPOS/doc-to-markdown/src:$REPOS/devtools-common/src${PYTHONPATH:+:$PYTHONPATH}"
cat > "$OUT/mcp.json" <<JSON
{"mcpServers": {"devtools": {"type": "stdio", "command": "$PY", "args": ["-m", "devtools_mcp.server"],
  "env": {"PYTHONPATH": "$PP", "DEVTOOLS_MCP_ROOT": "$WS",
          "DEVTOOLS_MCP_DOC2MD": "$PY -m doc_to_markdown", "DEVTOOLS_MCP_DESIGN_DIFF": "$PY -m design_diff",
          "DEVTOOLS_MCP_FIELDCHECK": "$PY -m field_validation", "DEVTOOLS_MCP_SPEC_TRACE": "$PY -m spec_trace"}}}}
JSON
cd "$WS"
claude -p "$(cat "$HERE/task.md")" --mcp-config "$OUT/mcp.json" --strict-mcp-config \
  --allowedTools "mcp__devtools__*" --max-turns 30 --output-format stream-json --verbose < /dev/null > "$OUT/run.jsonl"
"$PY" "$HERE/summarize.py" "$OUT/run.jsonl"
