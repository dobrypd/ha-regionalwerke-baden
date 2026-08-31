"""Live checks against the real RWB portal.

Excluded from CI: `pytest -m "not integration"`. To run:

    RWE_MFA_CODE=123456 pytest -m integration -s        # one-shot, manual code
    RWE_TOTP_SECRET=BASE32... pytest -m integration -s  # repeatable

Credentials are read from rwb_credentials.txt (gitignored). Nothing here prints a
metering code, address or objektId in full.
"""

import datetime as dt
import os
import pathlib
from zoneinfo import ZoneInfo

import aiohttp
import pytest

from custom_components.regionalwerke_baden.api import RwbClient, RwbMfaRequired
from custom_components.regionalwerke_baden.const import (
    HISTORIC_CHUNK,
    HISTORIC_EARLIEST_FALLBACK,
    ZEITRAUM_KWH,
)

pytestmark = pytest.mark.integration

# The portal's own day boundary; a UTC "today" is a day off between 00:00 and 02:00 local.
ZURICH = ZoneInfo("Europe/Zurich")

CREDENTIALS = pathlib.Path(__file__).parent.parent / "rwb_credentials.txt"


def _credentials() -> tuple[str, str]:
    if not CREDENTIALS.exists():
        pytest.skip("rwb_credentials.txt not present")
    values = {}
    for line in CREDENTIALS.read_text().splitlines():
        if ":" in line and not line.strip().startswith("#"):
            key, _, value = line.partition(":")
            values[key.strip()] = value.strip()
    return values["email"], values["password"]


@pytest.fixture
def allow_outbound_sockets():
    """Reach the real portal.

    phcc's plugin calls `socket_allow_hosts(["127.0.0.1"])`, which installs a guard on
    `socket.socket.connect`. `enable_socket()` restores `socket.socket` but *not*
    `connect`, so it does not lift that guard — only `_remove_restrictions()` does.

    pytest-socket's own `pytest_runtest_teardown` calls `_remove_restrictions()` after
    every test, so the guard is only actually in place for the first test of the
    session — which is why exactly one test used to fail while the rest passed.
    """
    import pytest_socket

    pytest_socket._remove_restrictions()
    yield
    pytest_socket.socket_allow_hosts(["127.0.0.1"])
    pytest_socket.disable_socket(allow_unix_socket=True)


@pytest.fixture
async def live_client(allow_outbound_sockets):
    email, password = _credentials()
    code = os.environ.get("RWE_MFA_CODE")
    secret = os.environ.get("RWE_TOTP_SECRET")
    if not code and not secret:
        pytest.skip("set RWE_MFA_CODE or RWE_TOTP_SECRET to run live tests")

    async with aiohttp.ClientSession() as session:
        client = RwbClient(session, email, password, totp_secret=secret)
        try:
            await client.login()
        except RwbMfaRequired:
            if not code:
                raise
            await client.submit_mfa(code)
        await client.discover()
        yield client


async def test_live_discovery_matches_the_committed_fixture(live_client):
    """The whole design rests on data-urls staying discoverable in the HTML."""
    disc = await live_client.discover()
    for key in (
        "versorger_lastgangdaten_get_objekte",
        "versorger_lastgangdaten_get_messlinien",
        "versorger_lastgangdaten_get_messdaten",
    ):
        assert key in disc.urls, f"portal no longer publishes {key}"
    assert "zeitraum" in disc.init


async def test_live_day_matches_the_portals_own_total(live_client):
    """Our kWh normalization must agree with the portal's own aggregate."""
    objekte = live_client.parse_objekte(await live_client.get_objekte())
    assert objekte, "no messpunkte on this account"
    obj = objekte[0]
    yesterday = dt.datetime.now(tz=ZURICH).date() - dt.timedelta(days=1)

    messlinien = live_client.parse_messlinien(
        await live_client.get_messlinien(obj["id"], obj["meteringcode"], yesterday)
    )
    assert messlinien, "no messlinien"

    for ml_id in messlinien:
        payload = await live_client.get_messdaten(
            obj["id"],
            obj["meteringcode"],
            ml_id,
            {obj["meteringcode"]: list(messlinien)},
            yesterday,
            zeitraum=ZEITRAUM_KWH,
        )
        points = live_client.normalize_to_kwh(payload, ml_id)
        assert len(points) in (92, 96, 100), f"unexpected interval count {len(points)}"

        portal_total = float(payload["chartData"]["aggregates"]["sum"][ml_id])
        assert sum(p.value_kwh for p in points) == pytest.approx(portal_total, abs=1e-3)

        hourly = live_client.aggregate_hourly(points)
        assert len(hourly) in (23, 24, 25)
        assert sum(h.value_kwh for h in hourly) == pytest.approx(portal_total, abs=1e-3)
        assert all(
            h.start[11:].startswith(f"{i:02d}:00:00") for i, h in enumerate(hourly)
        )


async def test_live_week_chunk_is_the_size_the_import_assumes(live_client):
    """The historic import fetches week2 chunks — confirm the shape it expects."""
    objekte = live_client.parse_objekte(await live_client.get_objekte())
    obj = objekte[0]
    yesterday = dt.datetime.now(tz=ZURICH).date() - dt.timedelta(days=1)
    messlinien = live_client.parse_messlinien(
        await live_client.get_messlinien(obj["id"], obj["meteringcode"], yesterday)
    )
    monday = yesterday - dt.timedelta(days=yesterday.weekday())
    ml_id = next(iter(messlinien))

    payload = await live_client.get_messdaten(
        obj["id"],
        obj["meteringcode"],
        ml_id,
        {obj["meteringcode"]: list(messlinien)},
        monday,
        zeitraum=HISTORIC_CHUNK,
    )
    points = live_client.normalize_to_kwh(payload, ml_id)
    assert len(points) >= 600, f"week2 returned only {len(points)} points"
    assert len(live_client.aggregate_hourly(points)) >= 150


async def test_live_history_starts_at_the_configured_fallback(live_client):
    """HISTORIC_EARLIEST_FALLBACK must still be the real start of history."""
    objekte = live_client.parse_objekte(await live_client.get_objekte())
    obj = objekte[0]
    yesterday = dt.datetime.now(tz=ZURICH).date() - dt.timedelta(days=1)
    messlinien = live_client.parse_messlinien(
        await live_client.get_messlinien(obj["id"], obj["meteringcode"], yesterday)
    )
    ml_id = next(iter(messlinien))

    async def has_data(datum: str) -> bool:
        payload = await live_client.get_messdaten(
            obj["id"],
            obj["meteringcode"],
            ml_id,
            {obj["meteringcode"]: list(messlinien)},
            datum,
            zeitraum=HISTORIC_CHUNK,
        )
        datasets = (payload.get("chartData") or {}).get("datasets") or []
        return any(d.get("data") for d in datasets)

    assert await has_data(HISTORIC_EARLIEST_FALLBACK), (
        f"{HISTORIC_EARLIEST_FALLBACK} no longer has data — update the constant"
    )
    assert not await has_data("2022-01-03"), (
        "history now reaches further back than assumed"
    )


# -- tariffs (no credentials needed: the file is published publicly by law) --


async def test_live_tariff_file_is_published_and_parses(allow_outbound_sockets):
    """The one live check that costs nothing — no login, no MFA code.

    Verified 2026-08-31: the 2026 file parses to 23.13 Rp./kWh, and 2025/2027 are
    absent, which is what _rate_for_year's per-year skip is built for.
    """
    from custom_components.regionalwerke_baden.api import (
        async_fetch_tariffs,
        parse_tariff_rate,
    )

    async with aiohttp.ClientSession() as session:
        payload = await async_fetch_tariffs(session, dt.datetime.now(tz=ZURICH).year)

    assert payload["dsoName"] == "Regionalwerke AG Baden"
    rate = parse_tariff_rate(
        payload, product="primastrom", grid_tariff="OL7", municipality="Baden"
    )
    # Sanity band, not an exact value — this is a live file that changes every year.
    assert 0.05 < rate.chf_per_kwh < 0.60, rate.components
    assert set(rate.components) == {"energy", "grid", "concession", "surcharges"}
