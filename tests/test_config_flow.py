"""Config, MFA, reauth and options flows."""

import pathlib

import pyotp
import pytest
from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.regionalwerke_baden.const import (
    CONF_COST_ENABLED,
    CONF_TOTP_SECRET,
    DOMAIN,
)
from custom_components.regionalwerke_baden.coordinator import _totp_secret_for

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
SECRET = "JBSWY3DPEHPK3PXP"


@pytest.fixture
def login_portal(portal):
    portal.set("/login", '<input name="_csrf_token" value="tok">')
    portal.set("/lastgangdaten", (FIXTURES / "lastgang_discovery.html").read_text())
    return portal


async def _start(hass):
    return await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )


async def test_form_shown(recorder_mock, hass, custom_integration, login_portal):
    result = await _start(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"


async def test_non_mfa_account_creates_entry(
    recorder_mock, hass, custom_integration, login_portal
):
    login_portal.set("/login", "<html>Willkommen</html>", method="POST")

    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"email": "User@Example.com", "password": "pw"}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "User@Example.com"
    assert result["data"] == {"email": "User@Example.com", "password": "pw"}
    assert CONF_TOTP_SECRET not in result["data"]


async def test_bad_credentials_show_invalid_auth(
    recorder_mock, hass, custom_integration, login_portal
):
    login_portal.set(
        "/login",
        '<form><input name="_username"><input name="_password"></form>',
        method="POST",
    )
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"email": "a@b.c", "password": "wrong"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}


async def test_mfa_account_manual_code(
    recorder_mock, hass, custom_integration, login_portal
):
    login_portal.set(
        "/login", '<form action="/2fa_check">Zugangscode</form>', method="POST"
    )
    login_portal.set("/2fa_check", "<html>Willkommen</html>", method="POST")

    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"email": "a@b.c", "password": "pw"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "mfa"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"code": "123456"}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {"email": "a@b.c", "password": "pw"}
    assert login_portal.last_form("/2fa_check")["_auth_code"] == "123456"


async def test_mfa_wrong_code_shows_invalid_auth(
    recorder_mock, hass, custom_integration, login_portal
):
    login_portal.set(
        "/login", '<form action="/2fa_check">Zugangscode</form>', method="POST"
    )
    login_portal.set("/2fa_check", "Zugangscode ungültig", method="POST")

    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"email": "a@b.c", "password": "pw"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"code": "000000"}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}


async def test_totp_secret_answers_mfa_and_is_stored(
    recorder_mock, hass, custom_integration, login_portal
):
    login_portal.set(
        "/login", '<form action="/2fa_check">Zugangscode</form>', method="POST"
    )
    login_portal.set("/2fa_check", "<html>Willkommen</html>", method="POST")

    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"email": "a@b.c", "password": "pw", CONF_TOTP_SECRET: SECRET},
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_TOTP_SECRET] == SECRET
    # login() answered the challenge from the secret — no manual code was needed.
    assert (
        login_portal.last_form("/2fa_check")["_auth_code"] == pyotp.TOTP(SECRET).now()
    )


async def test_duplicate_account_aborts(
    recorder_mock, hass, custom_integration, login_portal
):
    MockConfigEntry(
        domain=DOMAIN, unique_id="a@b.c", data={"email": "a@b.c", "password": "pw"}
    ).add_to_hass(hass)
    login_portal.set("/login", "<html>Willkommen</html>", method="POST")

    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"email": "A@B.c", "password": "pw"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_reauth_updates_the_password(
    recorder_mock, hass, custom_integration, login_portal
):
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id="a@b.c", data={"email": "a@b.c", "password": "old"}
    )
    entry.add_to_hass(hass)
    login_portal.set("/login", "<html>Willkommen</html>", method="POST")

    result = await entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"email": "a@b.c", "password": "new"}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data["password"] == "new"


async def test_options_flow_sets_and_clears_the_secret(
    recorder_mock, hass, custom_integration, login_portal
):
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="a@b.c",
        data={"email": "a@b.c", "password": "pw", CONF_TOTP_SECRET: "FROMSETUP"},
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_TOTP_SECRET: SECRET}
    )
    await hass.async_block_till_done()
    assert entry.options[CONF_TOTP_SECRET] == SECRET

    # Clearing it must actually clear it — the entry data secret must not win.
    from custom_components.regionalwerke_baden.coordinator import _totp_secret_for

    assert _totp_secret_for(entry) == SECRET

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_TOTP_SECRET: ""}
    )
    await hass.async_block_till_done()
    assert _totp_secret_for(entry) is None


async def test_reauth_completes_for_an_mfa_account(
    recorder_mock, hass, custom_integration, login_portal
):
    """Regression: the MFA step aborted a reauth with `already_configured`.

    async_step_mfa called _abort_if_unique_id_configured unconditionally, but the
    entry being re-authenticated already holds that unique id — so an MFA user could
    enter a valid code and still never repair the entry.
    """
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id="a@b.c", data={"email": "a@b.c", "password": "old"}
    )
    entry.add_to_hass(hass)
    login_portal.set(
        "/login", '<form action="/2fa_check">Zugangscode</form>', method="POST"
    )
    login_portal.set("/2fa_check", "<html>Willkommen</html>", method="POST")

    result = await entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"email": "a@b.c", "password": "new"}
    )
    assert result["step_id"] == "mfa"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"code": "123456"}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful", result
    assert entry.data["password"] == "new"
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1


async def test_reauth_with_a_different_account_is_rejected(
    recorder_mock, hass, custom_integration, login_portal
):
    """Reauth must not silently repoint an entry at another RWB account."""
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id="a@b.c", data={"email": "a@b.c", "password": "pw"}
    )
    entry.add_to_hass(hass)
    login_portal.set("/login", "<html>Willkommen</html>", method="POST")

    result = await entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"email": "someone-else@b.c", "password": "pw"}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "wrong_account"
    assert entry.data["email"] == "a@b.c"


async def test_wrong_totp_secret_falls_back_to_the_typed_code(
    recorder_mock, hass, custom_integration, login_portal
):
    """A bad stored secret used to surface as "unknown" and discard the typed code."""

    def two_fa(request):
        # A wrong secret never yields a valid code, so every auto-TOTP attempt is
        # rejected; only the manually typed code is accepted.
        submitted = login_portal.calls[-1]["form"].get("_auth_code")
        return (
            "<html>Willkommen</html>"
            if submitted == "123456"
            else "Zugangscode ungültig"
        )

    login_portal.set(
        "/login", '<form action="/2fa_check">Zugangscode</form>', method="POST"
    )
    login_portal.set("/2fa_check", two_fa, method="POST")

    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"email": "a@b.c", "password": "pw", CONF_TOTP_SECRET: SECRET},
    )
    assert result["step_id"] == "mfa"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"code": "123456"}
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    # Note: assert membership, not the last submission — once the entry exists the
    # coordinator starts its own login and posts another auto-TOTP code.
    submitted = [
        c["form"].get("_auth_code")
        for c in login_portal.calls
        if c["path"] == "/2fa_check"
    ]
    assert "123456" in submitted, submitted
    await hass.async_block_till_done()


async def test_bad_totp_secret_reports_invalid_auth_not_unknown(
    recorder_mock, hass, custom_integration, login_portal
):
    login_portal.set(
        "/login", '<form action="/2fa_check">Zugangscode</form>', method="POST"
    )
    login_portal.set("/2fa_check", "Zugangscode ungültig", method="POST")

    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"email": "a@b.c", "password": "pw", CONF_TOTP_SECRET: SECRET},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"code": "000000"}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}


async def test_reauth_secret_is_not_shadowed_by_options(
    recorder_mock, hass, custom_integration, login_portal
):
    """Regression: the coordinator reads the secret from options whenever that key
    exists, and the options flow always writes it — "" included. Reauth wrote only
    to entry.data, so for anyone who had ever opened Options the secret supplied
    while re-authenticating was ignored forever and MFA kept prompting."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="a@b.c",
        data={"email": "a@b.c", "password": "old"},
        # The state the options flow leaves behind after a save with no secret.
        options={CONF_TOTP_SECRET: "", CONF_COST_ENABLED: True},
    )
    entry.add_to_hass(hass)
    login_portal.set("/login", "<html>Willkommen</html>", method="POST")

    result = await entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"email": "a@b.c", "password": "new", CONF_TOTP_SECRET: "JBSWY3DPEHPK3PXP"},
    )
    await hass.async_block_till_done()

    assert result["reason"] == "reauth_successful"
    # What the coordinator will actually use.
    assert _totp_secret_for(entry) == "JBSWY3DPEHPK3PXP"
    # Unrelated options survive the write-through.
    assert entry.options[CONF_COST_ENABLED] is True
