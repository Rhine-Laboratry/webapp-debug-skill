# ワークフロー

## Phase 0: Preflight

- モード、設定、Git、スタック、アプリ起動候補を検出する。
- Sheets schemaと協調ロックを確認する。
- operation profile、DB分類、認証方式を解決する。
- DBガードを静的解析後、ブラウザ／DB接続前に検証する。

## Phase 1: Repository discovery

- フレームワーク固有の探索面を列挙する。
- ルート、Controller action、権限、validation、template、JS、mailer、帳票をsource anchor付きでInventoryへ登録する。
- API-only、Command、queue、外部連携は `OTHER_TEST_REQUIRED` として残す。

## Phase 2: Passive browser discovery

- ロールごとの認証stateを分離する。
- GET遷移、リンク、フォーム構造、表示要素、ネットワーク呼び出しを収集する。
- submit、delete、send、upload、外部API起動を実行しない。
- コード上存在するが到達不能なものは `UNREACHABLE` とし、Inventoryから消さない。

## Phase 3: Reconciliation

- 静的Inventoryとブラウザ観測をsource fingerprint、route、symbol、roleで照合する。
- コードだけにある項目、ブラウザだけにある項目、矛盾を明示する。
- 暫定仕様の根拠をScenarioへ保存する。
- 各discovery pass後にcoverage gateを評価する。`coverage.max_discovery_passes` を超えて無制限に反復しない。
- strict modeでは全有効Inventoryが `MAPPED` または `EXCLUDED_WITH_REASON` になるまでtestへ進まない。
- risk-gated modeは明示設定時だけ有効で、threshold達成時も残存 `DISCOVERY_GAP` を削除しない。

## Phase 4: Scenario modeling

- Feature／Story／Scenarioの3階層を使う。
- PASS／FAIL判定可能な最小単位をScenarioにする。
- 標準深度B。リスクでC／Dへ引き上げる。
- 日本語の箇条書きでprecondition、action、expected resultを書く。

## Phase 5: Test generation

- 既存Playwrightを再利用する。なければ `tests/e2e/` に独立環境を作る。
- Scenario IDをテスト名と生成コメントへ埋め込む。
- 既存テストとアプリコードを変更しない。
- locator安定性を記録する。

## Phase 6: Test execution

- seedは非破壊、idempotent、test_run_id所有権付き。
- workers 1、retry 1。
- attemptごとに履歴を追記する。
- failure時は期待値判定に加えてconsole、network、HTTP、download、authorizationを確認する。

## Phase 7: Failure analysis

- Trace、screenshot、video、console、network、CakePHP log、DB read、監視ログを相互参照する。
- defect classification、severity、probable cause、suspected files、confidenceを記録する。
- 原因候補を確定原因と断定しない。

## Phase 8: Cleanup and closure

- 証跡取得後、現在run所有データだけを子から親の順にcleanupする。
- cleanup結果を必ず記録する。
- 4つの完了条件を検証する。
- coverage gate未達ならtestへ進まず、残件、risk、reason code、最大pass到達有無を示す。
- 未完了なら残件と理由を示す。
