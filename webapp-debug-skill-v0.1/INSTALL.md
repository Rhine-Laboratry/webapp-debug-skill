# 導入手順

## 1. ファイル配置

このディレクトリの内容を対象リポジトリ直下へコピーする。

## 2. 設定作成

```bash
mkdir -p .webapp-debug
cp skills/webapp-debug/assets/webapp-debug.config.example.yml .webapp-debug/config.yml
```

`.webapp-debug/config.yml` で最低限、次を設定する。

- project ID／name
- app base URL／start command／readiness URL
- Google Spreadsheet ID
- allowed hosts
- DB classification
- expected host pattern
- expected database pattern
- sentinel query／expected value
- seed command

接続文字列やpasswordをconfigへ複製しない。既存のlocal.phpまたは環境変数を参照する。

## 3. Google認証

サービスアカウントJSONはリポジトリ外へ置き、環境変数でpathを渡す。

```bash
export GOOGLE_APPLICATION_CREDENTIALS=/secure/path/service-account.json
```

Spreadsheetを事前作成する場合、サービスアカウントへ編集権限を与える。

## 4. Git除外

```bash
cat skills/webapp-debug/assets/gitignore.fragment >> .gitignore
```

重複行は整理する。

## 5. 初期化

Codex:

```text
$webapp-debug init
```

Claude Code:

```text
/webapp-debug init
```

## 6. 最初の安全な実行

```text
webapp-debug discover
```

`discover` は非破壊。DBガード未成立の場合、静的解析だけを実施し、ブラウザ探索をblockする。

## 7. テスト実行

DBガード、認証、seed、cleanup所有権を確認後に `test` または `full` を明示実行する。
