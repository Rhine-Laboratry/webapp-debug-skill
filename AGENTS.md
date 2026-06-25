# AGENTS.md

## リポジトリの目的

このリポジトリは、Webアプリケーションのコードベースとブラウザ動作から機能を棚卸しし、日本語のFeature／User Story／Scenario、Playwrightテスト、Google Sheets上の進捗・不具合記録を生成するAgent Skillを提供する。

現在の優先事項は、仕様書中心のv0.1を、繰り返し安全に実行できるv0.2へ強化すること。実装順序と受け入れ条件は `docs/IMPLEMENTATION_PLAN.md` を正とする。

## 最初に読むファイル

作業開始時に次を順に読む。

1. `AGENTS.md`
2. `docs/IMPLEMENTATION_PLAN.md`
3. `DECISIONS.md`
4. `skills/webapp-debug/SKILL.md`
5. 変更対象に対応する `skills/webapp-debug/references/` と `assets/`

## 作業プロトコル

- 1回の作業では、実装計画の1フェーズだけを対象にする。依頼にフェーズ指定がなければ、未完了の最初のフェーズを対象とする。
- 作業前に `git status --short --branch` を確認し、既存の未コミット変更を上書きしない。
- 変更前に対象フェーズの受け入れ条件と非対象範囲を要約する。
- フェーズ外の改善を見つけても、勝手に実装せず未解決事項として報告する。
- 実装後は、そのフェーズで指定された全検証を実行する。実行できない検証は、コマンド、阻害要因、未検証範囲を明記する。
- `docs/IMPLEMENTATION_PLAN.md` の完了チェックは、受け入れ条件を満たし、テスト結果を確認した場合だけ更新する。

## 変更禁止・安全規則

- `main` へ直接push、force-push、履歴改変をしない。
- 明示されない限り、commit、push、tag、release作成をしない。
- 実在するGoogle Sheets、DB、メール、外部APIへ、テスト中にアクセスしない。単体テストではfake／mockを使う。
- Google Sheets統合テストは、専用環境変数による明示的opt-inがない限り実行しない。
- アプリケーションコード、既存Playwrightテスト、利用者の `.webapp-debug/config.yml` を無断で変更しない。
- `local.php`、`app_local.php`、`database.php`、環境変数、認証stateから読み取った秘密値を、stdout、stderr、例外、テストsnapshot、WAL、Google Sheets、生成物へ出力しない。
- DBパスワード、接続文字列、Cookie、Authorization、Set-Cookie、APIキー、トークン、秘密鍵をログに残さない。
- 共有または不明DBに対するsnapshot restore、TRUNCATE、広範囲DELETEを実装・実行しない。
- 所有権が `test_run_id`、追跡テーブル、またはManifestで証明できないデータの更新・削除を許可しない。
- 破壊的操作はfail-openにしない。設定不足、schema不一致、ロック競合、redaction不能は停止理由として扱う。

## Skillメタデータの規則

- `skills/webapp-debug/SKILL.md` と `.agents/skills/webapp-debug/SKILL.md` のfrontmatterは、Codex互換の `name` と `description` のみにする。
- Codexの暗黙起動制御、UI表示、default promptは `agents/openai.yaml` に置く。
- Claude Code固有の `disable-model-invocation`、`argument-hint`、`$ARGUMENTS` は `.claude/skills/webapp-debug/SKILL.md` だけで使用する。
- 共通SKILLにプラットフォーム固有キーを混在させない。
- Codex用とClaude用のラッパーは、共通の正準Skillを参照する薄いファイルに保つ。

## Python実装規則

- Python 3.11以上を対象とする。
- CLIは `scripts/` に置き、再利用可能な処理は `src/webapp_debug_skill/` に置く。
- 全公開関数へ型注釈を付ける。ファイル操作には `pathlib` を使う。
- YAMLは `yaml.safe_load` で読み込み、任意オブジェクトのdeserializationを許可しない。
- JSON Schema Draft 2020-12を使用する。
- CLIは正常時、入力／schema不正、安全ポリシーによる停止、外部サービス失敗、ロック競合を区別する終了コードを返す。
- CLIには `--help` と機械可読な `--format json` を用意する。
- 外部状態を変更するCLIには `--dry-run` を実装し、dry-run中はネットワーク書き込みとローカル状態変更を行わない。
- ファイル更新は一時ファイルへ書いてからatomic replaceする。既存ファイルを暗黙に上書きしない。
- 例外メッセージをそのまま表示せず、redactionを通した安全なエラーへ変換する。
- Google API呼び出しをドメインロジックから分離し、fake clientで単体テストできる設計にする。

## テスト規則

- 新しい挙動には正常系、境界値、拒否系、秘密情報非漏えいのテストを追加する。
- 単体テストはネットワーク、実DB、実Google認証情報へ依存させない。
- 一時ファイルはpytestの一時ディレクトリを使用する。
- 時刻、UUID、host情報を直接参照する処理は注入可能にして、テストを決定的にする。
- redactionテストでは、入力した秘密値がstdout、stderr、出力ファイル、例外文字列のいずれにも残らないことを確認する。
- lock／WALテストでは、期限切れ、競合、所有者不一致、途中失敗、再実行を含める。

実装基盤が追加された後の標準検証コマンドは次とする。

```bash
python -m pytest -q
python -m ruff check .
python -m ruff format --check .
python scripts/validate_skill.py --root .
python scripts/validate_sheets_schema.py \
  --schema skills/webapp-debug/assets/google-sheets-schema.json
python scripts/validate_config.py \
  --config skills/webapp-debug/assets/webapp-debug.config.example.yml \
  --mode init
```

フェーズ途中で未実装のコマンドは無理に実行せず、そのフェーズで利用可能になった時点から必須にする。

## Gitと差分の規則

- 作業ブランチの推奨名は `feat/v0.2-runtime-hardening`。
- 無関係な整形、ファイル移動、命名変更を同じ差分へ混ぜない。
- 生成されたcredential、`.env`、`.webapp-debug/`、Playwright auth state、artifact、WALを追跡しない。
- lockfileを導入した場合は、依存定義と同じ変更でcommit対象にする。
- 変更報告には、変更ファイル、ユーザー可視の挙動、実行した検証、未解決事項を含める。

## Definition of Done

フェーズ完了には以下をすべて要求する。

- 対象フェーズの受け入れ条件を満たしている。
- 新規・既存テストが成功している。
- 安全停止がfail-closedである。
- 秘密情報を含むfixtureを使った漏えいテストが成功している。
- README／INSTALL／DECISIONSと実ファイルが矛盾していない。
- dry-runと通常実行の差がテストされている。
- 未対応事項を実装済みのように記述していない。

## Review guidelines

レビューでは特に次を確認する。

- Codex用frontmatterに許可外キーが混入していないか。
- schemaだけでなくmode／capability固有の安全条件を検証しているか。
- Google Sheetsの未知列や人間編集列を破壊しないか。
- lockを強い排他保証として誤表現していないか。v0.2のlockは単一ライター前提の協調ロックである。
- WALへ書く前にデータがredaction済みか。
- unsupportedなbinary artifactを安全に処理したと誤認させていないか。
- `DISCOVERY_GAP` を隠したり、threshold達成を100% coverageと表示したりしていないか。
