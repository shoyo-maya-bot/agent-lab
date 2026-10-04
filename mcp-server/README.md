# devtools-mcp

devtools の CLI を、AI エージェント（VS Code の Copilot・Claude など）の **道具（MCP のツール）** として使えるようにするサーバーです。

| ツール | 中身 |
|---|---|
| `tools_status` | 使える CLI と作業フォルダ |
| `doc2md_convert` | Office / PDF / draw.io → Markdown（出力は `.mcp-out/doc2md/`） |
| `design_diff_excel` / `design_diff_spec` | 設計書の新旧差分 / API 定義書と OpenAPI の整合 |
| `fieldcheck_validate` | 表の項目間検証 |
| `spec_trace_check` | 要件と実装・テストの対応表の検査 |

安全のための決まり:
- 扱えるのは作業フォルダ（`DEVTOOLS_MCP_ROOT`、既定はカレントディレクトリ）の中のファイルだけ
- CLI が書き出すのは `.mcp-out/` の下だけ。元のファイルは書き換えない
- 外部への送信・書き込みを伴うツール（office-replace の実行、Backlog・Slack）は入れていない
- stdio で動く（ネットワークのポートを開けない）

## 使う

```bash
pip install -e .            # devtools-mcp コマンドが入る。doc2md などの CLI は各リポジトリの setup で入れておく
```

VS Code（`.vscode/mcp.json`）の例:

```json
{
  "servers": {
    "devtools": {
      "type": "stdio",
      "command": "devtools-mcp",
      "env": { "DEVTOOLS_MCP_ROOT": "${workspaceFolder}" }
    }
  }
}
```

CLI が PATH に無いときは `DEVTOOLS_MCP_DOC2MD="python -m doc_to_markdown"` のように、環境変数でコマンドを指定します。

## テスト

```bash
pip install -e ".[dev]" && pytest
```

偽の CLI で引数と安全の決まりを確かめ、MCP クライアントから stdio で呼ぶテストもあります。
隣に design-diff のリポジトリがあれば、本物のサンプルで差分を取るテストも走ります。

## メモ

MCP の Python SDK は 2.x で API が変わりました（`FastMCP` → `MCPServer` など）。今は 1.x に固定しています（`mcp>=1.10,<2`）。2.x への移行は別の作業として行います。
