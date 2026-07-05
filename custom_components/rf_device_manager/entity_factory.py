"""Entity base classes and dynamic entity creation.

Every stored RF code materializes as exactly one entity on the platform
matching its ``entity_type``. Platforms register through
``async_setup_code_platform`` which adds entities live whenever codes are
created in the options flow (no config entry reload required) and lets
entities remove themselves when their code is deleted.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
import logging
from typing import Any

from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity, EntityDescription
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import (
    EventStateChangedData,
    async_track_state_change_event,
)
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    CONF_ICON,
    DOMAIN,
    ENTITY_TYPE_BINARY_SENSOR,
    ENTITY_TYPE_BUTTON,
    ENTITY_TYPE_COVER,
    ENTITY_TYPE_FAN,
    ENTITY_TYPE_LIGHT,
    ENTITY_TYPE_NUMBER,
    ENTITY_TYPE_SELECT,
    ENTITY_TYPE_SENSOR,
    ENTITY_TYPE_SWITCH,
    EVENT_CODE_RECEIVED,
    MANUFACTURER,
    MODEL,
    RECEIVE_ONLY_TYPES,
)
from .coordinator import RFDeviceCoordinator, RFDeviceManagerConfigEntry
from .device_manager import get_domain_data
from .models import CapturedSignal, RFCode, RFCommandData

_LOGGER = logging.getLogger(__name__)

# Static per-type entity descriptions (default icons; device classes and
# features are configured per code through the options flow).
ENTITY_DESCRIPTIONS: dict[str, EntityDescription] = {
    ENTITY_TYPE_SWITCH: EntityDescription(key=ENTITY_TYPE_SWITCH),
    ENTITY_TYPE_LIGHT: EntityDescription(key=ENTITY_TYPE_LIGHT),
    ENTITY_TYPE_FAN: EntityDescription(key=ENTITY_TYPE_FAN),
    ENTITY_TYPE_COVER: EntityDescription(key=ENTITY_TYPE_COVER),
    ENTITY_TYPE_BUTTON: EntityDescription(key=ENTITY_TYPE_BUTTON, icon="mdi:remote"),
    ENTITY_TYPE_SELECT: EntityDescription(
        key=ENTITY_TYPE_SELECT, icon="mdi:tune-variant"
    ),
    ENTITY_TYPE_NUMBER: EntityDescription(key=ENTITY_TYPE_NUMBER, icon="mdi:tune"),
    ENTITY_TYPE_SENSOR: EntityDescription(
        key=ENTITY_TYPE_SENSOR, icon="mdi:radio-tower"
    ),
    ENTITY_TYPE_BINARY_SENSOR: EntityDescription(key=ENTITY_TYPE_BINARY_SENSOR),
}

CodeEntityFactory = Callable[[RFDeviceCoordinator, RFCode], Entity]


class RFCodeEntity(CoordinatorEntity[RFDeviceCoordinator], RestoreEntity):
    """Base entity backed by one stored RF code."""

    _attr_has_entity_name = True
    _attr_assumed_state = True
    _removing = False

    def __init__(self, coordinator: RFDeviceCoordinator, code: RFCode) -> None:
        """Initialize from the coordinator snapshot and a code record."""
        super().__init__(coordinator)
        self._code_id = code.id
        self._attr_unique_id = code.unique_id
        self.entity_description = ENTITY_DESCRIPTIONS[code.entity_type]
        device = coordinator.data.device
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, device.id)},
            name=device.name,
            manufacturer=MANUFACTURER,
            model=MODEL,
        )

    # ------------------------------------------------------------------
    # Code / device access
    # ------------------------------------------------------------------

    @property
    def code(self) -> RFCode | None:
        """Return the live code record, or ``None`` after deletion."""
        return self.coordinator.data.codes.get(self._code_id)

    @property
    def rf_device(self):
        """Return the live device config."""
        return self.coordinator.data.device

    # ------------------------------------------------------------------
    # Presentation
    # ------------------------------------------------------------------

    @property
    def name(self) -> str | None:
        """Return the code name (live, so store renames apply instantly)."""
        code = self.code
        return code.name if code else None

    @property
    def icon(self) -> str | None:
        """Return the user icon override or the type default."""
        code = self.code
        if code and (icon := code.settings.get(CONF_ICON)):
            return icon
        return super().icon

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Expose code metadata for automations and debugging."""
        code = self.code
        if code is None:
            return None
        attributes: dict[str, Any] = {
            "rf_device_id": code.device_id,
            "rf_code_id": code.id,
            "commands": sorted(code.commands),
        }
        if code.description:
            attributes["description"] = code.description
        return attributes

    # ------------------------------------------------------------------
    # Availability
    # ------------------------------------------------------------------

    @property
    def _requires_transmitter(self) -> bool:
        """Return whether this entity sends RF (vs. receive-only)."""
        code = self.code
        return code is not None and code.entity_type not in RECEIVE_ONLY_TYPES

    @property
    def available(self) -> bool:
        """Available when the code exists/enabled and the radio is usable."""
        if not super().available:
            return False
        code = self.code
        if code is None or not code.enabled or not self.rf_device.enabled:
            return False
        if self._requires_transmitter:
            state = self.hass.states.get(self.rf_device.transmitter)
            return state is not None and state.state != "unavailable"
        return True

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def async_added_to_hass(self) -> None:
        """Track the transmitter's availability for instant updates."""
        await super().async_added_to_hass()
        if self._requires_transmitter and self.rf_device.transmitter:

            @callback
            def _transmitter_changed(
                _event: Event[EventStateChangedData],
            ) -> None:
                self.async_write_ha_state()

            self.async_on_remove(
                async_track_state_change_event(
                    self.hass, [self.rf_device.transmitter], _transmitter_changed
                )
            )

    @callback
    def _handle_coordinator_update(self) -> None:
        """Self-remove when the code was deleted, otherwise refresh state."""
        if self.code is None:
            if not self._removing:
                self._removing = True
                self.hass.async_create_task(self.async_remove(force_remove=True))
            return
        super()._handle_coordinator_update()

    # ------------------------------------------------------------------
    # Sending
    # ------------------------------------------------------------------

    def _command(self, slot: str) -> RFCommandData | None:
        """Return the command stored in a slot, if any."""
        code = self.code
        return code.commands.get(slot) if code else None

    async def _async_send_slot(self, slot: str, repeat: int | None = None) -> None:
        """Transmit the command of a slot through the device transmitter.

        Raises:
            HomeAssistantError: If the slot is not configured or sending
                fails.
        """
        command = self._command(slot)
        if command is None:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="slot_not_configured",
                translation_placeholders={
                    "slot": slot,
                    "name": self.code.name if self.code else self._code_id,
                },
            )
        rf_manager = get_domain_data(self.hass).rf_manager
        await rf_manager.async_send(
            command, self.rf_device.transmitter, repeat=repeat
        )


class RFReceiveCodeEntity(RFCodeEntity):
    """Base for receive-side entities that match incoming RF signals.

    Subscribes to the receiver hub while added to Home Assistant; whenever a
    capture's fingerprint matches one of the code's command slots,
    ``_on_rf_match`` is invoked and ``rf_device_manager_code_received`` is
    fired on the event bus for automations.
    """

    _hub_unsub: CALLBACK_TYPE | None = None

    async def async_added_to_hass(self) -> None:
        """Subscribe to the receiver hub."""
        await super().async_added_to_hass()
        hub = get_domain_data(self.hass).rf_manager.hub
        self._hub_unsub = hub.async_subscribe(self._handle_signal)
        self.async_on_remove(self._async_unsubscribe_hub)

    @callback
    def _async_unsubscribe_hub(self) -> None:
        """Detach from the receiver hub."""
        if self._hub_unsub is not None:
            self._hub_unsub()
            self._hub_unsub = None

    @callback
    def _handle_signal(self, signal: CapturedSignal) -> None:
        """Match an incoming capture against the code's fingerprints."""
        code = self.code
        if code is None or not code.enabled or signal.fingerprint is None:
            return
        for slot, command in code.commands.items():
            if command.fingerprint and command.fingerprint == signal.fingerprint:
                _LOGGER.debug(
                    "RF match on %s: slot %s (%s)",
                    self.entity_id,
                    slot,
                    signal.fingerprint,
                )
                self.hass.bus.async_fire(
                    EVENT_CODE_RECEIVED,
                    {
                        "entity_id": self.entity_id,
                        "rf_device_id": code.device_id,
                        "code_id": code.id,
                        "code_name": code.name,
                        "slot": slot,
                        "source": signal.source,
                    },
                )
                self._on_rf_match(slot, signal)
                return

    @callback
    def _on_rf_match(self, slot: str, signal: CapturedSignal) -> None:
        """Handle a matched slot; implemented by receive platforms."""
        raise NotImplementedError


@callback
def async_setup_code_platform(
    hass: HomeAssistant,
    entry: RFDeviceManagerConfigEntry,
    async_add_entities: AddEntitiesCallback,
    entity_type: str,
    factory: CodeEntityFactory,
) -> None:
    """Create entities for all codes of a type and add new ones live.

    A coordinator listener diffs the stored codes on every update, so codes
    created through the options flow appear immediately without reloading
    the config entry. Deleted codes remove their entity via
    ``RFCodeEntity._handle_coordinator_update``.
    """
    coordinator = entry.runtime_data
    known_ids: set[str] = set()

    @callback
    def _async_sync_entities() -> None:
        codes: Iterable[RFCode] = coordinator.data.codes.values()
        new_codes = [
            code
            for code in codes
            if code.entity_type == entity_type and code.id not in known_ids
        ]
        if not new_codes:
            return
        known_ids.update(code.id for code in new_codes)
        async_add_entities(factory(coordinator, code) for code in new_codes)

    entry.async_on_unload(coordinator.async_add_listener(_async_sync_entities))
    _async_sync_entities()
