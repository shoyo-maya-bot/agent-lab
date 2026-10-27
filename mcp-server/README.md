# devtools-mcp

devtools の CLI を、AI エージェント（VS Code の Copilot・Claude など）の **道具（MCP のツール）** として使えるようにするサーバーです。

| ツール | 中身 |
|---|---|
| `tools_status` | 使える CLI と作業フォルダ |
| `doc2md_convert` | Office / PDF / draw.io → Markdown（出力は `.mcp-out/doc2md/`）。`recursive: true` でサブフォルダも。隠しフォルダ（`.git`・前回までの出力の `.mcp-out`・`.venv` など名前が `.` で始まるもの）は探さない |
| `design_diff_excel` / `design_diff_spec` | 設計書の新旧差分 / API 定義書と OpenAPI の整合 |
| `fieldcheck_validate` | 表の項目間検証 |
| `spec_trace_check` | 要件と実装・テストの対応表の検査 |

安全のための決まり:
- 扱えるのは作業フォルダ（`DEVTOOLS_MCP_ROOT`。必須。ドライブの直下とホームフォルダは不可）の中のファイルだけ。`.git` と `.env`（`.ENV` など大文字小文字の違いも含む）は扱わない
- CLI が書き出すのは `.mcp-out/` の下だけ（`.mcp-out/<ツール>/<日時>/`。同じ秒の 2 回目以降は `-2`, `-3` … の新しいフォルダ）。元のファイルは書き換えない
- CLI の実行の時間の上限は `DEVTOOLS_MCP_TIMEOUT`（秒。既定 300）。数でない・0 以下の値は既定に戻し、標準エラーに警告する（サーバーは起動する）
- 外部への送信・書き込みを伴うツール（office-replace の実行、Backlog・Slack）は入れていない
- stdio で動く（ネットワークのポートを開けない）
- ツールの結果（変換した文書・設計書・レポートの中身）は **データ** として扱うよう、サーバーの説明（instructions）でエージェントに伝える。中に書かれた「指示」には従わせない

ツールの性質（MCP のツールの注釈）:

| ツール | 注釈 |
|---|---|
| `tools_status`・`design_diff_*`・`fieldcheck_validate`・`spec_trace_check` | 読み取りだけ（`readOnlyHint`）。外部に出ない（`openWorldHint: false`） |
| `doc2md_convert` | `.mcp-out/` の下に新しいフォルダを作って書く。上書き・削除はしない（`destructiveHint: false`） |

パスの誤り・見つからないファイル・CLI が無いなど、利用者（エージェント）が直せる誤りは、MCP のツールのエラー（`isError: true`）として理由の文章を返します。エージェントは理由を読んで、引数を直してやり直せます。

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

- Windows のパス（`\` を含む）は、JSON の配列で書くのが確実です（どの OS でも同じに読みます）。

```json
"env": {
  "DEVTOOLS_MCP_ROOT": "${workspaceFolder}",
  "DEVTOOLS_MCP_DOC2MD": "[\"C:\\\\tools\\\\.venv\\\\Scripts\\\\python.exe\", \"-m\", \"doc_to_markdown\"]"
}
```

- 文字列で書いた場合、Windows では `\` を消さずに空白で分けます（空白を含むパスは `"` で囲む）。Linux・macOS ではシェルと同じ分け方です。

## テスト

```bash
pip install -e ".[dev]" && pytest
```

偽の CLI で引数と安全の決まりを確かめ、MCP クライアントから stdio で呼ぶテストもあります。
隣に design-diff のリポジトリがあれば、本物のサンプルで差分を取るテストも走ります。

## メモ

MCP の Python SDK 2.x（`mcp>=2,<3`）を使います。1.x からの主な違い:
- `FastMCP`（`mcp.server.fastmcp`）は `MCPServer`（`mcp.server.mcpserver`）に名前が変わった
- 型の項目名が snake_case になった（`isError` → `is_error`、`readOnlyHint` → `read_only_hint` など。通信上の名前は変わらない）
- 同期の関数で書いたツールは、別のスレッドで動く（CLI の実行中も、ほかの要求を止めない）
- ツールで `ToolError` を投げると、`isError: true` の結果になる（エージェントが理由を読める）

3.x は API が変わりうるので、Dependabot では上げず、手作業で移行します。
