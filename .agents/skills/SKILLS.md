# Skills — regionalwerke_baden (LLM coding agents)

> Repo: HA custom component `regionalwerke_baden` for `rwb-kundenportal.ch`. Energy via `recorder` external statistics (D+1, `cloud_polling`). Audience: LLM agents writing code/tests/reviews. Keep this file high-level and actionable — details live in `ARCHITECTURE.md` / `RULES.md` / source.

**When to load what** — `testing.md` when writing or fixing tests, `review.md` when reviewing any diff, `design.md` when adding a feature, changing scheduling/auth, or choosing patterns. `SKILLS.md` (this file) is the index and the short-form all three.

---

## 1. Testing

**Pyramid for this repo:** pure unit (`tests/test_api.py` parsing/normalization — no `hass`), HTTP-surface tests against a **real local aiohttp server**, coordinator/config-flow tests against a **real recorder** (`recorder_mock`), and `tests/test_live_integration.py` marked `integration` for the live portal. CI runs `pytest -m "not integration"`; never hit the live portal there.

**Stack (from web + HA ecosystem):**
- `pytest` + `pytest-asyncio` with `asyncio_mode = auto` in `pytest.ini`. `pytest-homeassistant-custom-component` **pinned to the exact core version you deploy** (see `RULES.md` §3) — unpinned it drags core to a beta.
- `conftest.py` provides `custom_integration` (wrapping `enable_custom_integrations`, opt-in rather than autouse so pure-parsing tests stay fast), plus the `portal` / `portal_client` fixtures. `portal` needs `socket_enabled` — phcc blocks sockets by default.
- `MockConfigEntry(domain=DOMAIN, data={email,password}, options={totp_secret}, entry_id="test")` + `entry.add_to_hass(hass)` before `async_setup_entry`. Don't stub `ConfigEntry` by hand.

**What to mock, what not to:**
- **Don't mock the HTTP layer.** The `portal` fixture serves a real aiohttp app and monkeypatches `api.BASE_URL` at it. A mocking library missed the `objektId` typo entirely and `aioresponses` is already incompatible with aiohttp 3.14; a real server tests actual query encoding and form posts.
- Coordinator tests use a real `Store` (`hass_storage`) **and a real recorder** (`recorder_mock`) so resume and sum bugs surface.
- **Don't mock `async_add_external_statistics`.** Let it write, then read back with `statistics_during_period` and assert on the rows: hour-aligned starts, no duplicate starts, monotonic `sum`, and the right totals per direction. Mocking it hides both the invalid-`statistic_id` rejection and the sum reset.

**Translations:** `test_translations.py` asserts `strings.json == translations/en.json` and identical key sets across `en`/`de`/`pl`, plus that every `entity.sensor.*` key is actually claimed by a sensor's `_attr_translation_key`. Add a language → add it to `LANGUAGES` and the tests enforce parity.

**Fixture invariants (fail the test if broken):**
- `day`/`day2` = **96** points, `week2` = **672** points (7×96). `sum(aggregates) == sum(data)` within `1e-6`. `kw` → `kwh` is `*0.25` (15 min), `kwh` is passthrough — test both paths.
- Keep fixtures anonymized: fake `99999`/`CH999…`/`10001` (`Wirk Lieferung`) / `10002` (`Wirk Rücklieferung`), no real `PHPSESSID`/CSRF/HTML. New fixture = synthesize `data-urls` JSON + `intervals`/`datasets` with honest math.
- Parametrize boundaries (`@pytest.mark.parametrize`) — empty `objekte`, zero-valued `Rücklieferung`, 672→ 96 split, `otpauth://` vs plain secret — instead of duplicating test functions.
- Tag slow HA tests `@pytest.mark.integration` so `pytest -m "not integration"` stays fast on every push.

**Before push:** `pytest -x` on the touched module, then full suite. No `assert True` smoke tests; every test asserts a specific outcome including one negative path.

→ Full playbook: [`testing/SKILL.md`](testing/SKILL.md)

---

## 2. Review

Apply on every diff, not only when asked to "review." Use as a checklist, not prose advice.

**Must-pass gates (request changes if violated):**

- [ ] **MFA** — `_is_2fa_page` checks path `/2fa` + `action="/2fa_check"`/`Zugangscode`, not domain substring. `_normalize_totp_secret` handles `otpauth://` URI, whitespace, hyphens, lower→upper. Secret resolved by `_totp_secret_for(entry)` with **options overriding data**, so Options can clear as well as rotate. Historic import saves `{done:false, cursors}` and pauses on `RwbMfaRequired` without a secret, otherwise re-logins and retries the chunk.
- [ ] **Discovery / CSRF** — `GET /login` extracts `_csrf_token` (`_RE_CSRF`) and preserves `PHPSESSID` in the entry's **own** cookie jar; discovered `data-urls`/`data-init` via `html.unescape`→`json.loads` cached to `_urls`. Fallbacks come from `const.py` `ENDPOINT_*` (never re-typed as literals) and are fallbacks only, not primary routing. Guards `_require_urls()` before any `get_*`.
- [ ] **Statistics contract** — `statistic_id` passes HA's `valid_statistic_id` (one `:`, lowercase — build it with `slugify`). 15-min points aggregated to **hourly** before pushing. `StatisticMetaData` carries `mean_type` + `unit_class` (not `has_mean`). `normalize_to_kwh` selects the dataset by **messlinie id**, never `datasets[0]`. `sum` seeded from the recorder via `get_last_statistics`/`statistics_during_period` — never from zero, never from a persisted cumulative. Direction from `ml_name` (`Lieferung` without `Rück` → `consumption`, `Rück` → `production`).
- [ ] **Multi-tenant** — `Store(hass, 1, f"{DOMAIN}_historic_{entry.entry_id}")` per entry, with cursors keyed **per meteringcode** inside it. Session from `async_create_clientsession` (own cookie jar), never `async_get_clientsession`. No global `objekte`. Each entry independently resumable and Monday-aligned (`_to_monday`); multiple RWB accounts must coexist.
- [ ] **Service vs polling** — `manifest.json` `integration_type: service`, `iot_class: cloud_polling`, `dependencies: ["recorder"]`. Poll via `async_track_time_change` at 03:30 local, not a rolling `update_interval`. Energy goes via `async_add_external_statistics`, not `state_class` sensors; `sensor.py` is diagnostics-only and uses `EntityCategory.DIAGNOSTIC` (the enum, not the string). No live-rate/push/webhook additions.
- [ ] **Resilience / errors** — `RwbError`/`RwbAuthError`/`RwbMfaRequired`/`RwbDiscoveryError` hierarchy; an expired session on any AJAX endpoint raises `RwbAuthError`/`RwbMfaRequired`, never "invalid JSON". `aiohttp.ClientError` wrapped, `REQUEST_TIMEOUT total=180s` mirrors portal JS. Historic import runs as `entry.async_create_background_task` under the shared `_portal_lock`. `HACS` + `pyotp` requirement intact.
- [ ] **Privacy** — No real portal HTML, credentials, or metering IDs in diff or fixtures. **No response body in an exception message** — debug log only. `rwb_credentials.txt` gitignored. Transport artifacts (`rwe.bundle`, `*.tar.gz`, `PUSH_INSTRUCTIONS`) never deleted.
- [ ] **Commits** — One layer per commit (scaffold→api→config→coordinator→sensor→test→ci→docs), independently reviewable. Author/committer is the local git identity; no AI or tool trailers.
- [ ] **No maintainer PII** — the repo is published. No real name, personal email or address in tracked files or commit messages. Two intended exceptions only: `@dobrypd` in `codeowners`, and the copyright line in `LICENSE`.

→ Full checklist with file pointers: [`review/SKILL.md`](review/SKILL.md)

---

## 3. Design

**Choose the pattern before writing code:**

| Decision | Rule for this repo |
|----------|--------------------|
| **Service, not entity platform** | Energy has no `SensorEntity` with `state_class`; push to Energy Dashboard via `async_add_external_statistics`. Diagnostics only as `CoordinatorEntity`. |
| **Polling, not push** | D+1 data, `async_track_time_change` at 03:30 local (`update_interval=None`), throttle `0.35s` between weeks. `entry.async_create_background_task(_import_full_history)` so setup never blocks, serialized against the poll by `_portal_lock`. Don't add websockets, webhooks, or sub-hour polling. |
| **Store, not yields** | Per-entry `Store` at `.storage/regionalwerke_baden_historic_<entry_id>` with `{done, cursors: {meteringcode: date}, at}`. Persist after each week and on MFA pause. Cumulative totals are **not** stored — they are re-read from the recorder, which cannot drift out of sync with what was actually written. Deleting the file is the explicit "re-import" contract. |
| **Discover, not hardcode** | Only `BASE_URL` + `LASTGANG_PATH` are constants; everything else comes from `data-urls` HTML at runtime. Fallbacks are safety nets. |
| **Thin client, thick coordinator** | `api.py` owns parsing/regex/normalization and raises typed errors; `coordinator.py` owns scheduling, retry (`UpdateFailed`/`ConfigEntryAuthFailed`), chunking, and statistics mapping. Inject the session — `async_create_clientsession(hass)` per entry, so each account gets its own cookie jar. |

**Adding a feature?** Ask: (1) which layer does it belong to? (2) does it preserve per-entry isolation and Monday-aligned cumulative sums? (3) does it stay within `cloud_polling` budget (no extra auth probes per week, no session churn)? If any answer is no, reshape before coding.

→ Full principles with sequence diagram: [`design/SKILL.md`](design/SKILL.md)

---

## References (what informed this synthesis)

- **Testing** — `pytest-expert` skill (AgentVerse): layered async fixtures, `httpx.AsyncClient` + `ASGITransport`, shared fakes, `@pytest.mark.parametrize` for boundaries, `@pytest.mark.integration` split, never assert exact non-deterministic output. `ha-meteo-imgw-pib`, `neopool-modbus`, `places` HA repos: `pytest-homeassistant-custom-component` + `MockConfigEntry` + `auto_enable_custom_integrations` + snapshot/statistics patterns.
- **Review** — `ivx-cf-code-review`, `ai-llm/agent-introspection`, `aif-security-checklist` skills: actionable checklist over generic advice, OWASP LLM Top-10 for secret handling, provider-call shape assertions both directions.
- **Design** — `harness-engineering` (LLM proposes intent, harness executes; defense in depth; agent boundaries = permission boundaries), `ha-coordinator` (DataUpdateCoordinator mandatory for polling HA integrations; single coordinated poll), `ha-integration-dev` (`cloud_polling` vs `local_push` vs `service` trade-offs).
- **Repo sources** — `PRINCIPLES.md` + `RULES.md` + `api.py` / `coordinator.py` / `config_flow.py` / `sensor.py` / `tests/fixtures/*`.

---

## Maintenance

Keep this index under 200 lines. Detail lives in `testing/`, `review/`, `design/`. When web patterns change, update the per-skill `SKILL.md` and bump this index's References.
