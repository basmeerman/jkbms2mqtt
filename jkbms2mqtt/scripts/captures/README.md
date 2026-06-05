Captures from real BMS hardware. Used as test fixtures and as a record of the offset audit. Files contain no secrets.

## Files in this directory

| File | What it is |
|---|---|
| `BMS_1.txt` | First-pass capture — settings + RT + info, single packed-bit probe at `0x1114` returning `0x3200`. Used by `tests/integration/test_decoder_against_real_capture.py`. |
| `BMS_1_sweep.txt` | Comprehensive sweep — same BMS, every spec'd block in chunks. Probed both spec address `0x108A` (returns `0x0000`) and empirical `0x1114` (matches the BLE app's Control-tab toggles). Confirmed the heating bit is the **low** byte of reg `0x1268` (the high byte is `TempSensorAbsent`). Cited as evidence in `docs/FIELD_AUDIT.md`. |
| `BLE_export_template.md` | Fill-in template for cross-referencing the JK app's BLE-side values against a sweep dump. See `docs/VERIFICATION_RUNBOOK.md`. |
