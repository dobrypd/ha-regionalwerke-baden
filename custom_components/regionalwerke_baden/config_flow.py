"""Config flow — public: supports MFA and non-MFA accounts.

Non-MFA: email + password → done.
MFA:   email + password → MFA step (6-digit code). Optionally store TOTP secret
       for fully unattended operation (historic import can run 6+ min past
       session expiry without manual re-auth).
"""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_create_clientsession

from .api import RwbAuthError, RwbClient, RwbMfaRequired
from .const import (
    CONF_COST_ENABLED,
    CONF_COST_GRID,
    CONF_COST_MUNICIPALITY,
    CONF_COST_PRODUCT,
    CONF_COST_SURCHARGE,
    CONF_TOTP_SECRET,
    DEFAULT_COST_GRID,
    DEFAULT_COST_MUNICIPALITY,
    DEFAULT_COST_PRODUCT,
    DEFAULT_COST_SURCHARGE,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required("email"): str,
        vol.Required("password"): str,
        vol.Optional(CONF_TOTP_SECRET): str,
    }
)
STEP_MFA_SCHEMA = vol.Schema(
    {vol.Required("code"): str, vol.Optional(CONF_TOTP_SECRET): str}
)


def _client(
    hass: HomeAssistant, email: str, password: str, totp_secret: str | None
) -> RwbClient:
    # A throwaway session per flow — never the shared HA one, whose cookie jar is
    # global and would clobber a running coordinator's portal session.
    return RwbClient(
        async_create_clientsession(hass), email, password, totp_secret=totp_secret
    )


async def _validate(
    hass: HomeAssistant, email: str, password: str, totp_secret: str | None = None
) -> None:
    client = _client(hass, email, password, totp_secret)
    await client.login()
    await client.discover()


class RwbConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._email: str | None = None
        self._password: str | None = None
        self._totp_secret: str | None = None

    async def async_step_user(self, user_input: dict[str, str] | None = None):
        errors: dict[str, str] = {}
        if user_input is not None:
            email = user_input["email"].strip()
            password = user_input["password"]
            totp_secret = (user_input.get(CONF_TOTP_SECRET) or "").strip() or None
            try:
                await _validate(self.hass, email, password, totp_secret)
            except RwbMfaRequired:
                self._email, self._password, self._totp_secret = (
                    email,
                    password,
                    totp_secret,
                )
                return await self.async_step_mfa()
            except RwbAuthError as err:
                _LOGGER.warning("Auth failed: %s", err)
                errors["base"] = "invalid_auth"
            except Exception:
                _LOGGER.exception("Unexpected validate error")
                errors["base"] = "unknown"
            else:
                return await self._finish(email, password, totp_secret)

        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_SCHEMA, errors=errors
        )

    async def async_step_mfa(self, user_input: dict[str, str] | None = None):
        errors: dict[str, str] = {}
        if user_input is not None:
            assert self._email and self._password
            code = user_input["code"].strip()
            totp_secret = (
                user_input.get(CONF_TOTP_SECRET) or self._totp_secret or ""
            ).strip() or None
            client = _client(self.hass, self._email, self._password, totp_secret)
            try:
                try:
                    # With a valid secret login() answers the challenge itself, so the
                    # manual code is not submitted on top (that would burn an attempt).
                    # A missing *or rejected* secret lands here and falls back to it.
                    await client.login()
                except RwbMfaRequired:
                    await client.submit_mfa(code)
                await client.discover()
            except RwbAuthError:
                errors["base"] = "invalid_auth"
            except Exception:
                _LOGGER.exception("MFA step failed")
                errors["base"] = "unknown"
            else:
                return await self._finish(self._email, self._password, totp_secret)

        return self.async_show_form(
            step_id="mfa", data_schema=STEP_MFA_SCHEMA, errors=errors
        )

    async def _finish(self, email: str, password: str, totp_secret: str | None):
        """Create the entry, or update the existing one when re-authenticating.

        A reauth flow must never call _abort_if_unique_id_configured: the entry it is
        repairing already holds this unique id, so the flow would abort with
        "already_configured" and leave the entry broken forever.
        """
        data = _entry_data(email, password, totp_secret)
        await self.async_set_unique_id(email.lower())
        if self.source == config_entries.SOURCE_REAUTH:
            self._abort_if_unique_id_mismatch(reason="wrong_account")
            entry = self._get_reauth_entry()
            updates: dict[str, Any] = {"data_updates": data}
            if CONF_TOTP_SECRET in entry.options:
                # The coordinator prefers options whenever the key is present, and
                # the options flow always writes it — "" included. Writing the
                # secret to data alone would leave it shadowed forever.
                updates["options"] = {
                    **entry.options,
                    CONF_TOTP_SECRET: totp_secret or "",
                }
            return self.async_update_reload_and_abort(entry, **updates)
        self._abort_if_unique_id_configured()
        return self.async_create_entry(title=email, data=data)

    async def async_step_reauth(self, entry_data: dict[str, Any]):
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, str] | None = None):
        errors: dict[str, str] = {}
        entry = self._get_reauth_entry()
        if user_input is not None:
            email = (user_input.get("email") or entry.data["email"]).strip()
            password = user_input.get("password") or entry.data["password"]
            # Same precedence the coordinator uses, so the prefill and the fallback
            # are the secret actually in force rather than a stale one from data.
            stored = (
                entry.options.get(CONF_TOTP_SECRET)
                if CONF_TOTP_SECRET in entry.options
                else entry.data.get(CONF_TOTP_SECRET)
            )
            totp_secret = (
                user_input.get(CONF_TOTP_SECRET) or stored or ""
            ).strip() or None
            try:
                await _validate(self.hass, email, password, totp_secret)
            except RwbMfaRequired:
                self._email, self._password, self._totp_secret = (
                    email,
                    password,
                    totp_secret,
                )
                return await self.async_step_mfa()
            except RwbAuthError:
                errors["base"] = "invalid_auth"
            except Exception:
                _LOGGER.exception("Reauth failed")
                errors["base"] = "unknown"
            else:
                return await self._finish(email, password, totp_secret)

        return self.async_show_form(
            step_id="reauth_confirm", data_schema=STEP_USER_SCHEMA, errors=errors
        )

    @staticmethod
    def async_get_options_flow(config_entry) -> RwbOptionsFlow:
        return RwbOptionsFlow()


class RwbOptionsFlow(config_entries.OptionsFlow):
    async def async_step_init(self, user_input=None):
        opts = self.config_entry.options
        if user_input is not None:
            # Stored in options, which the coordinator prefers over data — so this
            # can both override and clear a secret captured during setup.
            return self.async_create_entry(
                data={
                    CONF_TOTP_SECRET: (user_input.get(CONF_TOTP_SECRET) or "").strip(),
                    CONF_COST_ENABLED: user_input.get(CONF_COST_ENABLED, False),
                    CONF_COST_PRODUCT: (
                        user_input.get(CONF_COST_PRODUCT) or DEFAULT_COST_PRODUCT
                    ).strip(),
                    CONF_COST_GRID: (
                        user_input.get(CONF_COST_GRID) or DEFAULT_COST_GRID
                    ).strip(),
                    CONF_COST_MUNICIPALITY: (
                        user_input.get(CONF_COST_MUNICIPALITY)
                        or DEFAULT_COST_MUNICIPALITY
                    ).strip(),
                    CONF_COST_SURCHARGE: float(
                        user_input.get(CONF_COST_SURCHARGE, DEFAULT_COST_SURCHARGE)
                    ),
                }
            )

        current = opts.get(
            CONF_TOTP_SECRET, self.config_entry.data.get(CONF_TOTP_SECRET, "")
        )
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Optional(CONF_TOTP_SECRET, default=current): str,
                    vol.Optional(
                        CONF_COST_ENABLED, default=opts.get(CONF_COST_ENABLED, False)
                    ): bool,
                    vol.Optional(
                        CONF_COST_PRODUCT,
                        default=opts.get(CONF_COST_PRODUCT, DEFAULT_COST_PRODUCT),
                    ): str,
                    vol.Optional(
                        CONF_COST_GRID,
                        default=opts.get(CONF_COST_GRID, DEFAULT_COST_GRID),
                    ): str,
                    vol.Optional(
                        CONF_COST_MUNICIPALITY,
                        default=opts.get(
                            CONF_COST_MUNICIPALITY, DEFAULT_COST_MUNICIPALITY
                        ),
                    ): str,
                    vol.Optional(
                        CONF_COST_SURCHARGE,
                        default=opts.get(CONF_COST_SURCHARGE, DEFAULT_COST_SURCHARGE),
                    ): vol.Coerce(float),
                }
            ),
        )


def _entry_data(email: str, password: str, totp_secret: str | None) -> dict[str, str]:
    data = {"email": email, "password": password}
    if totp_secret:
        data[CONF_TOTP_SECRET] = totp_secret
    return data
