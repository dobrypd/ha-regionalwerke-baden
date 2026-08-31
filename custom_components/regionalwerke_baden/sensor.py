"""Diagnostics sensors. Energy itself is via recorder statistics (Energy Dashboard)."""

from __future__ import annotations

import datetime as dt

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import BASE_URL
from .const import DOMAIN
from .coordinator import RwbCoordinator


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coord: RwbCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([RwbLastSyncSensor(coord), RwbObjekteSensor(coord)])


class RwbDiagnosticSensor(CoordinatorEntity[RwbCoordinator], SensorEntity):
    """Shared identity: one service device per account, translated entity names."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coord: RwbCoordinator, key: str) -> None:
        super().__init__(coord)
        self._attr_translation_key = key
        self._attr_unique_id = f"{coord.entry.entry_id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coord.entry.entry_id)},
            # Brand, not entry.title: the title is the account email, which would slug
            # into entity ids like sensor.user_example_com_last_sync. Multiple accounts
            # are disambiguated by the config entry title shown above the device.
            name="Regionalwerke Baden",
            manufacturer="Regionalwerke AG Baden",
            model="Kundenportal",
            entry_type=DeviceEntryType.SERVICE,
            configuration_url=BASE_URL,
        )


class RwbLastSyncSensor(RwbDiagnosticSensor):
    _attr_icon = "mdi:sync"
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, coord: RwbCoordinator) -> None:
        super().__init__(coord, "last_sync")

    @property
    def native_value(self) -> dt.datetime | None:
        # DataUpdateCoordinator has no last_update_success_time — only the
        # last_update_success bool — so the coordinator records this itself.
        return self.coordinator.last_sync


class RwbObjekteSensor(RwbDiagnosticSensor):
    _attr_icon = "mdi:home"

    def __init__(self, coord: RwbCoordinator) -> None:
        super().__init__(coord, "objekte")

    @property
    def native_value(self) -> int:
        return len((self.coordinator.data or {}).get("objekte", []))

    @property
    def extra_state_attributes(self):
        # Only the metering codes, never `bezeichnung` — that is the customer's
        # postal address, and state attributes are persisted by the recorder and
        # served over the REST/WebSocket APIs and in downloaded diagnostics.
        # The metering code is already visible in the Energy Dashboard statistic id.
        return {
            "metering_points": [
                obj["meteringcode"]
                for obj in (self.coordinator.data or {}).get("objekte", [])
            ]
        }
