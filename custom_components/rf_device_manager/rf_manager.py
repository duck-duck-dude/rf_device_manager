"""RF communication layer: sending, receiving and learning.

Sending goes through the official ``radio_frequency`` entity platform that
Home Assistant 2026.5 introduced (``async_send_command`` helper +
``rf_protocols.RadioFrequencyCommand``), so any compatible transmitter -
most prominently ESPHome nodes with the ``ir_rf_proxy`` component and
sub-GHz hardware such as a CC1101 - works out of the box.

Receiving/learning uses the ESPHome native API directly: Home Assistant core
does not (yet) expose RF *receiver* entities, but the ESPHome integration's
runtime data provides the ``aioesphomeapi`` client whose
``subscribe_infrared_rf_receive`` stream delivers raw captures from any
``ir_rf_proxy`` receiver (``RadioFrequencyInfo`` with the RECEIVER
capability) - the exact mechanism core uses for its infrared receivers.
A Home Assistant bus event (``esphome.rf_code_received``) is supported as an
additional, hardware-agnostic capture source.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
from functools import partial
import logging
from typing import Any

from homeassistant.components.radio_frequency import (
    async_get_transmitters as rf_async_get_transmitters,
    async_send_command as rf_async_send_command,
)
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_track_time_interval
from rf_protocols import ModulationType, RadioFrequencyCommand

from . import rf_analysis
from .const import (
    DEFAULT_LEARN_TIMEOUT,
    DOMAIN,
    ESPHOME_DOMAIN,
    ESPHOME_FALLBACK_EVENT,
    LEARN_COLLECT_WINDOW,
    LEARN_MAX_SIGNALS,
    MIN_SIGNAL_PULSES,
    RADIO_FREQUENCY_DOMAIN,
    RECEIVER_MAINTENANCE_INTERVAL,
)
from .models import CapturedSignal, RFCommandData

try:  # ESPHome is only an after_dependency; degrade gracefully without it.
    from aioesphomeapi import RadioFrequencyCapability, RadioFrequencyInfo

    HAS_AIOESPHOMEAPI = True
except ImportError:  # pragma: no cover - environment without ESPHome
    RadioFrequencyCapability = None  # type: ignore[assignment]
    RadioFrequencyInfo = None  # type: ignore[assignment]
    HAS_AIOESPHOMEAPI = False

_LOGGER = logging.getLogger(__name__)

SignalCallback = Callable[[CapturedSignal], None]


class _StoredCommand(RadioFrequencyCommand):
    """A stored/learned RF code replayed as raw OOK timings.

    We subclass the stable top-level ``rf_protocols.RadioFrequencyCommand``
    (the same import Home Assistant core uses) and set the attributes the
    transmitter reads directly. ``super().__init__`` is deliberately skipped
    to stay immune to constructor changes across rf-protocols releases; the
    transmitter only consumes ``frequency``, ``modulation``, ``repeat_count``
    and ``get_raw_timings()``.
    """

    def __init__(self, frequency: int, timings: list[int], repeat_count: int) -> None:
        """Initialize from normalized raw timings."""
        self.frequency = frequency
        self.modulation = ModulationType.OOK
        self.repeat_count = repeat_count
        self.symbol_rate = None
        self.output_power = None
        self._timings = timings

    def get_raw_timings(self) -> list[int]:
        """Return the signed alternating microsecond timings."""
        return self._timings


@dataclass(slots=True)
class TransmitterInfo:
    """Description of an available RF transmitter entity."""

    entity_id: str
    name: str
    available: bool
    device_id: str | None = None


@dataclass(slots=True)
class _ReceiverMeta:
    """Metadata of one ESPHome RF receiver."""

    name: str
    frequency: int | None


# ---------------------------------------------------------------------------
# Transmitter discovery / validation
# ---------------------------------------------------------------------------


@callback
def async_list_transmitters(hass: HomeAssistant) -> list[TransmitterInfo]:
    """Return all ``radio_frequency`` transmitter entities known to HA.

    The entity registry is the primary source (covers currently unavailable
    transmitters); plain states are merged in for completeness.
    """
    transmitters: dict[str, TransmitterInfo] = {}
    ent_reg = er.async_get(hass)

    for entry in ent_reg.entities.values():
        if entry.domain != RADIO_FREQUENCY_DOMAIN or entry.disabled:
            continue
        state = hass.states.get(entry.entity_id)
        transmitters[entry.entity_id] = TransmitterInfo(
            entity_id=entry.entity_id,
            name=entry.name or entry.original_name or entry.entity_id,
            available=state is not None and state.state != STATE_UNAVAILABLE,
            device_id=entry.device_id,
        )

    for entity_id in hass.states.async_entity_ids(RADIO_FREQUENCY_DOMAIN):
        if entity_id in transmitters:
            continue
        state = hass.states.get(entity_id)
        transmitters[entity_id] = TransmitterInfo(
            entity_id=entity_id,
            name=(state and state.name) or entity_id,
            available=state is not None and state.state != STATE_UNAVAILABLE,
        )

    return sorted(transmitters.values(), key=lambda info: info.name.casefold())


@callback
def async_validate_transmitter(
    hass: HomeAssistant, entity_id: str, frequency: int
) -> str | None:
    """Validate that a transmitter exists and supports a frequency.

    Returns ``None`` when valid, otherwise an error key
    (``transmitter_not_found`` / ``unsupported_frequency``).
    """
    known = {info.entity_id for info in async_list_transmitters(hass)}
    if entity_id not in known:
        return "transmitter_not_found"
    try:
        compatible = rf_async_get_transmitters(hass, frequency, ModulationType.OOK)
    except HomeAssistantError:
        # Component not loaded yet or no transmitters registered; the
        # registry check above already guarantees existence.
        return None
    if compatible and entity_id not in compatible:
        return "unsupported_frequency"
    return None


# ---------------------------------------------------------------------------
# Receiver hub
# ---------------------------------------------------------------------------


class ReceiverHub:
    """Multiplexes RF captures from all available sources to subscribers.

    Sources:

    * ESPHome nodes with ``ir_rf_proxy`` RF receivers, via the native API
      subscription (auto-discovered, resilient against reconnects through a
      periodic maintenance pass).
    * The Home Assistant bus event ``esphome.rf_code_received`` as a
      hardware-agnostic fallback (``timings`` list in the event data).

    The hub only holds active subscriptions while at least one consumer
    (learning session or receive-matching entity) is subscribed.
    """

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize the hub."""
        self._hass = hass
        self._subscribers: set[SignalCallback] = set()
        self._esphome_unsubs: dict[str, CALLBACK_TYPE] = {}
        self._bus_unsub: CALLBACK_TYPE | None = None
        self._timer_unsub: CALLBACK_TYPE | None = None
        self._cached_names: list[str] = []

    @callback
    def async_subscribe(self, signal_callback: SignalCallback) -> CALLBACK_TYPE:
        """Subscribe to captured signals; returns an unsubscribe callback."""
        self._subscribers.add(signal_callback)
        if len(self._subscribers) == 1:
            self._async_start()

        @callback
        def _unsubscribe() -> None:
            self._subscribers.discard(signal_callback)
            if not self._subscribers:
                self._async_stop()

        return _unsubscribe

    @callback
    def async_receiver_names(self) -> list[str]:
        """Return the receiver names found by the last discovery pass."""
        return list(self._cached_names)

    async def async_discover(
        self,
    ) -> dict[str, tuple[str, dict[tuple[int, int], _ReceiverMeta]]]:
        """Detect RF receivers on all connected ESPHome nodes.

        The static infos cached by the ESPHome integration are tried first,
        but current Home Assistant core only stores ``RadioFrequencyInfo``
        for *transmitters* (RF receiver entities do not exist yet), so
        receivers are usually only visible through a direct entity listing
        from the device (``list_entities_services``).

        Returns ``{entry_id: (node_name, receivers)}`` and refreshes the
        name cache used for UI display.
        """
        discovered: dict[str, tuple[str, dict[tuple[int, int], _ReceiverMeta]]] = {}
        names: list[str] = []
        if HAS_AIOESPHOMEAPI:
            for entry in self._hass.config_entries.async_entries(ESPHOME_DOMAIN):
                if entry.state is not ConfigEntryState.LOADED:
                    continue
                runtime_data = getattr(entry, "runtime_data", None)
                if runtime_data is None or not getattr(
                    runtime_data, "available", False
                ):
                    continue
                client = getattr(runtime_data, "client", None)
                if client is None:
                    continue
                receivers = self._receivers_from_info(runtime_data)
                if not receivers:
                    receivers = await self._receivers_from_device(client)
                if not receivers:
                    continue
                device_info = getattr(runtime_data, "device_info", None)
                node_name = getattr(device_info, "name", None) or entry.title
                discovered[entry.entry_id] = (node_name, receivers)
                names.extend(
                    f"{node_name}: {meta.name}" if meta.name else node_name
                    for meta in receivers.values()
                )
        self._cached_names = sorted(names)
        return discovered

    @staticmethod
    def _build_receiver_metas(infos: Any) -> dict[tuple[int, int], _ReceiverMeta]:
        """Build receiver metadata from RadioFrequencyInfo objects."""
        receivers: dict[tuple[int, int], _ReceiverMeta] = {}
        for info in infos:
            capabilities = int(getattr(info, "capabilities", 0))
            if not capabilities & int(RadioFrequencyCapability.RECEIVER):
                continue
            freq_min = int(getattr(info, "frequency_min", 0))
            freq_max = int(getattr(info, "frequency_max", 0))
            frequency = freq_min if freq_min and freq_min == freq_max else None
            receivers[(int(getattr(info, "device_id", 0)), int(info.key))] = (
                _ReceiverMeta(
                    name=getattr(info, "name", "") or "RF receiver",
                    frequency=frequency,
                )
            )
        return receivers

    @classmethod
    def _receivers_from_info(
        cls, runtime_data: Any
    ) -> dict[tuple[int, int], _ReceiverMeta]:
        """Extract receivers from the ESPHome integration's static infos."""
        info_map = getattr(runtime_data, "info", None)
        if not isinstance(info_map, dict):
            return {}
        rf_infos = info_map.get(RadioFrequencyInfo) or {}
        return cls._build_receiver_metas(rf_infos.values())

    async def _receivers_from_device(
        self, client: Any
    ) -> dict[tuple[int, int], _ReceiverMeta]:
        """Ask the ESPHome node itself for its RF receiver entities."""
        try:
            entities, _services = await client.list_entities_services()
        except Exception:  # noqa: BLE001 - connection may drop mid-call
            _LOGGER.debug("Could not list entities of ESPHome node", exc_info=True)
            return {}
        return self._build_receiver_metas(
            info for info in entities if isinstance(info, RadioFrequencyInfo)
        )

    @callback
    def _async_start(self) -> None:
        """Attach all capture sources."""
        self._bus_unsub = self._hass.bus.async_listen(
            ESPHOME_FALLBACK_EVENT, self._handle_bus_event
        )
        self._hass.async_create_task(self._async_attach_esphome())
        self._timer_unsub = async_track_time_interval(
            self._hass,
            self._async_maintain,
            timedelta(seconds=RECEIVER_MAINTENANCE_INTERVAL),
        )
        _LOGGER.debug("Receiver hub started")

    @callback
    def _async_stop(self) -> None:
        """Detach all capture sources."""
        if self._bus_unsub is not None:
            self._bus_unsub()
            self._bus_unsub = None
        if self._timer_unsub is not None:
            self._timer_unsub()
            self._timer_unsub = None
        for unsub in self._esphome_unsubs.values():
            try:
                unsub()
            except Exception:  # noqa: BLE001 - client may already be gone
                _LOGGER.debug("Error detaching ESPHome subscription", exc_info=True)
        self._esphome_unsubs.clear()
        _LOGGER.debug("Receiver hub stopped")

    @callback
    def _async_maintain(self, _now: Any = None) -> None:
        """Drop dead ESPHome subscriptions and attach new/reconnected nodes."""
        stale = [
            entry_id
            for entry_id in self._esphome_unsubs
            if not self._is_entry_available(entry_id)
        ]
        for entry_id in stale:
            unsub = self._esphome_unsubs.pop(entry_id)
            try:
                unsub()
            except Exception:  # noqa: BLE001
                _LOGGER.debug("Error detaching stale subscription", exc_info=True)
        self._hass.async_create_task(self._async_attach_esphome())

    def _is_entry_available(self, entry_id: str) -> bool:
        """Return whether an ESPHome config entry is loaded and connected."""
        entry = self._hass.config_entries.async_get_entry(entry_id)
        if entry is None or entry.state is not ConfigEntryState.LOADED:
            return False
        runtime_data = getattr(entry, "runtime_data", None)
        return bool(runtime_data and getattr(runtime_data, "available", False))

    async def _async_attach_esphome(self) -> None:
        """Subscribe to the IR/RF receive stream of all reachable nodes."""
        if not self._subscribers:
            return
        discovered = await self.async_discover()
        for entry_id, (node_name, receivers) in discovered.items():
            if entry_id in self._esphome_unsubs:
                continue
            entry = self._hass.config_entries.async_get_entry(entry_id)
            if entry is None:
                continue
            client = getattr(getattr(entry, "runtime_data", None), "client", None)
            subscribe = getattr(client, "subscribe_infrared_rf_receive", None)
            if subscribe is None:
                _LOGGER.debug(
                    "ESPHome node %s has RF receivers but no receive API "
                    "(aioesphomeapi too old?)",
                    node_name,
                )
                continue
            try:
                unsub = subscribe(
                    partial(self._handle_esphome_event, receivers, node_name)
                )
            except Exception:  # noqa: BLE001 - connection may drop mid-call
                _LOGGER.debug(
                    "Could not subscribe to %s, retrying on next maintenance pass",
                    node_name,
                    exc_info=True,
                )
                continue
            self._esphome_unsubs[entry_id] = unsub
            _LOGGER.debug(
                "Subscribed to %d RF receiver(s) on %s", len(receivers), node_name
            )

    @callback
    def _handle_esphome_event(
        self,
        receivers: dict[tuple[int, int], _ReceiverMeta],
        node_name: str,
        event: Any,
    ) -> None:
        """Handle one InfraredRFReceiveEvent from an ESPHome node."""
        key = (int(getattr(event, "device_id", 0)), int(getattr(event, "key", 0)))
        meta = receivers.get(key)
        if meta is None:
            return  # IR receiver or unknown proxy entity on the same node
        timings = list(getattr(event, "timings", []) or [])
        self._async_dispatch(
            timings,
            source=f"{node_name}: {meta.name}",
            frequency=meta.frequency,
        )

    @callback
    def _handle_bus_event(self, event: Event) -> None:
        """Handle the ``esphome.rf_code_received`` fallback bus event."""
        raw = (
            event.data.get("timings")
            or event.data.get("raw")
            or event.data.get("data")
        )
        if raw is None:
            return
        if isinstance(raw, str):
            try:
                timings = rf_analysis.parse_timings_text(raw)
            except rf_analysis.RFInputError:
                _LOGGER.debug("Ignoring unparsable bus event payload: %s", raw)
                return
        elif isinstance(raw, (list, tuple)):
            try:
                timings = [int(value) for value in raw]
            except (TypeError, ValueError):
                return
        else:
            return
        frequency = event.data.get("frequency")
        self._async_dispatch(
            timings,
            source=event.data.get("source", "event"),
            frequency=int(frequency) if frequency else None,
        )

    @callback
    def _async_dispatch(
        self, timings: list[int], source: str, frequency: int | None
    ) -> None:
        """Analyze a capture and fan it out to all subscribers."""
        normalized = rf_analysis.normalize(timings)
        if len(normalized) < MIN_SIGNAL_PULSES:
            return
        signal = CapturedSignal(
            timings=normalized,
            source=source,
            frequency=frequency,
            analysis=rf_analysis.analyze(normalized),
        )
        _LOGGER.debug(
            "RF capture from %s: %d pulses, fingerprint %s",
            source,
            len(normalized),
            signal.fingerprint,
        )
        for subscriber in tuple(self._subscribers):
            try:
                subscriber(signal)
            except Exception:  # noqa: BLE001 - one bad subscriber must not break others
                _LOGGER.exception("Error in RF signal subscriber")


# ---------------------------------------------------------------------------
# Learning
# ---------------------------------------------------------------------------


@dataclass
class LearnSession:
    """One learning pass: wait for signals, collect, deduplicate.

    ``async_capture`` blocks until the first signal arrives (or the timeout
    expires), then keeps collecting for a short window so button repeats and
    neighbouring codes show up as a selectable list.
    """

    hub: ReceiverHub
    timeout: float = DEFAULT_LEARN_TIMEOUT
    collect_window: float = LEARN_COLLECT_WINDOW
    max_signals: int = LEARN_MAX_SIGNALS
    signals: list[CapturedSignal] = field(default_factory=list)

    async def async_capture(self) -> list[CapturedSignal]:
        """Capture signals; raises ``TimeoutError`` when nothing is received."""
        first_signal = asyncio.Event()
        seen: dict[str, CapturedSignal] = {}

        @callback
        def _collect(signal: CapturedSignal) -> None:
            key = signal.fingerprint or f"raw-{len(self.signals)}"
            if (existing := seen.get(key)) is not None:
                existing.analysis["times_seen"] = (
                    existing.analysis.get("times_seen", 1) + 1
                )
                return
            if len(self.signals) >= self.max_signals:
                return
            signal.analysis.setdefault("times_seen", 1)
            seen[key] = signal
            self.signals.append(signal)
            first_signal.set()

        unsubscribe = self.hub.async_subscribe(_collect)
        try:
            async with asyncio.timeout(self.timeout):
                await first_signal.wait()
            await asyncio.sleep(self.collect_window)
        finally:
            unsubscribe()

        self.signals.sort(
            key=lambda sig: sig.analysis.get("times_seen", 1), reverse=True
        )
        return self.signals


# ---------------------------------------------------------------------------
# Manager facade
# ---------------------------------------------------------------------------


class RFManager:
    """Facade bundling transmit, receive and learn functionality."""

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize the manager and its receiver hub."""
        self._hass = hass
        self.hub = ReceiverHub(hass)

    async def async_send(
        self,
        command: RFCommandData,
        transmitter: str,
        *,
        frequency: int | None = None,
        repeat: int | None = None,
    ) -> None:
        """Send a stored command through a ``radio_frequency`` transmitter.

        Raises:
            HomeAssistantError: If the command holds no timings, the
                transmitter is missing/unavailable or transmission fails.
        """
        timings = rf_analysis.normalize(command.data)
        if not timings:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="empty_command"
            )
        # Note: a transmitter that has never sent reports "unknown" - that is
        # a valid, ready state; only "unavailable" (or a missing entity)
        # blocks sending.
        state = self._hass.states.get(transmitter)
        if state is None or state.state == STATE_UNAVAILABLE:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="transmitter_unavailable",
                translation_placeholders={"transmitter": transmitter},
            )
        rf_command = _StoredCommand(
            frequency=frequency or command.frequency,
            timings=timings,
            repeat_count=repeat if repeat is not None else command.repeat,
        )
        _LOGGER.debug(
            "Sending %d pulses at %d Hz via %s (repeat=%d)",
            len(timings),
            rf_command.frequency,
            transmitter,
            rf_command.repeat_count,
        )
        await rf_async_send_command(self._hass, transmitter, rf_command)

    async def async_send_raw(
        self,
        transmitter: str,
        timings: list[int],
        frequency: int,
        repeat: int = 0,
    ) -> None:
        """Send raw timings (service helper)."""
        command = RFCommandData(data=timings, frequency=frequency, repeat=repeat)
        await self.async_send(command, transmitter)

    def new_learn_session(
        self, timeout: float = DEFAULT_LEARN_TIMEOUT
    ) -> LearnSession:
        """Create a fresh learning session."""
        return LearnSession(hub=self.hub, timeout=timeout)
