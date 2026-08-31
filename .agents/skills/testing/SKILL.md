# Testing — regionalwerke_baden

> Companion to `SKILLS.md`. Load this file when writing or fixing tests.

## Pyramid

- **Unit (no `hass`)** — `api.py` pure functions: `_extract_discovered`, `_is_2fa_page`, `_normalize_totp_secret`, `normalize_to_kwh`, `aggregate_hourly`, `parse_objekte`/`parse_messlinien`. Anonymized fixtures; import the module directly (`pytest.ini` sets `testpaths`, no `sys.path` hacking).
- **HTTP surface (no `hass`)** — `portal` / `portal_client` fixtures run a **real local aiohttp server** and point `api.BASE_URL` at it. This is where request shape is asserted.
- **Integration (with `hass`)** — `config_flow.py` + `coordinator.py`: `MockConfigEntry` → `async_setup_entry` → real `Store` (`hass_storage`) → **real recorder** (`recorder_mock`) → read statistics back with `statistics_during_period`.
- **Live** — `tests/test_live_integration.py`, marked `integration`, driven by `RWE_MFA_CODE` / `RWE_TOTP_SECRET` env vars. Guards the assumptions that would silently rot: discovery keys still present, our kWh total equals the portal's own aggregate, `week2` still returns a full week, `HISTORIC_EARLIEST_FALLBACK` is still the real start of history. Never in CI.

## Setup

```ini
# pytest.ini
[pytest]
testpaths = tests
asyncio_mode = auto
markers =
    integration: hits the live RWB portal; excluded from CI (pytest -m "not integration")
```

`requirements-test.txt` pins `homeassistant` **and** `pytest-homeassistant-custom-component` to the version on the deployment box (`ssh homeassistant.local cat /homeassistant/.HA_VERSION`). The plugin pins core exactly; installing it unpinned upgraded core to a beta underneath us.

```python
# tests/conftest.py — the fixtures that matter
custom_integration   # wraps enable_custom_integrations; opt-in, not autouse
portal               # real aiohttp server + monkeypatched api.BASE_URL; needs socket_enabled
portal_client        # RwbClient wired to it, discovery pre-seeded
```

Fixture ordering rules that are not optional:

- `recorder_mock` **before** `hass` in every signature, or phcc asserts `not hass_fixture_setup`.
- `await hass.async_block_till_done(wait_background_tasks=True)` to wait for the historic import.
- `await hass.config.async_set_time_zone("Europe/Zurich")` before any date-window assertion — fixtures are `+02:00`, HA's test default is US/Pacific.

## Mocking contracts

| Layer | Approach | Assert both directions |
|-------|----------|------------------------|
| `RwbClient._get/_post` | **Real** aiohttp server (`portal`) | Request: `portal.last_query(path)` / `portal.last_form(path)` — `objektId`, `meteringcode`, `messlinieId`, `messlinienIds` JSON, `datum`, `zeitraum`. Response: JSON vs login HTML vs 2FA HTML vs HTTP 500 |
| `coordinator.Store` | **Real** via `hass_storage` | After each week `{done, cursors: {meteringcode: date}}`, cursor Monday-aligned |
| `async_add_external_statistics` | **Real** via `recorder_mock` | Read back with `statistics_during_period`: hour-aligned starts, unique starts, monotonic `sum`, per-direction totals |

Do not mock these. A `ClientSession` fake cannot catch a malformed query string, and an `AsyncMock` around `async_add_external_statistics` accepts a `statistic_id` that HA itself rejects — both were live bugs that mocks would have hidden. There is no `tests/fakes/`; the `portal` fixture is the shared fake.

## Fixture invariants (fail the test if broken)

```
day / day2 = 96 points     (92 or 100 on DST transition days — assert `in (92, 96, 100)`)
week2      = 672 points    (7 × 96)
aggregate_hourly(96) == 24 points, and total energy is unchanged
sum(chartData.datasets[k].data) == chartData.aggregates.sum[ml_id]  (±1e-6)
kw value *0.25 == kwh value  (15 min)
intervals[i].from/until ↔ datasets[k].data[i]  (1:1, no off-by-one)
datasets carry {id, meteringcode, label} — select by `id`, never by position
```

Verified live 2026-08-30: `day2` → `unit=kwh`, 96 points, total equals the portal's own `aggregates.sum`; `week2` → 672 points; datasets carry the messlinie `id`.

Keep `tests/fixtures/` honest: `objekte.json` (`99999` → `CH999…`), `messlinien.json` (`10001`/`10002`), `messdaten_day_kw.json` / `day2_kwh.json` / `week2`. When you add a month fixture, document expected ~2928/2976 points.

## Patterns to copy

```python
@pytest.mark.parametrize("raw,expected", [
    ("JBSW Y3DP EHPK 3PX", "JBSWY3DPEHPK3PX"),
    ("otpauth://totp/RWB?secret=JBSWY3DPEHPK3PX&issuer=RWB", "JBSWY3DPEHPK3PX"),
    ("jbsw-y3dp-ehpk-3px", "JBSWY3DPEHPK3PX"),
])
def test_normalize_totp_secret(raw, expected):
    assert _normalize_totp_secret(raw) == expected

async def test_historic_resume(recorder_mock, hass, custom_integration, rwb_portal, hass_storage):
    # recorder_mock BEFORE hass; cursors are per meteringcode; no cumulatives are stored
    hass_storage[f"{DOMAIN}_historic_e1"] = {
        "version": 1, "key": f"{DOMAIN}_historic_e1",
        "data": {"done": False, "cursors": {MCODE: "2024-01-08"}},
    }
    ...
    await hass.async_block_till_done(wait_background_tasks=True)
```

- Use `@pytest.mark.parametrize` for boundaries, not near-duplicate functions.
- Reserve the `integration` marker for tests that hit the **live portal** — HA-based tests are fast (the full offline suite is ~2 s) and belong in the default run.
- Run `pytest -x` on the touched module before pushing; `pytest -m "not integration"` before PR.
- **Prove each regression test fails against the bug it names.** Re-introduce the defect, watch it fail, restore. Done for the sum reset, the invalid `statistic_id`, and the missing hourly aggregation.

## Anti-patterns

- Hitting the live portal in any test that is not marked `integration`.
- Asserting on a dynamic `sum` absolute value — assert monotonicity, continuity from the seeded total, and offset math.
- Mixing `asyncio_mode` conventions or forgetting `@pytest.mark.asyncio` so async tests silently pass without awaiting.
- Mocking the HTTP layer or `async_add_external_statistics` — both hid real shipped bugs.
- "Integration test" that still mocks `Store`/`recorder` — hides the real resume/dedup bug.
- A fake portal that answers every date and every `zeitraum` identically: `_discover_earliest` then walks back to its floor, and a `week2` chunk that returns one day makes the import look correct while leaving gaps.

## Anonymization

New fixture steps: capture → replace `meteringcode`/`bezeichnung`/address/CSRF/`PHPSESSID` → verify 96/672 + `kw*0.25` still hold → commit. Never commit raw portal HTML.
