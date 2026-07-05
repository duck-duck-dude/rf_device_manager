"""Data update coordinator for RF Device Manager config entries.

The coordinator is push-based (no polling): every storage mutation triggers
``async_refresh_from_store`` so entities and dynamically added platforms
react immediately without a config entry reload.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DOMAIN
from .models import RFCode, RFDeviceConfig
from .storage import RFDeviceStore

_LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class RFDeviceData:
    """Snapshot of one managed RF device and its codes."""

    device: RFDeviceConfig
    codes: dict[str, RFCode]


RFDeviceManagerConfigEntry = ConfigEntry["RFDeviceCoordinator"]


class RFDeviceCoordinator(DataUpdateCoordinator[RFDeviceData]):
    """Coordinator exposing the store content of a single RF device."""

    config_entry: RFDeviceManagerConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: RFDeviceManagerConfigEntry,
        store: RFDeviceStore,
        device_id: str,
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN}_{device_id}",
            update_interval=None,
        )
        self.store = store
        self.device_id = device_id

    def _build_data(self) -> RFDeviceData:
        """Assemble the current snapshot from the store."""
        device = self.store.async_get_device(self.device_id)
        if device is None:
            raise UpdateFailed(f"RF device {self.device_id} missing from store")
        return RFDeviceData(
            device=device,
            codes=self.store.async_codes_for_device(self.device_id),
        )

    async def _async_update_data(self) -> RFDeviceData:
        """Fetch the snapshot (initial refresh)."""
        return self._build_data()

    @callback
    def async_refresh_from_store(self) -> None:
        """Push a fresh snapshot to all listeners after a store mutation."""
        self.async_set_updated_data(self._build_data())
