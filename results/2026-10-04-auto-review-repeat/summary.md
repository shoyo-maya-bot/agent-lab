| 構成 | 検出（P01〜P08） | 1 PR あたりの指摘 | 正解以外の MUST（P） | 誤警報 MUST・WANT（N01〜N03） | Opus の呼び出し | 費用の合計 | 1 PR あたり | 1 PR あたりの時間 |
|---|---|---|---|---|---|---|---|---|
| standard | 24 / 24 | 2.6 | 4 | 3 | 0 | $0.85 | $0.018 | 10 秒 |
| deep | 24 / 24 | 3.1 | 4 | 0 | 48 | $2.13 | $0.044 | 18 秒 |
| auto | 24 / 24 | 2.8 | 3 | 2 | 26 | $2.32 | $0.048 | 17 秒 |

おとりの PR（D01〜。正しい変更なので MUST は誤り）:

| 構成 | 実行数 | MUST を見せた PR | 見せた MUST | うち検証済み | うち要確認 | うち未検証 | 検証で却下した MUST | 1 PR あたり |
|---|---|---|---|---|---|---|---|---|
| standard | 15 | 3 | 4 | 0 | 0 | 4 | 0 | $0.016 |
| deep | 15 | 0 | 0 | 0 | 0 | 0 | 0 | $0.044 |
| auto | 15 | 2 | 2 | 0 | 2 | 0 | 2 | $0.028 |

| PR | 構成 | 検出 | 重要度 | 指摘数 |
|---|---|---|---|---|
| N01 | auto | - | - | 2 |
| N01 | auto | - | - | 2 |
| N01 | auto | - | - | 3 |
| N01 | deep | - | - | 0 |
| N01 | deep | - | - | 1 |
| N01 | deep | - | - | 1 |
| N01 | standard | - | - | 2 |
| N01 | standard | - | - | 2 |
| N01 | standard | - | - | 3 |
| N02 | auto | - | - | 0 |
| N02 | auto | - | - | 0 |
| N02 | auto | - | - | 0 |
| N02 | deep | - | - | 1 |
| N02 | deep | - | - | 1 |
| N02 | deep | - | - | 2 |
| N02 | standard | - | - | 0 |
| N02 | standard | - | - | 0 |
| N02 | standard | - | - | 1 |
| N03 | auto | - | - | 0 |
| N03 | auto | - | - | 0 |
| N03 | auto | - | - | 0 |
| N03 | deep | - | - | 0 |
| N03 | deep | - | - | 0 |
| N03 | deep | - | - | 0 |
| N03 | standard | - | - | 0 |
| N03 | standard | - | - | 0 |
| N03 | standard | - | - | 0 |
| P01 | auto | ○ | MUST | 3 |
| P01 | auto | ○ | MUST | 3 |
| P01 | auto | ○ | MUST | 4 |
| P01 | deep | ○ | MUST | 3 |
| P01 | deep | ○ | MUST | 3 |
| P01 | deep | ○ | MUST | 4 |
| P01 | standard | ○ | MUST | 3 |
| P01 | standard | ○ | MUST | 3 |
| P01 | standard | ○ | MUST | 4 |
| P02 | auto | ○ | MUST | 4 |
| P02 | auto | ○ | MUST | 5 |
| P02 | auto | ○ | MUST | 6 |
| P02 | deep | ○ | MUST | 6 |
| P02 | deep | ○ | MUST | 6 |
| P02 | deep | ○ | MUST | 6 |
| P02 | standard | ○ | MUST | 4 |
| P02 | standard | ○ | MUST | 4 |
| P02 | standard | ○ | MUST | 5 |
| P03 | auto | ○ | MUST | 4 |
| P03 | auto | ○ | MUST | 4 |
| P03 | auto | ○ | MUST | 4 |
| P03 | deep | ○ | MUST | 4 |
| P03 | deep | ○ | MUST | 4 |
| P03 | deep | ○ | MUST | 4 |
| P03 | standard | ○ | MUST | 4 |
| P03 | standard | ○ | MUST | 4 |
| P03 | standard | ○ | MUST | 4 |
| P04 | auto | ○ | MUST | 3 |
| P04 | auto | ○ | MUST | 3 |
| P04 | auto | ○ | MUST | 3 |
| P04 | deep | ○ | MUST | 2 |
| P04 | deep | ○ | MUST | 2 |
| P04 | deep | ○ | MUST | 3 |
| P04 | standard | ○ | MUST | 3 |
| P04 | standard | ○ | MUST | 3 |
| P04 | standard | ○ | MUST | 3 |
| P05 | auto | ○ | MUST | 3 |
| P05 | auto | ○ | MUST | 4 |
| P05 | auto | ○ | MUST | 4 |
| P05 | deep | ○ | MUST | 3 |
| P05 | deep | ○ | MUST | 3 |
| P05 | deep | ○ | MUST | 4 |
| P05 | standard | ○ | MUST | 3 |
| P05 | standard | ○ | MUST | 3 |
| P05 | standard | ○ | MUST | 3 |
| P06 | auto | ○ | MUST | 6 |
| P06 | auto | ○ | MUST | 7 |
| P06 | auto | ○ | MUST | 7 |
| P06 | deep | ○ | MUST | 6 |
| P06 | deep | ○ | MUST | 8 |
| P06 | deep | ○ | MUST | 9 |
| P06 | standard | ○ | MUST | 5 |
| P06 | standard | ○ | MUST | 5 |
| P06 | standard | ○ | MUST | 6 |
| P07 | auto | ○ | MUST | 3 |
| P07 | auto | ○ | MUST | 3 |
| P07 | auto | ○ | MUST | 3 |
| P07 | deep | ○ | MUST | 3 |
| P07 | deep | ○ | MUST | 3 |
| P07 | deep | ○ | MUST | 3 |
| P07 | standard | ○ | MUST | 3 |
| P07 | standard | ○ | MUST | 3 |
| P07 | standard | ○ | MUST | 3 |
| P08 | auto | ○ | MUST | 2 |
| P08 | auto | ○ | MUST | 3 |
| P08 | auto | ○ | WANT | 2 |
| P08 | deep | ○ | MUST | 3 |
| P08 | deep | ○ | MUST | 3 |
| P08 | deep | ○ | MUST | 3 |
| P08 | standard | ○ | MUST | 3 |
| P08 | standard | ○ | MUST | 3 |
| P08 | standard | ○ | WANT | 3 |
