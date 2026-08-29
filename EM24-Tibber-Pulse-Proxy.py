#!/usr/bin/env python3
"""Carlo-Gavazzi-EM24-Modbus-TCP-Emulation mit Echtzeitwerten eines Tibber Pulse."""

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


SML_START = b"\x1b\x1b\x1b\x1b\x01\x01\x01\x01"
SML_END = b"\x1b\x1b\x1b\x1b\x1a"
OBIS_IMPORT = b"\x01\x00\x01\x08\x00\xff"
OBIS_EXPORT = b"\x01\x00\x02\x08\x00\xff"
OBIS_POWER = b"\x01\x00\x10\x07\x00\xff"
VOLTAGE_LN = Decimal("230")
VOLTAGE_LL = Decimal("400")
FREQUENCY_HZ = Decimal("50")
MODEL_REGISTER = 11
EM24_MODEL_NUMBER = 1648


class SmlParseError(ValueError):
    pass


class Em24SlaveContext(ModbusSlaveContext):
    def getValues(self, function_code, address, count=1):
        logging.getLogger(__name__).info(
            "Modbus-Leseanfrage: Funktion %s, Register %s, Anzahl %s",
            function_code,
            address,
            count,
        )
        # Register 11 wird vom 32-Bit-Wert auf Adresse 10 ueberschrieben.
        if address == MODEL_REGISTER and count == 1:
            return [EM24_MODEL_NUMBER]
        return super().getValues(function_code, address, count)


def setting(name, default=""):
    return os.getenv(name, default).strip()


def tibber_url():
    host = setting("TIBBER_BRIDGE_HOST")
    port = setting("TIBBER_BRIDGE_PORT", "80")
    node_id = setting("TIBBER_BRIDGE_NODEID")
    if not host or not node_id:
        raise ValueError("TIBBER_BRIDGE_HOST und TIBBER_BRIDGE_NODEID muessen gesetzt sein.")
    return f"http://{host}:{port}/data.json?node_id={node_id}"


def fetch_sml():
    request = Request(tibber_url())
    username = setting("TIBBER_BRIDGE_USER")
    password = setting("TIBBER_BRIDGE_PASSWORD")
    if username:
        token = base64.b64encode(f"{username}:{password}".encode()).decode()
        request.add_header("Authorization", f"Basic {token}")
    with urlopen(request, timeout=5) as response:
        return response.read()


def parse_element(payload, offset):
    if offset >= len(payload):
        raise SmlParseError("Unerwartetes Ende des SML-Payloads.")
    if payload[offset] == 0:
        return None, offset + 1

    first = payload[offset]
    element_type = first & 0x70
    length = 0
    header_length = 0
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
        values = []
        for _ in range(length):
            value, position = parse_element(payload, position)
            values.append(value)
        return values, position

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
                values[obis] = Decimal(raw_value) * (Decimal(10) ** (scaler or 0))

    missing = [obis.hex() for obis in (OBIS_IMPORT, OBIS_EXPORT, OBIS_POWER) if obis not in values]
    if missing:
        raise SmlParseError(f"Fehlende OBIS-Werte: {', '.join(missing)}")
    return values[OBIS_POWER], values[OBIS_IMPORT], values[OBIS_EXPORT]


def write_i32(context, address, value):
    builder = BinaryPayloadBuilder(byteorder=Endian.BIG, wordorder=Endian.LITTLE)
    builder.add_32bit_int(round(value))
    registers = builder.to_registers()
    context.setValues(3, address, registers)
    context.setValues(4, address, registers)


def write_i16(context, address, value, unsigned=False):
    builder = BinaryPayloadBuilder(byteorder=Endian.BIG, wordorder=Endian.LITTLE)
    (builder.add_16bit_uint if unsigned else builder.add_16bit_int)(round(value))
    registers = builder.to_registers()
    context.setValues(3, address, registers)
    context.setValues(4, address, registers)


def write_text(context, address, value):
    builder = BinaryPayloadBuilder(byteorder=Endian.BIG, wordorder=Endian.LITTLE)
    builder.add_string(value)
    registers = builder.to_registers()
    context.setValues(3, address, registers)
    context.setValues(4, address, registers)


def write_static_registers(context, meter_id):
    write_i16(context, MODEL_REGISTER, EM24_MODEL_NUMBER)
    write_i32(context, 768, 0)
    write_i16(context, 770, 4126)
    write_i16(context, 771, 68)
    write_i16(context, 772, 4127)
    write_i16(context, 773, 67)
    write_i16(context, 774, 0)
    write_i16(context, 848, 4128)
    write_text(context, 20480, "MB24DINAV23XE1X")
    write_i16(context, 4096, 9999)
    write_i16(context, 4097, 0)
    write_i16(context, 4098, 0)
    write_i32(context, 4099, 10)
    write_i32(context, 4101, 10)
    for address, value in enumerate(range(1, 10), start=4103):
        write_i16(context, address, value)
    write_i32(context, 4112, 15)
    write_i16(context, 4360, 2)
    write_i16(context, 4361, 2)
    for address, value in enumerate((1, 3, 1, 3, 3, 1, 2, 3), start=40960):
        write_i16(context, address, value)
    write_i16(context, 41216, 3)


def write_measurements(context, power_w, import_wh, export_wh):
    phase_power = power_w / 3
    phase_current_ma = abs(phase_power) / VOLTAGE_LN * 1000
    import_centikwh = import_wh / 10
    export_centikwh = export_wh / 10

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

    for address in (46, 47, 48, 49):
        write_i16(context, address, 1000)
    write_i16(context, 50, 0)
    write_i16(context, 51, FREQUENCY_HZ * 10, unsigned=True)

    phase_values = {
        254: 2400, 256: 256, 258: VOLTAGE_LN * 10, 260: VOLTAGE_LL * 10,
        262: power_w * 10, 264: abs(power_w) * 10, 266: 0, 268: 1000,
        270: 0, 272: FREQUENCY_HZ * 10, 274: import_centikwh, 276: 0,
        278: export_centikwh, 280: 0, 282: 0, 284: 0,
    }
    for phase in range(3):
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
        stop_event.wait(max(0.0, 1.0 - (monotonic() - started)))


def main():
    logging.basicConfig(
        level=setting("TIBBER_BRIDGE_LOGLEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s: %(message)s",
    )
    if logging.getLogger().isEnabledFor(logging.DEBUG):
        logging.getLogger("pymodbus").setLevel(logging.DEBUG)
    host = setting("EM24_METER_HOST", "0.0.0.0")
    port = int(setting("EM24_METER_PORT", "502"))
    meter_id = int(setting("EM24_METER_ID", "12345678"))

    context = Em24SlaveContext(
        hr=ModbusSequentialDataBlock(0, [0] * 65536),
        ir=ModbusSequentialDataBlock(0, [0] * 65536),
    )
    write_static_registers(context, meter_id)
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
    identity.VendorName = "Carlo Gavazzi"
    identity.ProductCode = "EM24"
    identity.ProductName = "EM24 Tibber Pulse Proxy"
    identity.ModelName = "MB24DINAV23XE1X"
    logging.info("EM24-Modbus-TCP-Server auf %s:%s, Unit-ID 1", host, port)
    try:
        StartTcpServer(context=ModbusServerContext(slaves={1: context}, single=False), identity=identity, address=(host, port))
    finally:
        stop_event.set()
        thread.join(timeout=2)


if __name__ == "__main__":
    main()