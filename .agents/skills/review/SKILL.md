# Review Checklist — regionalwerke_baden

> Companion to `SKILLS.md`. Load on every diff. Check each gate — request changes if violated.
>
> **Line anchors below are indicative, not authoritative** — they were accurate at one commit and drift. Grep for the symbol, don't trust the number. The HA-side contracts each gate depends on are recorded, with what breaks when they are violated, in [`.agents/rules/RULES.md`](../../rules/RULES.md) §2.

## 1. MFA / TOTP

- [ ] 2FA detection checks URL **path** (`path.startswith("/2fa")`) + HTML markers (`action="/2fa_check"`, `Zugangscode`), not domain substring — `api.py:88-94`
- [ ] `_normalize_totp_secret` strips `otpauth://` URI (`parse_qs` `secret`), whitespace/hyphens, uppercases — `api.py:97-109` ; covered for spaced, hyphenated, URI, lower-case inputs
- [ ] `pyotp.TOTP(secret).now()` is the only TOTP path; `totp_secret` resolved by `_totp_secret_for(entry)` where **options override data**, so Options can clear a secret captured at setup
- [ ] Options flow can rotate *and clear* the secret without re-add; coordinator listener calls `client.set_totp_secret()` (which reuses `_normalize_totp_secret` — don't re-implement the normalization inline)
- [ ] Historic import mid-stream: `RwbMfaRequired` without secret → save `{done:false, cursors}` and stop (paused); with secret → `login()` + `discover()` + retry the chunk

## 2. Discovery / CSRF / Session

- [ ] `GET /login` → `_RE_CSRF` → `POST /login` with `_username/_password/_csrf_token` + `Referer` — `api.py:26,67-69,169-186`
- [ ] `PHPSESSID` preserved in a **per-entry** cookie jar from `async_create_clientsession`; the config flow uses its own throwaway session too. `async_get_clientsession` is a bug here — its jar is global to HA. Don't call `session.close()`; HA registers `detach()` on entry unload already
- [ ] `discover()` → `GET LASTGANG_PATH` → `data-urls`/`data-init` via `html.unescape`→`json.loads` → `Discovered(urls, init)` cached to `self._urls` — `api.py:72-85,231-244` ; raises `RwbDiscoveryError` if missing
- [ ] Domain fetches use discovered keys (`versorger_lastgangdaten_get_objekte/_messlinien/_messdaten`) with `const.py` fallbacks, not hard-coded primary routes — `api.py:255,275,303`
- [ ] Headers on discovered endpoints: `Referer`, `X-Requested-With: XMLHttpRequest`, `Accept: application/json` — `api.py:256,284,316`
- [ ] `ensure_authenticated()` probes `GET LASTGANG_PATH` → `RwbAuthError` / `RwbMfaRequired`
- [ ] **Every** AJAX endpoint goes through `_get_json`, which maps a login page → `RwbAuthError` and a 2FA page → `RwbMfaRequired` *before* attempting to decode JSON. An expired session reported as "invalid JSON" means HA never offers reauth
- [ ] No response body in any exception message — `_LOGGER.debug` only (bodies carry name, address, metering code)

## 3. Monotonic sum / External statistics

- [ ] `normalize_to_kwh` maps `chartData.intervals[i].from/until` 1:1 to the data of the dataset whose `id` equals the requested `messlinieId` — **never `datasets[0]`**, or Lieferung values land in the Rücklieferung statistic. `unit==kwh` passthrough else `kw*0.25`
- [ ] `aggregate_hourly` runs before every push. HA's long-term statistics are hourly by contract; 15-minute rows are silently accepted and then mis-read. Buckets keyed on the tz-aware hour (DST fall-back safety)
- [ ] `statistic_id` built with `slugify` and passes `valid_statistic_id`: one `:`, lowercase, no `__`. `f"{DOMAIN}:{mcode}:{direction}"` is **rejected outright** — two colons and uppercase
- [ ] Metadata: `has_sum=True`, `mean_type=StatisticMeanType.NONE`, `unit_class=EnergyConverter.UNIT_CLASS`, `unit_of_measurement="kWh"`, `source=DOMAIN`. `has_mean` is deprecated — don't pass it
- [ ] Direction from `ml_name`: `Lieferung` without `Rück` → `consumption`, `Rück` → `production`
- [ ] `sum` seeded by `_sum_before()` from the recorder, then advanced in memory for the run. Never from zero, never from a persisted cumulative
- [ ] Recorder imports are **hard** — no `try/except ImportError → None` fallback. A silent fallback turned every push into a no-op on cores lacking one symbol. `manifest.json` declares `"dependencies": ["recorder"]`

## 4. Multi-tenant / Store

- [ ] Per-entry `Store(hass, 1, f"{DOMAIN}_historic_{entry.entry_id}")` → `.storage/regionalwerke_baden_historic_<entry_id>` — `coordinator.py:62`
- [ ] Shape `{done, cursors: {meteringcode: date}, at}` — cursors **per meteringcode**, not one shared cursor, or a second meter resumes at the first meter's position and skips its own history. Resume is Monday-aligned (`_to_monday`)
- [ ] No global `objekte`; no persisted cumulatives at all; multiple `ConfigEntry` can coexist
- [ ] Deleting the storage file is the documented re-import trigger — `README.md:30`

## 5. Service vs polling contract

- [ ] `manifest.json` `integration_type: service`, `iot_class: cloud_polling`, `config_flow: true`, `pyotp>=2.9.0` — `manifest.json:9,12-13`
- [ ] `update_interval=None` + `async_track_time_change(hour=DEFAULT_SCAN_HOUR, minute=DEFAULT_SCAN_MINUTE)`. A rolling 24 h interval drifts to whenever HA last restarted, and leaves `DEFAULT_SCAN_*` as dead constants contradicting the README
- [ ] `sensor.py` uses `EntityCategory.DIAGNOSTIC` (the enum — a plain string raises `ValueError` in the entity registry) and reads `coordinator.last_sync`; `DataUpdateCoordinator` has no `last_update_success_time`
- [ ] `translations/en.json` exists and matches `strings.json` — custom integrations do not load `strings.json` at runtime
- [ ] Historic: `week2` 7-day chunks, `throttle 0.35s`, background `hass.async_create_task(_import_full_history())` so daily poll returns immediately — `coordinator.py:90-91,182-253`
- [ ] Daily: `BACKFILL_DAYS=2`, `ZEITRAUM_KWH=day2`, per-day per-messlinie — `coordinator.py:104-149`
- [ ] Energy via `recorder` external statistics only; `sensor.py` diagnostics only (`rwb_last_sync`, `rwb_objekte`) — `sensor.py`

## 6. Errors / timeouts

- [ ] Hierarchy `RwbError / RwbAuthError / RwbMfaRequired(csrf) / RwbDiscoveryError` — `api.py:34-52` ; coordinator maps to `ConfigEntryAuthFailed` vs `UpdateFailed` — `coordinator.py:95-100`
- [ ] `aiohttp.ClientError` → `RwbError`, `ClientTimeout(total=180)` — `api.py:19-21,143-159` ; 500 `"Ups! Ein Fehler"` handled — `api.py:255-261`

## 7. Privacy / packaging

- [ ] No real portal HTML / credentials / metering IDs in diff; `rwb_credentials.txt` gitignored; fixtures use `99999`/`CH999…` — `tests/fixtures/*`
- [ ] Don't delete `rwe.bundle` / `*.tar.gz` / `PUSH_INSTRUCTIONS` (user property) — `RULES.md`
- [ ] Commits: one layer per commit, author/committer is the local git identity, no `Co-authored-by` / AI / tool trailer
- [ ] No maintainer PII anywhere in the diff — real name, personal email, address. Intended exceptions: `@dobrypd` in `manifest.json` `codeowners` (HACS requires it) and the `LICENSE` copyright line
