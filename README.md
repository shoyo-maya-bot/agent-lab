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
| [auto-review/](auto-review/) | **PR の自動レビュー**。危なさで振り分け、ふだんは Sonnet 1 回・必要なときだけ Opus が検証（[考え方と費用](auto-review/README.md)）。費用・精度の主張は未検証（v2 で確かめる） |
| [bench/prs/](bench/prs/) | PR 単位の課題 16 件（弱いところを 1 つ戻した PR 8 件 + 問題の無い PR 3 件 + 危なそうに見えるが正しい「おとり」5 件） |
| [bench/replay/](bench/replay/) | CI 用の **手書きの合成の返答**（実際のモデルの出力ではない）。モデルを呼ばずに auto-review の配線の退行を見る |
| [bench/prs_v2/](bench/prs_v2/) | **PR の課題 v2**（33 件: 不具合 17・問題なし 8・おとり 8）。コメントとテストを消さず、関係のない変更を混ぜ、答えの漏れを検査して作る（[make_prs_v2.py](bench/prs_v2/make_prs_v2.py)・[leak_check.py](bench/prs_v2/leak_check.py)） |
| [runner/pr_bench.py](runner/pr_bench.py) | PR の課題を構成ごと（Haiku・Sonnet・Opus・auto）に流して比べる。`--bench bench/prs_v2` で v2、`--estimate-only` で費用の見積もりだけ（モデルは呼ばない） |
| [runner/blind_sheet.py](runner/blind_sheet.py)・[blind_score.py](runner/blind_score.py) | 目隠しの採点。モデル・構成を伏せて順番を混ぜた採点表と鍵を作り、人の採点を鍵と合わせて検出率・誤った MUST・費用を信頼区間付きで出す |
| [runner/mcp-e2e/](runner/mcp-e2e/) | エージェントに MCP ツールだけで点検させる実地検証（`run.sh`） |
| [examples/](examples/) | VS Code・Claude Code への MCP の設定例、auto-review の GitHub Actions と Copilot のプロンプト |
| [results/](results/) | 実験の結果。[モデルの大きさによる違い](results/2026-10-04-model-compare/README.md)、[MCP の実地検証](results/2026-10-04-mcp-e2e/README.md)、[PR 単位のレビューと自動の振り分け](results/2026-10-04-auto-review/README.md)、[3 回ずつ流したばらつきと誤った MUST の却下](results/2026-10-04-auto-review-repeat/README.md) |

## 開発

```bash
pip install -c constraints.txt -e "mcp-server[dev]" -e "auto-review[dev]" pyyaml   # constraints.txt は CI（deps-lock）が作る. 無ければ -c を外す
pytest -q mcp-server/tests runner/tests auto-review/tests
```

CI は Linux（Python 3.12）と Windows（Python 3.12）で単体テストを流し、bandit（SAST）と pip-audit（依存の脆弱性）も確かめます。

## 変更点

- **auto-review: 予算をすべての呼び出しに効かせる**（2026-10-08）. これまで予算（`budget.per_pr_usd`）は deep 段にしか効かなかった.
  一次（standard）と `--only` も呼ぶ前に見積もり、超えるなら全文を減らし、それでも超えるなら呼ばない（`--yes` なら終了コード 3、JSON の `over_budget: true`）.
  見積もりは伏せ字（Masker）をかけた後の文字列で数える. 変更ファイルは読む前にサイズで上限を見る. 例の workflow は `${{ }}` を run: に埋め込まない（env 経由）
- **auto-review: モデルの出力をスキーマで検証する**（2026-10-07）. 壊れた JSON（末尾のカンマなど）・`findings` という語を含むだけの文章・形の違う JSON
  （題名や重要度の無い指摘を含む）は、これまで「指摘 0 件」になることがあったが、いまは「結果を読めません」で終了コード 3 になる.
  anthropic の呼び出し先は、道具（ツール）を強制する構造化出力で受け取る. 検証役（deep）の出力が読めないときは、standard の結果を「検証なし」として出し、注記を残す
- **auto-review: 公開 API**（`auto_review.findings`）: `load_output`・`FINDINGS_SCHEMA`・`VERDICTS_SCHEMA`・`findings_from_data`・`parse_verification`・
  `UnreadableOutput`・`norm_severity`（重要度の読み替えの表は `SEVERITY_ALIASES` の 1 か所）. 非公開の `_json_blocks` は無くした
- **auto-review: トークンの見積もり**（`auto_review.tokens.estimate_tokens`）: 日本語など ASCII 以外は 1 文字 1 トークンで数える（これまでの「3 文字で 1 トークン」は日本語で 2〜3 倍甘かった）. 予算の確認が厳しくなり、deep の範囲が絞られやすくなる
- **auto-review: 差分の読み取り**: 空白を含むパス・git の引用符付きのパス（日本語）を正しく読み、hunk の行数に沿って `--- `・`+++ ` で始まる変更の行を捨てない
- **mcp-server**: `DEVTOOLS_MCP_*` を JSON の配列でも書ける. Windows では文字列の `\` を消さない. `.ENV`・`.Git` など大文字小文字の違いも秘密情報の場所として扱う. CLI には `PYTHONUTF8=1`・`PYTHONIOENCODING=utf-8` を渡して UTF-8 で出力させる（Windows で日本語の出力が落ちていた）

## セキュリティ・ライセンス

- 脆弱性を見つけたら、公開の Issue ではなく [SECURITY.md](SECURITY.md) の方法で知らせてください
- ライセンスは [MIT](LICENSE) です
