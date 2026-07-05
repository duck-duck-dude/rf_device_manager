"""Switch platform for RF Device Manager.

Optimistic (assumed-state) switches: ``on`` sends the on command, ``off``
sends the off command - or the on command again for toggle-style remotes
without a dedicated off code.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchDeviceClass, SwitchEntity
from homeassistant.const import STATE_ON
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_DEVICE_CLASS, ENTITY_TYPE_SWITCH
from .coordinator import RFDeviceCoordinator, RFDeviceManagerConfigEntry
from .entity_factory import RFCodeEntity, async_setup_code_platform
from .models import RFCode, SLOT_OFF, SLOT_ON

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RFDeviceManagerConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up RF switches for the config entry."""
    async_setup_code_platform(
        hass, entry, async_add_entities, ENTITY_TYPE_SWITCH, RFSwitch
    )


class RFSwitch(RFCodeEntity, SwitchEntity):
    """An RF code exposed as a switch."""

    _attr_is_on = False

    def __init__(self, coordinator: RFDeviceCoordinator, code: RFCode) -> None:
        """Initialize the switch."""
        super().__init__(coordinator, code)
        if device_class := code.settings.get(CONF_DEVICE_CLASS):
            try:
                self._attr_device_class = SwitchDeviceClass(device_class)
            except ValueError:
                self._attr_device_class = None

    async def async_added_to_hass(self) -> None:
        """Restore the optimistic state."""
        await super().async_added_to_hass()
        if (last_state := await self.async_get_last_state()) is not None:
            self._attr_is_on = last_state.state == STATE_ON

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Send the on command."""
        await self._async_send_slot(SLOT_ON)
        self._attr_is_on = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Send the off command (falls back to the on/toggle command)."""
        slot = SLOT_OFF if self._command(SLOT_OFF) is not None else SLOT_ON
        await self._async_send_slot(slot)
        self._attr_is_on = False
        self.async_write_ha_state()
