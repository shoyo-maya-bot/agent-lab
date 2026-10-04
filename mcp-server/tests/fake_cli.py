"""テスト用の偽 CLI: 受け取った引数を JSON で返す. FAKE_EXIT で終了コードを変える. -d があればそこに Markdown を 1 つ作る."""

import json
import os
import sys
from pathlib import Path

args = sys.argv[1:]
if "-d" in args:
    out = Path(args[args.index("-d") + 1])
    out.mkdir(parents=True, exist_ok=True)
    (out / "doc.md").write_text("# doc\n", encoding="utf-8")
print(json.dumps({"args": args, "summary": {"error": 0}}, ensure_ascii=False))
print("log line", file=sys.stderr)
sys.exit(int(os.environ.get("FAKE_EXIT", "0")))
