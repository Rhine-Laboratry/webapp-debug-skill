# webapp-debug skill v0.1

CakePHP／JavaScriptを含むWebアプリケーションを対象に、コードベースと実ブラウザから機能を棚卸しし、日本語のユーザーストーリー／Scenarioへ変換し、Playwrightテストを生成・実行して、Google Sheetsへ進捗・結果・不具合・証跡を記録するAgent Skillの初稿です。

## 配置

このパッケージをリポジトリ直下へコピーしてください。次の2つのラッパーが、共通の正準Skillを読み込みます。

- Codex: `.agents/skills/webapp-debug/SKILL.md`
- Claude Code: `.claude/skills/webapp-debug/SKILL.md`
- 共通の正準Skill: `skills/webapp-debug/SKILL.md`

ラッパーから共通Skillへの相対パスを維持するため、ディレクトリ構成は変更しないでください。

## 呼び出し

Codex:

```text
$webapp-debug init
$webapp-debug discover
$webapp-debug test
$webapp-debug full
$webapp-debug resume
$webapp-debug report
```

Claude Code:

```text
/webapp-debug init
/webapp-debug discover
/webapp-debug test
/webapp-debug full
/webapp-debug resume
/webapp-debug report
```

モードを省略した場合はヘルプだけを返し、副作用のある処理を開始しません。

## 初期導入

1. `skills/webapp-debug/assets/webapp-debug.config.example.yml` を `.webapp-debug/config.yml` へコピーします。
2. `spreadsheet_id`、ローカルアプリの起動方法、接続先DBの安全条件を設定します。
3. Googleサービスアカウントの認証情報はリポジトリへ保存せず、`GOOGLE_APPLICATION_CREDENTIALS` 等の環境変数から渡します。
4. `.gitignore` に `skills/webapp-debug/assets/gitignore.fragment` の内容を反映します。
5. 最初に `init` を実行して、設定、Google Sheetsのタブ、Playwright環境を検査します。

## 正準データの範囲

Google Sheetsは、機能インベントリ、Scenario、テスト実行履歴、不具合、証跡メタデータ、進捗ステータスの唯一の正準データです。ローカルの `.webapp-debug/config.yml` は、マシン固有の実行ポリシーと秘密情報を含まない接続設定を保持します。実行途中の同期障害時だけ、`.webapp-debug/state/` にwrite-ahead logを保存します。

## v0.1の境界

- 正式アダプター: CakePHP 3.x〜5.xを想定
- CakePHP 2.x: generic PHP解析＋ブラウザ探索
- E2E対象: 画面、画面由来Ajax、アップロード／ダウンロード、メール、CSV／PDF、JavaScript画面動作
- Inventoryのみ: UIを持たないAPI、Command／cron、キュー、外部連携
- 対象外: ピクセル単位のVisual Regression、性能試験、能動的脆弱性診断
- 不具合修正: 行わず、原因候補と根拠、確信度まで記録

## 主要ファイル

- `skills/webapp-debug/SKILL.md`: 実行手順とガード
- `skills/webapp-debug/references/`: 詳細仕様
- `skills/webapp-debug/assets/webapp-debug.config.example.yml`: 設定テンプレート
- `skills/webapp-debug/assets/config.schema.json`: 設定のJSON Schema
- `skills/webapp-debug/assets/google-sheets-schema.json`: Google Sheetsタブ／列定義
- `skills/webapp-debug/assets/playwright.config.example.ts`: Playwright既定値
- `DECISIONS.md`: 合意済み要件

## 実装上の注意

この初稿はAgent Skillのワークフロー、データ契約、安全条件を定義します。Google Sheets同期CLIやCakePHP AST解析器を固定実装として同梱してはいません。各対象リポジトリの構成差を調査した上で、Skillが `tests/e2e/` と `.webapp-debug/generated-tools/` に必要な補助コードを生成する設計です。
