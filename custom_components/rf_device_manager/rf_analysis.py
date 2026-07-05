"""RF signal analysis, decoding and encoding helpers.

Pure-Python utilities (no Home Assistant imports) that operate on raw OOK
timing trains: signed, alternating microseconds where positive values are
marks (carrier on) and negative values are spaces (carrier off).

Provided functionality:

* normalizing and trimming captured pulse trains
* splitting captures into repeated frames and picking the dominant one
* estimating the base pulse unit and quantizing frames
* stable fingerprints for matching received signals against stored codes
* decoding PWM (rc-switch style) and PT2262/EV1527 tri-state codes
* encoding binary / tri-state codes back into raw timings
* parsing Pronto hex and free-form manual timing input
"""

from __future__ import annotations

from collections import Counter
import hashlib
import json
import re
from typing import Any

from .const import IDLE_TRIM_US, MIN_SIGNAL_PULSES

# Splitting captures into frames: a space at least this long - or this many
# times the median pulse - terminates a frame.
_FRAME_GAP_MIN_US = 3_500
_FRAME_GAP_FACTOR = 6

# Quantization limits
_MAX_QUANT = 127

# rc-switch compatible protocol timing table. Each waveform is expressed in
# multiples of the base pulse length: (high units, low units). Frames are
# transmitted as all data bits followed by the sync waveform. Protocol 6
# (HT6P20B) is inverted and intentionally not supported.
RC_PROTOCOLS: dict[int, dict[str, Any]] = {
    1: {"pulse": 350, "sync": (1, 31), "zero": (1, 3), "one": (3, 1)},
    2: {"pulse": 650, "sync": (1, 10), "zero": (1, 2), "one": (2, 1)},
    3: {"pulse": 100, "sync": (30, 71), "zero": (4, 11), "one": (9, 6)},
    4: {"pulse": 380, "sync": (1, 6), "zero": (1, 3), "one": (3, 1)},
    5: {"pulse": 500, "sync": (6, 14), "zero": (1, 2), "one": (2, 1)},
    7: {"pulse": 150, "sync": (2, 62), "zero": (1, 6), "one": (6, 1)},
    8: {"pulse": 200, "sync": (3, 130), "zero": (7, 16), "one": (3, 16)},
}

_TRISTATE_TO_BITS = {"0": "00", "1": "11", "F": "01"}
_BITS_TO_TRISTATE = {"00": "0", "11": "1", "01": "F"}

# Pronto clock: one unit equals ``frequency_divider * 0.241246`` microseconds.
_PRONTO_CLOCK = 0.241246


class RFInputError(ValueError):
    """Raised when user-provided RF data cannot be parsed.

    ``error_key`` maps to a translation key used by the config flow.
    """

    def __init__(self, error_key: str, message: str) -> None:
        """Initialize with a translation key and a technical message."""
        super().__init__(message)
        self.error_key = error_key


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------


def ensure_signed(timings: list[int]) -> list[int]:
    """Return timings as signed alternating marks/spaces.

    Sources that deliver unsigned durations are converted by assuming the
    train starts with a mark and alternates strictly.
    """
    cleaned = [int(value) for value in timings if int(value) != 0]
    if not cleaned:
        return []
    if any(value < 0 for value in cleaned):
        return cleaned
    return [
        value if index % 2 == 0 else -value for index, value in enumerate(cleaned)
    ]


def trim_idle(timings: list[int], threshold: int = IDLE_TRIM_US) -> list[int]:
    """Drop leading/trailing idle gaps longer than ``threshold`` microseconds.

    Long idle periods around a capture are not part of the code and desync
    some receivers when re-transmitted.
    """
    trimmed = list(timings)
    while trimmed and abs(trimmed[0]) > threshold:
        trimmed.pop(0)
    while trimmed and abs(trimmed[-1]) > threshold:
        trimmed.pop()
    return trimmed


def normalize(timings: list[int]) -> list[int]:
    """Fully normalize a raw capture (signed + idle-trimmed)."""
    return trim_idle(ensure_signed(timings))


# ---------------------------------------------------------------------------
# Frame handling
# ---------------------------------------------------------------------------


def split_frames(timings: list[int]) -> list[list[int]]:
    """Split a pulse train into frames on long inter-frame gaps.

    The terminating gap (sync space) stays attached to its frame so frames
    remain directly transmittable.
    """
    if not timings:
        return []
    absolutes = sorted(abs(value) for value in timings)
    median = absolutes[len(absolutes) // 2]
    gap_threshold = max(_FRAME_GAP_MIN_US, _FRAME_GAP_FACTOR * median)

    frames: list[list[int]] = []
    current: list[int] = []
    for value in timings:
        current.append(value)
        if value < 0 and abs(value) >= gap_threshold:
            frames.append(current)
            current = []
    if current:
        frames.append(current)
    return frames


def dominant_frame(frames: list[list[int]]) -> list[int]:
    """Return the most representative frame of a capture.

    Frames are grouped by length; from the most common length group the
    median-duration frame is picked, which suppresses truncated first/last
    frames and single-bit glitches.
    """
    if not frames:
        return []
    if len(frames) == 1:
        return frames[0]

    length_counts = Counter(len(frame) for frame in frames)
    best_length = max(length_counts, key=lambda length: (length_counts[length], length))
    candidates = [frame for frame in frames if len(frame) == best_length]
    candidates.sort(key=lambda frame: sum(abs(value) for value in frame))
    return candidates[len(candidates) // 2]


def estimate_base_unit(frame: list[int]) -> int:
    """Estimate the base pulse unit (shortest timing cluster) of a frame."""
    durations = sorted(abs(value) for value in frame)
    if not durations:
        return 0
    # Drop the sync gap for unit estimation.
    if len(durations) > 4:
        durations = durations[:-1]

    clusters: list[list[int]] = [[durations[0]]]
    for duration in durations[1:]:
        cluster = clusters[-1]
        mean = sum(cluster) / len(cluster)
        if duration <= mean * 1.35:
            cluster.append(duration)
        else:
            clusters.append([duration])

    for cluster in clusters:
        if len(cluster) >= 2 or len(clusters) == 1:
            return round(sum(cluster) / len(cluster))
    return round(sum(clusters[0]) / len(clusters[0]))


def quantize_frame(frame: list[int], unit: int) -> list[int]:
    """Quantize frame durations to multiples of the base unit (signed)."""
    if unit <= 0:
        return []
    quantized: list[int] = []
    for value in frame:
        steps = min(_MAX_QUANT, max(1, round(abs(value) / unit)))
        quantized.append(steps if value > 0 else -steps)
    return quantized


def fingerprint(timings: list[int]) -> str | None:
    """Return a jitter-tolerant fingerprint of a capture, or ``None``.

    The dominant frame is quantized relative to its own base unit, so the
    fingerprint is stable across captures with a few percent timing jitter.
    """
    normalized = normalize(timings)
    if len(normalized) < MIN_SIGNAL_PULSES:
        return None
    frame = dominant_frame(split_frames(normalized))
    unit = estimate_base_unit(frame)
    symbols = quantize_frame(frame, unit)
    if not symbols:
        return None
    digest = hashlib.sha1(
        ",".join(str(symbol) for symbol in symbols).encode()
    ).hexdigest()[:12]
    return f"{len(symbols)}:{digest}"


def matches(timings: list[int], known_fingerprint: str | None) -> bool:
    """Return whether a capture matches a stored fingerprint."""
    if not known_fingerprint:
        return False
    return fingerprint(timings) == known_fingerprint


# ---------------------------------------------------------------------------
# Decoding
# ---------------------------------------------------------------------------


def _to_pairs(frame: list[int]) -> list[tuple[int, int]]:
    """Convert a frame into (mark, space) pairs, dropping partial edges."""
    values = frame[1:] if frame and frame[0] < 0 else list(frame)
    pairs: list[tuple[int, int]] = []
    for index in range(0, len(values) - 1, 2):
        mark, space = values[index], values[index + 1]
        if mark > 0 and space < 0:
            pairs.append((mark, -space))
    return pairs


def _guess_protocol(short: float, long: float, sync_low: float) -> int | None:
    """Match measured PWM ratios against the rc-switch protocol table."""
    if short <= 0:
        return None
    long_ratio = long / short
    sync_ratio = sync_low / short
    best: tuple[float, int] | None = None
    for protocol_id, spec in RC_PROTOCOLS.items():
        zero_high, zero_low = spec["zero"]
        expected_long = max(zero_low, spec["one"][0]) / min(zero_high, spec["one"][1])
        expected_sync = spec["sync"][1] / min(zero_high, spec["one"][1])
        error = abs(long_ratio - expected_long) / expected_long + abs(
            sync_ratio - expected_sync
        ) / max(expected_sync, 1)
        if error < 0.35 and (best is None or error < best[0]):
            best = (error, protocol_id)
    return best[1] if best else None


def decode_frame(frame: list[int]) -> dict[str, Any]:
    """Try to decode a single frame as a PWM (rc-switch style) bit stream.

    Returns a dict that may contain ``bits`` (binary string), ``hex``,
    ``tristate``, ``pulse_length`` and ``protocol_guess``. Empty dict when
    the frame does not look like a simple PWM code.
    """
    pairs = _to_pairs(frame)
    if len(pairs) < 5:
        return {}

    # The final pair carries the sync gap.
    *bit_pairs, sync_pair = pairs
    if len(bit_pairs) < 4:
        return {}

    durations = [duration for pair in bit_pairs for duration in pair]
    shorts = [d for d in durations if d <= min(durations) * 1.6]
    longs = [d for d in durations if d > min(durations) * 1.6]
    if not shorts or not longs:
        return {}
    short = sum(shorts) / len(shorts)
    long = sum(longs) / len(longs)
    if long / short < 1.4:
        return {}

    bits: list[str] = []
    for mark, space in bit_pairs:
        mark_is_long = abs(mark - long) < abs(mark - short)
        space_is_long = abs(space - long) < abs(space - short)
        if mark_is_long == space_is_long:
            return {}  # not a clean short/long PWM pair
        bits.append("1" if mark_is_long else "0")

    bit_string = "".join(bits)
    result: dict[str, Any] = {
        "bits": bit_string,
        "bit_count": len(bit_string),
        "hex": f"0x{int(bit_string, 2):X}",
        "pulse_length": round(short),
    }

    if len(bit_string) % 2 == 0:
        groups = [bit_string[i : i + 2] for i in range(0, len(bit_string), 2)]
        if all(group in _BITS_TO_TRISTATE for group in groups):
            result["tristate"] = "".join(_BITS_TO_TRISTATE[g] for g in groups)

    protocol = _guess_protocol(short, long, sync_pair[1])
    if protocol is not None:
        result["protocol_guess"] = protocol
    return result


def analyze(timings: list[int]) -> dict[str, Any]:
    """Run the full analysis pipeline over a capture.

    Returns a JSON-serializable dict used for previews, storage metadata and
    service responses.
    """
    normalized = normalize(timings)
    frames = split_frames(normalized)
    frame = dominant_frame(frames)
    unit = estimate_base_unit(frame)

    analysis: dict[str, Any] = {
        "pulse_count": len(normalized),
        "frame_count": len(frames),
        "frame_length": len(frame),
        "base_unit_us": unit,
        "duration_ms": round(sum(abs(v) for v in normalized) / 1000, 1),
        "fingerprint": fingerprint(normalized),
        "dominant_frame": frame,
    }
    analysis.update(decode_frame(frame))
    return analysis


def describe_signal(analysis: dict[str, Any], language: str = "en") -> str:
    """Return a compact one-line human readable signal summary."""
    german = language.lower().startswith("de")
    parts: list[str] = [
        f"{analysis.get('pulse_count', 0)} {'Pulse' if german else 'pulses'}",
        f"{analysis.get('frame_count', 0)} Frames",
    ]
    if unit := analysis.get("base_unit_us"):
        parts.append(f"~{unit} µs")
    if bit_count := analysis.get("bit_count"):
        parts.append(f"{bit_count} Bit" if german else f"{bit_count} bit")
    if hex_code := analysis.get("hex"):
        parts.append(hex_code)
    if tristate := analysis.get("tristate"):
        parts.append(f"Tri-State {tristate}")
    if protocol := analysis.get("protocol_guess"):
        parts.append(f"rc-switch P{protocol}")
    return " · ".join(parts)


def preview_timings(timings: list[int], limit: int = 20) -> str:
    """Return the first ``limit`` timings as a display string."""
    shown = " ".join(str(value) for value in timings[:limit])
    return f"{shown} …" if len(timings) > limit else shown


# ---------------------------------------------------------------------------
# Encoding
# ---------------------------------------------------------------------------


def encode_binary(
    bits: str,
    pulse_length: int | None = None,
    protocol: int = 1,
    frames: int = 4,
) -> list[int]:
    """Encode a binary string into raw timings using an rc-switch protocol.

    ``frames`` complete frames (bits + sync) are concatenated so a single
    transmission is reliably picked up by typical OOK receivers.
    """
    spec = RC_PROTOCOLS.get(protocol)
    if spec is None:
        raise RFInputError("invalid_protocol", f"Unknown rc protocol {protocol}")
    if not bits or any(bit not in "01" for bit in bits):
        raise RFInputError("invalid_binary", f"Not a binary string: {bits!r}")

    pulse = int(pulse_length or spec["pulse"])
    if pulse <= 0:
        raise RFInputError("invalid_pulse_length", "Pulse length must be positive")

    frame: list[int] = []
    for bit in bits:
        high, low = spec["one"] if bit == "1" else spec["zero"]
        frame.extend((high * pulse, -low * pulse))
    sync_high, sync_low = spec["sync"]
    frame.extend((sync_high * pulse, -sync_low * pulse))
    return frame * max(1, frames)


def tristate_to_bits(code: str) -> str:
    """Convert a PT2262/EV1527 tri-state code (0/1/F) into a bit string."""
    bits: list[str] = []
    for char in code.strip().upper():
        if char not in _TRISTATE_TO_BITS:
            raise RFInputError(
                "invalid_tristate", f"Invalid tri-state character: {char!r}"
            )
        bits.append(_TRISTATE_TO_BITS[char])
    if not bits:
        raise RFInputError("invalid_tristate", "Empty tri-state code")
    return "".join(bits)


def encode_tristate(
    code: str, pulse_length: int | None = None, protocol: int = 1, frames: int = 4
) -> list[int]:
    """Encode a tri-state code into raw timings."""
    return encode_binary(
        tristate_to_bits(code), pulse_length=pulse_length, protocol=protocol,
        frames=frames,
    )


# ---------------------------------------------------------------------------
# Parsers for manual input
# ---------------------------------------------------------------------------


def parse_timings_text(text: str) -> list[int]:
    """Parse manual raw-timing input (JSON array or separated integers)."""
    stripped = text.strip()
    if not stripped:
        raise RFInputError("invalid_timings", "Empty timing input")

    values: list[int]
    if stripped.startswith("["):
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError as err:
            raise RFInputError("invalid_timings", f"Invalid JSON: {err}") from err
        if not isinstance(parsed, list):
            raise RFInputError("invalid_timings", "JSON input is not a list")
        raw_items = parsed
    else:
        raw_items = re.split(r"[\s,;]+", stripped)

    try:
        values = [int(float(item)) for item in raw_items if str(item).strip()]
    except (TypeError, ValueError) as err:
        raise RFInputError("invalid_timings", f"Non-numeric timing value: {err}") from err

    values = ensure_signed(values)
    if len(values) < MIN_SIGNAL_PULSES:
        raise RFInputError("too_few_pulses", "Timing train too short")
    if any(abs(value) > 10_000_000 or abs(value) < 1 for value in values):
        raise RFInputError("invalid_timings", "Timing value out of range")
    return values


def parse_pronto(text: str) -> tuple[list[int], int]:
    """Parse a Pronto hex code into raw timings.

    Returns ``(timings, carrier_hz)``. Only learned-format codes (preamble
    ``0000``/``0100``) with a valid clock word are supported; the burst pairs
    are converted to microseconds using the encoded clock.
    """
    try:
        words = [int(word, 16) for word in text.split()]
    except ValueError as err:
        raise RFInputError("invalid_pronto", f"Invalid Pronto hex: {err}") from err

    if len(words) < 6:
        raise RFInputError("invalid_pronto", "Pronto code too short")
    preamble, clock, once_pairs, repeat_pairs = words[:4]
    if preamble not in (0x0000, 0x0100):
        raise RFInputError("invalid_pronto", f"Unsupported preamble {preamble:04X}")
    if clock == 0:
        raise RFInputError("invalid_pronto", "Pronto clock word is zero")

    unit_us = clock * _PRONTO_CLOCK
    carrier_hz = round(1_000_000 / unit_us)

    data = words[4:]
    pair_count = once_pairs if once_pairs else repeat_pairs
    needed = pair_count * 2
    if needed == 0 or len(data) < needed:
        raise RFInputError("invalid_pronto", "Pronto burst length mismatch")

    burst = data[:needed]
    timings = [
        round(value * unit_us) if index % 2 == 0 else -round(value * unit_us)
        for index, value in enumerate(burst)
    ]
    timings = [value if value != 0 else (1 if index % 2 == 0 else -1)
               for index, value in enumerate(timings)]
    if len(timings) < MIN_SIGNAL_PULSES:
        raise RFInputError("too_few_pulses", "Pronto burst too short")
    return timings, carrier_hz
