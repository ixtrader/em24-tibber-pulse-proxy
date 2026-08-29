#!/usr/bin/env python3
"""Carlo-Gavazzi-EM24-Modbus-TCP-Emulation mit Echtzeitwerten eines Tibber Pulse.

Das Modul liest zyklisch den SML-Frame einer Tibber-Pulse-Bridge
(``/data.json?node_id=...``), dekodiert daraus die OBIS-Werte fuer Bezug,
Einspeisung und Momentanleistung und stellt sie unter Unit-ID 1 im
Registerlayout eines EM24 bereit. Verbraucher wie ein Victron
MultiPlus II GX koennen den Dienst dadurch unveraendert als EM24 einbinden.

Der Tibber Pulse misst nur die Summenwirkleistung. Sie wird deshalb gleichmaessig
auf drei Phasen verteilt; Spannung, Strom, Frequenz und Leistungsfaktor sind
plausible Ersatzwerte, da der Zaehler sie nicht liefert.

Konfiguration ueber Umgebungsvariablen:

* ``TIBBER_BRIDGE_HOST``, ``TIBBER_BRIDGE_PORT``, ``TIBBER_BRIDGE_NODEID``
* ``TIBBER_BRIDGE_USER``, ``TIBBER_BRIDGE_PASSWORD``, ``TIBBER_BRIDGE_LOGLEVEL``
* ``EM24_METER_HOST``, ``EM24_METER_PORT``, ``EM24_METER_ID``
"""

import base64
import logging
import os
import threading
from decimal import Decimal
from time import monotonic
from urllib.request import Request, urlopen

from pymodbus.constants import Endian
from pymodbus.datastore import ModbusSequentialDataBlock, ModbusServerContext, ModbusSlaveContext
from pymodbus.device import ModbusDeviceIdentification
from pymodbus.payload import BinaryPayloadBuilder
from pymodbus.server import StartTcpServer


# Escape-Sequenzen, die einen SML-Frame nach IEC 62056-5-3 begrenzen.
SML_START = b"\x1b\x1b\x1b\x1b\x01\x01\x01\x01"
SML_END = b"\x1b\x1b\x1b\x1b\x1a"

# OBIS-Kennzahlen: 1-0:1.8.0*255, 1-0:2.8.0*255 und 1-0:16.7.0*255.
OBIS_IMPORT = b"\x01\x00\x01\x08\x00\xff"
OBIS_EXPORT = b"\x01\x00\x02\x08\x00\xff"
OBIS_POWER = b"\x01\x00\x10\x07\x00\xff"

# Ersatzwerte, da der Tibber Pulse ausschliesslich Wirkleistung und
# Zaehlerstaende liefert.
VOLTAGE_LN = Decimal("230")
VOLTAGE_LL = Decimal("400")
FREQUENCY_HZ = Decimal("50")

# Victron akzeptiert einen Zaehler nur, wenn Register 11 die EM24-Modellnummer
# 1648 (0x670) liefert.
MODEL_REGISTER = 11
EM24_MODEL_NUMBER = 1648
BUILD_VERSION = os.getenv("BUILD_VERSION", "dev")


class SmlParseError(ValueError):
    """Der gelesene Payload enthaelt keinen auswertbaren SML-Frame."""


class Em24SlaveContext(ModbusSlaveContext):
    """Registerspeicher des emulierten EM24 mit Protokollierung der Zugriffe."""

    def getValues(self, function_code, address, count=1):
        """Beantwortet eine Modbus-Leseanfrage.

        Wird vom Pymodbus-Server bei jeder Registeranfrage aufgerufen.
        :param function_code: Modbus-Funktionscode, 3 fuer Holding-, 4 fuer
            Input-Register.
        :param address: Startadresse des angeforderten Registers.
        :param count: Anzahl der zu lesenden Register.
        :return: Liste der Registerwerte.
        """
        logging.getLogger(__name__).info(
            "Modbus-Leseanfrage: Funktion %s, Register %s, Anzahl %s",
            function_code,
            address,
            count,
        )
        # Die Modellnummer wird hier erzeugt, weil der 32-Bit-Spannungswert auf
        # Adresse 10 das Register 11 mit seinem High-Word ueberschreibt.
        if address == MODEL_REGISTER and count == 1:
            return [EM24_MODEL_NUMBER]
        return super().getValues(function_code, address, count)


def setting(name, default=""):
    """Liest eine Umgebungsvariable ohne umgebende Leerzeichen.

    :param name: Name der Umgebungsvariable.
    :param default: Rueckgabewert, falls die Variable nicht gesetzt ist.
    :return: Wert der Variable als String.
    """
    return os.getenv(name, default).strip()


def tibber_url():
    """Baut die Daten-URL der Tibber-Pulse-Bridge aus den Umgebungsvariablen.

    :return: Vollstaendige URL auf ``data.json`` des konfigurierten Knotens.
    :raises ValueError: Wenn Host oder Node-ID fehlen.
    """
    host = setting("TIBBER_BRIDGE_HOST")
    port = setting("TIBBER_BRIDGE_PORT", "80")
    node_id = setting("TIBBER_BRIDGE_NODEID")
    if not host or not node_id:
        raise ValueError("TIBBER_BRIDGE_HOST und TIBBER_BRIDGE_NODEID muessen gesetzt sein.")
    return f"http://{host}:{port}/data.json?node_id={node_id}"


def fetch_sml():
    """Laedt den binaeren SML-Frame von der Tibber-Pulse-Bridge.

    Die Bridge verlangt HTTP-Basic-Authentifizierung und antwortet trotz des
    Dateinamens ``data.json`` nicht mit JSON, sondern mit Rohdaten.
    :return: Antwortkoerper als Bytefolge.
    """
    request = Request(tibber_url())
    username = setting("TIBBER_BRIDGE_USER")
    password = setting("TIBBER_BRIDGE_PASSWORD")
    if username:
        token = base64.b64encode(f"{username}:{password}".encode()).decode()
        request.add_header("Authorization", f"Basic {token}")
    with urlopen(request, timeout=5) as response:
        return response.read()


def parse_element(payload, offset):
    """Dekodiert ein einzelnes SML-Element ab ``offset``.

    Ruft sich fuer Listen rekursiv selbst auf.
    :param payload: Inhalt des SML-Frames ohne Start- und Endsequenz.
    :param offset: Byteposition, an der das Element beginnt.
    :return: Tupel aus dekodiertem Wert und naechster Byteposition.
    :raises SmlParseError: Bei unvollstaendigen Daten oder unbekanntem Typ.
    """
    if offset >= len(payload):
        raise SmlParseError("Unerwartetes Ende des SML-Payloads.")
    # 0x00 markiert das Ende einer SML-Nachricht und traegt keinen Wert.
    if payload[offset] == 0:
        return None, offset + 1

    first = payload[offset]
    element_type = first & 0x70
    length = 0
    header_length = 0
    # Der Laengenheader ist fortsetzbar: Bit 7 zeigt ein weiteres Laengenbyte an.
    while True:
        if offset + header_length >= len(payload):
            raise SmlParseError("Unvollstaendiger SML-Laengenheader.")
        header = payload[offset + header_length]
        length = (length << 4) | (header & 0x0F)
        header_length += 1
        if not header & 0x80:
            break

    position = offset + header_length
    if element_type == 0x70:
        # Bei Listen zaehlt die Laenge Elemente, nicht Bytes.
        values = []
        for _ in range(length):
            value, position = parse_element(payload, position)
            values.append(value)
        return values, position

    # Bei Skalaren zaehlt die Laenge Bytes inklusive Header.
    value_length = length - header_length
    if value_length < 0 or position + value_length > len(payload):
        raise SmlParseError("Unvollstaendiger SML-Wert.")
    raw_value = payload[position : position + value_length]
    position += value_length
    if element_type == 0x00:
        return (raw_value if raw_value else None), position
    if element_type == 0x40:
        return bool(raw_value and raw_value[0]), position
    if element_type == 0x50:
        return int.from_bytes(raw_value, byteorder="big", signed=True), position
    if element_type == 0x60:
        return int.from_bytes(raw_value, byteorder="big", signed=False), position
    raise SmlParseError(f"Unbekannter SML-Typ 0x{element_type:02x}.")


def find_entries(value):
    """Sucht rekursiv alle OBIS-Listeneintraege einer SML-Nachricht.

    Ein Eintrag besteht aus sieben Feldern, beginnend mit der 6 Byte langen
    OBIS-Kennzahl.
    :param value: Dekodierte SML-Struktur oder Teilbaum daraus.
    :return: Generator ueber die gefundenen Listeneintraege.
    """
    if not isinstance(value, list):
        return
    if (
        len(value) == 7
        and isinstance(value[0], bytes)
        and len(value[0]) == 6
        and (value[3] is None or isinstance(value[3], int))
    ):
        yield value
    for child in value:
        yield from find_entries(child)


def read_meter_values():
    """Liest und dekodiert die aktuellen Zaehlerwerte des Tibber Pulse.

    :return: Tupel aus Wirkleistung in W, Bezug in Wh und Einspeisung in Wh.
    :raises SmlParseError: Wenn der Frame unvollstaendig ist oder OBIS-Werte
        fehlen.
    """
    payload = fetch_sml()
    start = payload.find(SML_START)
    end = payload.find(SML_END, start + len(SML_START))
    if start < 0 or end < 0:
        raise SmlParseError("Keinen vollstaendigen SML-Frame erhalten.")

    messages = []
    content = payload[start + len(SML_START) : end]
    offset = 0
    while offset < len(content):
        value, offset = parse_element(content, offset)
        if value is not None:
            messages.append(value)

    values = {}
    for message in messages:
        for obis, _, _, _, scaler, raw_value, _ in find_entries(message):
            if isinstance(raw_value, int):
                # Der Skalierungsfaktor ist der Zehnerexponent des Rohwerts.
                values[obis] = Decimal(raw_value) * (Decimal(10) ** (scaler or 0))

    missing = [obis.hex() for obis in (OBIS_IMPORT, OBIS_EXPORT, OBIS_POWER) if obis not in values]
    if missing:
        raise SmlParseError(f"Fehlende OBIS-Werte: {', '.join(missing)}")
    return values[OBIS_POWER], values[OBIS_IMPORT], values[OBIS_EXPORT]


def write_i32(context, address, value):
    """Schreibt einen vorzeichenbehafteten 32-Bit-Wert in zwei Register.

    Der EM24 erwartet die Wortreihenfolge Low-Word zuerst. Geschrieben wird in
    Holding- und Input-Register, da Clients beide Funktionscodes nutzen.
    :param context: Registerspeicher des emulierten Zaehlers.
    :param address: Startadresse des Registerpaars.
    :param value: Zu schreibender Wert, wird kaufmaennisch gerundet.
    :return: None; der Registerspeicher wird direkt veraendert.
    """
    builder = BinaryPayloadBuilder(byteorder=Endian.BIG, wordorder=Endian.LITTLE)
    builder.add_32bit_int(round(value))
    registers = builder.to_registers()
    context.setValues(3, address, registers)
    context.setValues(4, address, registers)


def write_i16(context, address, value, unsigned=False):
    """Schreibt einen 16-Bit-Wert in ein einzelnes Register.

    :param context: Registerspeicher des emulierten Zaehlers.
    :param address: Adresse des Registers.
    :param value: Zu schreibender Wert, wird kaufmaennisch gerundet.
    :param unsigned: True fuer vorzeichenlose Werte wie die Frequenz.
    :return: None; der Registerspeicher wird direkt veraendert.
    """
    builder = BinaryPayloadBuilder(byteorder=Endian.BIG, wordorder=Endian.LITTLE)
    (builder.add_16bit_uint if unsigned else builder.add_16bit_int)(round(value))
    registers = builder.to_registers()
    context.setValues(3, address, registers)
    context.setValues(4, address, registers)


def write_text(context, address, value):
    """Schreibt eine ASCII-Zeichenkette in aufeinanderfolgende Register.

    :param context: Registerspeicher des emulierten Zaehlers.
    :param address: Startadresse des Textblocks.
    :param value: Zu schreibende Zeichenkette.
    :return: None; der Registerspeicher wird direkt veraendert.
    """
    builder = BinaryPayloadBuilder(byteorder=Endian.BIG, wordorder=Endian.LITTLE)
    builder.add_string(value)
    registers = builder.to_registers()
    context.setValues(3, address, registers)
    context.setValues(4, address, registers)


def write_static_registers(context, meter_id):
    """Schreibt die unveraenderlichen Kennungs- und Konfigurationsregister.

    Die Werte entsprechen dem produktiv erprobten EM24-Proxy, damit Victron den
    Zaehler als bekanntes Geraet akzeptiert.
    :param context: Registerspeicher des emulierten Zaehlers.
    :param meter_id: Zaehler-ID aus ``EM24_METER_ID``. Register 768 bleibt
        bewusst 0, weil der Referenzzaehler dort ebenfalls 0 meldet.
    :return: None; der Registerspeicher wird direkt veraendert.
    """
    write_i16(context, MODEL_REGISTER, EM24_MODEL_NUMBER)
    write_i32(context, 768, 0)  # Seriennummer
    write_i16(context, 770, 4126)  # Messmodul: Version
    write_i16(context, 771, 68)  # Messmodul: Revision
    write_i16(context, 772, 4127)  # Kommunikationsmodul: Version
    write_i16(context, 773, 67)  # Kommunikationsmodul: Revision
    write_i16(context, 774, 0)  # aktueller Tarif
    write_i16(context, 848, 4128)  # Firmware-CRC des Messmoduls
    write_text(context, 20480, "MB24DINAV23XE1X")  # Geraetekennung
    write_i16(context, 4096, 9999)  # Passwort
    write_i16(context, 4097, 0)
    write_i16(context, 4098, 0)  # Messsystem
    write_i32(context, 4099, 10)  # Stromwandlerverhaeltnis
    write_i32(context, 4101, 10)  # Spannungswandlerverhaeltnis
    for address, value in enumerate(range(1, 10), start=4103):
        write_i16(context, address, value)
    write_i32(context, 4112, 15)  # Intervallzeit
    write_i16(context, 4360, 2)
    write_i16(context, 4361, 2)
    # Anwendungstyp, Standardseiten je Selektorstellung und Benutzer-IDs.
    for address, value in enumerate((1, 3, 1, 3, 3, 1, 2, 3), start=40960):
        write_i16(context, address, value)
    write_i16(context, 41216, 3)  # Frontselektor-Status


def write_measurements(context, power_w, import_wh, export_wh):
    """Bildet die Zaehlerwerte auf das EM24-Registerlayout ab.

    Wird bei jedem Aktualisierungszyklus aufgerufen. Die Summenwirkleistung
    wird gleichmaessig auf L1 bis L3 verteilt, der Phasenstrom daraus mit der
    angenommenen Sternspannung berechnet.
    :param context: Registerspeicher des emulierten Zaehlers.
    :param power_w: Momentane Wirkleistung in W, positiv bei Bezug.
    :param import_wh: Zaehlerstand Bezug in Wh.
    :param export_wh: Zaehlerstand Einspeisung in Wh.
    :return: None; der Registerspeicher wird direkt veraendert.
    """
    phase_power = power_w / 3
    phase_current_ma = abs(phase_power) / VOLTAGE_LN * 1000
    # Der EM24 fuehrt Energiezaehler in 0,01-kWh-Schritten.
    import_centikwh = import_wh / 10
    export_centikwh = export_wh / 10

    # Messwertblock ab 0x0000: Spannungen, Stroeme, Leistungen und Energien.
    values_i32 = {
        0: VOLTAGE_LN * 10, 2: VOLTAGE_LN * 10, 4: VOLTAGE_LN * 10,
        6: VOLTAGE_LL * 10, 8: VOLTAGE_LL * 10, 10: VOLTAGE_LL * 10,
        12: phase_current_ma, 14: phase_current_ma, 16: phase_current_ma,
        18: phase_power * 10, 20: phase_power * 10, 22: phase_power * 10,
        24: abs(phase_power) * 10, 26: abs(phase_power) * 10, 28: abs(phase_power) * 10,
        30: 0, 32: 0, 34: 0, 36: VOLTAGE_LN * 10, 38: VOLTAGE_LL * 10,
        40: power_w * 10, 42: abs(power_w) * 10, 44: 0,
        52: import_centikwh, 54: 0, 56: 0, 58: 0, 60: import_centikwh, 62: 0,
        64: import_centikwh / 3, 66: import_centikwh / 3, 68: import_centikwh / 3,
        70: 0, 72: 0, 74: 0, 76: 0, 78: export_centikwh, 80: 0,
        82: 2400, 84: 11, 86: 22, 88: 33, 90: 44, 92: 118, 94: 120, 96: 122,
    }
    for address, value in values_i32.items():
        write_i32(context, address, value)

    # Leistungsfaktoren als 16-Bit-Werte, 1000 entspricht cos phi = 1.
    for address in (46, 47, 48, 49):
        write_i16(context, address, 1000)
    write_i16(context, 50, 0)  # Phasenfolge L1-L2-L3
    write_i16(context, 51, FREQUENCY_HZ * 10, unsigned=True)

    # Zweiter Messwertblock ab 0x00FE mit Summen- und Phasenwerten.
    phase_values = {
        254: 2400, 256: 256, 258: VOLTAGE_LN * 10, 260: VOLTAGE_LL * 10,
        262: power_w * 10, 264: abs(power_w) * 10, 266: 0, 268: 1000,
        270: 0, 272: FREQUENCY_HZ * 10, 274: import_centikwh, 276: 0,
        278: export_centikwh, 280: 0, 282: 0, 284: 0,
    }
    for phase in range(3):
        # Je Phase 14 Register: Spannungen, Strom, Leistungen, Leistungsfaktor.
        base = 286 + phase * 14
        phase_values.update({
            base: VOLTAGE_LL * 10, base + 2: VOLTAGE_LN * 10,
            base + 4: phase_current_ma, base + 6: phase_power * 10,
            base + 8: abs(phase_power) * 10, base + 10: 0, base + 12: 1000,
        })
    phase_values.update({
        328: 0, 330: import_centikwh, 332: 0,
        334: import_centikwh / 3, 336: import_centikwh / 3, 338: import_centikwh / 3,
        340: 10, 342: 20, 344: 30, 346: 40,
        348: 346, 350: 348, 352: 350, 354: 352,
        356: 11, 358: 22, 360: 33, 362: 44,
        364: 262, 366: 264, 368: 266, 370: 268,
        372: 270, 374: 272, 376: 274, 378: 276,
        380: 118, 382: 120, 384: 122,
    })
    for address, value in phase_values.items():
        write_i32(context, address, value)


def update_loop(context, meter_id, stop_event):
    """Aktualisiert die Register im Sekundentakt aus den Tibber-Pulse-Daten.

    Laeuft als Hintergrund-Thread, bis ``stop_event`` gesetzt wird. Faellt die
    Bridge aus, bleiben die zuletzt gelesenen Werte stehen, damit der Zaehler
    fuer Victron erreichbar bleibt.
    :param context: Registerspeicher des emulierten Zaehlers.
    :param meter_id: Zaehler-ID fuer die statischen Register.
    :param stop_event: threading.Event zum kontrollierten Beenden.
    :return: Kehrt erst nach dem Setzen von ``stop_event`` zurueck.
    """
    logger = logging.getLogger(__name__)
    power_w = import_wh = export_wh = Decimal(0)
    write_static_registers(context, meter_id)
    while not stop_event.is_set():
        started = monotonic()
        try:
            power_w, import_wh, export_wh = read_meter_values()
            logger.info("Tibber Pulse: P=%s W, Bezug=%s Wh, Einspeisung=%s Wh", power_w, import_wh, export_wh)
        except Exception as error:
            logger.warning("Tibber Pulse nicht erreichbar; letzte Werte bleiben aktiv: %s", error)
        write_measurements(context, power_w, import_wh, export_wh)
        # Zykluszeit einhalten, statt die Lesedauer zusaetzlich zu warten.
        stop_event.wait(max(0.0, 1.0 - (monotonic() - started)))


def main():
    """Initialisiert die Register und startet den Modbus-TCP-Server."""
    logging.basicConfig(
        level=setting("TIBBER_BRIDGE_LOGLEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s: %(message)s",
    )
    if logging.getLogger().isEnabledFor(logging.DEBUG):
        logging.getLogger("pymodbus").setLevel(logging.DEBUG)
    host = setting("EM24_METER_HOST", "0.0.0.0")
    port = int(setting("EM24_METER_PORT", "502"))
    meter_id = int(setting("EM24_METER_ID", "12345678"))
    logging.info("EM24 Tibber Pulse Proxy Build %s", BUILD_VERSION)

    context = Em24SlaveContext(
        hr=ModbusSequentialDataBlock(0, [0] * 65536),
        ir=ModbusSequentialDataBlock(0, [0] * 65536),
    )
    write_static_registers(context, meter_id)
    # Victron liest die Register unmittelbar nach dem Verbindungsaufbau. Der
    # erste Abruf erfolgt daher synchron, damit die Erkennung keine Nullwerte
    # sieht.
    try:
        power_w, import_wh, export_wh = read_meter_values()
        write_measurements(context, power_w, import_wh, export_wh)
        logging.info("Erste Tibber-Werte vor Serverstart geladen.")
    except Exception as error:
        write_measurements(context, Decimal(0), Decimal(0), Decimal(0))
        logging.warning("Erster Tibber-Abruf fehlgeschlagen: %s", error)
    stop_event = threading.Event()
    thread = threading.Thread(target=update_loop, args=(context, meter_id, stop_event), daemon=True)
    thread.start()

    identity = ModbusDeviceIdentification()
    identity.VendorName = f"em24-tibber-pulse-proxy {BUILD_VERSION}"
    identity.ProductCode = "EM24"
    identity.MajorMinorRevision = BUILD_VERSION
    identity.VendorUrl = "github.com/ixtrader/em24-tibber-pulse-proxy"
    identity.ProductName = f"EM24 Tibber Pulse Proxy {BUILD_VERSION}"
    identity.ModelName = "MB24DINAV23XE1X"
    identity.UserApplicationName = "Tibber Pulse to EM24 Modbus TCP"
    logging.info("EM24-Modbus-TCP-Server auf %s:%s, Unit-ID 1", host, port)
    try:
        StartTcpServer(context=ModbusServerContext(slaves={1: context}, single=False), identity=identity, address=(host, port))
    finally:
        stop_event.set()
        thread.join(timeout=2)


if __name__ == "__main__":
    main()