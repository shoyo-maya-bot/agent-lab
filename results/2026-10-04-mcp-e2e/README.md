# 2026-10-04 MCP の実地検証（エージェントが自作ツールを道具として使えるか）

## 条件

| 項目 | 内容 |
|---|---|
| エージェント | Claude Code（ヘッドレス実行 `claude -p`）。使えるツールは devtools の MCP ツールだけ（ファイルの直接読み・シェルは不可） |
| MCP サーバー | [mcp-server/](../../mcp-server/)（stdio） |
| 作業フォルダ | design-diff・office-review（fieldcheck）・ai-coding（template と対応表）・doc-to-markdown のサンプルを集めたもの |
| 仕込み | 対応表で「テスト済み」になっている 1 つのテスト（wrongHeaderIsRejected）からアサーションを消した |
| 指示 | [runner/mcp-e2e/task.md](../../runner/mcp-e2e/task.md)（6 つの手順と、MUST / WANT / NITS / Q での報告） |
| 再現 | `runner/mcp-e2e/run.sh <作業用の空フォルダ>` |

## 結果

| 項目 | 値 |
|---|---|
| ツールの呼び出し | 6 回（6 つの手順に 1 回ずつ）。引数の誤りによるやり直しは 0 回 |
| 往復の回数 | 8 |
| 時間 | 13 秒 |
| 費用 | 約 $0.20（Claude Code の表示） |
| 仕込みの検出 | **検出した**。「wrongHeaderIsRejected に assert が無く、要件の裏付けになっていない」を MUST で報告 |

呼び出しの記録:

| # | ツール | 結果 |
|---|---|---|
| 1 | tools_status | 4 つの CLI が使える |
| 2 | design_diff_excel（api） | 終了コード 0、差分 9 件 |
| 3 | design_diff_spec | 終了コード 0、不一致 5 件（と注記 1 件） |
| 4 | fieldcheck_validate | 終了コード 1、error 7・warning 1 |
| 5 | spec_trace_check（proven 必須） | 終了コード 1、error 1（仕込んだもの） |
| 6 | doc2md_convert | 終了コード 0、変換ロスの warning なし |

## 分かったこと

- **自作ツールは、MCP で包むだけでエージェントの道具として十分に使える**。JSON の結果と終了コードの意味（2 = 前提の誤り）をそろえておいたことが効いて、エージェントは結果を読み違えず、やり直しも要らなかった
- 各ツールの結果を横断して、「改訂後の Excel では name の桁が 60 なのに OpenAPI は 50 のまま」のように、**2 つのツールの結果を組み合わせた指摘** を自分で出した
- 1 回目に 1 つ不正確な点があった: OpenAPI の整合チェックの結果に「includeHistory・createdAt の不一致」は出ていない（= 一致している）のに、「確認できていない」と書いた
- そこで MCP サーバーの説明に「findings に出てこない項目は問題が無かったという意味」と 1 文足して 2 回目を流したところ、この不正確さは消えた（ほかの結果は同じ。費用は約 $0.10、13 秒）。**ツールの説明文はエージェントの解釈を直接変える**ので、プロンプトと同じく育てる対象になる
- 費用は 1 回 $0.2 程度。人が 6 つの CLI を順に実行して結果を読むより、明らかに速い

## 次にやること

- 同じ作業を MCP なし（シェルで CLI を実行）でさせて、往復の回数・費用・誤りを比べる
- VS Code の Copilot（エージェントモード）でも同じ作業をさせる（設定例: [examples/vscode-mcp.json](../../examples/vscode-mcp.json)）
