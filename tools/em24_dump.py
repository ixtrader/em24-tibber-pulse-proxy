#!/usr/bin/env python3
"""Gibt die implementierten EM24-Modbus-Register mit Feldnamen aus."""

import argparse
import sys

from pymodbus.client import ModbusTcpClient


FIELDS = [
    (0, 2, "L1-N Spannung [V*10]"), (2, 2, "L2-N Spannung [V*10]"), (4, 2, "L3-N Spannung [V*10]"),
    (6, 2, "L1-L2 Spannung [V*10]"), (8, 2, "L2-L3 Spannung [V*10]"), (10, 2, "L3-L1 Spannung [V*10]"),
    (12, 2, "Strom L1 [mA]"), (14, 2, "Strom L2 [mA]"), (16, 2, "Strom L3 [mA]"),
    (18, 2, "Wirkleistung L1 [W*10]"), (20, 2, "Wirkleistung L2 [W*10]"), (22, 2, "Wirkleistung L3 [W*10]"),
    (24, 2, "Scheinleistung L1 [VA*10]"), (26, 2, "Scheinleistung L2 [VA*10]"), (28, 2, "Scheinleistung L3 [VA*10]"),
    (30, 2, "Blindleistung L1 [var*10]"), (32, 2, "Blindleistung L2 [var*10]"), (34, 2, "Blindleistung L3 [var*10]"),
    (36, 2, "L-N Spannung Summe [V*10]"), (38, 2, "L-L Spannung Summe [V*10]"),
    (40, 2, "Wirkleistung gesamt [W*10]"), (42, 2, "Scheinleistung gesamt [VA*10]"), (44, 2, "Blindleistung gesamt [var*10]"),
    (46, 1, "Leistungsfaktor L1 [*1000]"), (47, 1, "Leistungsfaktor L2 [*1000]"), (48, 1, "Leistungsfaktor L3 [*1000]"),
    (49, 1, "Leistungsfaktor gesamt [*1000]"), (50, 1, "Phasenfolge"), (51, 1, "Frequenz [Hz*10]"),
    (52, 2, "kWh(+) TOT [*10]"), (54, 2, "kvarh(+) TOT [*10]"), (56, 2, "Demand Power"), (58, 2, "Demand Power max"),
    (60, 2, "kWh(+) PAR [*10]"), (62, 2, "kvarh(+) PAR [*10]"),
    (64, 2, "kWh(+) L1 [*10]"), (66, 2, "kWh(+) L2 [*10]"), (68, 2, "kWh(+) L3 [*10]"),
    (70, 2, "Wirkenergie Tarif 1"), (72, 2, "Wirkenergie Tarif 2"), (74, 2, "Wirkenergie Tarif 3"), (76, 2, "Wirkenergie Tarif 4"),
    (78, 2, "kWh(-) TOT [*10]"), (80, 2, "kvarh(-) TOT [*10]"), (82, 2, "Betriebsstunden [*100]"),
    (84, 2, "Blindenergie Tarif 1"), (86, 2, "Blindenergie Tarif 2"), (88, 2, "Blindenergie Tarif 3"), (90, 2, "Blindenergie Tarif 4"),
    (92, 2, "Scheindemand"), (94, 2, "Scheindemand max"), (96, 2, "DMD A max [*10]"),
    (254, 2, "Betriebsstunden [*100]"), (256, 2, "reserviert"), (258, 2, "L-N Spannung [V*10]"), (260, 2, "L-L Spannung [V*10]"),
    (262, 2, "Wirkleistung gesamt [W*10]"), (264, 2, "Scheinleistung gesamt [VA*10]"), (266, 2, "Blindleistung gesamt [var*10]"),
    (268, 2, "Leistungsfaktor gesamt [*1000]"), (270, 2, "Phasenfolge"), (272, 2, "Frequenz [Hz*10]"),
    (274, 2, "kWh(+) TOT [*10]"), (276, 2, "kvarh(+) TOT [*10]"), (278, 2, "kWh(-) TOT [*10]"), (280, 2, "kvarh(-) TOT [*10]"),
    (282, 2, "Demand Power"), (284, 2, "Demand Power max"),
    (286, 2, "L1-L2 Spannung [V*10]"), (288, 2, "L1-N Spannung [V*10]"), (290, 2, "Strom L1 [mA]"),
    (292, 2, "Wirkleistung L1 [W*10]"), (294, 2, "Scheinleistung L1 [VA*10]"), (296, 2, "Blindleistung L1 [var*10]"), (298, 2, "Leistungsfaktor L1 [*1000]"),
    (300, 2, "L2-L3 Spannung [V*10]"), (302, 2, "L2-N Spannung [V*10]"), (304, 2, "Strom L2 [mA]"),
    (306, 2, "Wirkleistung L2 [W*10]"), (308, 2, "Scheinleistung L2 [VA*10]"), (310, 2, "Blindleistung L2 [var*10]"), (312, 2, "Leistungsfaktor L2 [*1000]"),
    (314, 2, "L3-L1 Spannung [V*10]"), (316, 2, "L3-N Spannung [V*10]"), (318, 2, "Strom L3 [mA]"),
    (320, 2, "Wirkleistung L3 [W*10]"), (322, 2, "Scheinleistung L3 [VA*10]"), (324, 2, "Blindleistung L3 [var*10]"), (326, 2, "Leistungsfaktor L3 [*1000]"),
    (328, 2, "Phasenfolge"), (330, 2, "kWh(+) PAR [*10]"), (332, 2, "kvarh(+) PAR [*10]"),
    (334, 2, "kWh(+) L1 [*10]"), (336, 2, "kWh(+) L2 [*10]"), (338, 2, "kWh(+) L3 [*10]"),
    (340, 2, "Wirkenergie Tarif 1"), (342, 2, "Wirkenergie Tarif 2"), (344, 2, "Wirkenergie Tarif 3"), (346, 2, "Wirkenergie Tarif 4"),
    (348, 2, "reserviert"), (350, 2, "reserviert"), (352, 2, "reserviert"), (354, 2, "reserviert"),
    (356, 2, "Blindenergie Tarif 1"), (358, 2, "Blindenergie Tarif 2"), (360, 2, "Blindenergie Tarif 3"), (362, 2, "Blindenergie Tarif 4"),
    (364, 2, "reserviert"), (366, 2, "reserviert"), (368, 2, "reserviert"), (370, 2, "reserviert"),
    (372, 2, "reserviert"), (374, 2, "reserviert"), (376, 2, "reserviert"), (378, 2, "reserviert"),
    (380, 2, "Scheindemand"), (382, 2, "Scheindemand max"), (384, 2, "DMD A max [*10]"),
    (11, 1, "EM24-Modellnummer"), (768, 2, "Seriennummer/Kennung"),
    (770, 1, "Messmodul Version"), (771, 1, "Messmodul Revision"), (772, 1, "Kommunikationsmodul Version"), (773, 1, "Kommunikationsmodul Revision"), (774, 1, "Aktueller Tarif"),
    (848, 1, "Firmware-CRC"), (4096, 1, "Passwort"), (4098, 1, "Messsystem"), (4099, 2, "Stromwandlerverhaeltnis"), (4101, 2, "Spannungswandlerverhaeltnis"), (4112, 2, "Intervallzeit"),
    (4360, 1, "Passwort 1"), (4361, 1, "Passwort 2"), (5000, 1, "Statusregister 5000"), (5664, 1, "Statusregister 5664"),
    (20480, 8, "Geraetekennung (Text)"), (35168, 2, "Statusregister 35168"),
    (40960, 1, "Anwendungstyp"), (40961, 1, "Seite LOCK"), (40962, 1, "Seite 1"), (40963, 1, "Seite 2"), (40964, 1, "Seite kvarh"),
    (40965, 1, "ID Benutzer 1"), (40966, 1, "ID Benutzer 2"), (40967, 1, "ID Benutzer 3"), (41216, 1, "Frontselektor-Status"),
]


def decode(registers):
    """Dekodiert Low-Word-first-EM24-Register als vorzeichenbehaftete Zahl."""
    if len(registers) == 1:
        return registers[0] - 0x10000 if registers[0] > 0x7FFF else registers[0]
    value = registers[0] | (registers[1] << 16)
    return value - 0x100000000 if value > 0x7FFFFFFF else value


def print_device_information(client, unit_id):
    """Gibt die optionalen Modbus-Device-Identification-Felder aus."""
    response = client.read_device_information(slave=unit_id)
    if response.isError():
        print(f"Device Identification | Fehler: {response}")
        return

    field_names = {
        0: "VendorName",
        1: "ProductCode",
        2: "MajorMinorRevision",
        3: "VendorUrl",
        4: "ProductName",
        5: "ModelName",
        6: "UserApplicationName",
    }
    information = dict(response.information)
    # Der Standardabruf (Read-Code 1) liefert nur die Basic-Objekte 0 bis 2.
    # Read-Code 4 fragt die weiteren Objekte gezielt einzeln ab.
    for object_id in range(3, 7):
        extended = client.read_device_information(read_code=4, object_id=object_id, slave=unit_id)
        if not extended.isError():
            information.update(extended.information)

    print("Modbus Device Identification:")
    for object_id, value in sorted(information.items()):
        name = field_names.get(object_id, f"Object {object_id}")
        print(f"  {name:<22} | {value.decode('utf-8', 'replace')}")
    print()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1", help="Modbus-TCP-Host")
    parser.add_argument("--port", type=int, default=502, help="Modbus-TCP-Port")
    parser.add_argument("--unit-id", type=int, default=1, help="Modbus Unit-ID")
    arguments = parser.parse_args()

    client = ModbusTcpClient(arguments.host, port=arguments.port, timeout=3)
    if not client.connect():
        print(f"Keine Verbindung zu {arguments.host}:{arguments.port}.", file=sys.stderr)
        return 1
    try:
        print_device_information(client, arguments.unit_id)
        for address, count, label in FIELDS:
            response = client.read_holding_registers(address, count=count, slave=arguments.unit_id)
            if response.isError():
                print(f"{address:>6} | {label:<32} | Fehler: {response}")
                continue
            if address == 20480:
                text = b"".join(word.to_bytes(2, "big") for word in response.registers)
                print(f"{address:>6} | {label:<32} | {text.decode('ascii', 'replace').rstrip(chr(0) + ' ')}")
            else:
                print(f"{address:>6} | {label:<32} | {decode(response.registers)}")
    finally:
        client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())