# レビュー結果の例（採点の動作確認用。架空のレビュー）

| No | 重要度 | 箇所 | 指摘 |
|---|---|---|---|
| 1 | MUST | MemberService.java register | existsByEmail の後に insert しており、同時に登録されると一意制約違反で 500 になる |
| 2 | WANT | OrderImportService.java parse | 存在しない member_id の行があると外部キー違反でファイル全体がロールバックされる |
| 3 | NITS | MemberController.java | 変数名 m は member の方が読みやすい |
| 4 | WANT | build.gradle karateTest | baseUrl が systemProperty で渡されていない |
