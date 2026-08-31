"""Regionalwerke AG Baden integration."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_create_clientsession

from .const import DOMAIN
from .coordinator import RwbCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[str] = ["sensor"]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    # Own session, own cookie jar. async_get_clientsession shares one jar across all
    # of HA, so a second RWB account — or a config flow running beside the coordinator
    # — would silently overwrite this entry's PHPSESSID.
    # async_create_clientsession already registers detach() on this entry's unload.
    session = async_create_clientsession(hass)

    coordinator = RwbCoordinator(hass, entry, session)
    await coordinator.async_config_entry_first_refresh()
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unload
