"""Coordinator: statistic ids, secret precedence, and the statistics it writes."""

import asyncio
import datetime as dt
import json
import pathlib

import pytest
import voluptuous as vol
from homeassistant.components.energy.data import ENERGY_SOURCE_SCHEMA
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    list_statistic_ids,
    statistics_during_period,
    valid_statistic_id,
)
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import EnergyConverter
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.regionalwerke_baden import coordinator as coordinator_module
from custom_components.regionalwerke_baden.const import (
    BACKFILL_DAYS,
    CONF_COST_ENABLED,
    CONF_COST_PRODUCT,
    CONF_COST_SURCHARGE,
    CONF_SESSION,
    CONF_TOTP_SECRET,
    DOMAIN,
)
from custom_components.regionalwerke_baden.coordinator import (
    _statistic_id,
    _to_monday,
    _totp_secret_for,
)

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
MCODE = "CH9999999999999999999999999999999"
CONSUMPTION_ID = f"{DOMAIN}:ch9999999999999999999999999999999_consumption"
HISTORY_DAYS = 21  # how far back the fake portal pretends to hold data


# -- statistic ids --


@pytest.mark.parametrize(
    ("ml_name", "expected_suffix"),
    [
        ("Wirk Lieferung", "consumption"),
        ("Wirk Rücklieferung", "return"),
        ("Blind Something", "10001"),
    ],
)
def test_statistic_id_is_valid_for_home_assistant(ml_name, expected_suffix):
    """Regression: "{domain}:{MCODE}:{direction}" had a second colon and
    uppercase, so async_add_external_statistics rejected every single push."""
    statistic_id = _statistic_id(MCODE, ml_name, "10001")
    assert valid_statistic_id(statistic_id), statistic_id
    assert statistic_id == f"{DOMAIN}:{MCODE.lower()}_{expected_suffix}"


def test_statistic_id_distinguishes_directions():
    assert _statistic_id(MCODE, "Wirk Lieferung", "1") != _statistic_id(
        MCODE, "Wirk Rücklieferung", "2"
    )


# -- TOTP secret precedence --


def test_options_secret_overrides_and_can_clear_data_secret():
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"email": "a@b.c", "password": "p", CONF_TOTP_SECRET: "FROMDATA"},
    )
    assert _totp_secret_for(entry) == "FROMDATA"

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"email": "a@b.c", "password": "p", CONF_TOTP_SECRET: "FROMDATA"},
        options={CONF_TOTP_SECRET: "FROMOPTIONS"},
    )
    assert _totp_secret_for(entry) == "FROMOPTIONS"

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"email": "a@b.c", "password": "p", CONF_TOTP_SECRET: "FROMDATA"},
        options={CONF_TOTP_SECRET: ""},
    )
    assert _totp_secret_for(entry) is None


# -- end to end against the fake portal + a real recorder --


@pytest.fixture
def rwb_portal(portal):
    """A portal serving login, discovery and two days of both messlinien."""
    day2 = json.loads((FIXTURES / "messdaten_day2_kwh.json").read_text())
    rueck = json.loads((FIXTURES / "messdaten_rueck_kw.json").read_text())

    portal.set("/login", '<input name="_csrf_token" value="tok">')
    portal.set("/login", "<html>Willkommen</html>", method="POST")
    portal.set("/lastgangdaten", (FIXTURES / "lastgang_discovery.html").read_text())
    portal.set("/lastgangdaten/getObjekte", (FIXTURES / "objekte.json").read_text())
    portal.set(
        "/lastgangdaten/getMesslinien", (FIXTURES / "messlinien.json").read_text()
    )

    # The real portal has no data before HISTORIC_EARLIEST_FALLBACK and returns an
    # empty datasets list for those dates (verified live). Mirror that, so
    # _discover_earliest terminates instead of probing back to its 2020 floor.
    history_starts = dt_util.now().date() - dt.timedelta(days=HISTORY_DAYS)

    def messdaten(request):
        """Serve the fixture day re-dated to the request.

        A week2 chunk returns seven consecutive days, as the real portal does — the
        historic import depends on that shape.
        """
        datum = dt.date.fromisoformat(request.query["datum"])
        if datum < history_starts:
            return json.dumps(
                {"chartData": {"intervals": [], "datasets": [], "unit": "kwh"}}
            )

        source = day2 if request.query["messlinieId"] == "10001" else rueck
        payload = json.loads(json.dumps(source))
        cd = payload["chartData"]
        template_intervals = cd["intervals"]
        template_data = cd["datasets"][0]["data"]

        days = 7 if request.query["zeitraum"].startswith("week") else 1
        intervals, values = [], []
        for offset in range(days):
            day = (datum + dt.timedelta(days=offset)).isoformat()
            if day > (dt_util.now().date()).isoformat():
                break
            for interval in template_intervals:
                intervals.append(
                    {k: v.replace("2026-08-29", day) for k, v in interval.items()}
                )
            values.extend(template_data)

        cd["intervals"] = intervals
        cd["datasets"][0]["data"] = values
        return json.dumps(payload)

    portal.set("/lastgangdaten/getMessdaten", messdaten)
    return portal


async def _setup_entry(hass: HomeAssistant, hass_storage) -> MockConfigEntry:
    # The portal reports Europe/Zurich offsets; run HA there too so "local midnight"
    # means the same day boundary the portal used.
    await hass.config.async_set_time_zone("Europe/Zurich")
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"email": "user@example.com", "password": "pw"},
        entry_id="e1",
    )
    entry.add_to_hass(hass)
    # Historic import already done — this exercises the daily path in isolation.
    hass_storage[f"{DOMAIN}_historic_e1"] = {
        "version": 1,
        "minor_version": 1,
        "key": f"{DOMAIN}_historic_e1",
        "data": {"done": True},
    }
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


@pytest.fixture
async def configured_entry(
    recorder_mock, hass: HomeAssistant, custom_integration, rwb_portal, hass_storage
):
    return await _setup_entry(hass, hass_storage)


async def _sums(hass: HomeAssistant) -> list[tuple[dt.datetime, float, float]]:
    await async_wait_recording_done(hass)
    stats = await hass.async_add_executor_job(
        statistics_during_period,
        hass,
        dt_util.utcnow() - dt.timedelta(days=30),
        None,
        {CONSUMPTION_ID},
        "hour",
        None,
        {"state", "sum"},
    )
    return [
        (dt_util.utc_from_timestamp(r["start"]), r["state"], r["sum"])
        for r in stats.get(CONSUMPTION_ID, [])
    ]


async def test_writes_hourly_statistics_with_a_monotonic_sum(
    recorder_mock, hass, configured_entry
):
    rows = await _sums(hass)
    assert rows, "no statistics were written"

    # Hourly, not 15-minute: the long-term statistics table is hourly by contract.
    assert all(start.minute == 0 and start.second == 0 for start, _, _ in rows)
    assert len({start for start, _, _ in rows}) == len(rows)

    sums = [s for _, _, s in rows]
    assert sums == sorted(sums), "sum must be monotonic"
    # Two days of the fixture day (8.257 kWh each).
    assert sums[-1] == pytest.approx(8.257 * 2, abs=1e-2)


async def test_daily_repush_does_not_reset_the_sum(
    recorder_mock, hass, configured_entry
):
    """Regression: the daily poll restarted every cumulative sum at 0.0, which made
    the Energy Dashboard see a huge negative step then a huge positive one."""
    before = await _sums(hass)
    peak_before = before[-1][2]

    coordinator = hass.data[DOMAIN][configured_entry.entry_id]
    await coordinator.async_refresh()
    assert coordinator.last_update_success

    after = await _sums(hass)
    sums = [s for _, _, s in after]

    assert sums == sorted(sums), f"sum went backwards on re-push: {sums[:8]}"
    assert min(sums) > 0.0
    # Same window rewritten with the same values — the total must not move or reset.
    assert after[-1][2] == pytest.approx(peak_before, abs=1e-6)
    assert len(after) == len(before)


async def test_both_directions_get_their_own_statistic(
    recorder_mock, hass, configured_entry
):
    await async_wait_recording_done(hass)
    return_id = f"{DOMAIN}:ch9999999999999999999999999999999_return"
    stats = await hass.async_add_executor_job(
        statistics_during_period,
        hass,
        dt_util.utcnow() - dt.timedelta(days=30),
        None,
        {CONSUMPTION_ID, return_id},
        "hour",
        None,
        {"sum"},
    )
    assert CONSUMPTION_ID in stats
    assert return_id in stats
    # Rücklieferung is legitimately all zeros — it must not inherit Lieferung's values.
    assert stats[return_id][-1]["sum"] == pytest.approx(0.0)
    assert stats[CONSUMPTION_ID][-1]["sum"] > 0.0


async def test_last_sync_is_populated(recorder_mock, hass, configured_entry):
    coordinator = hass.data[DOMAIN][configured_entry.entry_id]
    assert coordinator.last_sync is not None
    entity_ids = [e for e in hass.states.async_entity_ids("sensor") if "last_sync" in e]
    assert entity_ids, hass.states.async_entity_ids("sensor")
    state = hass.states.get(entity_ids[0])
    assert state.state not in ("unknown", "unavailable")


async def test_daily_push_continues_from_existing_history(
    recorder_mock, hass, custom_integration, rwb_portal, hass_storage
):
    """The real regression: with history already in the recorder, the daily poll used
    to restart the cumulative sum at 0.0 — the Energy Dashboard then saw a huge
    negative step followed by a huge positive one.

    Seeds a prior run ending at 1000 kWh, then lets the daily poll append to it.
    """
    await hass.config.async_set_time_zone("Europe/Zurich")
    seeded_total = 1000.0
    # The poll covers [yesterday - (BACKFILL_DAYS-1), yesterday].
    window_start = dt_util.start_of_local_day(
        dt_util.now().date() - dt.timedelta(days=BACKFILL_DAYS)
    )
    history_start = window_start - dt.timedelta(hours=3)

    async_add_external_statistics(
        hass,
        StatisticMetaData(
            has_sum=True,
            mean_type=StatisticMeanType.NONE,
            name="seeded",
            source=DOMAIN,
            statistic_id=CONSUMPTION_ID,
            unit_class=EnergyConverter.UNIT_CLASS,
            unit_of_measurement="kWh",
        ),
        [
            StatisticData(
                start=history_start + dt.timedelta(hours=i),
                state=1.0,
                sum=seeded_total - 2 + i,
            )
            for i in range(3)
        ],
    )
    await async_wait_recording_done(hass)

    await _setup_entry(hass, hass_storage)

    rows = await _sums(hass)
    sums = [s for _, _, s in rows]
    assert sums == sorted(sums), f"sum went backwards: {sums[:8]}"

    new_rows = [(start, s) for start, _, s in rows if start >= window_start]
    assert new_rows, "the daily poll wrote nothing"
    assert new_rows[0][1] > seeded_total, (
        f"daily poll restarted the sum at {new_rows[0][1]} instead of continuing "
        f"from {seeded_total}"
    )
    assert new_rows[-1][1] == pytest.approx(
        seeded_total + 8.257 * BACKFILL_DAYS, abs=1e-2
    )

    # And a second poll over the same window — which now overlaps rows it already
    # wrote — must still continue from the history rather than from zero.
    coordinator = hass.data[DOMAIN]["e1"]
    await coordinator.async_refresh()
    assert coordinator.last_update_success
    repushed = [s for _, _, s in await _sums(hass)]
    assert repushed == sorted(repushed)
    assert (
        min(s for start, _, s in await _sums(hass) if start >= window_start)
        > seeded_total
    )


async def test_entry_unloads_cleanly(recorder_mock, hass, configured_entry):
    """Session detach, the 03:30 schedule and the update listener all unregister."""
    assert await hass.config_entries.async_unload(configured_entry.entry_id)
    await hass.async_block_till_done()
    assert configured_entry.entry_id not in hass.data.get(DOMAIN, {})


async def test_historic_import_runs_and_records_per_meter_cursors(
    recorder_mock, hass, custom_integration, rwb_portal, hass_storage, monkeypatch
):
    """A fresh install runs the background import and stores a cursor per meteringcode."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"email": "user@example.com", "password": "pw"},
        entry_id="e2",
    )
    entry.add_to_hass(hass)
    await hass.config.async_set_time_zone("Europe/Zurich")
    # Keep the import to a couple of weeks and drop the politeness delay.
    monkeypatch.setattr(
        coordinator_module,
        "HISTORIC_EARLIEST_FALLBACK",
        (dt_util.now().date() - dt.timedelta(days=HISTORY_DAYS)).isoformat(),
    )
    monkeypatch.setattr(coordinator_module, "HISTORIC_THROTTLE", 0)
    assert await hass.config_entries.async_setup(entry.entry_id)
    # The import is a background task on the entry — wait for it, not just the loop.
    await hass.async_block_till_done(wait_background_tasks=True)
    await async_wait_recording_done(hass)

    stored = hass_storage[f"{DOMAIN}_historic_e2"]["data"]
    assert stored["done"] is True
    # Cursors are keyed by meteringcode — a single shared cursor made a second meter
    # resume at the first meter's position and skip its own history.
    assert set(stored.get("cursors", {})) == {MCODE}

    rows = await _sums(hass)
    assert rows
    sums = [s for _, _, s in rows]
    assert sums == sorted(sums), "historic import produced a non-monotonic sum"


async def test_import_pauses_when_relogin_hits_mfa(
    recorder_mock,
    hass,
    custom_integration,
    rwb_portal,
    hass_storage,
    monkeypatch,
    caplog,
):
    """Regression: a re-login that itself hits the 2FA page escaped as RwbMfaRequired.

    RwbMfaRequired subclasses RwbError, so the caller's `except RwbError: continue`
    swallowed it and the import ground through every remaining week doing futile
    logins instead of saving a cursor and stopping.
    """
    from custom_components.regionalwerke_baden.api import RwbAuthError

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"email": "user@example.com", "password": "pw"},
        entry_id="e3",
    )
    entry.add_to_hass(hass)
    await hass.config.async_set_time_zone("Europe/Zurich")
    monkeypatch.setattr(
        coordinator_module,
        "HISTORIC_EARLIEST_FALLBACK",
        (dt_util.now().date() - dt.timedelta(days=HISTORY_DAYS)).isoformat(),
    )
    monkeypatch.setattr(coordinator_module, "HISTORIC_THROTTLE", 0)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    coordinator = hass.data[DOMAIN]["e3"]
    # Session dies, and the portal now demands MFA that we cannot answer.
    calls = {"n": 0}

    async def dead_session(*args, **kwargs):
        calls["n"] += 1
        raise RwbAuthError("session expired")

    monkeypatch.setattr(coordinator._client, "get_messdaten", dead_session)
    # The 2FA page is what a re-login lands on, and no TOTP secret is stored.
    rwb_portal.set(
        "/login", '<form action="/2fa_check">Zugangscode</form>', method="POST"
    )
    assert not coordinator._client.has_totp_secret

    # Resume from a stored cursor so the run goes straight to the chunk loop
    # (a fresh run would call _discover_earliest, which fetches too).
    hass_storage[f"{DOMAIN}_historic_e3"] = {
        "version": 1,
        "key": f"{DOMAIN}_historic_e3",
        "data": {
            "done": False,
            "cursors": {
                MCODE: (dt_util.now().date() - dt.timedelta(days=14)).isoformat()
            },
        },
    }
    coordinator._historic_done = False
    await coordinator._import_full_history()

    assert "paused" in caplog.text.lower()
    stored = hass_storage[f"{DOMAIN}_historic_e3"]["data"]
    assert stored["done"] is False
    assert stored["cursors"], "a resume cursor must be persisted when pausing"
    # It stopped instead of retrying every remaining week.
    assert calls["n"] <= 2, f"kept hammering the portal: {calls['n']} fetches"


async def test_no_second_import_while_one_is_running(
    recorder_mock, hass, custom_integration, rwb_portal, hass_storage, monkeypatch
):
    """_historic_done is only set at the end, so a poll mid-import must not respawn."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"email": "user@example.com", "password": "pw"},
        entry_id="e4",
    )
    entry.add_to_hass(hass)
    await hass.config.async_set_time_zone("Europe/Zurich")
    monkeypatch.setattr(
        coordinator_module,
        "HISTORIC_EARLIEST_FALLBACK",
        (dt_util.now().date() - dt.timedelta(days=HISTORY_DAYS)).isoformat(),
    )
    monkeypatch.setattr(coordinator_module, "HISTORIC_THROTTLE", 0)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    coordinator = hass.data[DOMAIN]["e4"]
    started = []

    async def slow_import():
        started.append(1)
        await asyncio.sleep(3600)

    coordinator._historic_done = False
    monkeypatch.setattr(coordinator, "_import_full_history", slow_import)

    await coordinator.async_refresh()  # spawns the (slow) import
    await coordinator.async_refresh()  # must not spawn a second one
    await coordinator.async_refresh()
    assert len(started) == 1, f"spawned {len(started)} concurrent imports"

    coordinator._import_task.cancel()


async def test_sum_seed_survives_a_gap_before_the_window(
    recorder_mock, hass, configured_entry
):
    """Regression: _sum_before looked only at the single preceding hour, so any gap
    (a week that failed mid-import) reseeded at 0.0 and broke monotonicity."""
    coordinator = hass.data[DOMAIN][configured_entry.entry_id]
    # A statistic the daily poll does not touch, so the gap is exactly what we set.
    statistic_id = f"{DOMAIN}:ch9999999999999999999999999999999_gaptest"

    window_start = dt_util.start_of_local_day(dt_util.now().date())
    history_end = window_start - dt.timedelta(days=10)  # far wider than one hour
    async_add_external_statistics(
        hass,
        StatisticMetaData(
            has_sum=True,
            mean_type=StatisticMeanType.NONE,
            name="seeded",
            source=DOMAIN,
            statistic_id=statistic_id,
            unit_class=EnergyConverter.UNIT_CLASS,
            unit_of_measurement="kWh",
        ),
        [StatisticData(start=history_end, state=1.0, sum=500.0)],
    )
    await async_wait_recording_done(hass)

    assert await coordinator._sum_before(statistic_id, window_start) == pytest.approx(
        500.0
    )


async def test_statistics_metadata_does_not_publish_the_address(
    recorder_mock, hass, configured_entry
):
    """Regression: the statistic name was f"RWB {bezeichnung[:30]} {ml_name}", which put
    the customer's postal address into statistics_meta and the Energy Dashboard picker —
    the same leak that was fixed for the diagnostics sensor's state attributes."""
    await async_wait_recording_done(hass)
    rows = await get_instance(hass).async_add_executor_job(
        list_statistic_ids, hass, None, "sum"
    )
    ours = [r for r in rows if r["statistic_id"].startswith(f"{DOMAIN}:")]
    assert ours, [r["statistic_id"] for r in rows]

    for row in ours:
        assert "Example Street" not in row["name"], row
        assert "Anonymized Object" not in row["name"], row


async def test_statistics_are_valid_energy_dashboard_grid_sources(
    recorder_mock, hass, configured_entry
):
    """The Energy Dashboard only offers a statistic as a grid source when it is a
    kWh sum. device_class/state_class do not apply: energy/validate.py short-circuits
    on `not valid_entity_id(stat_id)`, so external statistics are judged purely on
    this metadata."""
    await async_wait_recording_done(hass)
    rows = await get_instance(hass).async_add_executor_job(
        list_statistic_ids, hass, None, "sum"
    )
    ours = {
        r["statistic_id"]: r for r in rows if r["statistic_id"].startswith(f"{DOMAIN}:")
    }
    assert set(ours) == {
        CONSUMPTION_ID,
        f"{DOMAIN}:ch9999999999999999999999999999999_return",
    }

    for row in ours.values():
        assert row["has_sum"] is True, row
        assert row["mean_type"] == StatisticMeanType.NONE, row
        assert row["unit_class"] == EnergyConverter.UNIT_CLASS, row
        assert row["statistics_unit_of_measurement"] == "kWh", row
        assert row["source"] == DOMAIN, row

    # And HA's own energy preferences schema accepts them as a grid import/export pair.
    ENERGY_SOURCE_SCHEMA(
        [
            {
                "type": "grid",
                "stat_energy_from": CONSUMPTION_ID,
                "stat_energy_to": f"{DOMAIN}:ch9999999999999999999999999999999_return",
                "cost_adjustment_day": 0.0,
            }
        ]
    )


async def test_a_fixed_price_is_rejected_for_these_statistics(
    recorder_mock, hass, configured_entry
):
    """External statistics cannot carry entity/number price — HA requires a stat_cost
    statistic instead. Documented in the README so users do not hit this in the UI."""
    with pytest.raises(vol.Invalid, match="not supported for external statistics"):
        ENERGY_SOURCE_SCHEMA(
            [
                {
                    "type": "grid",
                    "stat_energy_from": CONSUMPTION_ID,
                    "number_energy_price": 0.29,
                    "cost_adjustment_day": 0.0,
                }
            ]
        )


async def test_diagnostics_sensor_does_not_publish_the_address(
    recorder_mock, hass, configured_entry
):
    """State attributes are recorded and served over the API — the customer's postal
    address must not be among them."""
    entity_ids = [e for e in hass.states.async_entity_ids("sensor") if "metering" in e]
    assert entity_ids, hass.states.async_entity_ids("sensor")
    state = hass.states.get(entity_ids[0])

    assert state.state == "1"
    assert state.attributes["metering_points"] == [MCODE]
    blob = repr(state.attributes)
    assert "Example Street" not in blob
    assert "bezeichnung" not in blob
    assert "Anonymized Object" not in blob


# -- cost statistics --

COST_ID = f"{CONSUMPTION_ID}_cost"
# primastrom 10.50 + OL7 9.20 + Baden concession 0.58 + surcharges 2.85 Rp./kWh.
EXPECTED_RATE = 0.2313


@pytest.fixture
def tariff_portal(rwb_portal):
    """The fake portal also serving this year's published tariff file."""
    year = dt_util.now().year
    rwb_portal.set(
        f"/fileadmin/Strompreise_ElCom/Baden_tariffs_{year}.json",
        (FIXTURES / "tariffs_2026.json").read_text(),
    )
    return rwb_portal


async def _setup_cost_entry(
    hass: HomeAssistant, hass_storage, **options
) -> MockConfigEntry:
    await hass.config.async_set_time_zone("Europe/Zurich")
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"email": "user@example.com", "password": "pw"},
        options={CONF_COST_ENABLED: True, CONF_COST_SURCHARGE: 2.85, **options},
        entry_id="ecost",
    )
    entry.add_to_hass(hass)
    hass_storage[f"{DOMAIN}_historic_ecost"] = {
        "version": 1,
        "minor_version": 1,
        "key": f"{DOMAIN}_historic_ecost",
        "data": {"done": True},
    }
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    await async_wait_recording_done(hass)
    return entry


async def _cost_rows(hass: HomeAssistant, statistic_id: str = COST_ID):
    await async_wait_recording_done(hass)
    stats = await hass.async_add_executor_job(
        statistics_during_period,
        hass,
        dt_util.utcnow() - dt.timedelta(days=30),
        None,
        {statistic_id},
        "hour",
        None,
        {"state", "sum"},
    )
    return stats.get(statistic_id, [])


async def test_cost_statistic_prices_every_hour_at_the_published_tariff(
    recorder_mock, hass, custom_integration, tariff_portal, hass_storage
):
    await _setup_cost_entry(hass, hass_storage)

    assert valid_statistic_id(COST_ID), COST_ID
    energy = await _sums(hass)
    cost = await _cost_rows(hass)
    assert cost, "no cost statistics were written"
    assert len(cost) == len(energy)

    sums = [row["sum"] for row in cost]
    assert sums == sorted(sums), "cost sum must be monotonic"
    # Two fixture days of 8.257 kWh, each kWh at 23.13 Rp.
    assert sums[-1] == pytest.approx(8.257 * 2 * EXPECTED_RATE, abs=1e-3)


async def test_cost_metadata_is_a_usable_stat_cost_source(
    recorder_mock, hass, custom_integration, tariff_portal, hass_storage
):
    """HA refuses a fixed or entity price on an external statistic, so cost can only
    reach the Energy Dashboard through stat_cost — which this metadata has to satisfy."""
    await _setup_cost_entry(hass, hass_storage)

    rows = await get_instance(hass).async_add_executor_job(
        list_statistic_ids, hass, None, "sum"
    )
    meta = next(r for r in rows if r["statistic_id"] == COST_ID)
    assert meta["has_sum"] is True
    assert meta["mean_type"] == StatisticMeanType.NONE
    assert meta["source"] == DOMAIN
    assert meta["statistics_unit_of_measurement"] == hass.config.currency
    assert meta["unit_class"] is None

    ENERGY_SOURCE_SCHEMA(
        [
            {
                "type": "grid",
                "stat_energy_from": CONSUMPTION_ID,
                "stat_cost": COST_ID,
                "cost_adjustment_day": 0.0,
            }
        ]
    )


async def test_return_to_grid_is_not_priced(
    recorder_mock, hass, custom_integration, tariff_portal, hass_storage
):
    """Feed-in compensation is re-set quarterly by the BFE and is not in the tariff
    file, so pricing exported kWh at the consumption rate would invent a number."""
    await _setup_cost_entry(hass, hass_storage)
    assert not await _cost_rows(
        hass, f"{DOMAIN}:ch9999999999999999999999999999999_return_cost"
    )


async def test_cost_is_off_unless_enabled(
    recorder_mock, hass, custom_integration, tariff_portal, hass_storage
):
    await _setup_cost_entry(hass, hass_storage, **{CONF_COST_ENABLED: False})
    assert await _sums(hass), "energy must still be written"
    assert not await _cost_rows(hass)


async def test_a_year_without_a_published_tariff_still_imports_the_energy(
    recorder_mock, hass, custom_integration, rwb_portal, hass_storage, caplog
):
    """Tariff files are per-year documents and the history predates them. A missing
    year must cost nothing but the cost rows — never the kWh."""
    await _setup_cost_entry(hass, hass_storage)  # rwb_portal serves no tariff file

    assert await _sums(hass), "energy import must survive a missing tariff"
    assert not await _cost_rows(hass)
    assert "No cost for" in caplog.text


async def test_changing_the_product_in_options_reprices_the_next_run(
    recorder_mock, hass, custom_integration, tariff_portal, hass_storage
):
    """Regression risk: the rate is cached per year, so a changed option would keep
    billing at the old tariff until HA restarted."""
    entry = await _setup_cost_entry(hass, hass_storage)
    coordinator = hass.data[DOMAIN][entry.entry_id]
    assert await coordinator._rate_for_year(dt_util.now().year) == pytest.approx(
        EXPECTED_RATE
    )

    hass.config_entries.async_update_entry(
        entry,
        options={**entry.options, CONF_COST_PRODUCT: "einfachstrom"},
    )
    await hass.async_block_till_done()

    # einfachstrom is 10.20 rather than 10.50 Rp./kWh.
    assert await coordinator._rate_for_year(dt_util.now().year) == pytest.approx(
        EXPECTED_RATE - 0.003
    )


# -- failure paths: the portal is down, and stays down --


@pytest.mark.parametrize(
    ("ml_name", "expected_suffix"),
    [
        ("Wirk Lieferung", "consumption"),
        ("Wirk Rücklieferung", "return"),
        # Reactive energy also contains "Lieferung". It used to take the active
        # line's id, so both series were summed into one and consumption doubled.
        ("Blind Lieferung", "10001"),
        ("Blind Rücklieferung", "10001"),
    ],
)
def test_reactive_lines_do_not_collide_with_the_active_ones(ml_name, expected_suffix):
    stat_id = _statistic_id("CH999", ml_name, "10001")
    assert stat_id.endswith(f"_{expected_suffix}")
    if not ml_name.startswith("Wirk"):
        assert stat_id != _statistic_id("CH999", "Wirk Lieferung", "10002")


async def _historic_state(hass_storage) -> dict:
    return hass_storage[f"{DOMAIN}_historic_edown"]["data"]


async def test_a_failed_historic_import_is_not_recorded_as_done(
    recorder_mock, hass: HomeAssistant, custom_integration, rwb_portal, hass_storage
):
    """Regression: every week failing made `wrote` stay False, so no cursor was
    saved, the loop drained to yesterday, and the run still wrote done=True. The
    entry kept no history at all and never tried again."""
    await hass.config.async_set_time_zone("Europe/Zurich")
    # Only the historic week2 chunks fail; the daily poll keeps working, so the
    # entry sets up and the failure is isolated to the import. A non-JSON body is
    # what _get_json turns into RwbError.
    _, serve = rwb_portal.routes[("GET", "/lastgangdaten/getMessdaten")]

    def week_chunks_are_down(request):
        if request.query["zeitraum"].startswith("week"):
            return "<html>upstream is down</html>"
        return serve(request)

    rwb_portal.set("/lastgangdaten/getMessdaten", week_chunks_are_down)

    # Resume an import already in progress, so the run gets past _discover_earliest
    # (which probes with week2 too) and into the week loop that used to swallow
    # every failure and still finish.
    resume_from = _to_monday(dt_util.now().date() - dt.timedelta(days=14))
    hass_storage[f"{DOMAIN}_historic_edown"] = {
        "version": 1,
        "minor_version": 1,
        "key": f"{DOMAIN}_historic_edown",
        "data": {"done": False, "cursors": {MCODE: resume_from.isoformat()}},
    }

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"email": "user@example.com", "password": "pw"},
        entry_id="edown",
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    state = await _historic_state(hass_storage)
    assert state["done"] is False, "a run that imported nothing must not be 'done'"
    # The cursor must not have advanced past weeks that were never imported.
    assert state["cursors"][MCODE] == resume_from.isoformat()


async def test_a_meter_without_messlinien_leaves_the_import_open(
    recorder_mock, hass: HomeAssistant, custom_integration, rwb_portal, hass_storage
):
    """Same family: a meter whose getMesslinien came back empty was skipped, and the
    run still declared the whole history complete."""
    await hass.config.async_set_time_zone("Europe/Zurich")
    rwb_portal.set("/lastgangdaten/getMesslinien", "{}")

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"email": "user@example.com", "password": "pw"},
        entry_id="edown",
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    state = await _historic_state(hass_storage)
    assert state["done"] is False


async def test_a_transient_tariff_failure_is_retried(
    recorder_mock, hass: HomeAssistant, custom_integration, tariff_portal, hass_storage
):
    """Regression: any tariff error was cached as "no price for this year", so a
    single 502 disabled cost until Home Assistant restarted."""
    year = dt_util.now().year
    path = f"/fileadmin/Strompreise_ElCom/Baden_tariffs_{year}.json"
    tariff_portal.set(path, "bad gateway", status=502)

    await _setup_cost_entry(hass, hass_storage)
    assert not await _cost_rows(hass), "a 502 should not have produced cost rows"

    # The portal recovers; the next poll must price the energy rather than reuse
    # a cached "no tariff" answer.
    tariff_portal.set(path, (FIXTURES / "tariffs_2026.json").read_text())
    coordinator = hass.data[DOMAIN]["ecost"]
    await coordinator.async_refresh()
    assert coordinator.last_update_success

    assert await _cost_rows(hass), "cost was never retried after the portal recovered"


async def test_a_non_chf_currency_is_flagged(
    recorder_mock,
    hass: HomeAssistant,
    custom_integration,
    tariff_portal,
    hass_storage,
    caplog,
):
    """The tariff file is CHF and HA only checks the unit against the configured
    currency, so EUR would silently label CHF amounts with a euro sign."""
    hass.config.currency = "EUR"
    with caplog.at_level("WARNING"):
        await _setup_cost_entry(hass, hass_storage)

    assert "currency is EUR" in caplog.text


# -- reusing the portal session established during the config flow --


async def _setup_with_session(
    hass: HomeAssistant, hass_storage, session: dict[str, str] | None
) -> MockConfigEntry:
    await hass.config.async_set_time_zone("Europe/Zurich")
    data = {"email": "user@example.com", "password": "pw"}
    if session is not None:
        data[CONF_SESSION] = session
    entry = MockConfigEntry(domain=DOMAIN, data=data, entry_id="esess")
    entry.add_to_hass(hass)
    hass_storage[f"{DOMAIN}_historic_esess"] = {
        "version": 1,
        "minor_version": 1,
        "key": f"{DOMAIN}_historic_esess",
        "data": {"done": True},
    }
    ok = await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry, ok


async def test_an_mfa_account_without_a_totp_secret_can_set_up(
    recorder_mock, hass: HomeAssistant, custom_integration, rwb_portal, hass_storage
):
    """Regression, reported from a real deployment as
    "Failed to set up: MFA required: MFA code required".

    A one-time code is spent by the config flow. The coordinator then logged in
    again on its own, hit the 2FA page, and raised ConfigEntryAuthFailed — asking
    for a second code the user could not produce. Re-authenticating spent another
    code and looped, so a manual-MFA account could never finish setup at all.
    """
    # This portal demands MFA on every fresh login, like the reporter's account.
    rwb_portal.set(
        "/login", '<form action="/2fa_check">Zugangscode</form>', method="POST"
    )

    _, ok = await _setup_with_session(
        hass, hass_storage, {"PHPSESSID": rwb_portal.session_id}
    )

    assert ok, "setup must succeed on the session the config flow established"
    assert rwb_portal.call_count("/login", "POST") == 0, (
        "logging in again would demand a second one-time code"
    )
    assert rwb_portal.last_cookies("/lastgangdaten")["PHPSESSID"] == (
        rwb_portal.session_id
    )


async def test_a_dead_stored_session_falls_back_to_logging_in(
    recorder_mock, hass: HomeAssistant, custom_integration, rwb_portal, hass_storage
):
    """After ~30 days the PHPSESSID expires. A stale one must not wedge the entry:
    an account that can log in unattended just logs in again."""
    discovery = (FIXTURES / "lastgang_discovery.html").read_text()

    def expired_for_the_old_cookie(request):
        if request.cookies.get("PHPSESSID") == "STALE":
            return '<form><input name="_username"></form>'
        return discovery

    rwb_portal.set("/lastgangdaten", expired_for_the_old_cookie)

    _, ok = await _setup_with_session(hass, hass_storage, {"PHPSESSID": "STALE"})

    assert ok
    assert rwb_portal.call_count("/login", "POST") == 1, (
        "a dead session should be replaced by a normal login"
    )


async def test_a_fresh_login_is_saved_for_the_next_restart(
    recorder_mock, hass: HomeAssistant, custom_integration, rwb_portal, hass_storage
):
    """Whatever session the coordinator establishes is kept, so a restart reuses it
    instead of needing another code."""
    entry, ok = await _setup_with_session(hass, hass_storage, None)

    assert ok
    assert entry.data[CONF_SESSION] == {"PHPSESSID": rwb_portal.session_id}


async def test_without_a_stored_session_the_mfa_account_still_cannot_set_up(
    recorder_mock, hass: HomeAssistant, custom_integration, rwb_portal, hass_storage
):
    """The other half of the regression above: with no session to resume, the
    coordinator has to log in, the portal demands a code nobody can supply, and
    setup fails exactly as it did on the reporter's box. That contrast is what
    shows the stored session — not something else — is what fixes it."""
    rwb_portal.set(
        "/login", '<form action="/2fa_check">Zugangscode</form>', method="POST"
    )

    _, ok = await _setup_with_session(hass, hass_storage, None)

    assert not ok
    assert rwb_portal.call_count("/login", "POST") == 1
