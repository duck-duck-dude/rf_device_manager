"""Fan platform for RF Device Manager.

Optimistic RF fans with discrete speed steps, optional oscillation toggle
and optional direction commands - modelled after typical RF ceiling fan
remotes.
"""

from __future__ import annotations

import re
from typing import Any

from homeassistant.components.fan import (
    ATTR_OSCILLATING,
    ATTR_PERCENTAGE,
    DIRECTION_FORWARD,
    DIRECTION_REVERSE,
    FanEntity,
    FanEntityFeature,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util.percentage import (
    ordered_list_item_to_percentage,
    percentage_to_ordered_list_item,
)

from .const import ENTITY_TYPE_FAN
from .coordinator import RFDeviceCoordinator, RFDeviceManagerConfigEntry
from .entity_factory import RFCodeEntity, async_setup_code_platform
from .models import (
    RFCode,
    SLOT_DIRECTION_FORWARD,
    SLOT_DIRECTION_REVERSE,
    SLOT_OFF,
    SLOT_OSCILLATE,
    SLOT_SPEED_PREFIX,
)

PARALLEL_UPDATES = 1

_SPEED_SLOT_RE = re.compile(rf"^{SLOT_SPEED_PREFIX}(\d+)$")


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RFDeviceManagerConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up RF fans for the config entry."""
    async_setup_code_platform(hass, entry, async_add_entities, ENTITY_TYPE_FAN, RFFan)


class RFFan(RFCodeEntity, FanEntity):
    """An RF code exposed as a fan."""

    _attr_percentage = 0

    def __init__(self, coordinator: RFDeviceCoordinator, code: RFCode) -> None:
        """Initialize the fan and derive features from configured slots."""
        super().__init__(coordinator, code)
        self._speed_slots = self._ordered_speed_slots(code)

        features = FanEntityFeature.TURN_ON | FanEntityFeature.TURN_OFF
        if self._speed_slots:
            features |= FanEntityFeature.SET_SPEED
        if SLOT_OSCILLATE in code.commands:
            features |= FanEntityFeature.OSCILLATE
        if (
            SLOT_DIRECTION_FORWARD in code.commands
            and SLOT_DIRECTION_REVERSE in code.commands
        ):
            features |= FanEntityFeature.DIRECTION
        self._attr_supported_features = features

    @staticmethod
    def _ordered_speed_slots(code: RFCode) -> list[str]:
        """Return the configured speed slots ordered by speed number."""
        found: list[tuple[int, str]] = []
        for slot in code.commands:
            if match := _SPEED_SLOT_RE.match(slot):
                found.append((int(match.group(1)), slot))
        return [slot for _, slot in sorted(found)]

    @property
    def speed_count(self) -> int:
        """Return the number of discrete speeds."""
        return max(1, len(self._speed_slots))

    async def async_added_to_hass(self) -> None:
        """Restore the optimistic state."""
        await super().async_added_to_hass()
        if (last_state := await self.async_get_last_state()) is None:
            return
        if (percentage := last_state.attributes.get(ATTR_PERCENTAGE)) is not None:
            self._attr_percentage = percentage
        if (oscillating := last_state.attributes.get(ATTR_OSCILLATING)) is not None:
            self._attr_oscillating = oscillating
        if direction := last_state.attributes.get("direction"):
            self._attr_current_direction = direction

    async def async_set_percentage(self, percentage: int) -> None:
        """Send the speed command closest to the requested percentage."""
        if percentage == 0 or not self._speed_slots:
            await self._async_send_slot(SLOT_OFF)
            self._attr_percentage = 0
        else:
            slot = percentage_to_ordered_list_item(self._speed_slots, percentage)
            await self._async_send_slot(slot)
            self._attr_percentage = ordered_list_item_to_percentage(
                self._speed_slots, slot
            )
        self.async_write_ha_state()

    async def async_turn_on(
        self,
        percentage: int | None = None,
        preset_mode: str | None = None,
        **kwargs: Any,
    ) -> None:
        """Turn the fan on at the requested (or last known) speed."""
        if percentage is None:
            percentage = self._attr_percentage or 100
        await self.async_set_percentage(percentage)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Send the off command."""
        await self._async_send_slot(SLOT_OFF)
        self._attr_percentage = 0
        self.async_write_ha_state()

    async def async_oscillate(self, oscillating: bool) -> None:
        """Send the oscillation toggle command."""
        await self._async_send_slot(SLOT_OSCILLATE)
        self._attr_oscillating = oscillating
        self.async_write_ha_state()

    async def async_set_direction(self, direction: str) -> None:
        """Send the matching direction command."""
        slot = (
            SLOT_DIRECTION_REVERSE
            if direction == DIRECTION_REVERSE
            else SLOT_DIRECTION_FORWARD
        )
        await self._async_send_slot(slot)
        self._attr_current_direction = (
            DIRECTION_REVERSE if direction == DIRECTION_REVERSE else DIRECTION_FORWARD
        )
        self.async_write_ha_state()
