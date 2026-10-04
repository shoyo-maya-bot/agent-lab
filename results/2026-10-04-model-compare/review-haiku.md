| No | 重要度 | 箇所 | 指摘 | 根拠 |
|---|---|---|---|---|
| 1 | WANT | MemberService.register, 25-33行 | existsByEmail 確認から insert 実行までが非アトミックで、同時実行による重複登録のリスク。2つのリクエストが同時に同じメール、アドレスで登録した場合、両方とも existsByEmail() でpass してから insert が実行され、重複登録エラー（DB制約違反）となる可能性 | 仕様では「メールアドレスの重複チェック」に 409 を返すことを定めており、実装は existsByEmail + insert で実装。しかし、トランザクション内でも check-then-act パターンであり、並行実行環境では競合状態が発生 |
| 2 | WANT | MemberControllerTest.java, 全体 | 重複メール登録時の 409 応答のテストが欠落。API レベルでの 409 エラーレスポンス形式の検証がない | MemberServiceTest で重複チェック（52-59行）は確認されているが、MemberControllerTest では登録エラーをテストしていない。API レスポンスの一貫性確認不足 |
| 3 | Q | MemberMapper.xml とスキーマ定義 | email 列に対する一意制約（UNIQUE）がコード上で確認できない。存在しない場合、重複登録が DB で検出されず、不正な状態になる | MemberService は existsByEmail() で重複を確認し、見つかれば例外を投げるが、制約がない場合、フレームワークレベルでの保護がない |
| 4 | NITS | OrderImportJob.java, 45行 | エラーメッセージが messages.properties から読み込まれず、ログに直接埋め込まれている。「引数 --input=<s3://バケット/キー またはファイル> が必要です」は国際化非対応 | APPE0001 メッセージ ID は messages.properties に定義されているが、使用されていない。バッチログレベルなので実運用への影響は小さいが、メッセージ管理が一貫していない |
| 5 | WANT | OrderImportServiceTest.java, 13行 | OrderImportJobTest の OrderImportService モック（o -> 1）が常に insert 成功を返す。このため JobTest では重複チェックのシナリオが検証されていない | OrderImportServiceTest で重複シナリオ（35-43行）が確認されているため、ロジック自体は正しいが、ジョブ統合テストの検証範囲が狭い |
| 6 | WANT | MemberController.java, 37-40行 | register メソッドが Location URI の生成時に created.id() の null チェックを行わない。MyBatis useGeneratedKeys により実装では id が設定されるが、明示的なガード条件がない | MemberService.register は rec.toMember() で id をそのまま使用しており、null 値は想定されていないが、future-proof ではない |
