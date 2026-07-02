# CakePHP Static Discovery Rules

Phase 6A supports read-only static Inventory discovery for CakePHP applications.

## Scope

Supported surfaces:

- `config/routes.php`
- `src/Controller/**/*Controller.php`
- `templates/**/*`
- `src/Template/**/*`
- `plugins/*/config/routes.php`
- `plugins/*/src/Controller/**/*Controller.php`
- `plugins/*/templates/**/*`
- `src/Application.php`
- `src/Policy/**/*`
- `src/Middleware/**/*`
- `src/Model/Table/**/*Table.php`
- CakePHP 2 generic paths: `app/Config/routes.php`, `app/Controller/**/*Controller.php`, `app/View/**/*`

CakePHP 3.x through 5.x are the primary target. CakePHP 2.x is treated as generic PHP parsing; unsupported dynamic behavior remains a `DISCOVERY_GAP`.

## Safety

The static discovery CLI:

- Reads source files only.
- Does not execute PHP, Composer, npm, CakePHP commands, DB connections, browser automation, Google APIs, or network calls.
- Excludes `vendor/`, `node_modules/`, `tmp/`, `logs/`, `cache/`, `coverage/`, build output, `.env`, `local.php`, `app_local.php`, and `database.php`.
- Emits repo-relative paths only.
- Does not emit raw source bodies.
- Writes a local JSON snapshot atomically.

## Output

`scripts/discover_cakephp_inventory.py` emits JSON with top-level `Inventory`, `Discovery Gaps`, `summary`, and `source`.

Inventory rows use deterministic temporary IDs with the `INV-TEMP-` prefix. Initial statuses are `DISCOVERED` or `DISCOVERY_GAP`; the static discovery step does not mark rows as `MAPPED` or `EXCLUDED_WITH_REASON`.

## Limits

Route parsing is conservative. Dynamic route expressions, fallbacks, inherited actions, and ambiguous plugin/prefix mappings are not guessed aggressively. They are recorded as safe discovery gaps or lower-confidence candidates.

Authorization hints such as Admin prefix, Authentication/Authorization references, policies, middleware, `allowUnauthenticated`, and `skipAuthorization` are hints only. Phase 6A does not infer complete authorization rules.
