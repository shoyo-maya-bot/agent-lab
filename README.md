# agent-lab

AI エージェントに **どこまで任せられるか** を実験するリポジトリです。3 つの軸で試し、結果を数字で残します。

| 軸 | 何を試すか |
|---|---|
| MCP | 自作ツール（doc2md・design-diff・fieldcheck・qa・spec-trace）や GitHub をエージェントの道具としてつなぐ |
| 自律度 | 提案だけ → PR まで → CI が通れば自動マージ、と任せる範囲を段階的に広げる（使い捨ての環境で） |
| 相互レビュー | 別のモデル・別の役割のエージェントがレビューし合うと、見落としとノイズがどう変わるか |

課題には dev-environment の sample-app と架空の設計書だけを使い、実案件のデータは使いません。

方針と実験計画は [docs/DESIGN.md](docs/DESIGN.md) を参照してください。
PR レビューの実験のやり直し（答えの漏れを防いだ課題・人による目隠しの採点・費用の見積もり）は [docs/EXPERIMENT_V2.md](docs/EXPERIMENT_V2.md) です（準備だけで、まだ流していません）。

## 今あるもの

| フォルダ | 中身 |
|---|---|
| [mcp-server/](mcp-server/) | 自作ツール（doc2md・design-diff・fieldcheck・spec-trace）の MCP サーバー（段階 1） |
| [bench/bugs/](bench/bugs/) | sample-app の既知の弱いところ 8 件（レビューの正解）。対象は直す前の `exercise-base` ブランチ |
| [runner/score.py](runner/score.py) | レビュー結果を正解と突き合わせ、検出率・ノイズ率を出す |
| [reviewers/](reviewers/) | 実験で使うレビューのプロンプト |
| [auto-review/](auto-review/) | **PR の自動レビュー**。危なさで振り分け、ふだんは Sonnet 1 回・必要なときだけ Opus が検証（[考え方と費用](auto-review/README.md)） |
| [bench/prs/](bench/prs/) | PR 単位の課題 16 件（弱いところを 1 つ戻した PR 8 件 + 問題の無い PR 3 件 + 危なそうに見えるが正しい「おとり」5 件） |
| [bench/replay/](bench/replay/) | CI 用の **手書きの合成の返答**（実際のモデルの出力ではない）。モデルを呼ばずに auto-review の配線の退行を見る |
| [bench/prs_v2/](bench/prs_v2/) | **PR の課題 v2**（33 件: 不具合 17・問題なし 8・おとり 8）。コメントとテストを消さず、関係のない変更を混ぜ、答えの漏れを検査して作る（[make_prs_v2.py](bench/prs_v2/make_prs_v2.py)・[leak_check.py](bench/prs_v2/leak_check.py)） |
| [runner/pr_bench.py](runner/pr_bench.py) | PR の課題を構成ごと（Haiku・Sonnet・Opus・auto）に流して比べる。`--bench bench/prs_v2` で v2、`--estimate-only` で費用の見積もりだけ（モデルは呼ばない） |
| [runner/blind_sheet.py](runner/blind_sheet.py)・[blind_score.py](runner/blind_score.py) | 目隠しの採点。モデル・構成を伏せて順番を混ぜた採点表と鍵を作り、人の採点を鍵と合わせて検出率・誤った MUST・費用を信頼区間付きで出す |
| [runner/mcp-e2e/](runner/mcp-e2e/) | エージェントに MCP ツールだけで点検させる実地検証（`run.sh`） |
| [examples/](examples/) | VS Code・Claude Code への MCP の設定例、auto-review の GitHub Actions と Copilot のプロンプト |
| [results/](results/) | 実験の結果。[モデルの大きさによる違い](results/2026-10-04-model-compare/README.md)、[MCP の実地検証](results/2026-10-04-mcp-e2e/README.md)、[PR 単位のレビューと自動の振り分け](results/2026-10-04-auto-review/README.md)、[3 回ずつ流したばらつきと誤った MUST の却下](results/2026-10-04-auto-review-repeat/README.md) |

## セキュリティ・ライセンス

- 脆弱性を見つけたら、公開の Issue ではなく [SECURITY.md](SECURITY.md) の方法で知らせてください
- ライセンスは [MIT](LICENSE) です
