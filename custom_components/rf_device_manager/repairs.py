"""Repairs support for RF Device Manager.

Creates a fixable issue when a managed device references a transmitter
entity that no longer exists; the fix flow lets the user pick a
replacement transmitter without recreating the device.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant import data_entry_flow
from homeassistant.components.repairs import RepairsFlow
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.selector import EntitySelector, EntitySelectorConfig

from .const import (
    CONF_TRANSMITTER,
    DOMAIN,
    ISSUE_MISSING_TRANSMITTER,
    RADIO_FREQUENCY_DOMAIN,
)
from .rf_manager import async_list_transmitters


@callback
def issue_id_for_entry(entry_id: str) -> str:
    """Return the issue id used for a missing transmitter of an entry."""
    return f"{ISSUE_MISSING_TRANSMITTER}_{entry_id}"


@callback
def async_check_transmitter_issue(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Create or clear the missing-transmitter issue for an entry."""
    transmitter = entry.options.get(CONF_TRANSMITTER, "")
    known = {info.entity_id for info in async_list_transmitters(hass)}
    if transmitter and transmitter in known:
        ir.async_delete_issue(hass, DOMAIN, issue_id_for_entry(entry.entry_id))
        return
    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id_for_entry(entry.entry_id),
        is_fixable=True,
        severity=ir.IssueSeverity.WARNING,
        translation_key=ISSUE_MISSING_TRANSMITTER,
        translation_placeholders={
            "name": entry.title,
            "transmitter": transmitter or "—",
        },
        data={"entry_id": entry.entry_id},
    )


class MissingTransmitterRepairFlow(RepairsFlow):
    """Fix flow: choose a replacement transmitter for a device."""

    def __init__(self, entry: ConfigEntry) -> None:
        """Initialize with the affected config entry."""
        self._entry = entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> data_entry_flow.FlowResult:
        """Ask for the replacement transmitter and apply it."""
        if user_input is not None:
            new_options = {
                **self._entry.options,
                CONF_TRANSMITTER: user_input[CONF_TRANSMITTER],
            }
            self.hass.config_entries.async_update_entry(
                self._entry, options=new_options
            )
            ir.async_delete_issue(
                self.hass, DOMAIN, issue_id_for_entry(self._entry.entry_id)
            )
            return self.async_create_entry(data={})

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_TRANSMITTER): EntitySelector(
                        EntitySelectorConfig(domain=RADIO_FREQUENCY_DOMAIN)
                    )
                }
            ),
            description_placeholders={
                "name": self._entry.title,
                "transmitter": self._entry.options.get(CONF_TRANSMITTER, "—"),
            },
        )


async def async_create_fix_flow(
    hass: HomeAssistant,
    issue_id: str,
    data: dict[str, Any] | None,
) -> RepairsFlow:
    """Create the fix flow for an issue."""
    entry = None
    if data and (entry_id := data.get("entry_id")):
        entry = hass.config_entries.async_get_entry(entry_id)
    if entry is None:
        raise ValueError(f"Unknown repair issue: {issue_id}")
    return MissingTransmitterRepairFlow(entry)
