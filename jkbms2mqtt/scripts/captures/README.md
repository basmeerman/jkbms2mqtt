Captures from real BMS hardware. Used as test fixtures and as a record of the offset audit. Files contain no secrets.

## Files in this directory

| File | What it is |
|---|---|
| `BMS_1.txt` | First-pass capture — settings + RT + info, single packed-bit probe at `0x1114` returning `0x3200`. Used by `tests/integration/test_decoder_against_real_capture.py`. |
| `BMS_1_sweep.txt` | Comprehensive sweep — same BMS, every spec'd block in chunks. Probed both spec address `0x108A` (returns `0x0000`) and empirical `0x1114` (matches the BLE app's Control-tab toggles). Confirmed the heating bit is the **low** byte of reg `0x1268` (the high byte is `TempSensorAbsent`). Cited as evidence in `docs/FIELD_AUDIT.md`. |
| `BLE_export_template.md` | Fill-in template for cross-referencing the JK app's BLE-side values against a sweep dump. See `docs/VERIFICATION_RUNBOOK.md`. |

## `hw_evidence/` — write-path validation (issue #32)

Curated output of `scripts/hw_testbench.py`, kept as the record behind the
byte-direct write addressing fix. See `docs/HW_TESTBENCH.md` for the runbook.

| File | What it is |
|---|---|
| `BMS_<1-6>_baseline.json` | As-found settings of every pack in the bank, captured read-only before any write. Raw words plus decoded values. Also the restore source if a bench run ever aborts. Note pack 6 is configured for 360 Ah against 314 Ah elsewhere, and pack 1 has a 3.50 V over-voltage recovery against 3.58 V elsewhere — pre-existing differences, not faults. |
| `BMS_1_full_sweep_report.json` | First full write sweep: 30 `pass`, 5 `fail_write`, pack restored. The five failures are the two firmware quirks documented in `docs/HW_TESTBENCH.md` (FC06 unsupported; two parameters already at their ceiling), not addressing faults. |
| `BMS_1_retest_report.json` | The same five parameters after the bench gained a downward retry and an FC06→FC16 fallback: 5/5 `pass`. Together these put BMS 1 at 35/35 parameters written, verified and restored. |

Per-run output goes to `hw_snapshots/`, which is gitignored — it is regenerated
on every run and is the operator's local safety record.
