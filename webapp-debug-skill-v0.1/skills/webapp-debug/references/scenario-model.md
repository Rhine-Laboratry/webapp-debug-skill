# Scenarioモデル

## 階層

```text
Feature
  └─ User Story
       └─ Scenario
```

PASS／FAILの最小単位はScenarioとする。

## 日本語記述形式

```text
Feature: ユーザー管理

User Story:
- 利用者: 管理者
- 目的: 一般ユーザーの利用状態を管理する
- 価値: 利用停止対象のアクセスを防止できる

Scenario: 有効な一般ユーザーを停止できる
- 前提:
  - 管理者としてログインしている
  - このtest_run_idが作成した有効な一般ユーザーが存在する
- 操作:
  - ユーザー詳細を開く
  - 停止操作を実行する
- 期待結果:
  - 対象ユーザーが停止状態として表示される
  - 対象ユーザーはログインできない
  - 他の既存ユーザーは変更されない
```

## 原則

- 1つのScenarioに複数の独立判定を詰め込みすぎない。
- ただし、1操作の不可分な結果は同じScenarioでよい。
- actorは `anonymous`、`user`、`admin` を基本とする。
- preconditionにテストデータ所有権と必要ロールを明記する。
- expected resultは観測可能な結果にする。
- 実装詳細だけを期待結果にしない。
- code source referenceを必須にする。

## 期待動作status

- `PROVISIONAL_CODE`: コード由来の暫定期待
- `MANUAL_OVERRIDE`: 人間が期待動作を上書き
- `CONFLICT_CODE_TEST`: 実装と既存テストが矛盾
- `OBSERVED_ONLY`: コード根拠を特定できず観測のみ

`manual_expected_behavior` が設定された場合、テスト生成ではそれを使用する。自動生成した元期待値は保持する。

## Scenario深度

### B: 標準

- 正常系
- role／authorization
- 主要な異常系

### C: HIGH

- コード上の主要validation分岐
- 重要状態遷移
- ファイル形式、件数、権限差分

### D: CRITICAL

- 境界値
- 重要な組み合わせ
- 機密情報漏えい防止
- 不可逆操作の前後条件

## リスク加点候補

- authentication／authorization
- role変更
- delete、cancel、disable、state transition
- 個人情報、機密情報
- upload／download／CSV／PDF
- email
- external integration
- 大量データ
- admin-only
- 広範囲影響

## ID

- `feature_id`: `FEAT-000001`
- `story_id`: `STORY-000001`
- `scenario_id`: `SCN-000001`
- `scenario_version`: 1から開始

IDは削除・再利用しない。内容変更はversion更新、廃止は `RETIRED`、統合は `MERGED` とする。
