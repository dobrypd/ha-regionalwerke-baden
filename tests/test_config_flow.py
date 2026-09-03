"""Config, MFA, reauth and options flows."""

import pathlib

import pyotp
import pytest
from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.regionalwerke_baden.const import (
    CONF_COST_ENABLED,
    CONF_SESSION,
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


def _schema_keys(result) -> set[str]:
    return {key.schema for key in result["data_schema"].schema}


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
    assert result["data"]["email"] == "User@Example.com"
    assert result["data"]["password"] == "pw"
    # The established portal session rides along so setup need not log in again.
    assert result["data"][CONF_SESSION] == {"PHPSESSID": login_portal.session_id}
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
    assert result["data"]["email"] == "a@b.c"
    assert result["data"]["password"] == "pw"
    assert result["data"][CONF_SESSION] == {"PHPSESSID": login_portal.session_id}
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


async def test_reauth_code_only_uses_stored_credentials_and_persists_session(
    recorder_mock, hass, custom_integration, login_portal
):
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="a@b.c",
        data={
            "email": "a@b.c",
            "password": "stored-password",
            CONF_TOTP_SECRET: SECRET,
        },
    )
    entry.add_to_hass(hass)
    login_portal.set(
        "/login", '<form action="/2fa_check">Zugangscode</form>', method="POST"
    )

    def accept_manual_code(request):
        submitted = login_portal.calls[-1]["form"]["_auth_code"]
        return (
            "<html>Willkommen</html>"
            if submitted == "123456"
            else "Zugangscode ungültig"
        )

    login_portal.set("/2fa_check", accept_manual_code, method="POST")

    result = await entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"
    assert _schema_keys(result) == {"code", CONF_TOTP_SECRET}
    assert login_portal.call_count("/login", "POST") == 0

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"code": "123456"}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert login_portal.last_form("/login") == {
        "_username": "a@b.c",
        "_password": "stored-password",
        "_csrf_token": "tok",
    }
    assert login_portal.last_form("/2fa_check")["_auth_code"] == "123456"
    assert login_portal.call_count("/2fa_check", "POST") == 1
    assert entry.data["password"] == "stored-password"
    assert entry.data[CONF_TOTP_SECRET] == SECRET
    assert entry.data[CONF_SESSION] == {"PHPSESSID": login_portal.session_id}


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


@pytest.mark.parametrize(
    "user_input",
    [{"code": "123456"}, {CONF_TOTP_SECRET: SECRET}],
    ids=["one_time_code", "replacement_totp_secret"],
)
async def test_reauth_bad_stored_password_asks_only_for_password_then_reuses_mfa(
    recorder_mock, hass, custom_integration, login_portal, user_input
):
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id="a@b.c", data={"email": "a@b.c", "password": "old"}
    )
    entry.add_to_hass(hass)

    def login(request):
        if login_portal.calls[-1]["form"]["_password"] == "old":
            return '<form><input name="_username"><input name="_password"></form>'
        return '<form action="/2fa_check">Zugangscode</form>'

    def accept_submitted_mfa(request):
        submitted = login_portal.calls[-1]["form"]["_auth_code"]
        valid = (
            submitted == "123456"
            if "code" in user_input
            else pyotp.TOTP(SECRET).verify(submitted, valid_window=1)
        )
        return "<html>Willkommen</html>" if valid else "Zugangscode ungültig"

    login_portal.set("/login", login, method="POST")
    login_portal.set("/2fa_check", accept_submitted_mfa, method="POST")

    result = await entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_password"
    assert _schema_keys(result) == {"password"}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"password": "new"}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data["password"] == "new"
    assert entry.data[CONF_SESSION] == {"PHPSESSID": login_portal.session_id}


@pytest.mark.parametrize(
    ("user_input", "error"),
    [
        ({}, "code_or_secret_required"),
        (
            {"code": "123456", CONF_TOTP_SECRET: SECRET},
            "choose_one_auth_method",
        ),
    ],
    ids=["neither", "both"],
)
async def test_reauth_requires_exactly_one_auth_method(
    recorder_mock, hass, custom_integration, login_portal, user_input, error
):
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id="a@b.c", data={"email": "a@b.c", "password": "pw"}
    )
    entry.add_to_hass(hass)

    result = await entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"
    assert result["errors"] == {"base": error}
    assert login_portal.call_count("/login", "POST") == 0


@pytest.mark.parametrize(
    ("user_input", "error"),
    [
        ({"code": "000000"}, "invalid_code"),
        ({CONF_TOTP_SECRET: SECRET}, "invalid_totp"),
    ],
    ids=["one_time_code", "replacement_totp_secret"],
)
async def test_reauth_invalid_mfa_stays_on_mfa_form(
    recorder_mock, hass, custom_integration, login_portal, user_input, error
):
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id="a@b.c", data={"email": "a@b.c", "password": "pw"}
    )
    entry.add_to_hass(hass)
    login_portal.set(
        "/login", '<form action="/2fa_check">Zugangscode</form>', method="POST"
    )
    login_portal.set("/2fa_check", "Zugangscode ungültig", method="POST")

    result = await entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"
    assert result["errors"] == {"base": error}
    assert entry.data["email"] == "a@b.c"
    assert CONF_TOTP_SECRET not in entry.data


async def test_reauth_expired_code_after_password_returns_for_a_fresh_code(
    recorder_mock, hass, custom_integration, login_portal
):
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id="a@b.c", data={"email": "a@b.c", "password": "old"}
    )
    entry.add_to_hass(hass)

    def login(request):
        if login_portal.calls[-1]["form"]["_password"] == "old":
            return '<form><input name="_username"><input name="_password"></form>'
        return '<form action="/2fa_check">Zugangscode</form>'

    def accept_fresh_code(request):
        submitted = login_portal.calls[-1]["form"]["_auth_code"]
        return (
            "<html>Willkommen</html>"
            if submitted == "654321"
            else "Zugangscode ungültig"
        )

    login_portal.set("/login", login, method="POST")
    login_portal.set("/2fa_check", accept_fresh_code, method="POST")

    result = await entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"code": "123456"}
    )
    assert result["step_id"] == "reauth_password"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"password": "new"}
    )
    assert result["step_id"] == "reauth_confirm"
    assert result["errors"] == {"base": "invalid_code"}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"code": "654321"}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data["password"] == "new"


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


async def test_reauth_replacement_totp_secret_is_written_through_options(
    recorder_mock, hass, custom_integration, login_portal
):
    """Regression: the coordinator reads the secret from options whenever that key
    exists, and the options flow always writes it — "" included. Reauth wrote only
    to entry.data, so for anyone who had ever opened Options the secret supplied
    while re-authenticating was ignored forever and MFA kept prompting."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="a@b.c",
        data={
            "email": "a@b.c",
            "password": "stored-password",
            CONF_TOTP_SECRET: "JBSWY3DPEHPK3PXQ",
        },
        # Options win over data, so reauth must replace this value as well.
        options={CONF_TOTP_SECRET: "JBSWY3DPEHPK3PXR", CONF_COST_ENABLED: True},
    )
    entry.add_to_hass(hass)
    login_portal.set(
        "/login", '<form action="/2fa_check">Zugangscode</form>', method="POST"
    )

    def accept_replacement_secret(request):
        submitted = login_portal.calls[-1]["form"]["_auth_code"]
        return (
            "<html>Willkommen</html>"
            if pyotp.TOTP(SECRET).verify(submitted, valid_window=1)
            else "Zugangscode ungültig"
        )

    login_portal.set("/2fa_check", accept_replacement_secret, method="POST")

    result = await entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_TOTP_SECRET: SECRET}
    )
    await hass.async_block_till_done()

    assert result["reason"] == "reauth_successful"
    # What the coordinator will actually use.
    assert _totp_secret_for(entry) == SECRET
    assert entry.data[CONF_TOTP_SECRET] == SECRET
    assert entry.options[CONF_TOTP_SECRET] == SECRET
    assert entry.data["password"] == "stored-password"
    assert entry.data[CONF_SESSION] == {"PHPSESSID": login_portal.session_id}
    # Unrelated options survive the write-through.
    assert entry.options[CONF_COST_ENABLED] is True


async def test_the_mfa_session_is_stored_on_the_entry(
    recorder_mock, hass, custom_integration, login_portal
):
    """Regression: a one-time code can only be spent once, and the config flow spent
    it in a throwaway session. The entry kept only the credentials, so setup logged
    in again, hit the 2FA page and failed with "MFA code required" — a code the user
    had no way to supply. Re-authenticating just repeated the cycle."""
    login_portal.set(
        "/login", '<form action="/2fa_check">Zugangscode</form>', method="POST"
    )
    login_portal.set("/2fa_check", "<html>Willkommen</html>", method="POST")

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"email": "a@b.c", "password": "pw"}
    )
    assert result["step_id"] == "mfa"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"code": "123456"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY

    session = result["data"][CONF_SESSION]
    assert session["PHPSESSID"] == login_portal.session_id, (
        "the session that answered the MFA challenge must reach the entry"
    )
    # And no secret was invented along the way.
    assert CONF_TOTP_SECRET not in result["data"]
