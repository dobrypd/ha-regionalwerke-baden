"""Shared test fixtures."""

import json
import pathlib

import pytest

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


@pytest.fixture
def custom_integration(enable_custom_integrations):
    """Let HA load custom_components/regionalwerke_baden. Request it in tests that set up HA."""
    yield


@pytest.fixture
def load_fixture_json():
    def _load(name: str):
        return json.loads((FIXTURES / name).read_text())

    return _load


@pytest.fixture
def lastgang_html() -> str:
    return (FIXTURES / "lastgang_discovery.html").read_text()


class FakePortal:
    """A stand-in RWB portal served by a real aiohttp server.

    Keeps the client's own HTTP stack in the test: query encoding, form posts and
    redirects all behave as they do against the real site.
    """

    def __init__(self) -> None:
        self.routes: dict[tuple[str, str], tuple[int, str]] = {}
        self.calls: list[dict] = []
        self.base = ""

    def set(self, path: str, body, status: int = 200, method: str = "GET") -> None:
        """body is a string, or a callable taking the aiohttp request and returning one."""
        self.routes[(method.upper(), path)] = (status, body)

    def _calls_for(self, path: str, method: str | None = None) -> list[dict]:
        return [
            c
            for c in self.calls
            if c["path"] == path and (method is None or c["method"] == method)
        ]

    def last_query(self, path: str) -> dict[str, str]:
        return self._calls_for(path, "GET")[-1]["query"]

    def last_form(self, path: str) -> dict[str, str]:
        return self._calls_for(path, "POST")[-1]["form"]

    def call_count(self, path: str, method: str | None = None) -> int:
        return len(self._calls_for(path, method))


@pytest.fixture
async def portal(aiohttp_server, socket_enabled, monkeypatch):
    """Run the fake portal and point the client's BASE_URL at it."""
    from aiohttp import web

    from custom_components.regionalwerke_baden import api

    fake = FakePortal()

    async def handler(request: "web.Request") -> "web.Response":
        form = dict(await request.post()) if request.method == "POST" else {}
        fake.calls.append(
            {
                "path": request.path,
                "method": request.method,
                "query": dict(request.query),
                "form": form,
            }
        )
        status, body = fake.routes.get(
            (request.method, request.path), (404, "<html>not found</html>")
        )
        if callable(body):
            body = body(request)
        return web.Response(status=status, text=body, content_type="text/html")

    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", handler)
    server = await aiohttp_server(app)
    fake.base = f"http://{server.host}:{server.port}"
    monkeypatch.setattr(api, "BASE_URL", fake.base)
    # The tariff file lives on the public corporate site, not the portal. Point it at
    # the same fake server so no offline test can reach regionalwerke.ch for real.
    monkeypatch.setattr(api, "TARIFF_BASE_URL", fake.base)
    return fake


@pytest.fixture
async def portal_client(portal):
    """RwbClient wired to the fake portal, with discovery already done."""
    import aiohttp

    from custom_components.regionalwerke_baden.api import RwbClient

    async with aiohttp.ClientSession() as session:
        client = RwbClient(session, "user@example.com", "pw")
        client._urls = {
            "versorger_lastgangdaten_get_objekte": "/lastgangdaten/getObjekte",
            "versorger_lastgangdaten_get_messlinien": "/lastgangdaten/getMesslinien",
            "versorger_lastgangdaten_get_messdaten": "/lastgangdaten/getMessdaten",
        }
        yield client
