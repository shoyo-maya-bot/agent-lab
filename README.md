# agent-lab

AI エージェントに **どこまで任せられるか** を実験するリポジトリです。3 つの軸で試し、結果を数字で残します。

| 軸 | 何を試すか |
|---|---|
| MCP | 自作ツール（doc2md・design-diff・fieldcheck・qa・spec-trace）や GitHub をエージェントの道具としてつなぐ |
| 自律度 | 提案だけ → PR まで → CI が通れば自動マージ、と任せる範囲を段階的に広げる（使い捨ての環境で） |
| 相互レビュー | 別のモデル・別の役割のエージェントがレビューし合うと、見落としとノイズがどう変わるか |

課題には dev-environment の sample-app と架空の設計書だけを使い、実案件のデータは使いません。

方針と実験計画は [docs/DESIGN.md](docs/DESIGN.md) を参照してください。

## 今あるもの

| フォルダ | 中身 |
|---|---|
| [mcp-server/](mcp-server/) | 自作ツール（doc2md・design-diff・fieldcheck・spec-trace）の MCP サーバー（段階 1） |
| [bench/bugs/](bench/bugs/) | sample-app の既知の弱いところ 8 件（レビューの正解）。対象は直す前の `exercise-base` ブランチ |
| [runner/score.py](runner/score.py) | レビュー結果を正解と突き合わせ、検出率・ノイズ率を出す |
| [reviewers/](reviewers/) | 実験で使うレビューのプロンプト |
| [auto-review/](auto-review/) | **PR の自動レビュー**。危なさで振り分け、ふだんは Sonnet 1 回・必要なときだけ Opus が検証（[考え方と費用](auto-review/README.md)） |
| [bench/prs/](bench/prs/) | PR 単位の課題 16 件（弱いところを 1 つ戻した PR 8 件 + 問題の無い PR 3 件 + 危なそうに見えるが正しい「おとり」5 件） |
| [runner/pr_bench.py](runner/pr_bench.py) | PR の課題を構成ごと（Haiku・Sonnet・Opus・auto）に流して比べる |
| [runner/mcp-e2e/](runner/mcp-e2e/) | エージェントに MCP ツールだけで点検させる実地検証（`run.sh`） |
| [examples/](examples/) | VS Code・Claude Code への MCP の設定例、auto-review の GitHub Actions と Copilot のプロンプト |
| [results/](results/) | 実験の結果。[モデルの大きさによる違い](results/2026-10-04-model-compare/README.md)、[MCP の実地検証](results/2026-10-04-mcp-e2e/README.md)、[PR 単位のレビューと自動の振り分け](results/2026-10-04-auto-review/README.md)、[3 回ずつ流したばらつきと誤った MUST の却下](results/2026-10-04-auto-review-repeat/README.md) |
