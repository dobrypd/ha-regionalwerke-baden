# Rules

## Never touch author's notes

In readme there is a section "Author's notes" this is ment to be written by human. Never change it. You can prompt user in case there is a problem with it, or a typo. But never change it by yourself.

## Never delete untracked files
Untracked files are user property. Do not `rm`, `git clean`, or overwrite them.

Examples: `rwe.bundle`, `rwe_repo.tar.gz`, `PUSH_INSTRUCTIONS.md`, any `??` file in `git status`.

If a checkout would overwrite them, use `git checkout -f -B` (preserves unrelated untracked) or ask — never delete.

## Verified Home Assistant contracts

Checked against **homeassistant 2026.8.3** (the version on `homeassistant.local`) by reading the installed package. Re-verify after any core bump; none of these fail loudly.

| Contract | Reality | Consequence if broken |
|----------|---------|-----------------------|
| `statistic_id` format | Must match `^(?!.+__)(?!_)[\da-z_]+(?<!_):(?!_)[\da-z_]+(?<!_)$` — exactly one `:`, lowercase, no `__`, no leading/trailing `_` | `async_add_external_statistics` raises `HomeAssistantError("Invalid statistic_id")` and **nothing is written** |
| External statistics period | **Hourly.** `async_add_external_statistics` is documented "Add hourly statistics"; every reducer in `recorder/statistics.py` says "Reduce hourly statistics to …". There is **no** alignment check | Sub-hourly rows are silently inserted and mis-read downstream |
| `StatisticMetaData` | `mean_type` (`StatisticMeanType`) and `unit_class` are required keys; `has_mean` is deprecated | Missing `mean_type` triggers a `report_usage` warning that becomes an error in 2026.11 |
| Statistics use requires a manifest dependency | `"dependencies": ["recorder"]` — same as core's `opower` / `tibber` | Setup ordering is undefined; hassfest flags it |
| `DataUpdateCoordinator` | Has `last_update_success` (bool). There is **no** `last_update_success_time` | `AttributeError` on every state read |
| `entity_category` | Must be an `EntityCategory` instance — `entity_registry.py` raises `ValueError("entity_category must be a valid EntityCategory instance")`. `EntityCategory` is a `StrEnum`, so `==` passes but `isinstance` fails | Entity fails to register |
| Custom-integration translations | Loaded from `translations/<lang>.json` (`translation.py`, gated on a top-level `translations` dir). `strings.json` alone is **not** read at runtime | Config-flow UI shows raw keys |
| Per-entry sessions | `async_create_clientsession` already registers `detach()` on the entry's unload. Its `close` is wrapped by `warn_use` | Calling `session.close()` yourself raises "Detected code that closes the Home Assistant aiohttp session" |

## Pin the test stack to the deployment target
`pytest-homeassistant-custom-component` pins an exact `homeassistant` version. Installing it unpinned silently upgraded core to a beta. Pin both in `requirements-test.txt` and bump them together, matched to `/homeassistant/.HA_VERSION` on the target box.

## Fixture order for recorder tests
`recorder_mock` must be requested **before** `hass` in every signature — phcc asserts `not hass_fixture_setup` inside `recorder_db_url`. A background task on the config entry needs `await hass.async_block_till_done(wait_background_tasks=True)`; the plain call returns before it finishes.

## Test the timezone the data is in
Fixtures carry Europe/Zurich offsets; HA's test default is US/Pacific. `dt_util.start_of_local_day` then lands on a different day than the portal's, and date-window assertions fail for reasons unrelated to the code. Call `await hass.config.async_set_time_zone("Europe/Zurich")` before setup.

## Reaching the live portal from pytest

phcc calls `pytest_socket.socket_allow_hosts(["127.0.0.1"])`, which guards **`socket.socket.connect`**. `enable_socket()` restores `socket.socket` but *not* `connect`, so it does **not** lift that guard — only `_remove_restrictions()` does. pytest-socket's `pytest_runtest_teardown` then calls `_remove_restrictions()` after every test, so the guard is only ever active for the **first test of the session**: one test fails, the rest pass, and the cause looks like fixture ordering when it isn't. The `allow_outbound_sockets` fixture in `test_live_integration.py` calls `_remove_restrictions()` and restores phcc's state on teardown.

Verify socket plumbing with a throwaway two-test module doing a plain `GET /login` — the first test in the module is the one that matters. Costs no MFA code.

## Prove a regression test fails
A regression test that passes against the bug it names is worse than none. Re-introduce the defect, watch the test fail, restore. Done for: cumulative-sum reset, invalid `statistic_id`, missing hourly aggregation.
