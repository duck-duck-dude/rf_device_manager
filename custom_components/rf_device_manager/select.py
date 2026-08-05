"""Select platform for RF Device Manager.

Each select option maps to its own RF command - useful for mode dials,
scene keys or multi-channel remotes.
"""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import ENTITY_TYPE_SELECT
from .coordinator import RFDeviceCoordinator, RFDeviceManagerConfigEntry
from .entity_factory import RFCodeEntity, async_setup_code_platform
from .models import CapturedSignal, RFCode, SLOT_OPTION_PREFIX

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RFDeviceManagerConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up RF selects for the config entry."""
    async_setup_code_platform(
        hass, entry, async_add_entities, ENTITY_TYPE_SELECT, RFSelect
    )


class RFSelect(RFCodeEntity, SelectEntity):
    """An RF code exposed as a select."""

    _attr_current_option: str | None = None

    def __init__(self, coordinator: RFDeviceCoordinator, code: RFCode) -> None:
        """Initialize the select from the configured option slots."""
        super().__init__(coordinator, code)
        self._attr_options = [
            slot[len(SLOT_OPTION_PREFIX) :]
            for slot in code.commands
            if slot.startswith(SLOT_OPTION_PREFIX)
        ]

    async def async_added_to_hass(self) -> None:
        """Restore the optimistic state."""
        await super().async_added_to_hass()
        if (last_state := await self.async_get_last_state()) is not None and (
            last_state.state not in (STATE_UNKNOWN, STATE_UNAVAILABLE)
            and last_state.state in self._attr_options
        ):
            self._attr_current_option = last_state.state

    async def async_select_option(self, option: str) -> None:
        """Send the command mapped to the chosen option."""
        self._attr_current_option = option
        self.async_write_ha_state()  # optimistic state first for instant UI
        await self._async_send_slot(f"{SLOT_OPTION_PREFIX}{option}")

    @callback
    def _on_rf_match(self, slot: str, signal: CapturedSignal) -> None:
        """Reflect an external remote press in the selected option."""
        if not slot.startswith(SLOT_OPTION_PREFIX):
            return
        option = slot[len(SLOT_OPTION_PREFIX) :]
        if option in self._attr_options:
            self._attr_current_option = option
            self.async_write_ha_state()
