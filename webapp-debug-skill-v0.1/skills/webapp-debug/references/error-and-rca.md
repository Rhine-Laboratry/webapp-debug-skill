# エラー判定と原因分析

## 既定判定

FAIL:

- 期待結果と不一致
- 未処理JavaScript例外
- 想定外HTTP 500系
- 想定外HTTP 400系
- 認可されていない操作が成功
- 必須resource／request失敗
- downloadのMIME、filename、内容不一致
- allowlistにない `console.error`

WARNING:

- `console.warn`
- optional resourceの失敗
- accessibility診断
- locator stability低下

BLOCKED:

- 認証不足
- DBガード不成立
- 必要データを安全に準備できない
- 環境停止
- 未対応構成
- 外部依存が利用不可

FLAKY:

- 初回失敗、再試行成功

## Defect classification

- `PRODUCT_DEFECT`
- `TEST_AUTOMATION_DEFECT`
- `EXPECTATION_CONFLICT`
- `TEST_DATA_DEFECT`
- `ENVIRONMENT_DEFECT`
- `EXTERNAL_DEPENDENCY_DEFECT`
- `FLAKY`
- `UNKNOWN`

## Severity

- `CRITICAL`: 認可突破、重大な機密漏えい、広範囲な破壊、主要業務停止
- `HIGH`: 主要機能不能、重要データ不整合、管理機能の重大誤動作
- `MEDIUM`: 代替手段のある機能不全、主要でない異常系
- `LOW`: 軽微な表示、診断、限定条件

## 必須Defect項目

- expected_behavior
- actual_behavior
- reproduction_steps
- classification
- severity
- probable_cause
- suspected_files
- evidence
- confidence: `HIGH`／`MEDIUM`／`LOW`
- next_investigation

## 証拠収集順序

1. Playwright assertionとstep
2. Trace／DOM snapshot
3. screenshot／video
4. console／page error
5. network request／response status
6. CakePHP application log
7. web server／container log
8. DB read-only query
9. monitoring／external dependency log
10. source code pathと実行分岐

## 原因分析規則

- 時系列を作り、最初の異常と後続エラーを区別する。
- 証拠がない原因を断定しない。
- `probable_cause` は候補として書く。
- `suspected_files` はpathとsymbolを示す。秘密値を引用しない。
- app defectとtest defectを分ける。
- expectationが曖昧なら `EXPECTATION_CONFLICT` を優先する。
- failure後もcleanup結果を別軸で記録する。

## 再現手順

- actor
- precondition
- seed identifierの非秘密表現
- URL／route
- 操作順
- expected／actual
- run ID、scenario ID、commit

固定パスワード、Cookie、token、DB主キー以外の個人情報を含めない。
