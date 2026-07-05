"""Button platform for RF Device Manager.

Stateless buttons that fire a single RF command - ideal for remote keys
that have no meaningful on/off semantics (pairing keys, dimmer presets,
doorbell chimes).
"""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import ENTITY_TYPE_BUTTON
from .coordinator import RFDeviceManagerConfigEntry
from .entity_factory import RFCodeEntity, async_setup_code_platform
from .models import SLOT_PRESS

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RFDeviceManagerConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up RF buttons for the config entry."""
    async_setup_code_platform(
        hass, entry, async_add_entities, ENTITY_TYPE_BUTTON, RFButton
    )


class RFButton(RFCodeEntity, ButtonEntity):
    """An RF code exposed as a button."""

    async def async_press(self) -> None:
        """Send the stored command."""
        await self._async_send_slot(SLOT_PRESS)
