"""Binary sensor platform for RF Device Manager.

Receive-side binary sensors: a matched ``on`` capture turns the sensor on;
it turns off either through a dedicated ``off`` capture or automatically
after a configurable delay (motion sensors, doorbells, door contacts).
"""

from __future__ import annotations

from datetime import timedelta

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.const import STATE_ON
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_call_later

from .const import CONF_DEVICE_CLASS, CONF_OFF_DELAY, ENTITY_TYPE_BINARY_SENSOR
from .coordinator import RFDeviceCoordinator, RFDeviceManagerConfigEntry
from .entity_factory import RFReceiveCodeEntity, async_setup_code_platform
from .models import CapturedSignal, RFCode, SLOT_OFF, SLOT_ON

PARALLEL_UPDATES = 0

DEFAULT_OFF_DELAY = 5.0  # seconds


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RFDeviceManagerConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up RF binary sensors for the config entry."""
    async_setup_code_platform(
        hass, entry, async_add_entities, ENTITY_TYPE_BINARY_SENSOR, RFBinarySensor
    )


class RFBinarySensor(RFReceiveCodeEntity, BinarySensorEntity):
    """An RF code exposed as a receive-matching binary sensor."""

    _attr_is_on = False
    _off_cancel: CALLBACK_TYPE | None = None

    def __init__(self, coordinator: RFDeviceCoordinator, code: RFCode) -> None:
        """Initialize the binary sensor."""
        super().__init__(coordinator, code)
        if device_class := code.settings.get(CONF_DEVICE_CLASS):
            try:
                self._attr_device_class = BinarySensorDeviceClass(device_class)
            except ValueError:
                self._attr_device_class = None

    async def async_added_to_hass(self) -> None:
        """Restore the last state."""
        await super().async_added_to_hass()
        if (last_state := await self.async_get_last_state()) is not None:
            self._attr_is_on = last_state.state == STATE_ON

    async def async_will_remove_from_hass(self) -> None:
        """Cancel a pending auto-off timer."""
        await super().async_will_remove_from_hass()
        self._cancel_auto_off()

    @callback
    def _cancel_auto_off(self) -> None:
        """Cancel the pending auto-off callback, if any."""
        if self._off_cancel is not None:
            self._off_cancel()
            self._off_cancel = None

    @callback
    def _schedule_auto_off(self) -> None:
        """Arm the auto-off timer when no dedicated off code exists."""
        code = self.code
        if code is None or SLOT_OFF in code.commands:
            return
        delay = float(code.settings.get(CONF_OFF_DELAY, DEFAULT_OFF_DELAY))
        if delay <= 0:
            return

        @callback
        def _auto_off(_now) -> None:
            self._off_cancel = None
            self._attr_is_on = False
            self.async_write_ha_state()

        self._off_cancel = async_call_later(
            self.hass, timedelta(seconds=delay), _auto_off
        )

    @callback
    def _on_rf_match(self, slot: str, signal: CapturedSignal) -> None:
        """Flip the state according to the matched slot."""
        self._cancel_auto_off()
        if slot == SLOT_ON:
            self._attr_is_on = True
            self._schedule_auto_off()
        elif slot == SLOT_OFF:
            self._attr_is_on = False
        else:
            return
        self.async_write_ha_state()
