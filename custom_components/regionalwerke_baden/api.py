"""Async API client for RWB Kundenportal.

All fragile parsing isolated here. Endpoints are discovered from HTML
(data-urls) so route changes don't brick the integration; only BASE_URL
and LASTGANG_PATH are constants. Session handling + MFA re-auth is
explicit to stay robust for years.
"""

from __future__ import annotations

import html
import json
import logging
import re
from dataclasses import dataclass
from datetime import date
from datetime import datetime as dt_datetime
from typing import Any
from urllib.parse import parse_qs, urlparse

import aiohttp
from aiohttp import ClientResponse

from .const import (
    BASE_URL,
    ENDPOINT_MESSDATEN,
    ENDPOINT_MESSLINIEN,
    ENDPOINT_OBJEKTE,
    LASTGANG_PATH,
    LOGIN_PATH,
    TARIFF_BASE_URL,
    TARIFF_PATH_TEMPLATE,
    TFA_PATH,
)

_LOGGER = logging.getLogger(__name__)

_RE_CSRF = re.compile(r'name="_csrf_token" value="([^"]+)"')
_RE_DATA_URLS = re.compile(r'data-urls="([^"]+)"')
_RE_DATA_INIT = re.compile(r'data-init="([^"]+)"')

# Mirrors the JS timeout (18e4 ms) — generous for RWB's PHP backend.
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=180)


class RwbError(Exception):
    """Base error."""


class RwbAuthError(RwbError):
    """Auth failed (bad password / expired session)."""


class RwbMfaRequired(RwbError):
    """Login succeeded but MFA code required."""

    def __init__(self, message: str, csrf: str | None = None) -> None:
        super().__init__(message)
        self.csrf = csrf


class RwbDiscoveryError(RwbError):
    """Could not discover dynamic URLs."""


class RwbTariffError(RwbError):
    """The published tariff file is missing a needed price, or is unusable."""


class RwbTariffUnavailable(RwbTariffError):
    """No tariff file is published for that year (they are per-year documents)."""


@dataclass(frozen=True)
class Discovered:
    urls: dict[str, str]
    init: dict[str, Any]


@dataclass(frozen=True)
class TariffRate:
    """A flat CHF/kWh price, plus the components it was summed from (for the log)."""

    chf_per_kwh: float
    components: dict[str, float]


@dataclass(frozen=True)
class SeriesPoint:
    start: str  # ISO8601 with offset
    end: str
    value_kwh: float  # always kWh, normalized from kw if needed


def _extract_csrf(html_text: str) -> str | None:
    m = _RE_CSRF.search(html_text)
    return m.group(1) if m else None


def _extract_discovered(html_text: str) -> Discovered:
    m = _RE_DATA_URLS.search(html_text)
    if not m:
        raise RwbDiscoveryError("data-urls not found in /lastgangdaten")
    raw = html.unescape(m.group(1))
    try:
        urls = json.loads(raw)
    except json.JSONDecodeError as err:
        # All fragile parsing is meant to raise typed errors from this layer; a bare
        # JSONDecodeError escaping here surfaces as a generic UpdateFailed / "unknown".
        raise RwbDiscoveryError("data-urls is not valid JSON") from err
    init: dict[str, Any] = {}
    m2 = _RE_DATA_INIT.search(html_text)
    if m2:
        try:
            init = json.loads(html.unescape(m2.group(1)))
        except Exception:
            _LOGGER.debug("Failed to parse data-init", exc_info=True)
    return Discovered(urls=urls, init=init)


def _looks_like_login(html_text: str) -> bool:
    """True if the response is the login form rather than an authenticated page."""
    return 'name="_username"' in html_text


def _is_2fa_page(url: str, html_text: str) -> bool:
    """Robust 2FA detection — checks URL path, not substring of domain."""
    try:
        path = urlparse(str(url)).path
    except Exception:  # noqa: BLE001
        path = str(url)
    return (
        path.startswith("/2fa")
        or 'action="/2fa_check"' in html_text
        or "Zugangscode" in html_text
    )


def _normalize_totp_secret(raw: str) -> str:
    """Strip otpauth:// URI, whitespace, lower→upper, keep only base32 chars."""
    s = raw.strip()
    if s.startswith("otpauth://"):
        try:
            q = urlparse(s)
            qs = parse_qs(q.query)
            s = qs.get("secret", [s])[0]
        except Exception:  # noqa: BLE001
            # Never log the value itself — it is the TOTP secret.
            _LOGGER.debug("Could not parse otpauth:// URI, using the raw value")
    # Remove all whitespace, hyphens, make upper — pyotp expects base32 upper
    s = re.sub(r"[\s\-]+", "", s).upper()
    return s


class RwbClient:
    """Thin aiohttp wrapper with cookie jar persistence."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        email: str,
        password: str,
        totp_secret: str | None = None,
    ) -> None:
        self._session = session
        self._email = email
        self._password = password
        self._totp_secret = _normalize_totp_secret(totp_secret) if totp_secret else None
        if self._totp_secret == "":
            self._totp_secret = None
        self._urls: dict[str, str] | None = None

    @property
    def has_totp_secret(self) -> bool:
        return self._totp_secret is not None

    def set_totp_secret(self, raw: str | None) -> None:
        """Replace the stored secret, applying the same normalization as __init__."""
        self._totp_secret = _normalize_totp_secret(raw) if raw else None
        if self._totp_secret == "":
            self._totp_secret = None

    def _generate_totp(self) -> str | None:
        if not self._totp_secret:
            return None
        try:
            import pyotp  # type: ignore

            return pyotp.TOTP(self._totp_secret).now()
        except Exception:
            _LOGGER.warning("Failed to generate TOTP from stored secret", exc_info=True)
            return None

    # -- low level --

    async def _get(self, path: str, **kwargs: Any) -> ClientResponse:
        url = f"{BASE_URL}{path}" if path.startswith("/") else path
        kwargs.setdefault("timeout", REQUEST_TIMEOUT)
        try:
            resp = await self._session.get(url, **kwargs)
        except aiohttp.ClientError as err:
            raise RwbError(f"Network error GET {path}: {err}") from err
        return resp

    async def _post(
        self, path: str, data: dict[str, str], **kwargs: Any
    ) -> ClientResponse:
        url = f"{BASE_URL}{path}" if path.startswith("/") else path
        kwargs.setdefault("timeout", REQUEST_TIMEOUT)
        try:
            resp = await self._session.post(url, data=data, **kwargs)
        except aiohttp.ClientError as err:
            raise RwbError(f"Network error POST {path}: {err}") from err
        return resp

    # -- auth --

    async def login(self) -> None:
        """Full login. Raises RwbMfaRequired if 2FA page appears, RwbAuthError otherwise.

        If a TOTP secret was supplied, MFA is handled transparently (no user interaction)
        — ideal for public MFA users who want fully unattended operation.
        """
        # 1) GET login to get csrf + PHPSESSID
        resp = await self._get(LOGIN_PATH)
        if resp.status >= 400:
            raise RwbAuthError(f"GET {LOGIN_PATH} failed with {resp.status}")
        html_text = await resp.text()
        csrf = _extract_csrf(html_text)
        if not csrf:
            raise RwbAuthError("CSRF token not found on /login")

        # 2) POST login
        resp2 = await self._post(
            LOGIN_PATH,
            data={
                "_username": self._email,
                "_password": self._password,
                "_csrf_token": csrf,
            },
            headers={"Referer": f"{BASE_URL}{LOGIN_PATH}"},
        )
        if resp2.status >= 400:
            raise RwbAuthError(f"POST {LOGIN_PATH} failed with {resp2.status}")
        html2 = await resp2.text()
        if _is_2fa_page(str(resp2.url), html2):
            code = self._generate_totp()
            if code:
                _LOGGER.debug("MFA required — trying auto-TOTP")
                try:
                    await self.submit_mfa(code)
                    _LOGGER.info("Auto-TOTP succeeded")
                    return
                except RwbAuthError:
                    _LOGGER.warning("Auto-TOTP failed, falling through to manual MFA")
            raise RwbMfaRequired("MFA code required", csrf=csrf)
        if _looks_like_login(html2) and 'name="_password"' in html2:
            raise RwbAuthError("Login failed — check email/password")
        _LOGGER.debug("Login succeeded without MFA, url=%s", resp2.url)

    async def submit_mfa(self, code: str) -> None:
        """Submit TOTP/backup code. Raises RwbAuthError if code invalid."""
        resp = await self._post(
            TFA_PATH,
            data={"_auth_code": code.strip()},
            headers={"Referer": f"{BASE_URL}/2fa"},
        )
        if resp.status >= 400:
            raise RwbAuthError(f"POST {TFA_PATH} failed with {resp.status}")
        html_text = await resp.text()
        if "Zugangscode" in html_text or "ungültig" in html_text.lower():
            raise RwbAuthError("Invalid MFA code")
        _LOGGER.debug("MFA accepted, url=%s", resp.url)

    async def ensure_authenticated(self) -> None:
        """Cheap check: GET /lastgangdaten and see if we are redirected to login/2fa."""
        resp = await self._get(LASTGANG_PATH, allow_redirects=True)
        html_text = await resp.text()
        if _looks_like_login(html_text):
            raise RwbAuthError("Session expired — re-login required")
        if _is_2fa_page(str(resp.url), html_text):
            raise RwbMfaRequired("Session expired — MFA re-required")

    # -- discovery --

    async def discover(self) -> Discovered:
        """Fetch /lastgangdaten and parse data-urls. Caches result."""
        resp = await self._get(LASTGANG_PATH)
        if resp.status >= 400:
            raise RwbError(f"GET {LASTGANG_PATH} failed with {resp.status}")
        html_text = await resp.text()
        if _looks_like_login(html_text):
            raise RwbAuthError("Not authenticated — discover before login")
        if _is_2fa_page(str(resp.url), html_text):
            raise RwbMfaRequired("MFA required before discovery")
        disc = _extract_discovered(html_text)
        self._urls = disc.urls
        _LOGGER.debug("Discovered urls=%s init=%s", disc.urls, disc.init)
        return disc

    def _require_urls(self) -> dict[str, str]:
        if not self._urls:
            raise RwbDiscoveryError("Call discover() first")
        return self._urls

    # -- domain --

    def _ajax_headers(self) -> dict[str, str]:
        return {
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json",
            "Referer": f"{BASE_URL}{LASTGANG_PATH}",
        }

    async def _get_json(
        self, url: str, params: dict[str, str], what: str
    ) -> dict[str, Any]:
        """GET an AJAX endpoint and decode JSON.

        Maps an expired session to RwbAuthError/RwbMfaRequired so the coordinator can
        trigger re-auth instead of reporting a generic update failure. Response bodies
        may contain customer data, so they are only ever logged at debug level.
        """
        resp = await self._get(url, params=params, headers=self._ajax_headers())
        body = await resp.text()
        if _looks_like_login(body):
            raise RwbAuthError(f"{what}: session expired")
        if _is_2fa_page(str(resp.url), body):
            raise RwbMfaRequired(f"{what}: MFA re-required")
        if resp.status >= 400:
            _LOGGER.debug("%s failed with HTTP %s: %s", what, resp.status, body[:500])
            raise RwbError(f"{what} failed with HTTP {resp.status}")
        try:
            return json.loads(body)
        except json.JSONDecodeError as err:
            _LOGGER.debug("%s returned non-JSON: %s", what, body[:500])
            raise RwbError(f"{what}: invalid JSON response") from err

    async def get_objekte(self) -> dict[str, Any]:
        url = self._require_urls().get(
            "versorger_lastgangdaten_get_objekte", ENDPOINT_OBJEKTE
        )
        return await self._get_json(url, {"type": "detailpage"}, "getObjekte")

    async def get_messlinien(
        self,
        objekt_id: int | str,
        meteringcode: str,
        datum: date | str,
        zeitraum: str = "day",
    ) -> dict[str, Any]:
        url = self._require_urls().get(
            "versorger_lastgangdaten_get_messlinien", ENDPOINT_MESSLINIEN
        )
        params = {
            "objektId": str(objekt_id),
            "meteringcode": meteringcode,
            "datum": datum.isoformat() if isinstance(datum, date) else datum,
            "zeitraum": zeitraum,
            "type": "detailpage",
        }
        return await self._get_json(url, params, "getMesslinien")

    async def get_messdaten(
        self,
        objekt_id: int | str,
        meteringcode: str,
        messlinie_id: str,
        messlinien_ids: dict[str, list[str]],
        datum: date | str,
        zeitraum: str = "day2",
    ) -> dict[str, Any]:
        url = self._require_urls().get(
            "versorger_lastgangdaten_get_messdaten", ENDPOINT_MESSDATEN
        )
        params = {
            "objektId": str(objekt_id),
            "meteringcode": meteringcode,
            "messlinieId": str(messlinie_id),
            "messlinienIds": json.dumps(messlinien_ids),
            "messlinievorjahr": "false",
            "datum": datum.isoformat() if isinstance(datum, date) else datum,
            "zeitraum": zeitraum,
            "type": "detailpage",
            "vorjahr": "false",
        }
        return await self._get_json(url, params, "getMessdaten")

    # -- normalization --

    @staticmethod
    def normalize_to_kwh(
        payload: dict[str, Any], messlinie_id: str | None = None
    ) -> list[SeriesPoint]:
        """Convert raw getMessdaten payload to kWh SeriesPoints for HA statistics.

        Handles both kw (day) -> *0.25 and kwh (day2) already normalized.
        Payload shape from captures: {chartData: {intervals: [{from, until}], unit,
        datasets: [{id, meteringcode, label, data: [str]}], ...}, ...}

        We ask for all messlinien in messlinienIds, so the response may carry several
        datasets. Select by id — never trust ordering, or Lieferung values end up in
        the Rücklieferung statistic.
        """
        cd = payload.get("chartData") or {}
        intervals: list[dict[str, str]] = cd.get("intervals") or []
        datasets = cd.get("datasets") or []
        unit = (cd.get("unit") or "").lower()
        if not intervals or not datasets:
            return []
        if messlinie_id is None:
            dataset = datasets[0]
        else:
            dataset = next(
                (ds for ds in datasets if str(ds.get("id")) == str(messlinie_id)), None
            )
            if dataset is None:
                _LOGGER.debug(
                    "Messlinie %s absent from response (datasets: %s)",
                    messlinie_id,
                    [ds.get("id") for ds in datasets],
                )
                return []
        data = dataset.get("data") or []
        points: list[SeriesPoint] = []
        for idx, val_str in enumerate(data):
            if idx >= len(intervals):
                break
            iv = intervals[idx]
            try:
                v = float(str(val_str).replace(",", "."))
            except ValueError:
                continue
            if unit == "kw":
                v = v * 0.25  # 15min @ kW -> kWh
            points.append(SeriesPoint(start=iv["from"], end=iv["until"], value_kwh=v))
        return points

    @staticmethod
    def aggregate_hourly(points: list[SeriesPoint]) -> list[SeriesPoint]:
        """Sum 15-minute points into hourly buckets.

        HA's long-term statistics table is hourly by contract — async_add_external_statistics
        is documented as "Add hourly statistics from an external source" and every reducer in
        recorder/statistics.py says "Reduce hourly statistics to ...". It performs no alignment
        check, so sub-hourly rows are silently inserted and then mis-read downstream. Bucketing
        here also cuts a 3.7-year backfill from ~260k rows to ~65k.

        Keyed on the tz-aware hour, so the repeated wall-clock hour at the DST fall-back stays
        two distinct buckets (different UTC offsets) instead of collapsing into one.
        """
        buckets: dict[dt_datetime, list[SeriesPoint]] = {}
        for point in points:
            hour = dt_datetime.fromisoformat(point.start).replace(
                minute=0, second=0, microsecond=0
            )
            buckets.setdefault(hour, []).append(point)
        out: list[SeriesPoint] = []
        for hour in sorted(buckets):
            members = sorted(buckets[hour], key=lambda p: p.start)
            out.append(
                SeriesPoint(
                    start=hour.isoformat(),
                    end=members[-1].end,
                    value_kwh=sum(p.value_kwh for p in members),
                )
            )
        return out

    @staticmethod
    def parse_objekte(payload: dict[str, Any]) -> list[dict[str, str]]:
        """Extract list of {id, bezeichnung, meteringcode} from getObjekte payload.

        Real shape: {"data":{"objektId":105892,"objekte":{"105892":{"id":"105892"}}},
                     "formData":{"105892":{"id":"105892","bezeichnung":"...","messpunkte":{"CH...":{"meteringcode":...}}}}}
        """
        form_data = payload.get("formData") or {}
        out: list[dict[str, str]] = []
        for oid, obj in form_data.items():
            bezeichnung = obj.get("bezeichnung", "")
            messpunkte = obj.get("messpunkte") or {}
            for mcode in messpunkte:
                out.append(
                    {"id": str(oid), "bezeichnung": bezeichnung, "meteringcode": mcode}
                )
        return out

    @staticmethod
    def parse_messlinien(payload: dict[str, Any]) -> dict[str, str]:
        """Extract {id: name} from getMesslinien payload.

        Real shape: {"data":["68258"],"formData":{"68258":{"id":"68258","name":"Wirk Lieferung"}, ...}}
        """
        fd = payload.get("formData") or {}
        return {k: v.get("name", k) for k, v in fd.items()}


# -- tariffs ---------------------------------------------------------------
# Separate from RwbClient on purpose: this file lives on the public corporate
# site, needs no login, and is published under a legal obligation (Art. 7b
# StromVV) rather than by the portal we scrape.


async def async_fetch_tariffs(
    session: aiohttp.ClientSession, year: int
) -> dict[str, Any]:
    """Download the machine-readable tariff document for one calendar year."""
    url = f"{TARIFF_BASE_URL}{TARIFF_PATH_TEMPLATE.format(year=year)}"
    try:
        resp = await session.get(url, timeout=REQUEST_TIMEOUT)
    except aiohttp.ClientError as err:
        raise RwbTariffError(f"Network error fetching {year} tariffs: {err}") from err
    if resp.status == 404:
        raise RwbTariffUnavailable(f"No tariff file published for {year}")
    if resp.status >= 400:
        raise RwbTariffError(f"GET {year} tariffs failed with {resp.status}")
    try:
        # content_type=None: the file is served as application/json, but do not make
        # the parse depend on the header being right.
        return await resp.json(content_type=None)
    except (ValueError, aiohttp.ClientError) as err:
        raise RwbTariffError(f"Tariff file for {year} is not valid JSON") from err


def _constant_price(entries: list[dict[str, Any]], label: str) -> float:
    """The single all-day price of a tariff component.

    Every RWB entry is `tariffForm: "constant"` with `from == to == "00:00"`, so the
    integration writes one flat rate per hour. If RWB ever publishes real
    Hochtarif/Niedertarif windows, refuse loudly rather than silently billing every
    hour at whichever window happened to be listed first.
    """
    if not entries:
        raise RwbTariffError(f"Tariff {label!r} has no energy price")
    if len(entries) > 1 or entries[0].get("from") != entries[0].get("to"):
        raise RwbTariffError(
            f"Tariff {label!r} has time-of-use windows, which this integration does "
            "not support — it writes one flat rate per hour"
        )
    try:
        return float(entries[0]["price"])
    except (KeyError, TypeError, ValueError) as err:
        raise RwbTariffError(f"Tariff {label!r} has an unreadable price") from err


def _municipality_price(tariffs: list[dict[str, Any]], municipality: str) -> float:
    """The concession fee (Konzessionsabgabe) levied by one municipality."""
    seen: list[str] = []
    for tariff in tariffs:
        if tariff.get("tariffType") != "regional_fees":
            continue
        for entry in tariff.get("prices", {}).get("municipalityTaxes") or []:
            name = entry.get("municipalityName")
            seen.append(str(name))
            if name == municipality:
                return _constant_price(
                    entry.get("municipalityEnergy") or [], f"{municipality} concession"
                )
    raise RwbTariffError(
        f"No concession fee for municipality {municipality!r}. "
        f"Available: {', '.join(sorted(set(seen))) or 'none'}"
    )


def parse_tariff_rate(
    payload: dict[str, Any],
    *,
    product: str,
    grid_tariff: str,
    municipality: str,
    surcharge_rp: float = 0.0,
) -> TariffRate:
    """Total CHF per kWh: energy + grid + concession + surcharges.

    Only energy-proportional components. The file also carries CHF/month base and
    metering fees; those are not per-kWh, so folding them into an hourly statistic
    would mean inventing an amortisation. They are left out and documented.

    `surcharge_rp` covers the federal Netzzuschlag and the municipal Förderabgabe,
    which are charged on every kWh but are absent from the published file.
    """
    tariffs = payload.get("tariffs") or []
    by_name = {t.get("tariffName"): t for t in tariffs if isinstance(t, dict)}

    def pick(name: str, kind: str) -> dict[str, Any]:
        tariff = by_name.get(name)
        if tariff is None or tariff.get("tariffType") != kind:
            available = sorted(
                str(t.get("tariffName")) for t in tariffs if t.get("tariffType") == kind
            )
            raise RwbTariffError(
                f"No {kind} tariff named {name!r}. Available: "
                f"{', '.join(available) or 'none'}"
            )
        return tariff

    components = {
        "energy": _constant_price(
            pick(product, "electricity").get("prices", {}).get("energy") or [], product
        ),
        "grid": _constant_price(
            pick(grid_tariff, "grid").get("prices", {}).get("energy") or [], grid_tariff
        ),
        "concession": _municipality_price(tariffs, municipality),
        "surcharges": surcharge_rp / 100.0,  # Rp./kWh → CHF/kWh
    }
    return TariffRate(chf_per_kwh=sum(components.values()), components=components)
