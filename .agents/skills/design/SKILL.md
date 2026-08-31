# Design Principles — regionalwerke_baden

> Companion to `SKILLS.md`. Load when adding a feature, changing scheduling/auth, or choosing a pattern.

## The three load-bearing decisions

### 1. Service + cloud_polling, not entity platform

RWB publishes **D+1 after midnight**. There is no live rate to stream. The integration is `integration_type: service` + `iot_class: cloud_polling` — it exists to feed the **Energy Dashboard**, not to expose `SensorEntity` with `state_class`.

```
portal (D+1) → week2/day2 kWh → async_add_external_statistics → Energy Dashboard
diagnostics  → sensor.py (rwb_last_sync, rwb_objekte) only
```

Adding a real energy `SensorEntity` would duplicate the statistics path and break the HA contract. Don't.

### 2. DataUpdateCoordinator with background historic import

```
async_setup_entry → session = async_create_clientsession(hass)   # own cookie jar
  → RwbCoordinator(hass, entry, session)
  → async_config_entry_first_refresh()
  → async_forward_entry_setups([sensor])
  → async_track_time_change(03:30 local)   # update_interval=None

_async_update_data:
  async with _portal_lock:
    login → discover → _discovered=True   (once)
    ensure_authenticated or re-login       (every poll)
    → _fetch_recent_and_push()
  if not Store.done → entry.async_create_background_task(_import_full_history())
```

- Historic is one-time, ~191 weeks × N messlinien ≈ 380+ requests, `0.35s` throttle per week — background so setup never blocks, and on the **entry** so unloading cancels it.
- `_portal_lock` serializes the import against the daily poll. One portal session, one cookie jar: two interleaved request streams (or a re-login under a running fetch) corrupt both.
- Daily delta is 2 days (`BACKFILL_DAYS=2`, `ZEITRAUM_KWH=day2`) per messlinie, chronological within the window.
- Failures map to `ConfigEntryAuthFailed` (MFA/auth) vs `UpdateFailed` (generic) and retry next poll. An expired session must reach the coordinator as a typed auth error, not as a JSON decode failure, or reauth is never offered.

### 3. Per-entry Store, not global yields

```
Store(hass, 1, f"{DOMAIN}_historic_{entry.entry_id}")
  → .storage/regionalwerke_baden_historic_<entry_id>
  shape: {done: bool, cursors: {meteringcode: "YYYY-MM-DD"}, at: iso_ts}
```

- Persist **after each week** and on MFA pause. Cursors are keyed **per meteringcode**: one shared cursor made a second meter resume at the first meter's position and skip its own history.
- Cumulative totals are deliberately **not** persisted. The running `sum` is seeded from the recorder (`get_last_statistics`, then `statistics_during_period` when the window overlaps existing rows) and advanced in memory for the rest of the run. Persisted totals can drift out of sync with what was actually written; the recorder cannot.
- HA upserts by `start`, so a re-push is safe **only** if it continues the existing sum. Restarting at zero shows up as a huge negative step then a huge positive one in the Energy Dashboard.
- No global state — multiple RWB accounts coexist, each with its own cookie jar. Deleting the file re-triggers full import (documented UX).

## Supporting principles

- **Discover, don't hardcode** — Only `BASE_URL` + `LASTGANG_PATH` are constants; `data-urls` from HTML at runtime. Fallbacks (`/lastgangdaten/getObjekte` etc.) are resilience, not primary routing.
- **Thin client, thick coordinator** — `api.py` owns regex/parsing/normalization + typed errors; `coordinator.py` owns scheduling, chunking, persistence, statistics mapping. Inject `async_create_clientsession(hass)` per entry — not `async_get_clientsession`, whose cookie jar is shared across all of HA.
- **Robust MFA** — One login path serves manual code and auto-TOTP. Detect 2FA by URL path + HTML markers. Mid-import session expiry re-logins if secret present, otherwise pauses and resumes next poll.
- **Incremental history, Monday-aligned** — `_discover_earliest` probes **whole weeks** (`week2`) in 60-day steps from `HISTORIC_EARLIEST_FALLBACK=2023-01-02`; probing single days let one day of meter downtime truncate the entire history. Loop steps `HISTORIC_CHUNK_DAYS=7` as `week2` (672 points). Monday alignment ensures week chunks don't drift.
- **Hourly statistics** — the portal is 15-minute, HA's long-term table is hourly. Aggregate before pushing (`aggregate_hourly`), keyed on the tz-aware hour so the repeated hour on the DST fall-back night stays two rows.
- **Privacy by default** — Fixtures anonymized, credentials gitignored, TOTP secrets only in HA storage.

## When to apply

| Trigger | Use this principle |
|---------|-------------------|
| New data kind (e.g. monthly aggregate) | Extend `zeitraum` + `normalize_to_kwh` (select dataset by messlinie `id`), aggregate to hourly, push via the same statistics path with a recorder-seeded sum |
| Auth/portal change | Keep `discover()` first, add HTML fixture, update regex only — don't hardcode new routes |
| Scheduling change | Stay within `cloud_polling` + one fixed daily local time; `entry.async_create_background_task` under `_portal_lock` for anything >1 request |
| New config entry field | Store per-entry; **options override data** so the Options flow can clear as well as set, add Options flow rotation |
