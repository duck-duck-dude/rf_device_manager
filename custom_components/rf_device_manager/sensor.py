"""Sensor platform for RF Device Manager.

Receive-side sensors: incoming RF captures are matched against the learned
fingerprints of the code's value slots; on a match the sensor state becomes
the mapped value (e.g. weather station channels, remote key trackers).
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import CONF_UNIT, ENTITY_TYPE_SENSOR
from .coordinator import RFDeviceCoordinator, RFDeviceManagerConfigEntry
from .entity_factory import RFReceiveCodeEntity, async_setup_code_platform
from .models import CapturedSignal, RFCode, SLOT_VALUE_PREFIX

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RFDeviceManagerConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up RF sensors for the config entry."""
    async_setup_code_platform(
        hass, entry, async_add_entities, ENTITY_TYPE_SENSOR, RFSensor
    )


class RFSensor(RFReceiveCodeEntity, SensorEntity):
    """An RF code exposed as a receive-matching sensor."""

    _attr_native_value: str | None = None
    _last_received: str | None = None

    def __init__(self, coordinator: RFDeviceCoordinator, code: RFCode) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, code)
        self._attr_native_unit_of_measurement = code.settings.get(CONF_UNIT) or None

    async def async_added_to_hass(self) -> None:
        """Restore the last matched value."""
        await super().async_added_to_hass()
        if (last_state := await self.async_get_last_state()) is not None and (
            last_state.state not in (STATE_UNKNOWN, STATE_UNAVAILABLE)
        ):
            self._attr_native_value = last_state.state

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Add the last reception timestamp to the base attributes."""
        attributes = dict(super().extra_state_attributes or {})
        if self._last_received:
            attributes["last_received"] = self._last_received
        return attributes or None

    @callback
    def _on_rf_match(self, slot: str, signal: CapturedSignal) -> None:
        """Set the sensor state to the value mapped to the matched slot."""
        if not slot.startswith(SLOT_VALUE_PREFIX):
            return
        self._attr_native_value = slot[len(SLOT_VALUE_PREFIX) :]
        self._last_received = dt_util.utcnow().isoformat(timespec="seconds")
        self.async_write_ha_state()
