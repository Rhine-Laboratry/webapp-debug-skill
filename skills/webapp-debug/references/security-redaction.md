# 秘密情報と個人情報の取扱い

## 常に除去する情報

- password
- Cookie
- session ID
- Authorization Header
- API key／token
- private key
- Google service account private key
- DB username／password／DSN
- `local.php` 等の秘密設定値

## 出力禁止先

- 会話本文
- Google Sheets
- Test Runs／Defects
- screenshot filename
- Trace添付名
- generated test
- local Manifest
- command line argument

## Redaction

ログ、network body、HTML、screenshotに個人情報が含まれる場合は、保存前または外部アップロード前にマスクする。完全な自動マスクを保証できない場合、外部アップロードを停止し、ローカルartifactを `REDACTION_REVIEW_REQUIRED` とする。

最低限のmask対象:

- email address
- phone number
- postal address
- customer identifier
- access tokenらしい文字列
- credit card相当の連続数字

## Artifact

- ローカル保存を必須とする。
- 外部ストレージはoptional。
- EvidenceにSHA-256を記録する。
- authentication storageStateはEvidenceへ登録しない。
- retention expiryを記録する。

## Safe reporting

接続確認は次のように報告する。

```text
DB host pattern: MATCHED
DB name pattern: MATCHED
sentinel: MATCHED
```

生値は表示しない。
