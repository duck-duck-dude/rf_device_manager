<p align="center">
  <img src="brand/rf_device_manager/icon@2x.png" width="128" alt="RF Device Manager">
</p>

<h1 align="center">RF Device Manager</h1>

<p align="center">
  <a href="https://github.com/hacs/integration"><img src="https://img.shields.io/badge/HACS-Custom-41BDF5.svg" alt="HACS Custom"></a>
  <img src="https://img.shields.io/badge/Home%20Assistant-2026.5%2B-blue.svg" alt="Home Assistant 2026.5+">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-green.svg" alt="MIT License"></a>
</p>

Vollständige RF-Geräteverwaltung für Home Assistant — **komplett über die Web-UI**.
RF-Codes erstellen, anlernen, empfangen, bearbeiten, testen, verwalten und senden,
ohne jemals YAML oder Python anzufassen.

Baut auf der **`radio_frequency`-Entity-Plattform aus Home Assistant 2026.5** und dem
**ESPHome `ir_rf_proxy`** auf (z. B. CC1101-/SX127x-Hardware). Konzeptionell inspiriert
von [clevrdavid/rf_fan](https://github.com/clevrdavid/rf_fan), funktional erweitert zu
einer generischen RF-Geräteverwaltung.

*Full UI-based RF device management for Home Assistant: create, learn, receive, edit,
test and send RF codes — no YAML required. Built on the HA 2026.5 `radio_frequency`
platform and the ESPHome IR/RF proxy.*

## ✨ Funktionen

- **Geräteverwaltung über Config Flow**: Geräte erstellen, duplizieren, umbenennen,
  neu konfigurieren, deaktivieren und löschen — jedes RF-Gerät ist ein echtes Gerät im
  Device Registry (inkl. `via_device`-Verknüpfung zum ESPHome-Knoten).
- **RF-Sender-Auswahl**: Automatische Erkennung aller `radio_frequency`-Transmitter
  (ESPHome ir_rf_proxy, Broadlink RM4 Pro, …) inkl. Frequenz-Validierung.
- **RF-Codes anlernen**: Geführter Assistent mit Schrittanzeige pro Befehl,
  Fortschrittsanzeige („Warte auf RF-Signal…“), Mehrfachsignal-Auswahl mit dekodierter
  Vorschau (Pulse, Basiseinheit, Bits, Hex, Tri-State, rc-switch-Protokollvermutung)
  und Live-Test vor dem Speichern.
- **Manuelle Eingabe**: Raw-Timings, Tri-State (PT2262/EV1527), Binär mit
  rc-switch-Protokoll 1–8, Pronto-Hex — alles wird intern zu Raw-Timings normalisiert.
- **9 Entity-Typen**: Switch, Light (Helligkeits-Stepping), Fan (Stufen, Oszillation,
  Drehrichtung), Cover (Auf/Zu/Stopp), Button, Select, Number sowie Sensor und
  Binary Sensor (**empfangsseitig** per Fingerprint-Matching, mit Auto-Aus).
- **Empfang**: Eingehende Signale von ESPHome-RF-Empfängern werden dekodiert und
  gematcht; jedes erkannte Signal feuert das Event `rf_device_manager_code_received`.
- **Services**: `send_code`, `send_raw`, `learn_code` (liefert Signale als Response).
- **Repairs & Diagnostics**: Fehlender Sender → behebbares Issue mit Ersatz-Auswahl.
- **Persistenz** über die HA Storage API (`.storage/rf_device_manager`), versioniert.
- Übersetzungen: **Deutsch** und **Englisch**.

## 📋 Voraussetzungen

- Home Assistant **2026.5 oder neuer** (`radio_frequency`-Plattform).
- **Senden**: mindestens ein `radio_frequency`-Transmitter, z. B. ein ESPHome-Knoten
  mit Sub-GHz-Hardware (CC1101) und `ir_rf_proxy`.
- **Anlernen/Empfangen**: ein ESPHome-`ir_rf_proxy`-RF-Receiver (empfohlen) oder der
  Event-Fallback (siehe unten). RF-Empfänger erscheinen in Home Assistant nicht als
  Entities — die Integration findet sie automatisch über die ESPHome-API.

### ESPHome-Beispiel (CC1101, Senden + Empfangen)

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

### Event-Fallback fürs Anlernen (optional)

Knoten ohne ir_rf_proxy-Receiver können Captures als HA-Event liefern:

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

### HACS (empfohlen)

1. HACS → drei Punkte (⋮) → **Benutzerdefinierte Repositories**.
2. Repository-URL eintragen, Kategorie **Integration**, hinzufügen.
3. **RF Device Manager** suchen und installieren.
4. Home Assistant neu starten.

### Manuell

1. Den Ordner `custom_components/rf_device_manager` in das
   `custom_components`-Verzeichnis der HA-Konfiguration kopieren.
2. Home Assistant neu starten.

## 🚀 Bedienung

1. **Einstellungen → Geräte & Dienste → Integration hinzufügen → RF Device Manager**.
2. **Gerät erstellen**: Name, Beschreibung, RF-Sender, Frequenz, Wiederholungen.
3. Auf dem Gerät **Konfigurieren** öffnen → *Neuen RF-Code hinzufügen*.
4. Entitätstyp wählen, typspezifische Optionen setzen (z. B. Ventilator-Stufen).
5. Pro Befehl: **anlernen** (Taste drücken), Signal auswählen, **testen**, speichern —
   oder manuell eingeben.
6. Die Entität erscheint sofort — ohne Neustart und ohne Reload.

Codes lassen sich jederzeit über *RF-Codes verwalten* bearbeiten, einzeln neu
anlernen, testen, deaktivieren oder löschen. Geräte-Grundeinstellungen sind zusätzlich
über **Neu konfigurieren** (Reconfigure) erreichbar.

## 🔧 Services

| Service | Beschreibung |
|---|---|
| `rf_device_manager.send_code` | Gespeicherten Befehl eines Geräts senden (`device_id`, `code`, optional `command`, `repeat`). |
| `rf_device_manager.send_raw` | Ad-hoc-RF-Daten senden (Raw/Tri-State/Binär/Pronto) über einen beliebigen Transmitter. |
| `rf_device_manager.learn_code` | Auf RF-Signale warten; liefert die analysierten Captures als Response-Daten. |

## 🔔 Events

| Event | Wann |
|---|---|
| `rf_device_manager_code_received` | Ein empfangenes Signal passt zu einem gespeicherten Code (Daten: `entity_id`, `code_name`, `slot`, `source`, …). |
| `rf_device_manager_code_learned` | Ein neuer Code wurde gespeichert. |

## 🖼️ Icon / Branding

Die Icons in `brand/rf_device_manager/` (256 px + 512 px) sind für das
[home-assistant/brands](https://github.com/home-assistant/brands)-Repository
vorbereitet: Per Pull Request nach `custom_integrations/rf_device_manager/`
(`icon.png`, `icon@2x.png`) einreichen — danach zeigt Home Assistant das Icon
automatisch im Integrations-Dialog an.

## 🏗️ Architektur

Siehe [ARCHITECTURE.md](ARCHITECTURE.md) für Datenmodell, Modulübersicht,
API-Grundlagen (radio_frequency / aioesphomeapi) und Erweiterungspunkte
(IR, MQTT, weitere Protokolle).

## 📄 Lizenz

[MIT](LICENSE)
