<p align="center">
  <img src="brand/rf_device_manager/icon@2x.png" width="128" alt="RF Device Manager">
</p>

<h1 align="center">RF Device Manager</h1>

<p align="center">
  <a href="https://github.com/hacs/integration"><img src="https://img.shields.io/badge/HACS-Custom-41BDF5.svg" alt="HACS Custom"></a>
  <img src="https://img.shields.io/badge/Home%20Assistant-2026.5%2B-blue.svg" alt="Home Assistant 2026.5+">
  <a href="https://github.com/Gifford47/rf_device_manager/releases"><img src="https://img.shields.io/github/v/release/Gifford47/rf_device_manager" alt="Release"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-green.svg" alt="MIT License"></a>
</p>

Full RF device management for Home Assistant — **entirely from the web UI**.
Create, learn, receive, edit, test, manage and send RF codes without ever
touching YAML or Python.

Built on the **`radio_frequency` entity platform introduced in Home Assistant
2026.5** and the **ESPHome `ir_rf_proxy`** component (e.g. CC1101/SX127x
hardware). Conceptually inspired by
[clevrdavid/rf_fan](https://github.com/clevrdavid/rf_fan), extended into a
generic RF device manager.

## ✨ Features

- **Device management via config flow**: create, duplicate, rename,
  reconfigure, disable and delete devices — every RF device is a real device
  in the device registry (including a `via_device` link to its ESPHome node).
- **RF transmitter selection**: automatic discovery of all `radio_frequency`
  transmitters (ESPHome ir_rf_proxy, Broadlink RM4 Pro, …) with frequency
  validation.
- **Learning wizard**: step-by-step per command, progress display
  ("Waiting for RF signal…"), automatic **signal clustering** (jittery
  repeats of the same button collapse into one entry, ranked by how often
  it was received), decoded previews (protocol, bits, hex, tri-state,
  rc-switch protocol guess) and live testing before saving.
- **Manual entry**: raw timings, **Tasmota/Portisch RfRaw B0/B1** bucket
  codes (`AA B0 … 55`), tri-state (PT2262/EV1527), binary with rc-switch
  protocols 1–8, Pronto hex — everything is normalized to raw timings
  internally.
- **9 entity types**: switch, light (stepped brightness), fan (speeds,
  oscillation, direction), cover (open/close/stop), button, select, number,
  plus sensor and binary sensor (**receive-side** via fingerprint matching,
  with auto-off).
- **Receiving**: incoming signals from ESPHome RF receivers are decoded and
  matched; every recognized signal fires the
  `rf_device_manager_code_received` event for automations.
- **Services**: `send_code`, `send_raw`, `learn_code` (returns captured
  signals as response data).
- **Repairs & diagnostics**: a missing transmitter raises a fixable issue
  with a replacement picker.
- **Persistence** via the HA storage API (`.storage/rf_device_manager`),
  versioned and migratable. No YAML files.
- Translations: **English** and **German**.

## 📋 Requirements

- Home Assistant **2026.5 or newer** (`radio_frequency` platform).
- **Sending**: at least one `radio_frequency` transmitter, e.g. an ESPHome
  node with sub-GHz hardware (CC1101) and `ir_rf_proxy`.
- **Learning/receiving**: an ESPHome `ir_rf_proxy` RF receiver (recommended)
  or the event fallback (see below). RF receivers do not appear as entities
  in Home Assistant — the integration discovers them automatically through
  the ESPHome API.

### ESPHome example (CC1101, transmit + receive)

```yaml
remote_transmitter:
  id: rf_tx
  pin: GPIOXX
  carrier_duty_percent: 100%
  on_transmit:
    then:
      - cc1101.begin_tx: cc1101_radio
  on_complete:
    then:
      - cc1101.begin_rx: cc1101_radio

remote_receiver:
  id: rf_rx
  pin: GPIOXX

radio_frequency:
  - platform: ir_rf_proxy
    name: RF Transmitter
    frequency: 433.92MHz
    remote_transmitter_id: rf_tx
  - platform: ir_rf_proxy
    name: RF Receiver
    frequency: 433.92MHz
    remote_receiver_id: rf_rx
```

### Event fallback for learning (optional)

Nodes without an ir_rf_proxy receiver can forward captures as an HA event:

```yaml
remote_receiver:
  pin: GPIOXX
  on_raw:
    then:
      - homeassistant.event:
          event: esphome.rf_code_received
          data_template:
            timings: !lambda "return x;"
```

## 📦 Installation

### HACS (recommended)

1. HACS → three dots (⋮) → **Custom repositories**.
2. Add `https://github.com/Gifford47/rf_device_manager` with category
   **Integration**.
3. Search for **RF Device Manager** and install it.
4. Restart Home Assistant.

### Manual

1. Copy the `custom_components/rf_device_manager` folder into the
   `custom_components` directory of your HA configuration.
2. Restart Home Assistant.

## 🚀 Usage

1. **Settings → Devices & Services → Add Integration → RF Device Manager**.
2. **Create a device**: name, description, RF transmitter, frequency,
   repeats.
3. Open **Configure** on the device → *Add new RF code*.
4. Choose the entity type and set its type-specific options (e.g. fan speed
   count).
5. Per command: **learn** (press the remote button), pick the signal,
   **test**, save — or enter the code manually.
6. The entity appears immediately — no restart, no reload.

Codes can be edited, re-learned per command, tested, disabled or deleted at
any time via *Manage RF codes*. Device base settings are also available via
**Reconfigure**.

## 🧠 Protocol detection

The integration does **not** need to know a remote's protocol: every signal
is captured, stored and re-transmitted as **raw microsecond timings**, so
any fixed-code remote works even when its protocol is completely unknown.

On top of that, a decoder chain recognizes the *structural encoding
patterns* used by virtually all sub-GHz OOK remotes and shows the result in
the UI — in the learning wizard (signal list + confirmation step) and
permanently in each entity's `commands` attribute:

| Encoding | Structure | Typical devices |
|---|---|---|
| **Space distance** | constant mark, short/long space | EV1527, PT2262, most cheap 433 MHz remotes |
| **Pulse width** | constant space, short/long mark | SIRC-style remotes |
| **PWM (rc-switch)** | mark *and* space share the bit | rc-switch protocols 1–8 (guess included) |
| **Manchester** | every bit is a level transition | Oregon sensors, some fan remotes |

Because detection is structural, *new/unknown protocols within these
patterns decode automatically*. Signals that match no pattern still work
via the raw fallback — they just show as "Raw" with timing statistics.

Decoded bits also power the jitter-immune **fingerprints** used to cluster
signals while learning and to match incoming signals for (binary) sensors.

**Sending** replays the captured timings exactly as received (only
normalized: sign convention, glitch removal, idle trim — no rotation,
quantization or "cleaning") and repeats them, by default 8 times on air
like briefly holding the remote button, since OOK receivers only accept a
code after several identical frames. The repeat count can be overridden per
code, per device or per service call.

> **Limitation:** rolling-code systems (KeeLoq, Somfy RTS, …) send a
> different code on every press — record/replay cannot work for those by
> design.

## 🛠️ Extending the decoders

All protocol recognition lives in one place:
[`custom_components/rf_device_manager/decoders.py`](custom_components/rf_device_manager/decoders.py).
It is pure Python (no Home Assistant imports) and every decoder documents
its waveform, so the file doubles as the reference for *how* each encoding
is decoded.

Adding support for a new encoding:

```python
# decoders.py
def _decode_my_encoding(context: FrameContext) -> DecodedFrame | None:
    """Document the waveform here (see the existing decoders)."""
    ...  # return None unless the frame *cleanly* matches your pattern
    return DecodedFrame(encoding="my_encoding", bits=bits, pulse_length=unit)

DECODERS = (
    ...,
    ("my_encoding", _decode_my_encoding),  # order = priority, see comment
)

ENCODINGS["my_encoding"] = EncodingInfo(
    key="my_encoding", name_en="My encoding", name_de="Meine Kodierung"
)
```

Then add a test case in [`tests/test_decoders.py`](tests/test_decoders.py) —
ideally from a real ESPHome `remote_receiver` log line (the EV1527 test in
there was captured exactly that way). New rc-switch timing profiles for
manual entry are a one-line addition to the `RC_PROTOCOLS` table.

### Development / tests

The decoder pipeline is testable without a Home Assistant installation:

```bash
python tests/test_decoders.py
```

The same tests run in CI (`.github/workflows/validate.yaml`) alongside
hassfest and HACS validation.

## 🔧 Services

| Service | Description |
|---|---|
| `rf_device_manager.send_code` | Send a stored command of a managed device (`device_id`, `code`, optional `command`, `repeat`). |
| `rf_device_manager.send_raw` | Send ad-hoc RF data (raw/tri-state/binary/Pronto) through any transmitter. |
| `rf_device_manager.learn_code` | Listen for RF signals; returns the analyzed captures as response data. |

## 🔔 Events

| Event | When |
|---|---|
| `rf_device_manager_code_received` | A received signal matches a stored code (data: `entity_id`, `code_name`, `slot`, `source`, …). |
| `rf_device_manager_code_learned` | A new code was saved. |

## 🖼️ Icon / branding

The icons in `brand/rf_device_manager/` (256 px + 512 px) are prepared for
the [home-assistant/brands](https://github.com/home-assistant/brands)
repository: submit them via pull request to
`custom_integrations/rf_device_manager/` (`icon.png`, `icon@2x.png`) — after
the merge Home Assistant shows the icon automatically in the integrations
dialog.

## 🏗️ Architecture

See [ARCHITECTURE.md](ARCHITECTURE.md) (German) for the data model, module
overview, API foundations (radio_frequency / aioesphomeapi) and extension
points (IR, MQTT, additional protocols).

## 📄 License

[MIT](LICENSE)
