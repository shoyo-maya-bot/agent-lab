---
description: 差分を危なさで振り分けてレビューする（ふだんはこれ。Opus が要るときは最後に案内する）
agent: agent
# Copilot のモデル選択に表示される Sonnet の名前に合わせる
model: Claude Sonnet 5.5
tools: ['runCommands', 'search', 'changes']
---

# 振り分けつきレビュー（ふだん用）

1. ターミナルで次を実行し、振り分けの結果（tier と理由）を読む

   ```bash
   git diff origin/main...HEAD > .auto-review.diff
   auto-review plan .auto-review.diff
   ```

   `auto-review` が無ければ `pip install "auto-review @ git+https://github.com/shoyo-maya-bot/agent-lab.git@main#subdirectory=auto-review"`。

2. tier が `skip` なら「AI レビューは不要（理由）」とだけ答えて終わる
3. 差分（`.auto-review.diff`）と、変更されたファイルを読み、次の観点でレビューする
   正しさ（異常系・境界・同時実行・トランザクション）、エラー応答の一貫性、運用（終了コード・ログ・再実行）、セキュリティ、テストの抜け。
   表記・好みだけの指摘は出さない。差分が原因の問題を優先する
4. 結果を表で返す（最大 15 件、重要な順）

   | 重要度 | 箇所 | 指摘（どんな入力・状況で何が起きるか） | 根拠 |
   |---|---|---|---|

5. 最後に、次のどちらかに当てはまるときだけ「**/review-deep を実行してください**」と 1 行で案内する。当てはまらなければ何も書かない
   - 手順 1 の tier が `deep`
   - 手順 4 で MUST を 1 件以上出した（Opus に検証させる）
