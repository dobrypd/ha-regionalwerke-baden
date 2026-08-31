"""RwbClient: parsing, normalization and the HTTP surface."""

import json
import pathlib
from datetime import date

import pytest

from custom_components.regionalwerke_baden.api import (
    RwbAuthError,
    RwbClient,
    RwbError,
    RwbMfaRequired,
    RwbTariffError,
    RwbTariffUnavailable,
    _normalize_totp_secret,
    async_fetch_tariffs,
    parse_tariff_rate,
)

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
MCODE = "CH9999999999999999999999999999999"


def _json(name):
    return json.loads((FIXTURES / name).read_text())


# -- discovery / parsing --


def test_discovery_parses_lastgang_html():
    from custom_components.regionalwerke_baden.api import _extract_discovered

    disc = _extract_discovered((FIXTURES / "lastgang_discovery.html").read_text())
    assert (
        disc.urls["versorger_lastgangdaten_get_objekte"] == "/lastgangdaten/getObjekte"
    )
    assert (
        disc.urls["versorger_lastgangdaten_get_messdaten"]
        == "/lastgangdaten/getMessdaten"
    )
    assert disc.init["zeitraum"] == "day"


def test_parse_objekte():
    out = RwbClient.parse_objekte(_json("objekte.json"))
    assert out == [
        {
            "id": "99999",
            "bezeichnung": "Anonymized Object, Example Street 1, 5400 Baden",
            "meteringcode": MCODE,
        }
    ]


def test_parse_messlinien():
    assert RwbClient.parse_messlinien(_json("messlinien.json")) == {
        "10001": "Wirk Lieferung",
        "10002": "Wirk Rücklieferung",
    }


# -- normalization --


def test_normalize_kw_to_kwh():
    points_kw = RwbClient.normalize_to_kwh(_json("messdaten_day_kw.json"), "10001")
    assert len(points_kw) == 96
    assert points_kw[0].value_kwh == pytest.approx(0.031)  # 0.124 kW * 0.25 h
    assert points_kw[0].start == "2026-08-29T00:00:00+02:00"

    points_kwh = RwbClient.normalize_to_kwh(_json("messdaten_day2_kwh.json"), "10001")
    assert points_kwh[0].value_kwh == pytest.approx(points_kw[0].value_kwh)


def test_normalize_selects_dataset_by_messlinie_id():
    """Never trust dataset ordering: a mismatch must yield nothing, not the wrong line."""
    payload = _json("messdaten_day_kw.json")  # holds only messlinie 10001
    assert RwbClient.normalize_to_kwh(payload, "10001")
    assert RwbClient.normalize_to_kwh(payload, "10002") == []

    rueck = _json("messdaten_rueck_kw.json")  # only 10002
    assert RwbClient.normalize_to_kwh(rueck, "10002")
    assert RwbClient.normalize_to_kwh(rueck, "10001") == []


def test_normalize_picks_the_right_line_from_a_multi_dataset_payload():
    payload = _json("messdaten_day_kw.json")
    rueck = _json("messdaten_rueck_kw.json")
    payload["chartData"]["datasets"] = [
        rueck["chartData"]["datasets"][0],
        payload["chartData"]["datasets"][0],
    ]
    lieferung = RwbClient.normalize_to_kwh(payload, "10001")
    assert sum(p.value_kwh for p in lieferung) == pytest.approx(8.257, abs=1e-3)
    assert sum(p.value_kwh for p in RwbClient.normalize_to_kwh(payload, "10002")) == 0.0


def test_aggregate_hourly_preserves_energy():
    quarters = RwbClient.normalize_to_kwh(_json("messdaten_day2_kwh.json"), "10001")
    hourly = RwbClient.aggregate_hourly(quarters)
    assert len(quarters) == 96
    assert len(hourly) == 24
    assert sum(h.value_kwh for h in hourly) == pytest.approx(
        sum(q.value_kwh for q in quarters)
    )
    assert hourly[0].start == "2026-08-29T00:00:00+02:00"
    assert all(h.start.endswith(":00:00+02:00") for h in hourly)


def test_aggregate_hourly_keeps_dst_fallback_hours_separate():
    """02:00 happens twice on the DST fall-back night; the offsets keep them distinct."""
    from custom_components.regionalwerke_baden.api import SeriesPoint

    points = [
        SeriesPoint("2026-10-25T02:00:00+02:00", "2026-10-25T02:15:00+02:00", 1.0),
        SeriesPoint("2026-10-25T02:30:00+02:00", "2026-10-25T02:45:00+02:00", 1.0),
        SeriesPoint("2026-10-25T02:00:00+01:00", "2026-10-25T02:15:00+01:00", 3.0),
    ]
    hourly = RwbClient.aggregate_hourly(points)
    assert [h.value_kwh for h in hourly] == [2.0, 3.0]


def test_normalize_handles_empty_and_malformed():
    assert RwbClient.normalize_to_kwh({}, "10001") == []
    assert (
        RwbClient.normalize_to_kwh(
            {"chartData": {"intervals": [], "datasets": []}}, "10001"
        )
        == []
    )
    assert RwbClient.aggregate_hourly([]) == []


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("abcd efgh ijkl", "ABCDEFGHIJKL"),
        ("abcd-efgh", "ABCDEFGH"),
        ("  JBSWY3DPEHPK3PXP  ", "JBSWY3DPEHPK3PXP"),
        (
            "otpauth://totp/RWB:me?secret=JBSWY3DPEHPK3PXP&issuer=RWB",
            "JBSWY3DPEHPK3PXP",
        ),
    ],
)
def test_normalize_totp_secret(raw, expected):
    assert _normalize_totp_secret(raw) == expected


# -- HTTP surface --
#
# Driven against a real local aiohttp server rather than a mocking library: this is
# what actually exercises query-string construction, which is where the objektId bug
# lived, and it does not break every time aiohttp changes its internals.


async def test_get_messdaten_sends_the_object_id(portal, portal_client):
    """Regression: the parameter was misspelled, so every fetch raised NameError."""
    portal.set(
        "/lastgangdaten/getMessdaten", json.dumps(_json("messdaten_day2_kwh.json"))
    )
    payload = await portal_client.get_messdaten(
        "99999", MCODE, "10001", {MCODE: ["10001", "10002"]}, "2026-08-29"
    )

    query = portal.last_query("/lastgangdaten/getMessdaten")
    assert query["objektId"] == "99999"
    assert query["meteringcode"] == MCODE
    assert query["messlinieId"] == "10001"
    assert query["datum"] == "2026-08-29"
    assert query["zeitraum"] == "day2"
    assert json.loads(query["messlinienIds"]) == {MCODE: ["10001", "10002"]}
    assert payload["chartData"]["unit"] == "kwh"


async def test_get_messdaten_accepts_a_date_object(portal, portal_client):
    portal.set(
        "/lastgangdaten/getMessdaten", json.dumps(_json("messdaten_day2_kwh.json"))
    )
    await portal_client.get_messdaten(
        99999, MCODE, "10001", {MCODE: ["10001"]}, date(2026, 8, 29), zeitraum="week2"
    )
    query = portal.last_query("/lastgangdaten/getMessdaten")
    assert query["objektId"] == "99999"
    assert query["datum"] == "2026-08-29"
    assert query["zeitraum"] == "week2"


async def test_get_messlinien_sends_the_object_id(portal, portal_client):
    portal.set("/lastgangdaten/getMesslinien", json.dumps(_json("messlinien.json")))
    await portal_client.get_messlinien("99999", MCODE, date(2026, 8, 29))
    query = portal.last_query("/lastgangdaten/getMesslinien")
    assert query["objektId"] == "99999"
    assert query["zeitraum"] == "day"


async def test_get_objekte_parses(portal, portal_client):
    portal.set("/lastgangdaten/getObjekte", json.dumps(_json("objekte.json")))
    payload = await portal_client.get_objekte()
    assert RwbClient.parse_objekte(payload)[0]["meteringcode"] == MCODE


async def test_expired_session_becomes_auth_error_not_bad_json(portal, portal_client):
    """A redirect to the login form must trigger reauth, not a generic update failure."""
    portal.set(
        "/lastgangdaten/getObjekte",
        '<html><form><input name="_username"><input name="_password"></form></html>',
    )
    with pytest.raises(RwbAuthError):
        await portal_client.get_objekte()


async def test_expired_mfa_becomes_mfa_required(portal, portal_client):
    portal.set(
        "/lastgangdaten/getObjekte", '<form action="/2fa_check">Zugangscode</form>'
    )
    with pytest.raises(RwbMfaRequired):
        await portal_client.get_objekte()


async def test_http_error_does_not_leak_the_body(portal, portal_client):
    """Bodies carry the customer's address and metering code — debug log only."""
    portal.set(
        "/lastgangdaten/getObjekte",
        "Ups! Ein Fehler CH12345 Example Street 1",
        status=500,
    )
    with pytest.raises(RwbError) as err:
        await portal_client.get_objekte()
    assert "Example Street" not in str(err.value)
    assert "CH12345" not in str(err.value)


async def test_invalid_json_does_not_leak_the_body(portal, portal_client):
    portal.set(
        "/lastgangdaten/getObjekte", "<html>Herr Muster, Example Street 1</html>"
    )
    with pytest.raises(RwbError) as err:
        await portal_client.get_objekte()
    assert "Example Street" not in str(err.value)
    assert "Muster" not in str(err.value)


async def test_discover_reads_data_urls(portal, portal_client):
    portal.set("/lastgangdaten", (FIXTURES / "lastgang_discovery.html").read_text())
    disc = await portal_client.discover()
    assert (
        disc.urls["versorger_lastgangdaten_get_messdaten"]
        == "/lastgangdaten/getMessdaten"
    )


async def test_login_without_mfa(portal, portal_client):
    portal.set("/login", '<input name="_csrf_token" value="tok123">')
    portal.set("/login", "<html>Willkommen</html>", method="POST")
    await portal_client.login()
    assert portal.last_form("/login")["_csrf_token"] == "tok123"
    assert portal.last_form("/login")["_username"] == "user@example.com"


async def test_login_raises_mfa_required_without_secret(portal, portal_client):
    portal.set("/login", '<input name="_csrf_token" value="tok123">')
    portal.set("/login", '<form action="/2fa_check">Zugangscode</form>', method="POST")
    with pytest.raises(RwbMfaRequired):
        await portal_client.login()


async def test_login_answers_mfa_from_stored_secret(portal, portal_client):
    import pyotp

    secret = "JBSWY3DPEHPK3PXP"
    portal_client.set_totp_secret(f"otpauth://totp/RWB:me?secret={secret}&issuer=RWB")
    assert portal_client.has_totp_secret

    portal.set("/login", '<input name="_csrf_token" value="tok123">')
    portal.set("/login", '<form action="/2fa_check">Zugangscode</form>', method="POST")
    portal.set("/2fa_check", "<html>Willkommen</html>", method="POST")

    await portal_client.login()  # must not raise RwbMfaRequired

    submitted = portal.last_form("/2fa_check")["_auth_code"]
    assert submitted == pyotp.TOTP(secret).now()


async def test_login_falls_back_to_manual_when_auto_totp_rejected(
    portal, portal_client
):
    portal_client.set_totp_secret("JBSWY3DPEHPK3PXP")
    portal.set("/login", '<input name="_csrf_token" value="tok123">')
    portal.set("/login", '<form action="/2fa_check">Zugangscode</form>', method="POST")
    portal.set("/2fa_check", "Zugangscode ungültig", method="POST")
    with pytest.raises(RwbMfaRequired):
        await portal_client.login()


async def test_bad_password_raises_auth_error(portal, portal_client):
    portal.set("/login", '<input name="_csrf_token" value="tok123">')
    portal.set(
        "/login",
        '<form><input name="_username"><input name="_password"></form>',
        method="POST",
    )
    with pytest.raises(RwbAuthError):
        await portal_client.login()


async def test_missing_csrf_raises_auth_error(portal, portal_client):
    portal.set("/login", "<html>maintenance</html>")
    with pytest.raises(RwbAuthError):
        await portal_client.login()


def test_malformed_data_urls_raises_a_discovery_error():
    """All fragile parsing must leave this layer as a typed error, not JSONDecodeError."""
    from custom_components.regionalwerke_baden.api import (
        RwbDiscoveryError,
        _extract_discovered,
    )

    with pytest.raises(RwbDiscoveryError):
        _extract_discovered('<div data-urls="{not valid json"></div>')


def test_missing_data_urls_raises_a_discovery_error():
    from custom_components.regionalwerke_baden.api import (
        RwbDiscoveryError,
        _extract_discovered,
    )

    with pytest.raises(RwbDiscoveryError):
        _extract_discovered("<div>portal redesigned</div>")


# -- tariffs --


@pytest.fixture
def tariffs(load_fixture_json):
    """The real published RWB tariff document — public data, no customer content."""
    return load_fixture_json("tariffs_2026.json")


def test_tariff_rate_sums_the_energy_proportional_components(tariffs):
    """Verified 2026-08-31 against the file RWB publishes under Art. 7b StromVV."""
    rate = parse_tariff_rate(
        tariffs,
        product="primastrom",
        grid_tariff="OL7",
        municipality="Baden",
        surcharge_rp=2.85,
    )
    assert rate.components == pytest.approx(
        {"energy": 0.105, "grid": 0.092, "concession": 0.0058, "surcharges": 0.0285}
    )
    # 23.13 Rp./kWh. ElCom publishes 27.27 for the H4 profile; the difference is the
    # CHF/month base and metering fees, which are not per-kWh and are excluded.
    assert rate.chf_per_kwh == pytest.approx(0.2313)


def test_tariff_rate_follows_the_chosen_product_and_municipality(tariffs):
    cheaper = parse_tariff_rate(
        tariffs, product="einfachstrom", grid_tariff="OL7", municipality="Ennetbaden"
    )
    standard = parse_tariff_rate(
        tariffs, product="primastrom", grid_tariff="OL7", municipality="Baden"
    )
    assert cheaper.chf_per_kwh < standard.chf_per_kwh
    assert cheaper.components["energy"] == pytest.approx(0.102)
    assert cheaper.components["concession"] == pytest.approx(0.005)


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"product": "nope"}, "einfachstrom"),
        ({"grid_tariff": "nope"}, "OL7"),
        ({"municipality": "Zürich"}, "Ennetbaden"),
    ],
)
def test_unknown_tariff_names_report_what_is_available(tariffs, kwargs, expected):
    """The options flow takes free text, so the error has to be the discovery path."""
    args = {
        "product": "primastrom",
        "grid_tariff": "OL7",
        "municipality": "Baden",
    } | kwargs
    with pytest.raises(RwbTariffError) as err:
        parse_tariff_rate(tariffs, **args)
    assert expected in str(err.value)


def test_time_of_use_windows_are_refused_rather_than_silently_mispriced(tariffs):
    """Every RWB entry is all-day today. If that ever changes, billing every hour at
    whichever window happens to be listed first would be silently wrong."""
    for tariff in tariffs["tariffs"]:
        if tariff["tariffName"] == "primastrom":
            tariff["prices"]["energy"] = [
                {"from": "07:00", "to": "20:00", "price": 0.15, "priceUnit": "CHF/kWh"},
                {"from": "20:00", "to": "07:00", "price": 0.08, "priceUnit": "CHF/kWh"},
            ]
    with pytest.raises(RwbTariffError, match="time-of-use"):
        parse_tariff_rate(
            tariffs, product="primastrom", grid_tariff="OL7", municipality="Baden"
        )


async def test_fetch_tariffs_requests_the_year_and_parses_json(portal):
    """Regression guard of the same shape as the portal tests: assert the request the
    client builds, not just the value it returns."""
    import aiohttp

    path = "/fileadmin/Strompreise_ElCom/Baden_tariffs_2026.json"
    portal.set(path, json.dumps({"dsoName": "x", "tariffs": []}))
    async with aiohttp.ClientSession() as session:
        payload = await async_fetch_tariffs(session, 2026)
    assert payload["dsoName"] == "x"
    assert portal.call_count(path, "GET") == 1


async def test_a_year_with_no_published_file_is_its_own_error(portal):
    """Tariff files are per-year documents; 2025 and 2027 both 404 today. That must be
    distinguishable from a broken file so the import can skip the year quietly."""
    import aiohttp

    async with aiohttp.ClientSession() as session:
        with pytest.raises(RwbTariffUnavailable):
            await async_fetch_tariffs(session, 2019)
