"""Cover platform for RF Device Manager.

Optimistic RF covers (garage doors, shutters, awnings, gates) with open,
close and optional stop commands.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.cover import (
    CoverDeviceClass,
    CoverEntity,
    CoverEntityFeature,
)
from homeassistant.const import STATE_CLOSED, STATE_OPEN
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_DEVICE_CLASS, ENTITY_TYPE_COVER
from .coordinator import RFDeviceCoordinator, RFDeviceManagerConfigEntry
from .entity_factory import RFCodeEntity, async_setup_code_platform
from .models import CapturedSignal, RFCode, SLOT_CLOSE, SLOT_OPEN, SLOT_STOP

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RFDeviceManagerConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up RF covers for the config entry."""
    async_setup_code_platform(
        hass, entry, async_add_entities, ENTITY_TYPE_COVER, RFCover
    )


class RFCover(RFCodeEntity, CoverEntity):
    """An RF code exposed as a cover."""

    _attr_is_closed: bool | None = None

    def __init__(self, coordinator: RFDeviceCoordinator, code: RFCode) -> None:
        """Initialize the cover and derive features from configured slots."""
        super().__init__(coordinator, code)
        features = CoverEntityFeature.OPEN | CoverEntityFeature.CLOSE
        if SLOT_STOP in code.commands:
            features |= CoverEntityFeature.STOP
        self._attr_supported_features = features
        if device_class := code.settings.get(CONF_DEVICE_CLASS):
            try:
                self._attr_device_class = CoverDeviceClass(device_class)
            except ValueError:
                self._attr_device_class = None

    async def async_added_to_hass(self) -> None:
        """Restore the optimistic state."""
        await super().async_added_to_hass()
        if (last_state := await self.async_get_last_state()) is None:
            return
        if last_state.state == STATE_CLOSED:
            self._attr_is_closed = True
        elif last_state.state == STATE_OPEN:
            self._attr_is_closed = False

    async def async_open_cover(self, **kwargs: Any) -> None:
        """Send the open command (optimistic state first for instant UI)."""
        self._attr_is_closed = False
        self.async_write_ha_state()
        await self._async_send_slot(SLOT_OPEN)

    async def async_close_cover(self, **kwargs: Any) -> None:
        """Send the close command."""
        self._attr_is_closed = True
        self.async_write_ha_state()
        await self._async_send_slot(SLOT_CLOSE)

    async def async_stop_cover(self, **kwargs: Any) -> None:
        """Send the stop command; the resulting position is unknown."""
        self._attr_is_closed = None
        self.async_write_ha_state()
        await self._async_send_slot(SLOT_STOP)

    @callback
    def _on_rf_match(self, slot: str, signal: CapturedSignal) -> None:
        """Reflect an external remote press in the cover state."""
        if slot == SLOT_OPEN:
            self._attr_is_closed = False
        elif slot == SLOT_CLOSE:
            self._attr_is_closed = True
        elif slot == SLOT_STOP:
            self._attr_is_closed = None
        else:
            return
        self.async_write_ha_state()
