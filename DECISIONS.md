# 合意済み設計判断

## 目的

- リポジトリ内の全機能を定義済み探索面に沿って棚卸しする。
- コードに基づく暫定期待動作を、日本語のFeature／User Story／Scenarioへ変換する。
- Scenario単位で独立してPASS／FAILを判定する。
- Google Sheetsで進捗、履歴、不具合、証跡を一元管理する。
- 調査完了後、自動的に全Scenarioのテストフェーズへ移行する。

## 実行基盤

- リポジトリ内のAgent Skill
- Codex／ChatGPT系コーディングエージェント
- Claude Code
- 副作用のあるモードは明示呼び出しのみ

## 対象

- ホワイトボックス解析
- CakePHP、PHP、JavaScriptのバージョンはリポジトリごとに自動検出
- CakePHP 3.x〜5.xは正式アダプター想定
- CakePHP 2.xはgeneric PHP解析
- 一般ユーザー、管理者
- Chromium、1440×900、ja-JP、Asia/Tokyo
- 明示指定時のみスマートフォン／タブレットを追加

## 機能範囲

Playwright対象:

- A: ブラウザ画面
- B: 画面から呼ばれるAjax／API
- D: ファイルアップロード／ダウンロード
- E: メール送信
- F: CSV／PDF等の帳票
- J: JavaScriptだけで完結する画面動作

Inventory登録後に別方式へ分類:

- C: UIを持たないAPI
- G: バッチ、cron、CakePHP Command
- H: キュー、非同期処理
- I: 外部サービス連携

## 期待動作の優先順位

1. 実行時に有効なアプリケーションコード
2. Route、Middleware、Authentication、Authorization
3. Controller、Service、Model、Validation
4. Template、JavaScript
5. 既存自動テスト
6. README、設計資料、コメント
7. staging／ローカル実行環境で観測した挙動

実装と既存テストが矛盾する場合は実装を暫定仕様とする。矛盾自体は記録する。

## Scenario深度

- 標準: 正常系、権限、主要異常系
- HIGH: コード上の重要分岐を追加
- CRITICAL: 境界値と重要な組み合わせを追加
- リスク評価は自動、`manual_priority` で上書き可能

## テストコード

- `tests/e2e/generated/`
- `tests/e2e/fixtures/`
- `tests/e2e/pages/`
- 既存テストは編集しない
- アプリケーションコードは変更しない
- 自動コミットしない
- 各生成テストに `scenario_id` を埋め込む
- Playwrightがなければ `tests/e2e/` に独立環境を作る

## Google Sheets

- サービスアカウント
- 1スプレッドシート、複数タブ
- v1は単一ライター
- Skillのみが自動書き込み
- 人間編集可能列:
  - `review_status`
  - `notes`
  - `manual_override`
  - `manual_expected_behavior`
  - `manual_exclusion_reason`
  - `manual_priority`
- `Test Runs` は追記専用
- シート接続不能時、開始前なら停止。実行途中ならローカルWALへ保存し `PAUSED_SYNC_REQUIRED`

## DB安全条件

- `destructive_reset` 初期値は `false`
- DB接続を伴うブラウザ実行またはデータ操作前に、空でない `expected_host_pattern` と `expected_database_pattern` を要求
- sentinel未設定、不一致ならDB接続を伴う実行不可
- `local.php` 等の秘密情報は外部出力しない
- 共有DB／不明DBではスナップショット復元禁止
- 共有DB／不明DBでは `reset_scope: none` または `manual`
- Scenarioごとのseedは非破壊、idempotent
- 作成データは `test_run_id` と関連付ける
- 追跡テーブルまたはローカルManifestによる関連付けも有効
- cleanupは証跡取得後、現在の `test_run_id` 所有データだけに限定
- 所有関係が不明なデータは削除しない
- 既存共有データの更新／削除は禁止

## 実行ポリシー

- `discover` は非破壊
- `workers: 1`
- `retries: 1`
- Scenario timeout: 60秒
- Trace: failureとretryを保持
- Video／Screenshot: failure時に保持
- 再試行前にScenarioデータを再初期化
- 失敗後、証跡取得してからテスト所有データをcleanup

## 完了条件

調査完了:

- 全Inventoryが `MAPPED` または `EXCLUDED_WITH_REASON`

生成完了:

- 全テスト可能Scenarioが `GENERATED` または `BLOCKED`

実行完了:

- 全有効Scenarioが `PASS`、`FAIL`、`BLOCKED`、`FLAKY` のいずれか

文書化完了:

- 全 `FAIL`／`BLOCKED` に証跡、再現手順、原因候補がある
