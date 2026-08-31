# AGENTS.md — regionalwerke_baden

Drop-in for any coding agent. Read this before every task. Follows [agents.md](https://agents.md) standard — symlinked as `CLAUDE.md`/`GEMINI.md`.

> Stack: Home Assistant custom component `regionalwerke_baden` (`cloud_polling` + `service` + `pyotp`). Energy via `recorder` external statistics (D+1, **hourly**). Target core: **2026.8.3** (`ssh homeassistant.local cat /homeassistant/.HA_VERSION`). See [.agents/architecture/PRINCIPLES.md](.agents/architecture/PRINCIPLES.md) for high-level principles, [.agents/skills/SKILLS.md](.agents/skills/SKILLS.md) for capabilities.

---

## 0. Non-negotiables

1. **No untracked deletions.** `rwe.bundle`, `rwe_repo.tar.gz`, `PUSH_INSTRUCTIONS.md`, any `??` in `git status` are user property. Never `rm`/`git clean` them. Use `git checkout -f -B` if checkout would clobber.
2. **One layer per commit.** History is 8 commits `scaffold→api→config→coordinator→sensor→test→ci→docs`. Don't mix layers in one commit.
3. **Authorship is the maintainer's.** Commits use whatever `git config user.name` / `user.email` is already set locally — read it, never hard-code or invent an identity. Never add `Co-authored-by`, AI attribution, or tool trailers unless explicitly requested.
4. **Privacy.** This repository is intended to be published. No real portal HTML, metering IDs, or credentials — fixtures use `99999`/`CH999…`/`10001`/`10002` only. No personal data of the maintainer either: no real name, no personal email address, no home address, in any tracked file or commit message. Two deliberate exceptions: the GitHub handle `@dobrypd` (HACS requires it in `manifest.json` `codeowners`, and it is already public) and the copyright line in `LICENSE`, where the maintainer's real name is legally load-bearing. Do not "clean up" either.
5. **Verify, don't guess.** Run the code/tests before claiming done. For anything HA-side, read the installed `homeassistant` source in `.venv/` or write a test against the real recorder — the contracts under "Verified Home Assistant contracts" in [.agents/rules/RULES.md](.agents/rules/RULES.md) all fail silently.

---

## 1. Before writing code

- State plan in 1–2 sentences, list steps with verification per step.
- Read files you will touch + their callers (`custom_components/regionalwerke_baden/`, `tests/`). Use subagents to keep main context clean.
- Match existing patterns exactly (quotes, naming, layout). If project uses `pyotp.TOTP`, use it — don't invent.

---

## 2. Simplicity & surgical changes

- Minimum code that solves the task. No speculative abstractions or configurability.
- Every changed line must trace to the request. No drive-by refactors/formatting.

---

## 3. Stack

- Python, HA **`2026.8.0+`** (verified floor; `mean_type`/`unit_class`/`_get_reauth_entry` don't exist earlier), `pyotp>=2.9.0`, `aiohttp`, `DataUpdateCoordinator`.
- `manifest.json` `domain=regionalwerke_baden`, `iot_class=cloud_polling`, `integration_type=service`, `config_flow=true`, `dependencies=["recorder"]`.

---

## 4. Commands

- Install: `python3 -m venv .venv && .venv/bin/pip install -r requirements-test.txt -r requirements-lint.txt`. Pins core + `pytest-homeassistant-custom-component` **together** — unpinned, the plugin drags core to a beta.
- Test (single): `.venv/bin/pytest tests/test_api.py -k test_name -xvs`
- Test (all offline, the default): `.venv/bin/pytest -m "not integration"` (~5 s, 108 tests)
- Test (live portal): `RWE_MFA_CODE=123456 .venv/bin/pytest -m integration -s` — needs `rwb_credentials.txt` and a fresh code (or `RWE_TOTP_SECRET` to be repeatable)
- Lint: `ruff check .` and `ruff format . --check`, both green and enforced by CI. `ruff` is pinned in `requirements-lint.txt` because its **default** rule set widens between releases. There is still no ruff config — don't add one.

---

## 5. Layout

- Source: `custom_components/regionalwerke_baden/{const.py,manifest.json,__init__.py,api.py,config_flow.py,coordinator.py,sensor.py,strings.json,translations/{en,de,pl}.json}`
- Brand: `custom_components/regionalwerke_baden/brand/{icon.png,icon@2x.png}` — 256/512 px, served by HA itself; there is no entry in `home-assistant/brands`.
- CI: `.github/workflows/{validate.yml,tests.yml,lint.yml}` (hassfest + HACS, offline pytest, ruff) plus `requirements-lint.txt`, `.github/dependabot.yml` and `.github/ISSUE_TEMPLATE/`. Actions are pinned by commit SHA with a version comment — keep that form. Dependabot covers actions only: it must never bump `homeassistant` on its own, which is why `requirements-test.txt` is not listed.
- Tests: `tests/{conftest.py,test_api.py,test_config_flow.py,test_coordinator.py,test_translations.py,test_live_integration.py,fixtures/*}` + `pytest.ini`, `requirements-test.txt`. There is no `tests/fakes/`; the shared fake is the `portal` fixture in `conftest.py`.
- Helpers: `.agents/architecture/PRINCIPLES.md`, `.agents/skills/{testing,review,design}/SKILL.md`, `.agents/rules/RULES.md`
- Do not modify: `.storage/` (HA runtime), `__pycache__/`, `.venv/`.

---

## 6. Conventions

- Discovery over hardcode: only `BASE_URL`+`LASTGANG_PATH` are constants; `data-urls`/`data-init` from HTML at runtime; `ENDPOINT_*` fallbacks live in `const.py` only.
- Energy → `async_add_external_statistics`, **hourly** buckets (`aggregate_hourly`), `sum` seeded from the recorder (`_sum_before`) and never restarted at zero; `kw*0.25`; dataset selected by messlinie `id`. Diagnostics only in `sensor.py`.
- Multi-tenant: per-entry `Store(hass,1,f"regionalwerke_baden_historic_{entry_id}")` with cursors keyed per meteringcode, **and** a per-entry cookie jar via `async_create_clientsession`.
- Scheduling: `update_interval=None` + `async_track_time_change` at 03:30 local; historic import is a background task on the entry, serialized against the poll by `_portal_lock`.
- MFA: manual code vs auto-TOTP via `totp_secret`, **options override data**; `_is_2fa_page` checks path, not domain.
- Naming: domain `regionalwerke_baden`, class prefix `Rwb`, display abbreviation `RWB`. Repo `github.com/dobrypd/ha-regionalwerke-baden`. Never `rwe_*` — RWE AG is an unrelated German utility.
- i18n: `strings.json` is the source of truth and must stay byte-identical to `translations/en.json`; `de`/`pl` must carry the same key set. Entity names come from `entity.sensor.<translation_key>`, so sensors set `_attr_has_entity_name` + `_attr_translation_key` and never `_attr_name`.

---

## 7. Forbidden

- Hard-coding `getObjekte`/`getMesslinien` routes as primary (discovery is primary).
- Adding live-rate/push/webhook or sub-hour polling.
- Creating `SensorEntity` with `state_class` for energy.
- Hitting the live portal from anything not marked `integration`.
- Writing sub-hourly statistics rows, or a `statistic_id` that isn't `slugify`d.
- `try/except ImportError` around recorder imports — a silent fallback made every push a no-op.
- Mocking `async_add_external_statistics` or the HTTP layer in tests — both hid shipped bugs.
- `async_get_clientsession` (shared cookie jar) or calling `session.close()` yourself.

---

## 8. Where to look next

- Architecture principles → [.agents/architecture/PRINCIPLES.md](.agents/architecture/PRINCIPLES.md) (11 high-level rules)
- Skills index → [.agents/skills/SKILLS.md](.agents/skills/SKILLS.md) — load `testing`/`review`/`design` per task
- Rules → [.agents/rules/RULES.md](.agents/rules/RULES.md) + Project Learnings below

---

## 9. Project Learnings

> Append one concrete line per correction. Concrete > abstract.

- Never delete untracked files — use `git checkout -f -B` (learned 2026-08-30, user blocked `rm`).
- Group commits by layer; the published history is the 8 layer commits.
- Authorship is user's alone — removed `Muse Code` trailer on explicit request.
- A parameter typo (`objjekt_id`) survived every test and `py_compile` because the tests only exercised static parsers and never called the client (found 2026-08-30). Every network method needs at least one test that asserts the **request** it builds.
- Domain renamed `rwe_baden` → `regionalwerke_baden` on 2026-08-30: `rwe` collided with RWE AG, an unrelated German utility, and the brand is Regionalwerke AG Baden (portal `rwb-kundenportal.ch`). Class prefix `Rwb`.
- BSD `sed` on macOS has no `\b`; a word-boundary rename silently did nothing. Use Python `re` for identifier renames. zsh also does not word-split unquoted `$VAR` — iterate with `while IFS= read -r` (both hit 2026-08-30).
- `f"{DOMAIN}:{METERINGCODE}:{direction}"` is not a valid `statistic_id` — HA rejects two colons and uppercase, so every push raised. Build it with `slugify` and assert with `valid_statistic_id` (2026-08-30).
- HA long-term statistics are hourly; nothing validates it, so 15-minute rows were being written silently. Aggregate first (2026-08-30).
- The daily 2-day re-push restarted each `sum` at 0.0 while history held thousands of kWh. Seed from the recorder, never from a persisted total (2026-08-30).
- Mocks hid all of the above. Tests now use a real aiohttp server and a real recorder (2026-08-30).
- `pytest-homeassistant-custom-component` unpinned upgraded core to `2026.9.0b4` underneath the suite. Pin it with core, matched to the box (2026-08-30).
- `enable_socket()` does not lift pytest-socket's host guard (it lives on `socket.socket.connect`; only `_remove_restrictions()` clears it), and pytest-socket's teardown clears restrictions globally — so only the *first* test of a session is blocked. Diagnosed as fixture ordering twice before reading the source (2026-08-30).
- One-time MFA codes are scarce: verify test plumbing with an unauthenticated request first, then spend a code on the real run (2026-08-30).
- The address leak fixed in the diagnostics sensor survived in `StatisticMetaData.name` (`f"RWB {bezeichnung[:30]} {ml_name}"`), which the recorder persists in `statistics_meta` and the Energy Dashboard shows in its picker. When scrubbing a field, grep every sink, not just the one reported (2026-08-31).
- Energy Dashboard eligibility for *external* statistics has nothing to do with `device_class`/`state_class`/`unit_of_measurement` — `energy/validate.py` returns early on `not valid_entity_id(stat_id)`. Only the recorder metadata (`has_sum`, `mean_type`, `unit_class`, hourly monotonic `sum`) matters (2026-08-31).
- HA rejects `entity_energy_price`/`number_energy_price` on any external statistic ("Use stat_cost instead"), so this integration can never offer the docs' fixed/entity price options (2026-08-31).
- Export statistic renamed `_production` → `_return` on 2026-08-31: in HA, "production" is the Solar source and "Return to grid" is `stat_energy_to`; the old name invited double-counting. Safe only because nothing was deployed.
- Energy Dashboard cost comes from a *cost statistic* we compute and push (`<energy_id>_cost`, `unit_class=None`, unit `hass.config.currency`), never a price entity — HA rejects entity/number price on external statistics. Wired via `stat_cost` (2026-08-31).
- Prices come from the file RWB must publish under Art. 7b StromVV: `regionalwerke.ch/fileadmin/Strompreise_ElCom/Baden_tariffs_<year>.json`, public and unauthenticated. Do not scrape the portal or the corporate HTML for prices (2026-08-31).
- Those tariff files are per-year documents — 2025 and 2027 both 404 while 2026 is live — so rates are cached per year and a missing year silently skips cost, never the kWh (2026-08-31).
- Every RWB tariff entry is `tariffForm: "constant"` with `from == to == "00:00"`. `_constant_price` refuses anything else rather than pricing all day at the first listed window (2026-08-31).
- The tariff file omits Netzzuschlag and Förderabgabe (2.85 Rp./kWh in 2026) and its base/metering fees are CHF/month, not per kWh. File components alone = 23.13 Rp./kWh vs ElCom's published 27.27 for H4; the difference is 180.-/year of fixed fees over 4'500 kWh. Excluded on purpose (2026-08-31).
- `tests/conftest.py` points `api.TARIFF_BASE_URL` at the fake server too, so no offline test can reach regionalwerke.ch (2026-08-31).
- The offline suite passed only under `python -m pytest`, which puts the CWD on `sys.path`; bare `.venv/bin/pytest` could not import `custom_components` at all. Fixed with `pythonpath = .` in `pytest.ini` — a fresh venv is what exposed it (2026-08-31).
- Ruff 0.16.5 enables ~415 rules by **default** (RUF/S/SIM/DTZ/BLE included), not the old `E4,E7,E9,F`. Verified by running it in an empty directory: the wide rule set is not a config leaking in from somewhere (2026-08-31).
- HA serves a custom integration's brand images from `brand/` **inside the integration directory** (`loader.py` `has_branding` tests `"brand" in self._top_level_files`), not from a repo-root folder and not from `home-assistant/brands`. `logo.png` falls back to `icon.png`, so shipping `icon.png` + `icon@2x.png` is enough (2026-08-31).
- The tree was cleaned to zero ruff findings on 2026-08-31 (13 errors, 12 files reformatted) in three commits — import fix, `check` fixes, then `format` alone — so the mechanical reflow never hid a real change. The formatting commit was verified inert by comparing every file's AST against its parent (2026-08-31).
- The `.venv/` survived the `rwe` → `rwb` directory rename with stale shebangs, so every console script died with "bad interpreter". A venv is not relocatable — recreate it after a move rather than working around it with `python -m` (recreated 2026-08-31).
- aiohttp's *total* timeout raises a bare `asyncio.TimeoutError` (`helpers.py` `TimerContext`), which is **not** an `aiohttp.ClientError`. Every `except aiohttp.ClientError` needs `TimeoutError` beside it, or the timeout escapes the caller's `except RwbError` (2026-08-31).
- A skipped week or meter must mark the historic run incomplete. Draining the loop with every week failing and still saving `done: True` left the entry with no history and no retry — recoverable only by deleting the entry (2026-08-31).
- `_direction` matches substrings, so any name containing `Lieferung` collided onto the active line's `statistic_id`. Reactive lines (`Blind …`) are excluded explicitly; a colliding id silently doubles consumption (2026-08-31).
- Only `RwbTariffUnavailable` (the 404) is a permanent answer worth caching. Caching every `RwbTariffError` disabled cost for the year on one 502 until restart (2026-08-31).
- Reauth writes `entry.data`, but `_totp_secret_for` prefers `entry.options` whenever the key exists — and the options flow always writes it, `""` included. A reauth secret must be written through to options too (2026-08-31).
- `conftest`'s fake-portal route bodies may be async, which is how the timeout tests make a real client hit a real timeout without mocking the HTTP layer (2026-08-31).
- `manifest.json` keys must be `domain`, `name`, then alphabetical, or hassfest fails the build. Nothing local caught it until `tests/test_manifest.py`; HACS's own check additionally needs repository topics set, which is a GitHub setting and not in the tree (2026-08-31).
- A one-time MFA code is spent by the **config flow**. The coordinator used to `login()` again at setup, hit the 2FA page and failed with "MFA required: MFA code required" — a second code the user cannot produce, and re-auth looped. The entry carries `CONF_SESSION` (the PHPSESSID) and the coordinator resumes it; only a TOTP account can log in unattended (found on a real deployment 2026-08-31).
- Sessions are created with `aiohttp.CookieJar(unsafe=True)`. aiohttp drops cookies for IP-address hosts, so with the default jar the fake portal's PHPSESSID vanished and nothing could test session reuse. Harmless against the real domain (2026-08-31).
- Do not point the fake portal at a hostname: resolving one inside the HA test loop raises "Future attached to a different loop", and phcc's socket allowlist only has `127.0.0.1`. Keep `fake.base` on the bound IP (2026-08-31).
- Verified live 2026-08-30 against the real portal: discovery keys unchanged, `day2` = 96 points `unit=kwh` matching the portal's own aggregate, `week2` = 672, history starts `2023-01-02` and 2022 is empty — `HISTORIC_EARLIEST_FALLBACK` is correct.

---

## 10. How this file was built

Synthesizes [FerroxLabs/agents-md](https://github.com/FerroxLabs/agents-md) (Karpathy/Sean Donahoe principles) + [wkulinski/llm-skills .agents/skills/SKILL.md](https://github.com/wkulinski/llm-skills) + HA `DataUpdateCoordinator`/`MockConfigEntry` patterns.
