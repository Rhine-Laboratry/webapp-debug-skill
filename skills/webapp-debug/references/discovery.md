# 機能探索規則

## 探索優先順位

1. 実行時に有効なコード
2. Route／Middleware／Authentication／Authorization
3. Controller／Service／Model／Validation
4. Template／JavaScript
5. 既存テスト
6. README／設計資料／コメント
7. 実ブラウザで観測した挙動

## Stack detection

調査候補:

- `composer.json`、`composer.lock`
- `package.json`、npm／yarn／pnpm lockfile
- CakePHP bootstrap、Application、routes、plugins
- README、Makefile、Docker Compose、devcontainer
- PHP／Node version files

バージョンを推測だけで確定しない。検出根拠のpathをInventoryまたはrun metadataへ残す。

## CakePHP 3.x〜5.x

最低限、次を探索する。

- `config/routes.php` とplugin routes
- `src/Application.php`、Middleware queue
- `src/Controller/**/*Controller.php` のpublic action
- Component、Service、Domain層の呼び出し
- Authentication／Authorization設定、policy
- `src/Model/Table` のvalidation、rules、callbacks
- `src/Form`、Entity accessibility
- `templates/`、`plugins/*/templates/`、Element、Layout
- `src/Mailer`、Email設定
- CSV、PDF、download response、file upload
- `src/Command`、queue／job実装
- 既存PHPUnit／integration test

## CakePHP 2.x

generic解析として次を探索する。

- `app/Config/routes.php`
- `app/Controller`
- `app/Model`
- `app/View`
- Component、Behavior、Helper
- Console／Shell

専用解析を保証しない。解釈できない動的routing、magic method、plugin拡張は `DISCOVERY_GAP` にする。

## JavaScript

最低限、次を探索する。

- `webroot/js`、`assets`、`src`、template inline script
- DOM event listener
- form submit interception
- `fetch`、Axios、jQuery Ajax、XHR
- modal、tab、filter、sort、pagination
- client-side validation
- upload、download trigger
- role／feature flagによる分岐
- dynamically generated link／route

minified、vendor bundle、generated outputは原則除外し、source mapまたは元sourceを優先する。

## Inventory item types

- `UI_PAGE`
- `UI_ACTION`
- `AJAX_FROM_UI`
- `UPLOAD`
- `DOWNLOAD`
- `EMAIL`
- `REPORT_CSV`
- `REPORT_PDF`
- `CLIENT_INTERACTION`
- `API_ONLY`
- `COMMAND_CRON`
- `QUEUE_ASYNC`
- `EXTERNAL_INTEGRATION`
- `AUTHORIZATION_RULE`
- `VALIDATION_RULE`

## Testability classification

- `E2E_PLAYWRIGHT`
- `OTHER_TEST_REQUIRED`
- `NOT_TESTABLE_WITH_CURRENT_ACCESS`
- `EXCLUDED_WITH_REASON`

API-only、Command、queue、external integrationを除外扱いにせず、推奨テスト方式を記録する。

## Passive browser discovery

許可:

- GET navigation
- DOM、role、label、link、form fieldの収集
- network request metadataの収集
- tab、accordion等のローカル表示切替
- side effectがないとコードで確認できる検索／filter

禁止:

- POST／PUT／PATCH／DELETE submit
- delete／cancel／disable
- mail send
- upload
- external service invocation
- payment
- DB reset

不明な操作は実行せずInventoryへ登録する。

## Closure

Inventory itemごとに次のいずれかを必須にする。

- `MAPPED`: 1件以上のScenarioへ対応
- `EXCLUDED_WITH_REASON`: 明確な理由と推奨代替テスト方式あり

`NEW`、`DISCOVERING`、`DISCOVERY_GAP`、`UNREACHABLE` が残る限り調査完了ではない。
