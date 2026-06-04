"""Build the cross-source field matrix.

Sources:
- V1.0 spec — manually transcribed from BMS.RS485.Modbus.V1.0.pdf
- V1.1 spec — manually transcribed from BMS.RS485.Modbus.V1.1.pdf
- this repo — jkbms2mqtt source (Modbus register addresses + file:line)
- jean — upstream_reference/trame_specs.json (frame byte offsets, NOT Modbus)
- phinix — phinix-org/Multiple-JK-BMS-by-Modbus-RS485 (ESPHome YAML config)

Output: docs/FIELD_MATRIX.md with one row per spec field.
"""
import json
import re
from pathlib import Path

REPO = Path("/Users/basmeerman/Downloads/jkbms2mqtt")
PHINIX = Path("/tmp/jk-research/phinix")
JEAN_SPECS = REPO / "upstream_reference" / "trame_specs.json"

# -- 1. spec fields  ---------------------------------------------------------------
#
# Tuples are: (block_base, byte_offset, type, length, rw, name, unit_or_note)
# Hand-transcribed from the PDFs in jkbms2mqtt/docs/specifications/.
#
# Both V1.0 and V1.1 are listed in one table; missing-from-V1.0 entries are
# tagged with the 'v11_only' marker.

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
    (0x1000, 0x6C, "UINT32", 4, "RW", "CellCount",                "(cells)","v1.0+v1.1"),
    (0x1000, 0x70, "UINT32", 4, "RW", "BatChargeEN",              "1:open/0:close","v1.0+v1.1"),
    (0x1000, 0x74, "UINT32", 4, "RW", "BatDisChargeEN",           "1:open/0:close","v1.0+v1.1"),
    (0x1000, 0x78, "UINT32", 4, "RW", "BalanEN",                  "1:open/0:close","v1.0+v1.1"),
    (0x1000, 0x7C, "UINT32", 4, "RW", "CapBatCell",               "mAh",  "v1.0+v1.1"),
    (0x1000, 0x80, "UINT32", 4, "RW", "SCPDelay",                 "μs",   "v1.0+v1.1"),
    (0x1000, 0x84, "UINT32", 4, "RW", "VolStartBalan",            "mV",   "v1.0+v1.1"),
    (0x1000, 0x88, "UINT32", 4, "RW", "CellConWireRes0..31",      "μΩ × 32 cells","v1.0+v1.1"),
    (0x1000, 0x108,"UINT32", 4, "RW", "DevAddr",                  "(addr)","v1.0+v1.1"),
    (0x1000, 0x10C,"UINT32", 4, "RW", "TIMProdischarge",          "s",    "v1.0+v1.1"),
    (0x1000, 0x114,"UINT16", 2, "RW", "packed_bits (HeatEN..ChargingFloatMode)", "bitfield","v1.0:bits0-6 v1.1:bits0-9"),
    (0x1000, 0x116,"INT8",   2, "RW", "TMPBatOTA / TMPBatOTAR",   "°C",   "v1.0 only"),
    (0x1000, 0x118,"UINT8",  2, "RW", "TIMSmartSleep",            "H",    "v1.0+v1.1"),
    # ---- Real-time block 0x1200 ----
    (0x1200, 0x00, "UINT16", 2, "R",  "CellVol0..31",             "mV × 32 cells","v1.0+v1.1"),
    (0x1200, 0x40, "UINT32", 4, "R",  "CellSta",                  "bitmap","v1.0+v1.1"),
    (0x1200, 0x44, "UINT16", 2, "R",  "CellVolAve",               "mV",   "v1.0+v1.1"),
    (0x1200, 0x46, "UINT16", 2, "R",  "CellVdifMax",              "mV",   "v1.0+v1.1"),
    (0x1200, 0x48, "UINT8+UINT8", 2, "R", "MaxVolCellNbr / MinVolCellNbr", "(idx)","v1.0+v1.1"),
    (0x1200, 0x4A, "UINT16", 2, "R",  "CellWireRes0..31",         "mΩ × 32 cells","v1.0+v1.1"),
    (0x1200, 0x8A, "INT16",  2, "R",  "TempMos",                  "0.1°C","v1.0+v1.1"),
    (0x1200, 0x8C, "UINT32", 4, "R",  "CellWireResSta",           "bitmap","v1.0+v1.1"),
    (0x1200, 0x90, "UINT32", 4, "R",  "BatVol",                   "mV",   "v1.0+v1.1"),
    (0x1200, 0x94, "UINT32", 4, "R",  "BatWatt",                  "mW",   "v1.0+v1.1"),
    (0x1200, 0x98, "INT32",  4, "R",  "BatCurrent",               "mA",   "v1.0+v1.1"),
    (0x1200, 0x9C, "INT16",  2, "R",  "TempBat1",                 "0.1°C","v1.0+v1.1"),
    (0x1200, 0x9E, "INT16",  2, "R",  "TempBat2",                 "0.1°C","v1.0+v1.1"),
    (0x1200, 0xA0, "UINT32", 4, "R",  "alarms (22 bits)",         "bitfield","v1.0+v1.1"),
    (0x1200, 0xA4, "INT16",  2, "R",  "BalanCurrent",             "mA",   "v1.0+v1.1"),
    (0x1200, 0xA6, "UINT8+UINT8", 2, "R", "BalanSta / SOCStateOfcharge", "% (low byte)","v1.0+v1.1"),
    (0x1200, 0xA8, "INT32",  4, "R",  "SOCCapRemain",             "mAh",  "v1.0+v1.1"),
    (0x1200, 0xAC, "UINT32", 4, "R",  "SOCFullChargeCap",         "mAh",  "v1.0+v1.1"),
    (0x1200, 0xB0, "UINT32", 4, "R",  "SOCCycleCount",            "(count)","v1.0+v1.1"),
    (0x1200, 0xB4, "UINT32", 4, "R",  "SOCCycleCap",              "mAh",  "v1.0+v1.1"),
    (0x1200, 0xB8, "UINT8+UINT8", 2, "R", "SOCSOH / Precharge",   "% / 1:on","v1.0+v1.1"),
    (0x1200, 0xBA, "UINT16", 2, "R",  "UserAlarm",                "",     "v1.0+v1.1"),
    (0x1200, 0xBC, "UINT32", 4, "R",  "RunTime",                  "s",    "v1.0+v1.1"),
    (0x1200, 0xC0, "UINT8+UINT8", 2, "R", "Charge / Discharge",   "1:on/0:off","v1.0+v1.1"),
    (0x1200, 0xC2, "UINT16", 2, "R",  "UserAlarm2",               "",     "v1.0+v1.1"),
    (0x1200, 0xC4, "UINT16", 2, "R",  "TimeDcOCPR",               "s",    "v1.0+v1.1"),
    (0x1200, 0xC6, "UINT16", 2, "R",  "TimeDcSCPR",               "s",    "v1.0+v1.1"),
    (0x1200, 0xC8, "UINT16", 2, "R",  "TimeCOCPR",                "s",    "v1.0+v1.1"),
    (0x1200, 0xCA, "UINT16", 2, "R",  "TimeCSCPR",                "s",    "v1.0+v1.1"),
    (0x1200, 0xCC, "UINT16", 2, "R",  "TimeUVPR",                 "s",    "v1.0+v1.1"),
    (0x1200, 0xCE, "UINT16", 2, "R",  "TimeOVPR",                 "s",    "v1.0+v1.1"),
    (0x1200, 0xD0, "UINT8+UINT8", 2, "R", "TempSensorAbsent / Heating", "bits / 1:on","v1.0+v1.1"),
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
    (0x1200, 0xEE, "UINT8+UINT8", 2, "R", "RVD / ChargerPlugged", "1:in/0:out","v1.0+v1.1"),
    (0x1200, 0xF0, "UINT32", 4, "R",  "SysRunTicks",              "0.1s", "v1.0+v1.1"),
    (0x1200, 0xF4, "UINT32", 4, "R",  "PVDTrigTimestamps",        "0.1s", "v1.0 only"),
    (0x1200, 0xF8, "INT16",  2, "R",  "TempBat3",                 "0.1°C","v1.0+v1.1"),
    (0x1200, 0xFA, "INT16",  2, "R",  "TempBat4",                 "0.1°C","v1.0+v1.1"),
    (0x1200, 0xFC, "INT16",  2, "R",  "TempBat5",                 "0.1°C","v1.0+v1.1"),
    (0x1200, 0x100,"UINT32", 4, "R",  "RTCTicks",                 "from 2020-01-01","v1.0+v1.1"),
    (0x1200, 0x108,"UINT32", 4, "R",  "TimeEnterSleep",           "s",    "v1.0+v1.1"),
    (0x1200, 0x10C,"UINT8+UINT8", 2, "R", "PCLModuleSta / RVD",   "1:on/0:off","v1.0+v1.1"),
    # ---- Info block 0x1400 ----
    (0x1400, 0x00, "ASCII", 16, "R",  "ManufacturerDeviceID",     "",     "v1.0+v1.1"),
    (0x1400, 0x10, "ASCII",  8, "R",  "HardwareVersion",          "",     "v1.0+v1.1"),
    (0x1400, 0x18, "ASCII",  8, "R",  "SoftwareVersion",          "",     "v1.0+v1.1"),
    (0x1400, 0x20, "UINT32", 4, "R",  "ODDRunTime",               "s",    "v1.0+v1.1"),
    (0x1400, 0x24, "UINT32", 4, "R",  "PWROnTimes",               "(count)","v1.0+v1.1"),
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
    (0x1600, 0x00, "UINT16", 4, "W",  "VoltageCalibration",       "mV (V1.1) / 0.1V (V1.0)","v1.0+v1.1: encoding differs"),
    (0x1600, 0x04, "UINT16", 2, "W",  "Shutdown (V1.1 only)",     "",     "v1.1 only"),
    (0x1600, 0x06, "UINT16", 4, "W",  "CurrentCalibration",       "mA (V1.1) / 0.1A (V1.0)","v1.0+v1.1: layout/encoding differs"),
    (0x1600, 0x0A, "UINT16", 2, "W",  "LI-ION (one-touch)",       "",     "v1.0+v1.1"),
    (0x1600, 0x0C, "UINT16", 2, "W",  "LIFEPO4",                  "",     "v1.0+v1.1"),
    (0x1600, 0x0E, "UINT16", 2, "W",  "LTO",                      "",     "v1.0+v1.1"),
    (0x1600, 0x10, "UINT16", 2, "W",  "Emergency",                "",     "v1.0+v1.1"),
    (0x1600, 0x12, "UINT32", 4, "W",  "Timecalibration",          "",     "v1.0+v1.1"),
]

# -- 2. extract our repo decode citations -------------------------------------------

def find_in_repo(pattern, paths):
    """Return list of (file:line, line_text) for matching lines."""
    out = []
    for path in paths:
        for i, line in enumerate(path.read_text().splitlines(), 1):
            if re.search(pattern, line):
                rel = str(path.relative_to(REPO))
                out.append((f"{rel}:{i}", line.strip()))
    return out

REPO_PATHS = [
    REPO / "jkbms2mqtt" / "src" / "jkbms2mqtt" / "protocol" / "jk_modbus.py",
    REPO / "jkbms2mqtt" / "src" / "jkbms2mqtt" / "protocol" / "jk_settings.py",
]

# Map spec name -> impl identifier we expect to find
SPEC_TO_OURS = {
    "VolSmartSleep": "smart_sleep_voltage",
    "VolCellUV": "cell_voltage_undervoltage_protection",
    "VolCellUVPR": "cell_voltage_undervoltage_recovery",
    "VolCellOV": "cell_voltage_overvoltage_protection",
    "VolCellOVPR": "cell_voltage_overvoltage_recovery",
    "VolBalanTrig": "balance_trigger_voltage",
    "VolSOC100%": "cell_soc100_voltage",
    "VolSOC0%": "cell_soc0_voltage",
    "VolCellRCV": "cell_request_charge_voltage",
    "VolCellRFV": "cell_request_float_voltage",
    "VolSysPwrOff": "power_off_voltage",
    "CurBatCOC": "max_charge_current",
    "TIMBatCOCPDly": "charge_overcurrent_protection_delay",
    "TIMBatCOCPRDly": "charge_overcurrent_protection_recovery_time",
    "CurBatDcOC": "max_discharge_current",
    "TIMBatDcOCPDly": "discharge_overcurrent_protection_delay",
    "TIMBatDcOCPRDly": "discharge_overcurrent_protection_recovery_time",
    "TIMBatSCPRDly": "short_circuit_protection_recovery_time",
    "CurBalanMax": "max_balance_current",
    "TMPBatCOT": "charge_overtemperature_protection",
    "TMPBatCOTPR": "charge_overtemperature_protection_recovery",
    "TMPBatDcOT": "discharge_overtemperature_protection",
    "TMPBatDcOTPR": "discharge_overtemperature_protection_recovery",
    "TMPBatCUT": "charge_undertemperature_protection",
    "TMPBatCUTPR": "charge_undertemperature_protection_recovery",
    "TMPMosOT": "power_tube_overtemperature_protection",
    "TMPMosOTPR": "power_tube_overtemperature_protection_recovery",
    "CellCount": '"cell_count"',
    "BatChargeEN": "charging_switch",
    "BatDisChargeEN": "discharging_switch",
    "BalanEN": "balance_switch",
    "CapBatCell": "pack_capacity_setting",
    "SCPDelay": "short_circuit_protection_delay_us",
    "VolStartBalan": "balance_starting_voltage",
    "CellVol0..31": "_OFF_CELL_VOLT_0",
    "CellSta": "_OFF_CELL_PRESENT",
    "CellVolAve": "_OFF_CELL_AVG_V",
    "CellVdifMax": "_OFF_CELL_DELTA",
    "CellWireRes0..31": "_OFF_CELL_RES_0",
    "TempMos": "_OFF_MOS_TEMP",
    "BatVol": "_OFF_TOTAL_V",
    "BatWatt": "_OFF_TOTAL_POWER",
    "BatCurrent": "_OFF_TOTAL_CURRENT",
    "TempBat1": "_OFF_PROBE_1_TEMP",
    "TempBat2": "_OFF_PROBE_2_TEMP",
    "alarms (22 bits)": "_OFF_ALARM_BITS",
    "BalanCurrent": "_OFF_BALANCE_CURRENT",
    "BalanSta / SOCStateOfcharge": "_OFF_BALANCE_STATE_SOC",
    "SOCCapRemain": "_OFF_REMAINING_CAP",
    "SOCFullChargeCap": "_OFF_NOMINAL_CAP",
    "SOCCycleCount": "_OFF_CYCLE_COUNT",
    "SOCCycleCap": "_OFF_TOTAL_CYCLE_CAP",
    "SOCSOH / Precharge": "_OFF_SOH_PRECHARGE",
    "RunTime": "_OFF_RUNTIME",
    "Charge / Discharge": "_OFF_CHARGE_DISCHARGE",
    "TempSensorAbsent / Heating": "_OFF_TEMP_SENSOR_HEATING",
    "HeatCurrent": "_OFF_HEATING_CURRENT",
    "TempBat3": "_OFF_PROBE_3_TEMP",
    "TempBat4": "_OFF_PROBE_4_TEMP",
    "TempBat5": "_OFF_PROBE_5_TEMP",
    "ManufacturerDeviceID": "_OFF_MODEL",
    "HardwareVersion": "_OFF_HW_VERSION",
    "SoftwareVersion": "_OFF_SW_VERSION",
    "packed_bits (HeatEN..ChargingFloatMode)": "PACKED_BIT_REGISTER",
}

def find_ours(spec_name):
    """Return list of file:line citations or [] if not implemented."""
    if spec_name not in SPEC_TO_OURS: return []
    needle = SPEC_TO_OURS[spec_name]
    # escape regex meta
    pat = re.escape(needle)
    return find_in_repo(pat, REPO_PATHS)

# -- 3. phinix lookup --------------------------------------------------------------

phinix_db = json.loads(Path("/tmp/jk-research/phinix_fields.json").read_text())

def find_phinix(spec_name):
    """Match by id prefix; phinix appends 'S' for sensor / 'N' for number sometimes."""
    base = re.sub(r"\W", "", spec_name.split()[0].replace("%", "").replace("/", "").replace("(", ""))
    matches = []
    for pid, info in phinix_db.items():
        if pid == base or pid == base + "S" or pid == base + "N" or pid.startswith(base + "_") or (pid == base.replace("..31", "00")):
            matches.append((pid, info))
    return matches

# -- 4. Jean lookup (proprietary frame) --------------------------------------------

jean = json.loads(JEAN_SPECS.read_text())

def normalise_jean_name(n):
    return n.lower().replace("_v", "").replace("_a", "").replace("_w", "").rstrip("_")

JEAN_INDEX = {}
for trame_name, fields in jean.items():
    for idx, f in enumerate(fields):
        key = normalise_jean_name(f["name"])
        JEAN_INDEX[key] = (trame_name, idx, f)

JEAN_MAP = {
    "VolSmartSleep": "smart_sleep_voltage",
    "VolCellUV": "cell_voltage_undervoltage_protection",
    "VolCellUVPR": "cell_voltage_undervoltage_recovery",
    "VolCellOV": "cell_voltage_overvoltage_protection",
    "VolCellOVPR": "cell_voltage_overvoltage_recovery",
    "VolBalanTrig": "balance_trigger_voltage",
    "VolSOC100%": "cell_soc100",
    "VolSOC0%": "cell_soc0",
    "VolCellRCV": "cell_request_charge_voltage",
    "VolCellRFV": "cell_request_float_voltage",
    "VolSysPwrOff": "power_off_voltage",
    "CurBatCOC": "max_charge_current",
    "TIMBatCOCPDly": "charge_overcurrent_protection_delay",
    "TIMBatCOCPRDly": "charge_overcurrent_protection_recovery_time",
    "CurBatDcOC": "max_discharge_current",
    "TIMBatDcOCPDly": "discharge_overcurrent_protection_delay",
    "TIMBatDcOCPRDly": "discharge_overcurrent_protection_recovery_time",
    "TIMBatSCPRDly": "short_circuit_protection_recovery_time",
    "CurBalanMax": "max_balance_current",
    "TMPBatCOT": "charge_overtemperature_protection",
    "TMPBatDcOT": "discharge_overtemperature_protection",
    "TMPBatCUT": "charge_undertemperature_protection",
    "TMPMosOT": "power_tube_overtemperature_protection",
    "CellCount": "cell_count",
    "BatChargeEN": "switch_charge",
    "BatDisChargeEN": "switch_discharge",
    "BalanEN": "switch_balance",
    "CapBatCell": "battery_capacity",
    "SCPDelay": "scp_delay",
    "VolStartBalan": "balance_starting_voltage",
    "CellVol0..31": "cell_1_volt",
    "CellWireRes0..31": "cell_1_ohm",
    "TempMos": "mos_temp",
    "BatVol": "total_voltage",
    "BatCurrent": "total_current",
    "BatWatt": "total_power",
    "TempBat1": "probe_1_temp",
    "TempBat2": "probe_2_temp",
    "TempBat3": "probe_3_temp",
    "TempBat4": "probe_4_temp",
    "TempBat5": "probe_5_temp",
    "BalanCurrent": "balance_current",
    "BalanSta / SOCStateOfcharge": "soc_percentage",
    "SOCCapRemain": "remaining_capacity",
    "SOCFullChargeCap": "battery_capacity",
    "SOCCycleCount": "cycle_count",
    "SOCCycleCap": "cycle_capacity",
    "SOCSOH / Precharge": "soh_percentage",
    "RunTime": "total_runtime",
    "Charge / Discharge": "switch_charge",
    "TempSensorAbsent / Heating": "heating",
    "HeatCurrent": "heating_current",
    "ManufacturerDeviceID": "bms",
    "HardwareVersion": "fw",
    "SoftwareVersion": "sw",
}

def find_jean(spec_name):
    if spec_name not in JEAN_MAP: return None
    needle = JEAN_MAP[spec_name].lower()
    # search trames
    for trame_name, fields in jean.items():
        for f in fields:
            fname = f["name"].lower().rstrip("_v_a_w").replace("_v_", "_").replace("_w_", "_").replace("_a_", "_")
            if needle in fname or fname.startswith(needle):
                return (trame_name, f["offset"], f["type"], f.get("scale", ""))
    return None

# -- 5. emit markdown matrix --------------------------------------------------------

def fmt_ours(citations):
    if not citations: return "—"
    # Take the first match; collapse to "reg 0x... — file:line"
    rows = []
    for (loc, _) in citations[:2]:
        rows.append(loc)
    return "<br>".join(rows)

def fmt_phinix(matches):
    if not matches: return "—"
    out = []
    for pid, (reg, file, line, addr, off) in matches[:2]:
        addr_s = addr if addr else "?"
        off_s = f" off={off}" if off else ""
        out.append(f"`{pid}` addr={addr_s}{off_s}<br>{file}:{line}")
    return "<br>".join(out)

def fmt_jean(j):
    if not j: return "—"
    trame, off, tp, scale = j
    return f"{trame} byte {off} ({tp}) {scale}".rstrip()

rows = []
for (base, byte_off, typ, length, rw, name, unit, ver) in V11_FIELDS:
    spec_v10 = f"`0x{byte_off:04X}` ({typ})" if "v1.0" in ver else "—"
    spec_v11 = f"`0x{byte_off:04X}` ({typ})" if "v1.1" in ver else "—"
    # both: in v1.0+v1.1, show both
    if "v1.0+v1.1" in ver:
        spec_v10 = f"`0x{byte_off:04X}`"
        spec_v11 = f"`0x{byte_off:04X}`"
    elif "v1.0 only" in ver:
        spec_v10 = f"`0x{byte_off:04X}`"
        spec_v11 = "—"
    elif "v1.1 only" in ver:
        spec_v10 = "—"
        spec_v11 = f"`0x{byte_off:04X}`"

    ours = fmt_ours(find_ours(name))
    phx = fmt_phinix(find_phinix(name))
    jn = fmt_jean(find_jean(name))
    rows.append((base, name, typ, unit, spec_v10, spec_v11, ours, phx, jn))

# Group by block
print("# Field matrix — spec vs implementations\n")
print("Generated from `/tmp/jk-research/build_matrix.py`. One row per spec field; ")
print("the cell-arrays and wire-resistance arrays are condensed to a single row each.\n")
print("Citations are `file:line` from the repo. `—` = field not present / not decoded.\n")
print("**Important** — Jean's offsets are byte positions within his proprietary 300-byte ")
print("0x55AAEB90 frame (Trame 1=info, 2=settings, 3=real-time), **not** Modbus register ")
print("addresses; little-endian. Phinix's `address/offset` are taken verbatim from their ")
print("ESPHome YAML; effective Modbus register depends on ESPHome's offset semantics ")
print("(bytes within the read window).\n")
groups = {}
for r in rows:
    groups.setdefault(r[0], []).append(r)
block_titles = {
    0x1000: "Settings block `0x1000` (RW)",
    0x1200: "Real-time block `0x1200` (R)",
    0x1400: "Info block `0x1400` (R/RW)",
    0x1600: "Command block `0x1600` (W only)",
}
for base in sorted(groups):
    print(f"\n## {block_titles[base]}\n")
    print("| Field | Type | Unit | V1.0 byte | V1.1 byte | This repo | Phinix-org | Jean (frame) |")
    print("|---|---|---|---|---|---|---|---|")
    for (_, name, typ, unit, v10, v11, ours, phx, jn) in groups[base]:
        print(f"| `{name}` | {typ} | {unit} | {v10} | {v11} | {ours} | {phx} | {jn} |")
