# Regionalwerke Baden for Home Assistant

[![GitHub Release][releases-shield]][releases]
[![Validate][validate-shield]][validate]
[![Tests][tests-shield]][tests]
[![HACS Custom][hacs-shield]][hacs]
[![Home Assistant][ha-shield]][ha]
[![License][license-shield]][license]
![Project Maintenance][maintenance-shield]

_Home Assistant integration for **Regionalwerke AG Baden** — pulls your Smart Meter load profile from [rwb-kundenportal.ch](https://www.rwb-kundenportal.ch) once a day and feeds it straight into the **Energy Dashboard**, with Swiss tariff-based cost tracking._

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.][hacs-repo-badge]][hacs-repo]

## Author's note

This project was done by coding agents, with a little help from me. Therefore please don't expect too much from it. But, I use it daily, therefore my use case should work perfectly fine.

This is not the best solution. In order to gather this data from your meter you can utilize its official interface:
see [on the rwb page](https://www.regionalwerke.ch/privat-geschaeftskunden/services/smart-meter) and [meter's manual](https://www.regionalwerke.ch/fileadmin/dok/services/rwb_smart-meter_bedienunganleitung_03_2026.pdf). Proper integration will give you live data.
Here I am proposing simpler solution, near zero time investement and zero cost. Just create an account in rwb kundenportal and fetch the data from their page. It will be just a historical data with yesterday as the newest date.
But for my analysis this is more than needed.

## Features

- **Energy Dashboard, day one.** Grid consumption and return-to-grid are written as `recorder` external statistics — hourly, cumulative, and ready to select as grid sources.
- **Full history backfill.** On first setup it walks your entire load profile back to the earliest week the portal has (verified: `2023-01-02`), resuming from a persisted cursor if HA restarts mid-import.
- **Real cost tracking.** Prices come from the tariff file Regionalwerke Baden must publish by law — no scraping, no credentials, no guessing.
- **MFA and non-MFA accounts.** Enter a 6-digit code manually, or store your TOTP secret for fully unattended logins.
- **Multi-tenant safe.** Per-entry storage, per-entry cookie jar, per-meteringcode cursors.
- **English, German and Polish** UI translations.
- **No hard-coded API routes.** Endpoints are discovered from the portal HTML at runtime.

## Requirements

| | |
| --- | --- |
| Home Assistant | **2026.8.0 or newer** (developed and tested against 2026.8.3) |
| Account | A [rwb-kundenportal.ch](https://www.rwb-kundenportal.ch) login with a Smart Meter |
| Currency | Set Home Assistant's currency to **CHF** if you want cost tracking |

The version floor is real, not cautious: the statistics metadata this integration writes (`mean_type`, `unit_class`) and the config-flow helpers it uses do not exist in earlier cores.

## Installation

### HACS (recommended)

1. Click the button below, or in HACS go to **⋮ → Custom repositories**, add `https://github.com/dobrypd/ha-regionalwerke-baden` with category **Integration**.

   [![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.][hacs-repo-badge]][hacs-repo]

2. Install **Regionalwerke Baden**.
3. Restart Home Assistant.

### Manual

1. Copy `custom_components/regionalwerke_baden/` into your `config/custom_components/` directory.
2. Restart Home Assistant.

## Configuration

Add the integration: [![Add Integration][add-integration-badge]][add-integration] — or go to **Settings → Devices & Services → Add Integration** and search for *Regionalwerke Baden*.

1. Enter your portal **email** and **password**.
2. **Optionally** paste your **TOTP secret** (a base32 string or an `otpauth://` URI from your authenticator app).
3. If your account has MFA enabled and you did not supply a secret, enter the current **6-digit code**.

**Non-MFA accounts** need nothing further — email and password are enough, fully unattended.

**MFA accounts** have two options:

| | Setup | Re-login (~30 days, when `PHPSESSID` expires) |
| --- | --- | --- |
| **Manual** (default) | Enter the 6-digit code once | A *Re-authenticate* notification accepts a fresh code or lets you switch to automatic TOTP |
| **Automatic** | Paste your TOTP secret | Nothing — `pyotp` generates the code, and even the historic import re-auths mid-stream |

The secret is stored inside your Home Assistant config entry, exactly like the password, and can be changed or cleared later under **Configure**. Options always take precedence over whatever was captured during setup.

### Options

**Settings → Devices & Services → Regionalwerke Baden → Configure**

| Option | Key | Default |
| --- | --- | --- |
| TOTP secret | `totp_secret` | _(empty)_ |
| Track energy costs | `cost_enabled` | off |
| Electricity product | `cost_product` | `primastrom` |
| Grid tariff | `cost_grid_tariff` | `OL7` |
| Municipality | `cost_municipality` | `Baden` |
| Additional surcharges (Rp./kWh) | `cost_surcharge` | `2.85` |

### Entities and statistics

After the first sync you get:

| | |
| --- | --- |
| `regionalwerke_baden:ch…_consumption` | Statistic — *Wirk Lieferung*, wire to **Grid consumption** |
| `regionalwerke_baden:ch…_return` | Statistic — *Wirk Rücklieferung*, wire to **Return to grid** |
| `regionalwerke_baden:ch…_consumption_cost` | Statistic — cost, only with cost tracking on |
| `sensor.regionalwerke_baden_last_sync` | Diagnostic entity |
| `sensor.regionalwerke_baden_metering_points` | Diagnostic entity |

Both sensors are grouped under a *Regionalwerke Baden* service device.

> [!WARNING]
> Wire the export statistic to **Return to grid**, *not* to Solar production. In Home Assistant, "production" is the Solar source, and using it here would double-count your energy.

## Energy Dashboard

Validated against [HA's electricity grid documentation](https://www.home-assistant.io/docs/energy/electricity-grid/) on HA 2026.8.3.

That page tells you to "set and provide the `device_class`, `state_class`, and `unit_of_measurement`" — **that advice is for entity-backed sensors and does not apply here.** This integration writes *external statistics*, and `homeassistant/components/energy/validate.py` short-circuits on `not valid_entity_id(stat_id)` before it ever reads those attributes. An external statistic is judged purely on its recorder metadata, which is why this integration deliberately creates no energy `SensorEntity`:

| Metadata | Value |
| --- | --- |
| `statistic_id` | `regionalwerke_baden:ch…_consumption` / `…_return` |
| `source` | `regionalwerke_baden` |
| `unit_of_measurement` / `unit_class` | `kWh` / `energy` |
| `has_sum` / `mean_type` | `True` / `StatisticMeanType.NONE` |
| bucket | hourly, monotonic cumulative `sum` |

`tests/test_coordinator.py::test_statistics_are_valid_energy_dashboard_grid_sources` asserts this against a real recorder and runs the pair through HA's own `ENERGY_SOURCE_SCHEMA`.

Statistic ids are lowercase and use a single `:` separator, because HA validates them against `^(?!.+__)(?!_)[\da-z_]+(?<!_):(?!_)[\da-z_]+(?<!_)$` and rejects anything else outright.

### Cost tracking

The docs' *"Use an entity with the current price"* and *"Use a fixed price"* options **cannot be used with this integration.** HA rejects both for any external statistic:

> Entity or number price is not supported for external statistics. Use `stat_cost` instead

So the integration computes the money itself and writes a **cost statistic** you select under *"Use a statistic"*. Turn on **Track energy costs** in **Configure**, then set `stat_cost` to `regionalwerke_baden:ch…_consumption_cost`.

Prices come from the tariff file Regionalwerke Baden is legally obliged to publish — Art. 7b StromVV requires every Swiss grid operator to put all of its tariffs in one machine-readable JSON document at a public address:

```
https://www.regionalwerke.ch/fileadmin/Strompreise_ElCom/Baden_tariffs_<year>.json
```

Nothing is scraped and no credentials are involved. Every entry in that file is `tariffForm: "constant"` with `from == to == "00:00"`, so RWB has no Hochtarif/Niedertarif and each hour is priced at one flat rate. If RWB ever publishes real time-of-use windows the integration refuses the tariff rather than billing every hour at whichever window is listed first.

The rate is the sum of four energy-proportional components (2026 standard household, verified against the live file):

| Component | Option | 2026 |
| --- | --- | --- |
| Energy product | `cost_product` (`primastrom`) | 10.50 Rp./kWh |
| Grid usage | `cost_grid_tariff` (`OL7`) | 9.20 Rp./kWh |
| Concession fee | `cost_municipality` (`Baden`) | 0.58 Rp./kWh |
| Netzzuschlag + Förderabgabe | `cost_surcharge` | 2.85 Rp./kWh |
| **Total** | | **23.13 Rp./kWh** |

Get a name wrong and the log tells you what the file actually offers (`No electricity tariff named 'x'. Available: einfachstrom, primastrom, …`).

<details>
<summary><b>Four things cost tracking deliberately does not do</b></summary>

- **Fixed monthly fees are excluded.** The file also carries CHF/month base and metering charges (10.- + 5.-). Those are not per-kWh, so folding them into an hourly statistic would mean inventing an amortisation. This is the whole gap against ElCom's published H4 figure: 23.13 + 180.-/year over 4'500 kWh (4.00 Rp./kWh) = 27.13 ≈ **27.27 Rp./kWh**. If you want your bill total rather than the marginal cost of a kWh, add your own share to `cost_surcharge`.
- **The surcharges are not in the file.** Netzzuschlag (2.30) and Förderabgabe (0.55) are charged on every kWh but RWB does not publish them there, so they are an option with a 2026 default. Check it when the year rolls over.
- **Return to grid is not priced.** Feed-in compensation is re-set quarterly by the BFE against a reference market price and is not in the tariff file; pricing exported kWh at the consumption rate would be a made-up number.
- **Years without a published file get no cost rows.** These are per-year documents — 2025 and 2027 both 404 today — so the backfill prices only the years RWB actually published. The kWh are imported either way; a missing tariff never fails the energy import.

</details>

Costs are recorded in `hass.config.currency`, matching HA's own cost sensors, so set your HA currency to CHF.

## How it works

<details>
<summary><b>Energy data shape</b></summary>

The portal serves 15-minute intervals. HA's long-term statistics table is **hourly** by contract, so the four quarter-hours of each hour are summed before being written — total energy is unchanged, and a 3.7-year backfill costs ~65k rows instead of ~260k. The repeated wall-clock hour on the DST fall-back night stays two distinct rows, because the buckets are keyed on the tz-aware hour.

Each push looks up the running `sum` already stored for that statistic and continues from it, so the daily re-push of the last two days never resets the Energy Dashboard to zero.

</details>

<details>
<summary><b>First load — full historic import</b></summary>

On first successful setup a background task (tied to the config entry, so unloading cancels it):

- Discovers `objektId` / `meteringcode` / `messlinien` per object (multi-tenant safe, per-entry storage and per-entry cookie jar).
- Probes the earliest week with data (`2023-01-02` verified; 60-day steps back, fallback `HISTORIC_EARLIEST_FALLBACK`). Whole weeks are probed rather than single days, so one day of meter downtime cannot truncate the history.
- Iterates `Mon earliest → yesterday` in `week2` chunks (672 × 15-min points/week, `0.35 s` throttle), persisting a **per-meteringcode** cursor in `Store(.../regionalwerke_baden_historic_<entry_id>)` after every week so a restart resumes exactly where it stopped.
- Holds the same lock as the daily poll, so the two never interleave requests on one portal session.

Delete `.storage/regionalwerke_baden_historic_<entry_id>` and restart to re-trigger.

</details>

<details>
<summary><b>Daily polling</b></summary>

Runs at **03:30 local time** (`DEFAULT_SCAN_HOUR` / `DEFAULT_SCAN_MINUTE`) via `async_track_time_change`, plus once at startup — not a rolling 24 h interval, which would drift to whenever HA was last restarted. Each run fetches the last `BACKFILL_DAYS` (2) days as `day2` kWh, with the `kw → ×0.25` conversion applied when the portal answers in kW.

</details>

## Troubleshooting

| Symptom | What to do |
| --- | --- |
| *MFA code required* keeps coming back | Supply a fresh code or replacement TOTP secret via the re-auth notification, or store a TOTP secret under **Configure** to silence it for good. |
| No cost statistic appears | Check the log — a wrong tariff name is reported with the list of names the file actually offers. A year with no published tariff file silently skips cost, never the kWh. |
| Energy Dashboard shows nothing | The first sync must complete. Data is D+1, so the newest hour you will ever see is yesterday's. |
| History looks truncated | Delete `.storage/regionalwerke_baden_historic_<entry_id>` and restart to re-run the full import. |

RWB data is D+1 (there is no live rate); `unit=kwh` vs `kw` is auto-converted, and week/month/year are aggregated via `week2` / `month2` / `year2` where needed.

## Privacy

- `tests/fixtures/` uses **anonymized** IDs (`99999`, `CH999…`, `10001/10002`) and `Anonymized Object …` — no real customer data is committed.
- Never commit `rwb_credentials.txt` (gitignored). Use one-time MFA codes from your app; TOTP secrets are only stored inside HA.
- Portal responses can contain your name, address and metering code, so they are only ever written to the log at `debug` level and are never included in exception messages. The address (`bezeichnung`) is also kept out of everything the recorder persists — neither the diagnostics sensor's state attributes nor the statistics metadata `name` carry it; both are regression-tested. Only the metering code appears, and it is already public in the statistic id.

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-test.txt
.venv/bin/pytest -m "not integration"   # full offline suite
.venv/bin/pytest tests/test_api.py -k messdaten -xvs
```

`requirements-test.txt` pins `homeassistant` and `pytest-homeassistant-custom-component` together — the plugin pins an exact core version, so bump both at once and keep them matched to the HA you deploy to.

Tests run the client against a real local `aiohttp` server rather than a mocking library, so query-string construction and form posts are genuinely exercised; the coordinator tests run against a real recorder and assert on the statistics actually written.

UI translations ship for **English**, **German** and **Polish** (`custom_components/regionalwerke_baden/translations/`). `strings.json` is the source and must stay identical to `translations/en.json`; `tests/test_translations.py` enforces that and key parity across languages.

## Contributing

Issues and pull requests are welcome at [ha-regionalwerke-baden][repo]. Please read [AGENTS.md](AGENTS.md) first — it documents the conventions this repository is held to.

## License

[MIT](LICENSE) — maintained by [@dobrypd](https://github.com/dobrypd).

---

[repo]: https://github.com/dobrypd/ha-regionalwerke-baden
[releases]: https://github.com/dobrypd/ha-regionalwerke-baden/releases
[releases-shield]: https://img.shields.io/github/release/dobrypd/ha-regionalwerke-baden.svg?style=for-the-badge
[validate]: https://github.com/dobrypd/ha-regionalwerke-baden/actions/workflows/validate.yml
[validate-shield]: https://img.shields.io/github/actions/workflow/status/dobrypd/ha-regionalwerke-baden/validate.yml?branch=main&label=validate&style=for-the-badge
[tests]: https://github.com/dobrypd/ha-regionalwerke-baden/actions/workflows/tests.yml
[tests-shield]: https://img.shields.io/github/actions/workflow/status/dobrypd/ha-regionalwerke-baden/tests.yml?branch=main&label=tests&style=for-the-badge
[hacs]: https://github.com/hacs/integration
[hacs-shield]: https://img.shields.io/badge/HACS-Custom-orange.svg?style=for-the-badge
[ha]: https://www.home-assistant.io
[ha-shield]: https://img.shields.io/badge/Home%20Assistant-2026.8.0%2B-41BDF5.svg?style=for-the-badge&logo=homeassistant&logoColor=white
[license]: LICENSE
[license-shield]: https://img.shields.io/badge/license-MIT-blue.svg?style=for-the-badge
[maintenance-shield]: https://img.shields.io/badge/maintainer-%40dobrypd-blue.svg?style=for-the-badge
[hacs-repo]: https://my.home-assistant.io/redirect/hacs_repository/?owner=dobrypd&repository=ha-regionalwerke-baden&category=integration
[hacs-repo-badge]: https://my.home-assistant.io/badges/hacs_repository.svg
[add-integration]: https://my.home-assistant.io/redirect/config_flow_start/?domain=regionalwerke_baden
[add-integration-badge]: https://my.home-assistant.io/badges/config_flow_start.svg
