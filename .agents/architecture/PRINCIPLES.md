# Architecture Principles — regionalwerke_baden

> For LLM coding agents. High-level only — no file:line details. Repo: Home Assistant custom component for Regionalwerke AG Baden (https://www.rwb-kundenportal.ch), Energy Dashboard via external statistics.

## 1. Discover, don't hardcode
Portal routes change (`/lastgangdaten` HTML carries `data-urls`/`data-init`). Only `BASE_URL` + `LASTGANG_PATH` are stable. Parse at runtime; hard-coded endpoints will break silently. The `ENDPOINT_*` constants are fallbacks passed to `.get()`, never primary routing — and they live in `const.py` only, never re-typed as literals at the call site.

## 2. Separate concerns by layer
Scaffold (metadata/constants) → API (network/auth) → Config flow (user interaction) → Coordinator (scheduling/state) → Sensors (diagnostics) → Tests → CI → Docs. One layer per commit, independently reviewable.

## 3. Privacy by default
No real customer data in repo. Fixtures are anonymized (`99999`, `CH999…`, `10001/10002`). Credentials are gitignored (`rwb_credentials.txt`). TOTP secrets live only in HA storage. Portal responses carry the customer's name, address and metering code — they may be logged at `debug`, but must never be embedded in an exception message, which surfaces in the UI and in issue reports.

## 4. Robust MFA
Support both manual code and auto-TOTP from the same login path. Detect 2FA by URL path, not domain substring. Allow re-auth mid-operation (historic import spans ~380 requests). An expired session must surface as `RwbAuthError`/`RwbMfaRequired` — never as "invalid JSON" — or HA reports a generic update failure instead of prompting for reauth.

## 5. Incremental history, one monotonic sum
Full history is a one-time background import (week chunks, throttled, resumable via per-entry `Store`). Daily polling is delta only (2 days). The running `sum` is **seeded from the recorder** (`get_last_statistics`, falling back to `statistics_during_period` when the window overlaps rows that already exist), then advanced in memory for the rest of that run. Never restart a `sum` at zero and never persist your own cumulative totals — the recorder is the source of truth, and a re-push that resets the sum makes the Energy Dashboard show a huge negative step followed by a huge positive one.

## 6. Multi-tenant safe
Everything per `config_entry`: `entry_id`, `objektId`, `meteringcode`, `messlinien`, storage cursors **and the cookie jar**. Use `async_create_clientsession`, not `async_get_clientsession` — the shared session's jar is global to HA, so a second account or a config flow running beside the coordinator would overwrite this entry's `PHPSESSID`. Resume state is keyed per meteringcode, not per entry, or a second meter resumes at the first meter's position.

## 7. Cloud polling, D+1, at a fixed local time
Data is not live. Poll once per day at `DEFAULT_SCAN_HOUR:DEFAULT_SCAN_MINUTE` local via `async_track_time_change` — not a rolling `update_interval`, which drifts to whenever HA last restarted. Don't add live-rate or push assumptions. The historic import and the daily poll share one lock: one portal session must never carry two interleaved request streams.

## 8. Diagnostics, not energy entities
Energy values go to `recorder` external statistics for the Energy Dashboard. Sensors expose only diagnostics (`last_sync`, `objekte` count). Statistics are **hourly** by HA contract, so the portal's 15-minute intervals are summed into hourly buckets before being written.

## 9. Verify against the HA you deploy to, don't recall
Every recorder/coordinator/entity contract this integration depends on is version-sensitive and none of them fail loudly. Read the installed `homeassistant` package source, or write a test that exercises the real recorder, before asserting how an API behaves. `requirements-test.txt` pins the core version to the deployment target for exactly this reason. See `.agents/rules/RULES.md` for the contracts that have already bitten.

## 10. User property is untouchable
Untracked transport artifacts (`rwe.bundle`, `rwe_repo.tar.gz`, `PUSH_INSTRUCTIONS`) belong to the user. Never delete or clean them without explicit ask.

## 11. Authorship is user's
History author/committer is the user. No co-author trailers or AI attribution unless explicitly requested.
