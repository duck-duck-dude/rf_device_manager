"""Number platform for RF Device Manager.

Each configured value maps to its own RF command; requested values snap to
the nearest configured step (typical for RF dimmers or fan wall controls
with discrete levels).
"""

from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode, RestoreNumber
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_NUMBER_MODE, CONF_UNIT, ENTITY_TYPE_NUMBER
from .coordinator import RFDeviceCoordinator, RFDeviceManagerConfigEntry
from .entity_factory import RFCodeEntity, async_setup_code_platform
from .models import RFCode, SLOT_VALUE_PREFIX, format_number

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RFDeviceManagerConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up RF numbers for the config entry."""
    async_setup_code_platform(
        hass, entry, async_add_entities, ENTITY_TYPE_NUMBER, RFNumber
    )


class RFNumber(RFCodeEntity, RestoreNumber, NumberEntity):
    """An RF code exposed as a number with discrete mapped values."""

    _attr_native_value: float | None = None

    def __init__(self, coordinator: RFDeviceCoordinator, code: RFCode) -> None:
        """Initialize the number from the configured value slots."""
        super().__init__(coordinator, code)
        values: list[float] = []
        for slot in code.commands:
            if slot.startswith(SLOT_VALUE_PREFIX):
                try:
                    values.append(float(slot[len(SLOT_VALUE_PREFIX) :]))
                except ValueError:
                    continue
        self._values = sorted(values) or [0.0]

        self._attr_native_min_value = self._values[0]
        self._attr_native_max_value = self._values[-1]
        diffs = [
            second - first
            for first, second in zip(self._values, self._values[1:], strict=False)
            if second > first
        ]
        self._attr_native_step = min(diffs) if diffs else 1.0
        self._attr_native_unit_of_measurement = code.settings.get(CONF_UNIT) or None
        mode = code.settings.get(CONF_NUMBER_MODE, NumberMode.SLIDER)
        try:
            self._attr_mode = NumberMode(mode)
        except ValueError:
            self._attr_mode = NumberMode.SLIDER

    async def async_added_to_hass(self) -> None:
        """Restore the optimistic value."""
        await super().async_added_to_hass()
        if (last_data := await self.async_get_last_number_data()) is not None and (
            last_data.native_value is not None
        ):
            self._attr_native_value = last_data.native_value

    async def async_set_native_value(self, value: float) -> None:
        """Snap to the nearest configured value and send its command."""
        nearest = min(self._values, key=lambda candidate: abs(candidate - value))
        await self._async_send_slot(f"{SLOT_VALUE_PREFIX}{format_number(nearest)}")
        self._attr_native_value = nearest
        self.async_write_ha_state()
