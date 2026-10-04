作業フォルダの設計書・データ・実装を点検してください。devtools の MCP ツールだけを使い、ファイルを直接読んだりシェルを使ったりしないでください。

1. 使えるツールと作業フォルダを確かめる
2. design/before/API0001_会員照会.xlsx と design/after/API0001_会員照会.xlsx の差分を取る（設定の種類は api）
3. design/spec/API定義書.xlsx と design/spec/openapi.yaml の整合を確かめる
4. fieldcheck/expense.csv を fieldcheck/rules.yaml で検証する
5. traceability/specs の対応表を、proven を必須にして検査する（root は作業フォルダ）
6. design/expense-claim.docx を Markdown に変換する

最後に、手順ごとの結果を 1 行ずつ、そのあと重要な問題を MUST / WANT / NITS / Q を付けて最大 8 行で、日本語で報告してください。
ツールがエラー（exit_code 2 や error）を返したら、引数を見直して 1 回だけやり直してください。
