"""DataUpdateCoordinator — daily poll, historic full import, push to HA statistics."""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from typing import Any

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
    statistics_during_period,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.event import async_track_time_change
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util
from homeassistant.util import slugify
from homeassistant.util.unit_conversion import EnergyConverter

from .api import (
    RwbAuthError,
    RwbClient,
    RwbError,
    RwbMfaRequired,
    RwbTariffError,
    RwbTariffUnavailable,
    SeriesPoint,
    async_fetch_tariffs,
    parse_tariff_rate,
)
from .const import (
    BACKFILL_DAYS,
    CONF_COST_ENABLED,
    CONF_COST_GRID,
    CONF_COST_MUNICIPALITY,
    CONF_COST_PRODUCT,
    CONF_COST_SURCHARGE,
    CONF_SESSION,
    CONF_TOTP_SECRET,
    DEFAULT_COST_GRID,
    DEFAULT_COST_MUNICIPALITY,
    DEFAULT_COST_PRODUCT,
    DEFAULT_COST_SURCHARGE,
    DEFAULT_SCAN_HOUR,
    DEFAULT_SCAN_MINUTE,
    DOMAIN,
    HISTORIC_CHUNK,
    HISTORIC_CHUNK_DAYS,
    HISTORIC_EARLIEST_FALLBACK,
    ZEITRAUM_KW,
    ZEITRAUM_KWH,
)

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1
STORAGE_KEY = f"{DOMAIN}_historic"

# Politeness delay between historic chunk requests.
HISTORIC_THROTTLE = 0.35

# How far back to look for the preceding statistics row when re-pushing a window.
SUM_LOOKBACK_DAYS = 90


class RwbCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, session) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            # No update_interval: RWB publishes D+1, so we poll at a fixed local
            # time instead of drifting from whenever HA happened to start.
            update_interval=None,
        )
        self.entry = entry
        self._session = session
        self.last_sync: dt.datetime | None = None
        self._client = RwbClient(
            session,
            entry.data["email"],
            entry.data["password"],
            totp_secret=_totp_secret_for(entry),
        )
        self._discovered = False
        self._objekte: list[dict[str, str]] = []
        # Per-entry storage — critical for public multi-tenant HA installs
        self._store = Store(hass, STORAGE_VERSION, f"{STORAGE_KEY}_{entry.entry_id}")
        self._historic_done: bool | None = None
        # One portal session, one cookie jar: never let the historic import and the
        # daily poll interleave requests or re-login under each other.
        self._portal_lock = asyncio.Lock()
        # Running statistics sum per statistic_id, seeded from the recorder once per
        # run and advanced in memory (the recorder write is queued, so reading it
        # back immediately would race).
        self._run_sums: dict[str, float] = {}
        self._import_task: asyncio.Task | None = None
        self._cost = _cost_options(entry)
        _warn_on_currency(hass, self._cost["enabled"])
        # CHF/kWh per calendar year — the tariff file is a per-year document, and the
        # historic import spans years for which none was ever published. None means
        # "asked and there is nothing", so it is not retried for every week.
        self._tariff_rates: dict[int, float | None] = {}

        entry.async_on_unload(entry.add_update_listener(self._on_entry_update))
        entry.async_on_unload(
            async_track_time_change(
                hass,
                self._scheduled_refresh,
                hour=DEFAULT_SCAN_HOUR,
                minute=DEFAULT_SCAN_MINUTE,
                second=0,
            )
        )

    async def _scheduled_refresh(self, now: dt.datetime) -> None:
        _LOGGER.debug("Scheduled daily refresh at %s", now)
        await self.async_request_refresh()

    async def _on_entry_update(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Apply a TOTP secret changed via Options without requiring a re-add."""
        secret = _totp_secret_for(entry)
        self._client.set_totp_secret(secret)
        # Re-read the cost options and drop the cached rates: a changed product,
        # municipality or surcharge must not keep billing at the old rate.
        self._cost = _cost_options(entry)
        _warn_on_currency(self.hass, self._cost["enabled"])
        self._tariff_rates.clear()
        _LOGGER.info(
            "Config entry updated (TOTP secret: %s, cost tracking: %s)",
            "set" if secret else "cleared",
            "on" if self._cost["enabled"] else "off",
        )

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            async with self._portal_lock:
                await self._ensure_session()

                if self._historic_done is None:
                    stored = await self._store.async_load()
                    self._historic_done = bool(stored and stored.get("done"))

                data = await self._fetch_recent_and_push()

            if not self._historic_done and (
                self._import_task is None or self._import_task.done()
            ):
                # Background so setup isn't blocked for minutes; tied to the entry so
                # unloading cancels it. It takes _portal_lock like everything else.
                # _historic_done is only set when the import finishes, so without the
                # task check a poll landing mid-import would start a second one.
                self._import_task = self.entry.async_create_background_task(
                    self.hass, self._import_full_history(), f"{DOMAIN}_historic_import"
                )

            self.last_sync = dt_util.utcnow()
            return data

        except RwbMfaRequired as err:
            raise ConfigEntryAuthFailed(f"MFA required: {err}") from err
        except RwbAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except Exception as err:
            raise UpdateFailed(str(err)) from err

    async def _ensure_session(self) -> None:
        """Log in / re-login as needed. Caller must hold _portal_lock."""
        if not self._discovered:
            if not await self._resume_stored_session():
                await self._client.login()
                await self._client.discover()
                self._save_session()
            self._discovered = True
            return
        try:
            await self._client.ensure_authenticated()
        except (RwbAuthError, RwbMfaRequired):
            _LOGGER.info("Session expired, re-login")
            await self._client.login()
            await self._client.discover()
            self._save_session()

    async def _resume_stored_session(self) -> bool:
        """Adopt the portal session the config flow established, if it still works.

        A manual-MFA account cannot log in unattended: the one-time code was spent
        during the config flow. Logging in again here asked for a second code the
        user had no way to supply, so setup failed with "MFA code required" and
        re-authenticating just repeated the cycle. Reusing that session is what
        makes a manual-MFA account work at all, and it survives restarts.
        """
        if not (stored := self.entry.data.get(CONF_SESSION)):
            return False
        self._client.load_session(stored)
        try:
            await self._client.ensure_authenticated()
            await self._client.discover()
        except RwbError as err:
            _LOGGER.info(
                "Stored portal session is no longer usable (%s), logging in", err
            )
            return False
        _LOGGER.debug("Resumed the stored portal session")
        return True

    def _save_session(self) -> None:
        """Persist the current portal session so a restart does not need a new code."""
        cookies = self._client.export_session()
        if cookies and cookies != self.entry.data.get(CONF_SESSION):
            self.hass.config_entries.async_update_entry(
                self.entry, data={**self.entry.data, CONF_SESSION: cookies}
            )

    # -- recent (daily) --

    async def _fetch_recent_and_push(self) -> dict[str, Any]:
        obj_payload = await self._client.get_objekte()
        self._objekte = self._client.parse_objekte(obj_payload)
        if not self._objekte:
            _LOGGER.warning("No objects found")
            return {"objekte": []}

        self._run_sums.clear()
        summary: dict[str, Any] = {"objekte": self._objekte, "series": {}}
        end = dt_util.now().date() - dt.timedelta(days=1)
        start = end - dt.timedelta(days=BACKFILL_DAYS - 1)

        for obj in self._objekte:
            oid, mcode = obj["id"], obj["meteringcode"]
            ml_map = self._client.parse_messlinien(
                await self._client.get_messlinien(oid, mcode, end, zeitraum=ZEITRAUM_KW)
            )
            if not ml_map:
                continue

            for single in _daterange(start, end):
                for ml_id, ml_name in ml_map.items():
                    payload = await self._client.get_messdaten(
                        oid,
                        mcode,
                        ml_id,
                        {mcode: list(ml_map.keys())},
                        single,
                        zeitraum=ZEITRAUM_KWH,
                    )
                    points = self._client.normalize_to_kwh(payload, ml_id)
                    written = await self._push(mcode, ml_id, ml_name, points)
                    summary["series"].setdefault(mcode, {}).setdefault(ml_id, 0)
                    summary["series"][mcode][ml_id] += written

        return summary

    # -- historic full --

    async def _import_full_history(self) -> None:
        """Fetch every week from earliest to yesterday, pushing a monotonic sum."""
        async with self._portal_lock:
            try:
                await self._run_full_history()
            except Exception:
                _LOGGER.exception("Full historic import failed — will retry next poll")

    async def _run_full_history(self) -> None:
        _LOGGER.info("Starting full historic import")
        stored = await self._store.async_load() or {}
        # Cursors are per meteringcode: one shared cursor made a second meter resume
        # at the first meter's position and skip its own history.
        cursors: dict[str, str] = dict(stored.get("cursors") or {})

        objekte = self._client.parse_objekte(await self._client.get_objekte())
        if not objekte:
            _LOGGER.warning("Historic import: no objekte")
            await self._store.async_save({"done": True})
            self._historic_done = True
            return

        end = dt_util.now().date() - dt.timedelta(days=1)
        # Set by any skipped meter or failed week. The run is only "done" if it
        # actually imported everything: a portal outage used to drain the loop
        # with every week failing and still save done=True, leaving the entry
        # permanently empty and never retried.
        incomplete = False

        for obj in objekte:
            oid, mcode = obj["id"], obj["meteringcode"]
            ml_map = self._client.parse_messlinien(
                await self._client.get_messlinien(oid, mcode, end, zeitraum=ZEITRAUM_KW)
            )
            if not ml_map:
                _LOGGER.warning(
                    "No messlinien for %s; leaving its history for the next run",
                    mcode,
                )
                incomplete = True
                continue

            if (saved := cursors.get(mcode)) is not None:
                cur = dt.date.fromisoformat(saved)
                _LOGGER.info("Resuming historic import for %s at %s", mcode, cur)
            else:
                earliest = await self._discover_earliest(oid, mcode, ml_map)
                _LOGGER.info(
                    "Historic import earliest date for %s: %s", mcode, earliest
                )
                cur = _to_monday(earliest)

            # Sums restart from whatever the recorder already holds for this meter.
            self._run_sums.clear()

            while cur <= end:
                wrote = False
                for ml_id, ml_name in ml_map.items():
                    try:
                        payload = await self._fetch_chunk(
                            oid, mcode, ml_id, ml_map, cur
                        )
                    except _ImportPaused:
                        cursors[mcode] = cur.isoformat()
                        await self._store.async_save(
                            {"done": False, "cursors": cursors}
                        )
                        return
                    except RwbError as err:
                        _LOGGER.warning(
                            "Historic week %s %s failed: %s", cur, ml_id, err
                        )
                        incomplete = True
                        continue

                    points = [
                        p
                        for p in self._client.normalize_to_kwh(payload, ml_id)
                        if dt.datetime.fromisoformat(p.start).date() <= end
                    ]
                    written = await self._push(mcode, ml_id, ml_name, points)
                    wrote = wrote or bool(written)

                cur += dt.timedelta(days=HISTORIC_CHUNK_DAYS)
                if wrote:
                    # Persist after each week so a crash/restart resumes here.
                    cursors[mcode] = cur.isoformat()
                    await self._store.async_save({"done": False, "cursors": cursors})
                await asyncio.sleep(HISTORIC_THROTTLE)

        if incomplete:
            # Leave done=False so the next daily run resumes from the saved
            # cursors instead of declaring a half-imported history complete.
            await self._store.async_save({"done": False, "cursors": cursors})
            _LOGGER.warning(
                "Historic import finished with gaps; it will resume on the next run"
            )
            return

        await self._store.async_save(
            {"done": True, "cursors": cursors, "at": dt_util.utcnow().isoformat()}
        )
        self._historic_done = True
        _LOGGER.info("Full historic import completed")

    async def _fetch_chunk(
        self, oid: str, mcode: str, ml_id: str, ml_map: dict[str, str], cur: dt.date
    ) -> dict[str, Any]:
        """One historic week, retrying once through a re-login if the session died."""
        try:
            return await self._client.get_messdaten(
                oid,
                mcode,
                ml_id,
                {mcode: list(ml_map.keys())},
                cur,
                zeitraum=HISTORIC_CHUNK,
            )
        except (RwbAuthError, RwbMfaRequired) as err:
            self._pause_if_blocked(err, cur)
            _LOGGER.info("Historic import: re-login at %s (%s)", cur, err)
            try:
                await self._client.login()
                await self._client.discover()
            except (RwbAuthError, RwbMfaRequired) as relogin_err:
                # RwbMfaRequired subclasses RwbError, so letting this escape would hit
                # the caller's `except RwbError: continue` and grind through every
                # remaining week doing futile logins instead of saving a cursor.
                self._pause_if_blocked(relogin_err, cur)
                _LOGGER.warning(
                    "Historic import paused at %s — re-login failed: %s",
                    cur,
                    relogin_err,
                )
                raise _ImportPaused from relogin_err
            return await self._client.get_messdaten(
                oid,
                mcode,
                ml_id,
                {mcode: list(ml_map.keys())},
                cur,
                zeitraum=HISTORIC_CHUNK,
            )

    def _pause_if_blocked(self, err: RwbError, cur: dt.date) -> None:
        """Pause the import when only the user can unblock it."""
        if isinstance(err, RwbMfaRequired) and not self._client.has_totp_secret:
            _LOGGER.warning(
                "Historic import paused at %s — MFA re-required and no TOTP secret is "
                "stored. Add one in Options, or re-authenticate, to resume.",
                cur,
            )
            raise _ImportPaused from err

    async def _discover_earliest(
        self, oid: str, mcode: str, ml_map: dict[str, str]
    ) -> dt.date:
        """Find the first week with data.

        Probes whole weeks, not single days — one day of meter downtime used to be
        read as "history starts here" and truncate the import.
        """
        fallback = dt.date.fromisoformat(HISTORIC_EARLIEST_FALLBACK)
        ml_id = next(iter(ml_map))
        floor = dt.date(2020, 1, 1)

        async def has_data(day: dt.date) -> bool:
            payload = await self._client.get_messdaten(
                oid,
                mcode,
                ml_id,
                {mcode: list(ml_map.keys())},
                _to_monday(day),
                zeitraum=HISTORIC_CHUNK,
            )
            cd = payload.get("chartData") or {}
            # Datasets existing is the signal — Rücklieferung is legitimately all zeros.
            return any(ds.get("data") for ds in (cd.get("datasets") or []))

        async def narrow(good: dt.date, bad: dt.date) -> dt.date:
            """Bisect a known-good / known-empty pair down to week granularity.

            Without this the coarse step size is lost history: if data really starts
            2022-11-15, the probe at 2022-11-03 is empty and we would return the
            2023-01-02 step, silently skipping seven weeks.
            """
            while (good - bad).days > HISTORIC_CHUNK_DAYS:
                mid = bad + (good - bad) / 2
                if await has_data(mid):
                    good = mid
                else:
                    bad = mid
                await asyncio.sleep(HISTORIC_THROTTLE)
            return good

        if await has_data(fallback):
            earliest, empty = fallback, None
            cur = fallback - dt.timedelta(days=60)
            while cur >= floor:
                if not await has_data(cur):
                    empty = cur
                    break
                earliest = cur
                cur -= dt.timedelta(days=60)
                await asyncio.sleep(HISTORIC_THROTTLE)
            return await narrow(earliest, empty) if empty else earliest

        # Nothing at the fallback — walk forward for the first week that has data.
        empty = fallback
        cur = fallback + dt.timedelta(days=30)
        today = dt_util.now().date()
        while cur <= today:
            if await has_data(cur):
                return await narrow(cur, empty)
            empty = cur
            cur += dt.timedelta(days=30)
            await asyncio.sleep(HISTORIC_THROTTLE)
        return fallback

    # -- statistics --

    async def _push(
        self,
        meteringcode: str,
        ml_id: str,
        ml_name: str,
        points: list[SeriesPoint],
    ) -> int:
        """Aggregate to hourly and append to the external statistic. Returns rows written."""
        hourly = self._client.aggregate_hourly(points)
        if not hourly:
            return 0

        statistic_id = _statistic_id(meteringcode, ml_name, ml_id)
        first_start = dt.datetime.fromisoformat(hourly[0].start)

        if (running := self._run_sums.get(statistic_id)) is None:
            running = await self._sum_before(statistic_id, first_start)
            _LOGGER.debug("Seeded %s running sum at %.3f kWh", statistic_id, running)

        metadata = StatisticMetaData(
            has_sum=True,
            mean_type=StatisticMeanType.NONE,
            # Never `bezeichnung` — that is the customer's postal address, and this
            # name is persisted in statistics_meta and shown in the Energy Dashboard
            # picker. The metering code is already public in the statistic_id.
            name=f"RWB {meteringcode} {ml_name}",
            source=DOMAIN,
            statistic_id=statistic_id,
            unit_class=EnergyConverter.UNIT_CLASS,
            unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        )
        data: list[StatisticData] = []
        for point in hourly:
            running += point.value_kwh
            data.append(
                StatisticData(
                    start=dt.datetime.fromisoformat(point.start),
                    state=point.value_kwh,
                    sum=running,
                )
            )

        async_add_external_statistics(self.hass, metadata, data)
        self._run_sums[statistic_id] = running
        _LOGGER.debug(
            "Pushed %d hourly rows to %s (sum now %.3f)",
            len(data),
            statistic_id,
            running,
        )

        if self._cost["enabled"] and _direction(ml_name, ml_id) == "consumption":
            # Consumption only. What RWB pays for Rücklieferung is a feed-in
            # compensation the BFE re-sets quarterly; it is not in the tariff file and
            # is not this rate.
            await self._push_cost(statistic_id, meteringcode, ml_name, hourly)

        return len(data)

    async def _push_cost(
        self, energy_id: str, meteringcode: str, ml_name: str, hourly: list[SeriesPoint]
    ) -> None:
        """Write the matching cost statistic, so the Energy Dashboard can use stat_cost.

        External statistics cannot take a fixed or entity price — HA rejects both with
        "Use stat_cost instead" — so the only way to show cost for this data is to
        compute the money ourselves and push it as its own statistic.
        """
        priced: list[tuple[SeriesPoint, float]] = []
        for point in hourly:
            rate = await self._rate_for_year(
                dt.datetime.fromisoformat(point.start).year
            )
            if rate is not None:
                priced.append((point, rate))
        if not priced:
            return

        cost_id = f"{energy_id}_cost"
        first_start = dt.datetime.fromisoformat(priced[0][0].start)
        if (running := self._run_sums.get(cost_id)) is None:
            running = await self._sum_before(cost_id, first_start)

        data: list[StatisticData] = []
        for point, rate in priced:
            amount = point.value_kwh * rate
            running += amount
            data.append(
                StatisticData(
                    start=dt.datetime.fromisoformat(point.start),
                    state=amount,
                    sum=running,
                )
            )

        async_add_external_statistics(
            self.hass,
            StatisticMetaData(
                has_sum=True,
                mean_type=StatisticMeanType.NONE,
                name=f"RWB {meteringcode} {ml_name} cost",
                source=DOMAIN,
                statistic_id=cost_id,
                # Money is not a convertible unit class; HA stores cost statistics
                # under the instance currency, exactly as its own cost sensors do.
                unit_class=None,
                unit_of_measurement=self.hass.config.currency,
            ),
            data,
        )
        self._run_sums[cost_id] = running
        _LOGGER.debug(
            "Pushed %d cost rows to %s (sum now %.2f)", len(data), cost_id, running
        )

    async def _rate_for_year(self, year: int) -> float | None:
        """CHF/kWh for one year, fetched once. None when no usable tariff exists."""
        if year in self._tariff_rates:
            return self._tariff_rates[year]

        rate: float | None = None
        try:
            payload = await async_fetch_tariffs(self._session, year)
            parsed = parse_tariff_rate(
                payload,
                product=self._cost["product"],
                grid_tariff=self._cost["grid"],
                municipality=self._cost["municipality"],
                surcharge_rp=self._cost["surcharge"],
            )
        except RwbTariffUnavailable as err:
            # No file published for this year. That will not change today.
            _LOGGER.warning("No cost for %d — %s", year, err)
        except RwbTariffError as err:
            # Never fail the energy import over a price: the kWh are the point.
            # Not cached — a 502 or a dropped connection is worth retrying, and
            # caching it disabled cost for the whole year until HA restarted.
            _LOGGER.warning("No cost for %d — %s (will retry)", year, err)
            return None
        else:
            rate = parsed.chf_per_kwh
            _LOGGER.info(
                "Tariff %d: %.2f Rp./kWh (%s)",
                year,
                rate * 100,
                ", ".join(f"{k} {v * 100:.2f}" for k, v in parsed.components.items()),
            )

        self._tariff_rates[year] = rate
        return rate

    async def _sum_before(self, statistic_id: str, first_start: dt.datetime) -> float:
        """Running sum immediately before first_start, so re-pushes stay monotonic.

        Without this the daily poll restarted every statistic at 0 and the Energy
        Dashboard saw a huge negative step followed by a huge positive one.
        """
        recorder = get_instance(self.hass)
        last = await recorder.async_add_executor_job(
            get_last_statistics, self.hass, 1, statistic_id, True, {"sum", "start"}
        )
        rows = (last or {}).get(statistic_id)
        if not rows:
            return 0.0
        if dt_util.utc_from_timestamp(rows[0]["start"]) < first_start:
            return float(rows[0].get("sum") or 0.0)

        # We are rewriting a window that already exists — continue from the last row
        # before it. Looking only at the single preceding hour meant any gap (a week
        # that failed mid-import) reseeded at 0.0 and reintroduced the very
        # non-monotonic step this function exists to prevent.
        prior = await recorder.async_add_executor_job(
            statistics_during_period,
            self.hass,
            first_start - dt.timedelta(days=SUM_LOOKBACK_DAYS),
            first_start,
            {statistic_id},
            "hour",
            None,
            {"sum"},
        )
        found = (prior or {}).get(statistic_id)
        return float(found[-1].get("sum") or 0.0) if found else 0.0


class _ImportPaused(Exception):
    """Historic import cannot continue without user re-auth."""


def _totp_secret_for(entry: ConfigEntry) -> str | None:
    """Options win over data, so a secret set in Options can override *and* clear one."""
    if CONF_TOTP_SECRET in entry.options:
        return entry.options.get(CONF_TOTP_SECRET) or None
    return entry.data.get(CONF_TOTP_SECRET) or None


def _warn_on_currency(hass: HomeAssistant, enabled: bool) -> None:
    """The tariff file is in CHF, so any other instance currency mislabels the cost.

    HA's own validation only checks that the statistic's unit equals the configured
    currency, so an instance left on the EUR default silently shows CHF amounts
    under a euro sign.
    """
    if enabled and hass.config.currency != "CHF":
        _LOGGER.warning(
            "Cost tracking is on but the Home Assistant currency is %s. Tariffs are "
            "published in CHF, so the cost statistic will carry CHF amounts labelled "
            "%s — set the currency to CHF",
            hass.config.currency,
            hass.config.currency,
        )


def _cost_options(entry: ConfigEntry) -> dict[str, Any]:
    """Cost settings, all from Options — there is no cost setting in the setup flow."""
    opts = entry.options
    return {
        "enabled": bool(opts.get(CONF_COST_ENABLED, False)),
        "product": opts.get(CONF_COST_PRODUCT) or DEFAULT_COST_PRODUCT,
        "grid": opts.get(CONF_COST_GRID) or DEFAULT_COST_GRID,
        "municipality": opts.get(CONF_COST_MUNICIPALITY) or DEFAULT_COST_MUNICIPALITY,
        "surcharge": float(opts.get(CONF_COST_SURCHARGE, DEFAULT_COST_SURCHARGE)),
    }


def _direction(ml_name: str, ml_id: str) -> str:
    """Map a messlinie to the Energy Dashboard direction it belongs to."""
    if "Blind" in ml_name:
        # Reactive energy. Without this it matches "Lieferung" below and gets the
        # same statistic_id as the active line, so both are summed into one series
        # and consumption reads roughly double.
        return ml_id
    if "Rück" in ml_name:
        # HA calls this "Return to grid" (stat_energy_to); "production" is the Solar
        # source, and naming it that invites wiring it there and double-counting.
        return "return"
    if "Lieferung" in ml_name:
        return "consumption"
    return ml_id


def _statistic_id(meteringcode: str, ml_name: str, ml_id: str) -> str:
    """Build a valid statistic_id.

    Must match ^(?!.+__)(?!_)[\\da-z_]+(?<!_):(?!_)[\\da-z_]+(?<!_)$ — the previous
    "{domain}:{METERINGCODE}:{direction}" form had a second colon and uppercase, so
    async_add_external_statistics rejected every push with "Invalid statistic_id".
    """
    return f"{DOMAIN}:{slugify(f'{meteringcode}_{_direction(ml_name, ml_id)}')}"


def _daterange(start: dt.date, end: dt.date):
    cur = start
    while cur <= end:
        yield cur
        cur += dt.timedelta(days=1)


def _to_monday(d: dt.date) -> dt.date:
    return d - dt.timedelta(days=d.weekday())
