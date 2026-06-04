"""Build the cross-source field matrix with address + file:line citations.

Output columns per field:
  V1.0 byte / V1.1 byte: byte offset within the spec'd block base
  This repo:   byte 0xNN (reg 0xRRRR) @ file:line
  Phinix-org:  byte 0xNN (reg 0xRRRR) @ file:line — both YAML conventions
  Jean:        Trame N byte 0xNN (= spec byte 0xMM after 6-byte hdr) @ file:line

Each cited byte is back-computed from the literal address constant in the
source — not an inference. The block base used for each conversion is shown.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

REPO = Path("/Users/basmeerman/Downloads/jkbms2mqtt")
PHINIX = Path("/tmp/jk-research/phinix")
JEAN_JSON = REPO / "upstream_reference" / "trame_specs.json"
JEAN_REL = "upstream_reference/trame_specs.json"

OUR_SETTINGS = REPO / "jkbms2mqtt" / "src" / "jkbms2mqtt" / "protocol" / "jk_settings.py"
OUR_MODBUS = REPO / "jkbms2mqtt" / "src" / "jkbms2mqtt" / "protocol" / "jk_modbus.py"
OUR_SETTINGS_REL = "jkbms2mqtt/src/jkbms2mqtt/protocol/jk_settings.py"
OUR_MODBUS_REL = "jkbms2mqtt/src/jkbms2mqtt/protocol/jk_modbus.py"


# -- 1. spec field table -----------------------------------------------------------
# Hand-transcribed from the PDFs in jkbms2mqtt/docs/specifications/.
# Tuple: (block_base, byte, type, length, rw, spec_name, unit, presence)

V11_FIELDS = [
    # ---- Settings block 0x1000 ----
    (0x1000, 0x00, "UINT32", 4, "RW", "VolSmartSleep",            "mV",   "v1.0+v1.1"),
    (0x1000, 0x04, "UINT32", 4, "RW", "VolCellUV",                "mV",   "v1.0+v1.1"),
    (0x1000, 0x08, "UINT32", 4, "RW", "VolCellUVPR",              "mV",   "v1.0+v1.1"),
    (0x1000, 0x0C, "UINT32", 4, "RW", "VolCellOV",                "mV",   "v1.0+v1.1"),
    (0x1000, 0x10, "UINT32", 4, "RW", "VolCellOVPR",              "mV",   "v1.0+v1.1"),
    (0x1000, 0x14, "UINT32", 4, "RW", "VolBalanTrig",             "mV",   "v1.0+v1.1"),
    (0x1000, 0x18, "UINT32", 4, "RW", "VolSOC100%",               "mV",   "v1.0+v1.1"),
    (0x1000, 0x1C, "UINT32", 4, "RW", "VolSOC0%",                 "mV",   "v1.0+v1.1"),
    (0x1000, 0x20, "UINT32", 4, "RW", "VolCellRCV",               "mV",   "v1.0+v1.1"),
    (0x1000, 0x24, "UINT32", 4, "RW", "VolCellRFV",               "mV",   "v1.0+v1.1"),
    (0x1000, 0x28, "UINT32", 4, "RW", "VolSysPwrOff",             "mV",   "v1.0+v1.1"),
    (0x1000, 0x2C, "UINT32", 4, "RW", "CurBatCOC",                "mA",   "v1.0+v1.1"),
    (0x1000, 0x30, "UINT32", 4, "RW", "TIMBatCOCPDly",            "s",    "v1.0+v1.1"),
    (0x1000, 0x34, "UINT32", 4, "RW", "TIMBatCOCPRDly",           "s",    "v1.0+v1.1"),
    (0x1000, 0x38, "UINT32", 4, "RW", "CurBatDcOC",               "mA",   "v1.0+v1.1"),
    (0x1000, 0x3C, "UINT32", 4, "RW", "TIMBatDcOCPDly",           "s",    "v1.0+v1.1"),
    (0x1000, 0x40, "UINT32", 4, "RW", "TIMBatDcOCPRDly",          "s",    "v1.0+v1.1"),
    (0x1000, 0x44, "UINT32", 4, "RW", "TIMBatSCPRDly",            "s",    "v1.0+v1.1"),
    (0x1000, 0x48, "UINT32", 4, "RW", "CurBalanMax",              "mA",   "v1.0+v1.1"),
    (0x1000, 0x4C, "INT32",  4, "RW", "TMPBatCOT",                "0.1°C","v1.0+v1.1"),
    (0x1000, 0x50, "INT32",  4, "RW", "TMPBatCOTPR",              "0.1°C","v1.0+v1.1"),
    (0x1000, 0x54, "INT32",  4, "RW", "TMPBatDcOT",               "0.1°C","v1.0+v1.1"),
    (0x1000, 0x58, "INT32",  4, "RW", "TMPBatDcOTPR",             "0.1°C","v1.0+v1.1"),
    (0x1000, 0x5C, "INT32",  4, "RW", "TMPBatCUT",                "0.1°C","v1.0+v1.1"),
    (0x1000, 0x60, "INT32",  4, "RW", "TMPBatCUTPR",              "0.1°C","v1.0+v1.1"),
    (0x1000, 0x64, "INT32",  4, "RW", "TMPMosOT",                 "0.1°C","v1.0+v1.1"),
    (0x1000, 0x68, "INT32",  4, "RW", "TMPMosOTPR",               "0.1°C","v1.0+v1.1"),
    (0x1000, 0x6C, "UINT32", 4, "RW", "CellCount",                "cells","v1.0+v1.1"),
    (0x1000, 0x70, "UINT32", 4, "RW", "BatChargeEN",              "1:open/0:close","v1.0+v1.1"),
    (0x1000, 0x74, "UINT32", 4, "RW", "BatDisChargeEN",           "1:open/0:close","v1.0+v1.1"),
    (0x1000, 0x78, "UINT32", 4, "RW", "BalanEN",                  "1:open/0:close","v1.0+v1.1"),
    (0x1000, 0x7C, "UINT32", 4, "RW", "CapBatCell",               "mAh",  "v1.0+v1.1"),
    (0x1000, 0x80, "UINT32", 4, "RW", "SCPDelay",                 "μs",   "v1.0+v1.1"),
    (0x1000, 0x84, "UINT32", 4, "RW", "VolStartBalan",            "mV",   "v1.0+v1.1"),
    (0x1000, 0x88, "UINT32", 4, "RW", "CellConWireRes0..31",      "μΩ × 32 cells","v1.0+v1.1"),
    (0x1000, 0x108,"UINT32", 4, "RW", "DevAddr",                  "addr", "v1.0+v1.1"),
    (0x1000, 0x10C,"UINT32", 4, "RW", "TIMProdischarge",          "s",    "v1.0+v1.1"),
    (0x1000, 0x114,"UINT16", 2, "RW", "packed_bits",              "bitfield","v1.0:bits0-6 v1.1:bits0-9"),
    (0x1000, 0x116,"INT8",   2, "RW", "TMPBatOTA / TMPBatOTAR",   "°C",   "v1.0 only"),
    (0x1000, 0x118,"UINT8",  2, "RW", "TIMSmartSleep",            "H",    "v1.0+v1.1"),
    # ---- Real-time block 0x1200 ----
    (0x1200, 0x00, "UINT16", 2, "R",  "CellVol0..31",             "mV × 32","v1.0+v1.1"),
    (0x1200, 0x40, "UINT32", 4, "R",  "CellSta",                  "bitmap","v1.0+v1.1"),
    (0x1200, 0x44, "UINT16", 2, "R",  "CellVolAve",               "mV",   "v1.0+v1.1"),
    (0x1200, 0x46, "UINT16", 2, "R",  "CellVdifMax",              "mV",   "v1.0+v1.1"),
    (0x1200, 0x48, "UINT8+UINT8", 2, "R", "MaxVolCellNbr / MinVolCellNbr", "idx","v1.0+v1.1"),
    (0x1200, 0x4A, "UINT16", 2, "R",  "CellWireRes0..31",         "mΩ × 32","v1.0+v1.1"),
    (0x1200, 0x8A, "INT16",  2, "R",  "TempMos",                  "0.1°C","v1.0+v1.1"),
    (0x1200, 0x8C, "UINT32", 4, "R",  "CellWireResSta",           "bitmap","v1.0+v1.1"),
    (0x1200, 0x90, "UINT32", 4, "R",  "BatVol",                   "mV",   "v1.0+v1.1"),
    (0x1200, 0x94, "UINT32", 4, "R",  "BatWatt",                  "mW",   "v1.0+v1.1"),
    (0x1200, 0x98, "INT32",  4, "R",  "BatCurrent",               "mA",   "v1.0+v1.1"),
    (0x1200, 0x9C, "INT16",  2, "R",  "TempBat1",                 "0.1°C","v1.0+v1.1"),
    (0x1200, 0x9E, "INT16",  2, "R",  "TempBat2",                 "0.1°C","v1.0+v1.1"),
    (0x1200, 0xA0, "UINT32", 4, "R",  "alarms",                   "bitfield","v1.0+v1.1"),
    (0x1200, 0xA4, "INT16",  2, "R",  "BalanCurrent",             "mA",   "v1.0+v1.1"),
    (0x1200, 0xA6, "UINT8+UINT8", 2, "R", "BalanSta / SOCStateOfcharge", "%","v1.0+v1.1"),
    (0x1200, 0xA8, "INT32",  4, "R",  "SOCCapRemain",             "mAh",  "v1.0+v1.1"),
    (0x1200, 0xAC, "UINT32", 4, "R",  "SOCFullChargeCap",         "mAh",  "v1.0+v1.1"),
    (0x1200, 0xB0, "UINT32", 4, "R",  "SOCCycleCount",            "count","v1.0+v1.1"),
    (0x1200, 0xB4, "UINT32", 4, "R",  "SOCCycleCap",              "mAh",  "v1.0+v1.1"),
    (0x1200, 0xB8, "UINT8+UINT8", 2, "R", "SOCSOH / Precharge",   "%",    "v1.0+v1.1"),
    (0x1200, 0xBA, "UINT16", 2, "R",  "UserAlarm",                "",     "v1.0+v1.1"),
    (0x1200, 0xBC, "UINT32", 4, "R",  "RunTime",                  "s",    "v1.0+v1.1"),
    (0x1200, 0xC0, "UINT8+UINT8", 2, "R", "Charge / Discharge",   "1:on", "v1.0+v1.1"),
    (0x1200, 0xC2, "UINT16", 2, "R",  "UserAlarm2",               "",     "v1.0+v1.1"),
    (0x1200, 0xC4, "UINT16", 2, "R",  "TimeDcOCPR",               "s",    "v1.0+v1.1"),
    (0x1200, 0xC6, "UINT16", 2, "R",  "TimeDcSCPR",               "s",    "v1.0+v1.1"),
    (0x1200, 0xC8, "UINT16", 2, "R",  "TimeCOCPR",                "s",    "v1.0+v1.1"),
    (0x1200, 0xCA, "UINT16", 2, "R",  "TimeCSCPR",                "s",    "v1.0+v1.1"),
    (0x1200, 0xCC, "UINT16", 2, "R",  "TimeUVPR",                 "s",    "v1.0+v1.1"),
    (0x1200, 0xCE, "UINT16", 2, "R",  "TimeOVPR",                 "s",    "v1.0+v1.1"),
    (0x1200, 0xD0, "UINT8+UINT8", 2, "R", "TempSensorAbsent / Heating", "bits","v1.0+v1.1"),
    (0x1200, 0xD2, "UINT16", 2, "R",  "Reserved",                 "",     "v1.0+v1.1"),
    (0x1200, 0xD4, "UINT16", 2, "R",  "TimeEmergency",            "s",    "v1.0+v1.1"),
    (0x1200, 0xD6, "UINT16", 2, "R",  "BatDisCurCorrect",         "",     "v1.1 only"),
    (0x1200, 0xD8, "UINT16", 2, "R",  "VolChargCur",              "mV",   "v1.0+v1.1"),
    (0x1200, 0xDA, "UINT16", 2, "R",  "VolDischargCur",           "mV",   "v1.0+v1.1"),
    (0x1200, 0xDC, "FLOAT",  4, "R",  "BatVolCorrect",            "",     "v1.0+v1.1"),
    (0x1200, 0xE0, "UINT16", 2, "R",  "ChargePWMDutyCyle",        "%",    "v1.0 only"),
    (0x1200, 0xE2, "UINT16", 2, "R",  "DischargePWMDutyCyle",     "%",    "v1.0 only"),
    (0x1200, 0xE4, "UINT16", 2, "R",  "BatVol (0.01V scale)",     "0.01V","v1.0+v1.1"),
    (0x1200, 0xE6, "INT16",  2, "R",  "HeatCurrent",              "mA",   "v1.0+v1.1"),
    (0x1200, 0xEE, "UINT8+UINT8", 2, "R", "RVD / ChargerPlugged", "1:in", "v1.0+v1.1"),
    (0x1200, 0xF0, "UINT32", 4, "R",  "SysRunTicks",              "0.1s", "v1.0+v1.1"),
    (0x1200, 0xF4, "UINT32", 4, "R",  "PVDTrigTimestamps",        "0.1s", "v1.0 only"),
    (0x1200, 0xF8, "INT16",  2, "R",  "TempBat3",                 "0.1°C","v1.0+v1.1"),
    (0x1200, 0xFA, "INT16",  2, "R",  "TempBat4",                 "0.1°C","v1.0+v1.1"),
    (0x1200, 0xFC, "INT16",  2, "R",  "TempBat5",                 "0.1°C","v1.0+v1.1"),
    (0x1200, 0x100,"UINT32", 4, "R",  "RTCTicks",                 "from 2020","v1.0+v1.1"),
    (0x1200, 0x108,"UINT32", 4, "R",  "TimeEnterSleep",           "s",    "v1.0+v1.1"),
    (0x1200, 0x10C,"UINT8+UINT8", 2, "R", "PCLModuleSta / RVD",   "1:on", "v1.0+v1.1"),
    # ---- Info block 0x1400 ----
    (0x1400, 0x00, "ASCII", 16, "R",  "ManufacturerDeviceID",     "",     "v1.0+v1.1"),
    (0x1400, 0x10, "ASCII",  8, "R",  "HardwareVersion",          "",     "v1.0+v1.1"),
    (0x1400, 0x18, "ASCII",  8, "R",  "SoftwareVersion",          "",     "v1.0+v1.1"),
    (0x1400, 0x20, "UINT32", 4, "R",  "ODDRunTime",               "s",    "v1.0+v1.1"),
    (0x1400, 0x24, "UINT32", 4, "R",  "PWROnTimes",               "count","v1.0+v1.1"),
    (0x1400, 0xB2, "UINT8+UINT8", 2, "RW", "UART1MPRTOLNbr / CANMPRTOLNbr", "","v1.1 only"),
    (0x1400, 0xB4, "UINT8", 16, "R",  "UART1MPRTOLEnable",        "",     "v1.1 only"),
    (0x1400, 0xD4, "UINT8+UINT8", 2, "RW", "UART2MPRTOLNbr / UART2MPRTOLEnable[0]", "","v1.1 only"),
    (0x1400, 0xE4, "UINT8+UINT8", 2, "RW", "LCDBuzzerTrigger / DRY1Trigger", "","v1.1 only"),
    (0x1400, 0xE6, "UINT8+UINT8", 2, "RW", "DRY2Trigger / UARTMPTLVer", "","v1.1 only"),
    (0x1400, 0xE8, "INT32",  4, "RW", "LCDBuzzerTriggerVal",      "",     "v1.1 only"),
    (0x1400, 0xEC, "INT32",  4, "RW", "LCDBuzzerReleaseVal",      "",     "v1.1 only"),
    (0x1400, 0xF0, "INT32",  4, "RW", "DRY1TriggerVal",           "",     "v1.1 only"),
    (0x1400, 0xF4, "INT32",  4, "RW", "DRY1ReleaseVal",           "",     "v1.1 only"),
    (0x1400, 0xF8, "INT32",  4, "RW", "DRY2TriggerVal",           "",     "v1.1 only"),
    (0x1400, 0xFC, "INT32",  4, "RW", "DRY2ReleaseVal",           "",     "v1.1 only"),
    (0x1400, 0x100,"INT32",  4, "RW", "DataStoredPeriod",         "",     "v1.1 only"),
    (0x1400, 0x104,"UINT8+UINT8", 2, "RW", "RCVTime / RFVTime",   "0.1H", "v1.1 only"),
    (0x1400, 0x106,"UINT8+UINT8", 2, "R",  "CANMPTLVer / RVD",    "",     "v1.1 only"),
    # ---- Command block 0x1600 ----
    (0x1600, 0x00, "UINT16", 4, "W",  "VoltageCalibration",       "mV / 0.1V","encoding differs"),
    (0x1600, 0x04, "UINT16", 2, "W",  "Shutdown",                 "",     "v1.1 only"),
    (0x1600, 0x06, "UINT16", 4, "W",  "CurrentCalibration",       "mA / 0.1A","encoding differs"),
    (0x1600, 0x0A, "UINT16", 2, "W",  "LI-ION",                   "",     "v1.0+v1.1"),
    (0x1600, 0x0C, "UINT16", 2, "W",  "LIFEPO4",                  "",     "v1.0+v1.1"),
    (0x1600, 0x0E, "UINT16", 2, "W",  "LTO",                      "",     "v1.0+v1.1"),
    (0x1600, 0x10, "UINT16", 2, "W",  "Emergency",                "",     "v1.0+v1.1"),
    (0x1600, 0x12, "UINT32", 4, "W",  "Timecalibration",          "",     "v1.0+v1.1"),
]


# -- 2. our repo — extract literal address + line number ---------------------------
#
# For settings: grep the RegisterDef line, parse `address=0xNNNN`.
# For RT/info constants: grep `_OFF_NAME: Final = 0xNN`.

def _read_lines(path: Path) -> list[str]:
    return path.read_text().splitlines()


SETTINGS_TEXT = _read_lines(OUR_SETTINGS)
MODBUS_TEXT = _read_lines(OUR_MODBUS)


def grep_register_def(py_name: str) -> tuple[int, int] | None:
    """Find `name="py_name"` line; return (modbus_reg_address, 1-indexed line)."""
    pat = re.compile(r'name="' + re.escape(py_name) + r'"\s*,\s*address=(0x[0-9A-Fa-f]+)')
    for i, line in enumerate(SETTINGS_TEXT, 1):
        m = pat.search(line)
        if m:
            return (int(m.group(1), 16), i)
    return None


def grep_offset_const(const_name: str) -> tuple[int, int] | None:
    """Find `_OFF_*: Final = 0xNN`; return (buffer_offset, line)."""
    pat = re.compile(r"^" + re.escape(const_name) + r":\s*Final\s*=\s*(0x[0-9A-Fa-f]+)")
    for i, line in enumerate(MODBUS_TEXT, 1):
        m = pat.search(line)
        if m:
            return (int(m.group(1), 16), i)
    return None


def grep_packed_bit_reg() -> tuple[int, int] | None:
    pat = re.compile(r"^PACKED_BIT_REGISTER:\s*Final\s*=\s*(0x[0-9A-Fa-f]+)")
    for i, line in enumerate(SETTINGS_TEXT, 1):
        m = pat.search(line)
        if m:
            return (int(m.group(1), 16), i)
    return None


# Map spec name → (kind, python_identifier).
#   "reg" — settings RegisterDef name
#   "off" — _OFF_* constant in jk_modbus.py (RT block, base 0x1200)
#   "off_info" — _OFF_* constant in info block (base 0x1400)
#   "packed" — PACKED_BIT_REGISTER
OURS_BINDING = {
    # settings
    "VolSmartSleep":    ("reg", "smart_sleep_voltage"),
    "VolCellUV":        ("reg", "cell_voltage_undervoltage_protection"),
    "VolCellUVPR":      ("reg", "cell_voltage_undervoltage_recovery"),
    "VolCellOV":        ("reg", "cell_voltage_overvoltage_protection"),
    "VolCellOVPR":      ("reg", "cell_voltage_overvoltage_recovery"),
    "VolBalanTrig":     ("reg", "balance_trigger_voltage"),
    "VolSOC100%":       ("reg", "cell_soc100_voltage"),
    "VolSOC0%":         ("reg", "cell_soc0_voltage"),
    "VolCellRCV":       ("reg", "cell_request_charge_voltage"),
    "VolCellRFV":       ("reg", "cell_request_float_voltage"),
    "VolSysPwrOff":     ("reg", "power_off_voltage"),
    "CurBatCOC":        ("reg", "max_charge_current"),
    "TIMBatCOCPDly":    ("reg", "charge_overcurrent_protection_delay"),
    "TIMBatCOCPRDly":   ("reg", "charge_overcurrent_protection_recovery_time"),
    "CurBatDcOC":       ("reg", "max_discharge_current"),
    "TIMBatDcOCPDly":   ("reg", "discharge_overcurrent_protection_delay"),
    "TIMBatDcOCPRDly":  ("reg", "discharge_overcurrent_protection_recovery_time"),
    "TIMBatSCPRDly":    ("reg", "short_circuit_protection_recovery_time"),
    "CurBalanMax":      ("reg", "max_balance_current"),
    "TMPBatCOT":        ("reg", "charge_overtemperature_protection"),
    "TMPBatCOTPR":      ("reg", "charge_overtemperature_protection_recovery"),
    "TMPBatDcOT":       ("reg", "discharge_overtemperature_protection"),
    "TMPBatDcOTPR":     ("reg", "discharge_overtemperature_protection_recovery"),
    "TMPBatCUT":        ("reg", "charge_undertemperature_protection"),
    "TMPBatCUTPR":      ("reg", "charge_undertemperature_protection_recovery"),
    "TMPMosOT":         ("reg", "power_tube_overtemperature_protection"),
    "TMPMosOTPR":       ("reg", "power_tube_overtemperature_protection_recovery"),
    "CellCount":        ("reg", "cell_count"),
    "BatChargeEN":      ("reg", "charging_switch"),
    "BatDisChargeEN":   ("reg", "discharging_switch"),
    "BalanEN":          ("reg", "balance_switch"),
    "CapBatCell":       ("reg", "pack_capacity_setting"),
    "SCPDelay":         ("reg", "short_circuit_protection_delay_us"),
    "VolStartBalan":    ("reg", "balance_starting_voltage"),
    "packed_bits":      ("packed", "PACKED_BIT_REGISTER"),
    # real-time
    "CellVol0..31":     ("off", "_OFF_CELL_VOLT_0"),
    "CellSta":          ("off", "_OFF_CELL_PRESENT"),
    "CellVolAve":       ("off", "_OFF_CELL_AVG_V"),
    "CellVdifMax":      ("off", "_OFF_CELL_DELTA"),
    "CellWireRes0..31": ("off", "_OFF_CELL_RES_0"),
    "TempMos":          ("off", "_OFF_MOS_TEMP"),
    "BatVol":           ("off", "_OFF_TOTAL_V"),
    "BatWatt":          ("off", "_OFF_TOTAL_POWER"),
    "BatCurrent":       ("off", "_OFF_TOTAL_CURRENT"),
    "TempBat1":         ("off", "_OFF_PROBE_1_TEMP"),
    "TempBat2":         ("off", "_OFF_PROBE_2_TEMP"),
    "alarms":           ("off", "_OFF_ALARM_BITS"),
    "BalanCurrent":     ("off", "_OFF_BALANCE_CURRENT"),
    "BalanSta / SOCStateOfcharge": ("off", "_OFF_BALANCE_STATE_SOC"),
    "SOCCapRemain":     ("off", "_OFF_REMAINING_CAP"),
    "SOCFullChargeCap": ("off", "_OFF_NOMINAL_CAP"),
    "SOCCycleCount":    ("off", "_OFF_CYCLE_COUNT"),
    "SOCCycleCap":      ("off", "_OFF_TOTAL_CYCLE_CAP"),
    "SOCSOH / Precharge": ("off", "_OFF_SOH_PRECHARGE"),
    "RunTime":          ("off", "_OFF_RUNTIME"),
    "Charge / Discharge": ("off", "_OFF_CHARGE_DISCHARGE"),
    "TempSensorAbsent / Heating": ("off", "_OFF_TEMP_SENSOR_HEATING"),
    "HeatCurrent":      ("off", "_OFF_HEATING_CURRENT"),
    "TempBat3":         ("off", "_OFF_PROBE_3_TEMP"),
    "TempBat4":         ("off", "_OFF_PROBE_4_TEMP"),
    "TempBat5":         ("off", "_OFF_PROBE_5_TEMP"),
    # info
    "ManufacturerDeviceID": ("off_info", "_OFF_MODEL"),
    "HardwareVersion":   ("off_info", "_OFF_HW_VERSION"),
    "SoftwareVersion":   ("off_info", "_OFF_SW_VERSION"),
}


def find_ours(spec_name: str, spec_block_base: int) -> str:
    """Return 'byte 0xNN (reg 0xRRRR) @ file:line' or '—'."""
    binding = OURS_BINDING.get(spec_name)
    if not binding:
        return "—"
    kind, ident = binding
    if kind == "reg":
        hit = grep_register_def(ident)
        if not hit:
            return "—"
        reg, line = hit
        byte = (reg - spec_block_base) * 2
        return f"byte `0x{byte:02X}` (reg `0x{reg:04X}`)<br>{OUR_SETTINGS_REL}:{line}"
    elif kind == "packed":
        hit = grep_packed_bit_reg()
        if not hit:
            return "—"
        reg, line = hit
        # packed_bits lives in the settings block (0x1000)
        byte = (reg - 0x1000) * 2
        return f"byte `0x{byte:02X}` (reg `0x{reg:04X}`)<br>{OUR_SETTINGS_REL}:{line}"
    elif kind == "off":
        hit = grep_offset_const(ident)
        if not hit:
            return "—"
        off, line = hit
        reg = 0x1200 + off
        byte = off * 2
        return f"byte `0x{byte:02X}` (reg `0x{reg:04X}`)<br>{OUR_MODBUS_REL}:{line}"
    elif kind == "off_info":
        hit = grep_offset_const(ident)
        if not hit:
            return "—"
        off, line = hit
        reg = 0x1400 + off
        byte = off * 2
        return f"byte `0x{byte:02X}` (reg `0x{reg:04X}`)<br>{OUR_MODBUS_REL}:{line}"
    return "—"


# -- 3. phinix-org — walk YAML, extract literal address + offset --------------------

def phinix_block_walk():
    """Yield (id, address_hex, offset_hex_or_None, rel_file_path, line_of_id)."""
    for yf in sorted(PHINIX.rglob("*.yaml")):
        if "/.git/" in str(yf):
            continue
        text = yf.read_text(errors="replace")
        lines = text.split("\n")
        block = None
        block_id_line = None
        for i, line in enumerate(lines):
            if re.match(r"\s*-\s*platform:\s*modbus_controller", line):
                if block and block.get("id"):
                    rel = str(yf.relative_to(PHINIX))
                    yield (block["id"], block.get("address"), block.get("offset"),
                           rel, block_id_line)
                block = {}
                block_id_line = None
                continue
            if block is None:
                continue
            m_id = re.match(r"\s*id:\s*(\S+)", line)
            m_addr = re.match(r"\s*address:\s*(0x[0-9A-Fa-f]+|\d+)", line)
            m_off = re.match(r"\s*offset:\s*(0x[0-9A-Fa-f]+|\d+)", line)
            if m_id and "id" not in block:
                block["id"] = m_id.group(1)
                block_id_line = i + 1
            if m_addr and "address" not in block:
                block["address"] = m_addr.group(1)
            if m_off and "offset" not in block:
                block["offset"] = m_off.group(1)
        if block and block.get("id"):
            rel = str(yf.relative_to(PHINIX))
            yield (block["id"], block.get("address"), block.get("offset"),
                   rel, block_id_line)


# Build phinix lookup keyed by base ID (strip trailing S / N suffix used for sensor/number)
PHINIX_DB = {}
for pid, addr, off, file, line in phinix_block_walk():
    if not addr:
        continue
    base = pid.rstrip("SN")
    PHINIX_DB.setdefault(base, []).append((pid, addr, off, file, line))


# Map spec name → phinix base id key
PHINIX_BINDING = {
    "VolSmartSleep":   "VolSmartSleep",
    "VolCellUV":       "VolCellUV",
    "VolCellUVPR":     "VolCellUVPR",
    "VolCellOV":       "VolCellOV",
    "VolCellOVPR":     "VolCellOVPR",
    "VolBalanTrig":    "VolBalanTrig",
    "VolSOC100%":      "VolSOC100",
    "VolSOC0%":        "VolSOC0",
    "VolCellRCV":      "VolCellRCV",
    "VolCellRFV":      "VolCellRFV",
    "VolSysPwrOff":    "VolSysPwrOff",
    "CurBatCOC":       "CurBatCOC",
    "TIMBatCOCPDly":   "TIMBatCOCPDly",
    "TIMBatCOCPRDly":  "TIMBatCOCPRDly",
    "CurBatDcOC":      "CurBatDcOC",
    "TIMBatDcOCPDly":  "TIMBatDcOCPDly",
    "TIMBatDcOCPRDly": "TIMBatDcOCPRDly",
    "TIMBatSCPRDly":   "TIMBatSCPRDly",
    "CurBalanMax":     "CurBalanMax",
    "TMPBatCOT":       "TMPBatCOT",
    "TMPBatCOTPR":     "TMPBatCOTPR",
    "TMPBatDcOT":      "TMPBatDcOT",
    "TMPBatDcOTPR":    "TMPBatDcOTPR",
    "TMPBatCUT":       "TMPBatCUT",
    "TMPBatCUTPR":     "TMPBatCUTPR",
    "TMPMosOT":        "TMPMosOT",
    "TMPMosOTPR":      "TMPMosOTPR",
    "CellCount":       "CellCount",
    "BatChargeEN":     "BatChargeEN",
    "BatDisChargeEN":  "BatDisChargeEN",
    "BalanEN":         "BalanEN",
    "CapBatCell":      "CapBatCell",
    "SCPDelay":        "SCPDelay",
    "VolStartBalan":   "VolStartBalan",
    "CellConWireRes0..31": "CellConWireRes00",
    "DevAddr":         "DevAddr",
    "TIMProdischarge": "TIMProdischarge",
    "packed_bits":     "HeatEN",  # phinix exposes individual bit controls
    "TIMSmartSleep":   "TIMSmartSleep",
    # RT
    "CellVol0..31":    "CellVol0",
    "CellSta":         "CellSta",
    "CellVolAve":      "CellVolAve",
    "CellVdifMax":     "CellVdifMax",
    "CellWireRes0..31": "CellWireRes0",
    "TempMos":         "TempMos",
    "CellWireResSta":  "CellWireResSta",
    "BatVol":          "BatVol",
    "BatWatt":         "BatWatt",
    "BatCurrent":      "BatCurrent",
    "TempBat1":        "TempBat1",
    "TempBat2":        "TempBat2",
    "alarms":          "AlarmWireRes",
    "BalanCurrent":    "BalanCurrent",
    "BalanSta / SOCStateOfcharge": "BalanSta",
    "SOCCapRemain":    "SOCCapRemain",
    "SOCFullChargeCap": "SOCFullChargeCap",
    "SOCCycleCount":   "SOCCycleCount",
    "SOCCycleCap":     "SOCCycleCap",
    "SOCSOH / Precharge": "SOCSOH",
    "RunTime":         "RunTime",
    "Charge / Discharge": "Charge",
    "TimeDcOCPR":      "TimeDcOCPR",
    "TimeDcSCPR":      "TimeDcSCPR",
    "TimeCOCPR":       "TimeCOCPR",
    "TimeCSCPR":       "TimeCSCPR",
    "TimeUVPR":        "TimeUVPR",
    "TimeOVPR":        "TimeOVPR",
    "TempSensorAbsent / Heating": "MOS TempSensorAbsent",
    "TimeEmergency":   "TimeEmergency",
    "BatDisCurCorrect": "BatDisCurCorrect",
    "VolChargCur":     "VolChargCur",
    "VolDischargCur":  "VolDischargCur",
    "BatVolCorrect":   "BatVolCorrect",
    "BatVol (0.01V scale)": "BatVol01",
    "HeatCurrent":     "HeatCurrent",
    "SysRunTicks":     "SysRunTicks",
    "TempBat3":        "TempBat3",
    "TempBat4":        "TempBat4",
    "TempBat5":        "TempBat5",
    "RTCTicks":        "RTCTicks",
    "TimeEnterSleep":  "TimeEnterSleep",
    "PCLModuleSta / RVD": "PCLModuleSta",
    # info
    "ManufacturerDeviceID": "ManufacturerDeviceID",
    "HardwareVersion": "HardwareVersion",
    "SoftwareVersion": "SoftwareVersion",
    "ODDRunTime":      "ODDRunTime",
    "PWROnTimes":      "PWROnTimes",
    "UART1MPRTOLNbr / CANMPRTOLNbr": "UART1MPRTOLNbr_CANMPRTOLNbr",
    "UART1MPRTOLEnable": "UART1MPRTOLEnable",
    "UART2MPRTOLNbr / UART2MPRTOLEnable[0]": "UART2MPRTOLNbr_UART2MPRTOLEnable",
    "LCDBuzzerTrigger / DRY1Trigger": "LCDBuzzerTrigger_DRY1Trigger",
    "DRY2Trigger / UARTMPTLVer": "DRY2Trigger_UARTMPTLVer",
    "RCVTime / RFVTime": "RCVTime_RFVTime",
}


def find_phinix(spec_name: str, spec_block_base: int) -> str:
    """Return 'byte 0xNN (reg/addr 0xRRRR) @ file:line' for each phinix variant, or '—'."""
    key = PHINIX_BINDING.get(spec_name)
    if not key:
        return "—"
    entries = PHINIX_DB.get(key, [])
    if not entries:
        # try with suffix-stripped fuzzy
        for k in (key + "S", key + "N"):
            entries = PHINIX_DB.get(k.rstrip("SN"), [])
            if entries:
                break
    if not entries:
        return "—"
    out = []
    seen = set()
    for pid, addr_h, off_h, file, line in entries:
        addr = int(addr_h, 0) if addr_h else None
        off = int(off_h, 0) if off_h else None
        # Convention 1: address-only (number files): treats spec byte as a reg offset
        #   effective_byte = (addr - block_base) * 2
        # Convention 2: address + offset (sensor files): byte = offset directly
        if off is not None and addr is not None:
            byte = off  # spec byte offset (ESPHome semantics: bytes within window)
            label = f"`addr={addr_h} off={off_h}`<br>spec byte `0x{byte:02X}`"
        elif addr is not None:
            byte = (addr - spec_block_base) * 2
            label = f"`addr={addr_h}`<br>spec byte `0x{byte:02X}` (treats spec byte as reg)"
        else:
            continue
        key2 = (file, line)
        if key2 in seen:
            continue
        seen.add(key2)
        out.append(f"{label}<br>phinix/{file}:{line}")
    return "<br><br>".join(out[:2]) if out else "—"


# -- 4. Jean's frame — extract from trame_specs.json -------------------------------

JEAN_DATA = json.loads(JEAN_JSON.read_text())

# Build name-by-line index by scanning the file textually so we can cite line numbers.
JEAN_LINES = JEAN_JSON.read_text().splitlines()


def jean_line_for(name_quoted: str) -> int | None:
    needle = f'"name": "{name_quoted}"'
    for i, line in enumerate(JEAN_LINES, 1):
        if needle in line:
            return i
    return None


# Map spec name → exact field name in Jean's JSON (substring match).
JEAN_BINDING = {
    "VolSmartSleep":     ("Trame 2", "smart_sleep_voltage_V"),
    "VolCellUV":         ("Trame 2", "cell_voltage_undervoltage_protection_V"),
    "VolCellUVPR":       ("Trame 2", "cell_voltage_undervoltage_recovery_V"),
    "VolCellOV":         ("Trame 2", "cell_voltage_overvoltage_protection_V"),
    "VolCellOVPR":       ("Trame 2", "cell_voltage_overvoltage_recovery_V"),
    "VolBalanTrig":      ("Trame 2", "balance_trigger_voltage_V"),
    "VolSOC100%":        ("Trame 2", "cell_voltage_at_100SOC_V"),
    "VolSOC0%":          ("Trame 2", "cell_voltage_at_0SOC_V"),
    "VolCellRCV":        ("Trame 2", "cell_request_charge_voltage_V"),
    "VolCellRFV":        ("Trame 2", "cell_request_float_voltage_V"),
    "VolSysPwrOff":      ("Trame 2", "power_off_voltage_V"),
    "CurBatCOC":         ("Trame 2", "max_charge_current_A"),
    "TIMBatCOCPDly":     ("Trame 2", "charge_overcurrent_protection_delay_S"),
    "TIMBatCOCPRDly":    ("Trame 2", "charge_overcurrent_protection_recovery_time_S"),
    "CurBatDcOC":        ("Trame 2", "max_discharge_current_A"),
    "TIMBatDcOCPDly":    ("Trame 2", "discharge_overcurrent_protection_delay_S"),
    "TIMBatDcOCPRDly":   ("Trame 2", "discharge_overcurrent_protection_recovery_time_S"),
    "TIMBatSCPRDly":     ("Trame 2", "short_circuit_protection_recovery_time_S"),
    "CurBalanMax":       ("Trame 2", "max_balance_current_A"),
    "TMPBatCOT":         ("Trame 2", "charge_overtemperature_protection_C"),
    "TMPBatCOTPR":       ("Trame 2", "charge_overtemperature_protection_recovery_C"),
    "TMPBatDcOT":        ("Trame 2", "discharge_overtemperature_protection_C"),
    "TMPBatDcOTPR":      ("Trame 2", "discharge_overtemperature_protection_recovery_C"),
    "TMPBatCUT":         ("Trame 2", "charge_undertemperature_protection_C"),
    "TMPBatCUTPR":       ("Trame 2", "charge_undertemperature_protection_recovery_C"),
    "TMPMosOT":          ("Trame 2", "power_tube_overtemperature_protection_C"),
    "TMPMosOTPR":        ("Trame 2", "power_tube_overtemperature_protection_recovery_C"),
    "CellCount":         ("Trame 2", "cell_count"),
    "BatChargeEN":       ("Trame 2", "Switch_Charge"),
    "BatDisChargeEN":    ("Trame 2", "Switch_Discharge"),
    "BalanEN":           ("Trame 2", "Switch_Balance"),
    "CapBatCell":        ("Trame 2", "Battery_Capacity_Ah"),
    "VolStartBalan":     ("Trame 2", "balance_starting_voltage_V"),
    "CellVol0..31":      ("Trame 3", "Cell_1_volt_V"),
    "CellWireRes0..31":  ("Trame 3", "Cell_1_ohm_R"),
    "TempMos":           ("Trame 3", "Mos_temp"),
    "BatVol":            ("Trame 3", "Total_Voltage_V"),
    "BatCurrent":        ("Trame 3", "Total_Current_A"),
    "BatWatt":           ("Trame 3", "Total_Power_W"),
    "TempBat1":          ("Trame 3", "Probe_1_temp"),
    "TempBat2":          ("Trame 3", "Probe_2_temp"),
    "TempBat3":          ("Trame 3", "Probe_3_temp"),
    "TempBat4":          ("Trame 3", "Probe_4_temp"),
    "TempBat5":          ("Trame 3", "Probe_5_temp"),
    "BalanCurrent":      ("Trame 3", "Balance_current"),
    "BalanSta / SOCStateOfcharge": ("Trame 3", "SOC_percentage"),
    "SOCCapRemain":      ("Trame 3", "Remaining_Capacity_Ah"),
    "SOCCycleCount":     ("Trame 3", "Cycle_Count"),
    "SOCCycleCap":       ("Trame 3", "Cycle_Capacity_Ah"),
    "SOCSOH / Precharge": ("Trame 3", "SOH_percentage"),
    "RunTime":           ("Trame 3", "Total_runtime"),
    "Charge / Discharge": ("Trame 3", "Switch_Charge"),
    "TempSensorAbsent / Heating": ("Trame 3", "Heating"),
    "HeatCurrent":       ("Trame 3", "Heating_Current"),
    "ManufacturerDeviceID": ("Trame 1", "BMS_A"),
    "HardwareVersion":   ("Trame 1", "FW_A"),
    "SoftwareVersion":   ("Trame 1", "SW_N"),
}


def find_jean(spec_name: str, spec_byte: int) -> str:
    binding = JEAN_BINDING.get(spec_name)
    if not binding:
        return "—"
    trame, field_name = binding
    for f in JEAN_DATA.get(trame, []):
        if f["name"] == field_name:
            frame_byte = f["offset"]
            line = jean_line_for(field_name)
            type_ = f.get("type", "?")
            scale = f.get("scale", "")
            # equivalent spec byte (after 6-byte frame header)
            equiv = frame_byte - 6
            note = ""
            if equiv != spec_byte:
                note = f"<br>(≠ spec byte 0x{spec_byte:02X} − Jean's frame layout differs)"
            cite = f"{JEAN_REL}:{line}" if line else JEAN_REL
            return f"{trame} byte `0x{frame_byte:02X}` (= spec byte `0x{equiv:02X}`) {type_} {scale}<br>{cite}{note}"
    return "—"


# -- 5. emit markdown --------------------------------------------------------------

block_titles = {
    0x1000: "Settings block `0x1000` (RW)",
    0x1200: "Real-time block `0x1200` (R)",
    0x1400: "Info block `0x1400` (R/RW)",
    0x1600: "Command block `0x1600` (W only)",
}

print("# Field matrix — spec vs implementations\n")
print("Generated by [`scripts/build_field_matrix.py`](../scripts/build_field_matrix.py) ")
print("from five primary sources:\n")
print("1. **V1.0** — [`specifications/BMS.RS485.Modbus.V1.0.pdf`](specifications/BMS.RS485.Modbus.V1.0.pdf), byte offsets hand-transcribed.")
print("2. **V1.1** — [`specifications/BMS.RS485.Modbus.V1.1.pdf`](specifications/BMS.RS485.Modbus.V1.1.pdf), byte offsets hand-transcribed.")
print("3. **This repo** — `RegisterDef.address` (settings) or `_OFF_*: Final = …` (RT / info) in our protocol modules. Byte = `(reg − block_base) × 2` for settings/info; byte = `buffer_offset × 2` for the stitched RT block.")
print("4. **Phinix-org** — [Multiple-JK-BMS-by-Modbus-RS485](https://github.com/phinix-org/Multiple-JK-BMS-by-Modbus-RS485). Each YAML defines either `address` alone or `address + offset`; both literal numbers shown, plus the implied spec byte they map to.")
print("5. **Jean** — [`upstream_reference/trame_specs.json`](../../upstream_reference/trame_specs.json), the field map for the proprietary 0x55AAEB90 BLE/UART frame Jean's Node-RED add-on uses. **Different protocol.** Jean's frame byte = spec byte + 6 (header). Little-endian.\n")
print("`—` = field not present / not decoded in that source.\n")
print("## How phinix's two address conventions compare\n")
print("- Files under `include/backup/sensors/` and `include/devel/sensors/` write")
print("  `address: <block_base>` together with `offset: <byte>`. Under ESPHome's")
print("  modbus_controller semantics that targets the same register that")
print("  spec_byte_offset → block_base + byte/2 yields. Matrix shows: ")
print("  `addr=0xNNNN off=0xMM`, spec byte = `0xMM`.")
print("- Files under `include/backup/numbers/` and `include/modules/numbers/` write")
print("  `address: <block_base + spec_byte>` directly (no `offset:`). That's a")
print("  register-count interpretation: `spec_byte` is treated as a Modbus reg")
print("  increment, addressing the register `block_base + spec_byte` — which is")
print("  *not* the same Modbus register that the spec byte-offset interpretation")
print("  yields. Matrix shows: `addr=0xNNNN`, spec byte = `(addr − base) × 2`.\n")
print("If the two interpretations were equivalent the matrix would show the same")
print("spec byte in both rows. Where they differ, that is a real disagreement.\n")

rows = []
for (base, byte, typ, length, rw, name, unit, ver) in V11_FIELDS:
    v10 = f"`0x{byte:04X}`" if "v1.0" in ver or "v1.0+" in ver else "—"
    v11 = f"`0x{byte:04X}`" if "v1.1" in ver or "v1.0+" in ver else "—"
    if ver == "v1.0 only":
        v11 = "—"
    elif ver == "v1.1 only":
        v10 = "—"

    ours = find_ours(name, base)
    phx = find_phinix(name, base)
    jn = find_jean(name, byte)
    rows.append((base, name, typ, unit, v10, v11, ours, phx, jn))

groups = {}
for r in rows:
    groups.setdefault(r[0], []).append(r)

for base in sorted(groups):
    print(f"\n## {block_titles[base]}\n")
    print("| Field | Type | Unit | V1.0 byte | V1.1 byte | This repo (byte / reg) | Phinix-org (byte / reg / @file:line) | Jean (Trame byte / @file:line) |")
    print("|---|---|---|---|---|---|---|---|")
    for (_, name, typ, unit, v10, v11, ours, phx, jn) in groups[base]:
        print(f"| `{name}` | {typ} | {unit} | {v10} | {v11} | {ours} | {phx} | {jn} |")
