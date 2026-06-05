# Verification runbook — full-block sweep + BLE cross-check

A one-shot test cycle to verify every spec'd register on real hardware,
cross-checked against the JK app's BLE-reported settings. Designed to be
runnable in ~15 minutes from your side and produce a definitive
implementation-vs-firmware comparison.

## What it produces

Two artifacts that together pin down what each spec'd register holds on
**your** firmware variant:

1. `scripts/captures/BMS_<id>_sweep.txt` — every spec'd block (settings,
   real-time, info) dumped in <=120-register chunks, plus a few empirical
   alias probes. Modbus errors on the alias probes are normal.
2. `scripts/captures/BMS_<id>_BLE.md` — the BLE settings export template
   (`scripts/captures/BLE_export_template.md`) filled in with the values
   the JK app shows for the same BMS.

I'll diff the two and either commit a regression-fixture test that locks
every byte for your firmware, or flag any newly-discovered deviations.

## Pre-flight

- [ ] **Stop the add-on** in Home Assistant. Two clients hitting the gateway
      simultaneously cause RTU frame collisions and the sweep will read
      garbage.
- [ ] Confirm the gateway IP, port, and the BMS slave ID you want to dump.
      Pick **one** BMS for the first pass; we can repeat for the rest if
      this one passes clean.
- [ ] Open the JK app over BLE on the same BMS. Don't switch BMS between
      capturing the gateway dump and the BLE export — they must reflect
      the same firmware state.

## Step 1 — gateway dump (~30 s)

From any machine that can reach the gateway:

```bash
cd /Users/basmeerman/Downloads/jkbms2mqtt/jkbms2mqtt

/Users/basmeerman/Downloads/jkbms2mqtt/.venv/bin/python \
    -m scripts.dump_full_sweep \
    --gateway <gateway-ip> \
    --port 502 \
    --slave-id <bms-id> \
    > scripts/captures/BMS_<bms-id>_sweep.txt 2>&1
```

Replace `<gateway-ip>` and `<bms-id>`. The output file should be ~250 lines
of hex dump + ~80 lines of decoded interpretation. If the file looks much
shorter than that, something failed — paste it back anyway and I'll
diagnose.

To skip the empirical alias probes (cleaner output, no Modbus errors):

```bash
… -m scripts.dump_full_sweep --skip-alias …
```

I recommend running **with** alias probes the first time — the errors
themselves carry information about which addresses your firmware exposes.

## Step 2 — BLE export (~5–10 min)

1. Open the JK app on the same BMS over BLE.
2. Open [`scripts/captures/BLE_export_template.md`](../scripts/captures/BLE_export_template.md).
3. Save a copy named `scripts/captures/BMS_<id>_BLE.md`.
4. Fill in each field by pasting the value the app shows. Sections:

   | Tab in app | Section in template |
   |---|---|
   | Status (top) | Header, Status tab anchors |
   | Status (cells) | Per-cell voltages + wire resistances |
   | Settings (top) | Basic settings |
   | Settings (Advance) | Voltages / SoC, currents / timing |
   | Settings (text) | Text fields |
   | Control | Toggles |
   | Settings (Wire Res.) | Configured wire resistance (only if non-zero) |

   Leave a row blank if the app doesn't show that field. Don't make values up.

5. Save and send the file back along with the sweep dump.

## Step 3 — restart the add-on

After capturing both files, restart the add-on. The sweep is read-only and
makes no changes to the BMS, so nothing to revert.

## What I'll do with the data

1. **Per-field cross-check.** Every spec'd register gets one of:
   - **VERIFIED** — decoder value matches BLE app value within rounding.
   - **DEVIATION** — values disagree by more than rounding; flagged for
     follow-up. Captured in `docs/FIELD_AUDIT.md` so future contributors
     see the firmware variant.
   - **UNVERIFIED** — neither source has a value to compare (zero in
     dump, blank in BLE).

2. **Commit the sweep as a second regression fixture.** Today's
   `BMS_1.txt` capture is locked in
   `tests/integration/test_decoder_against_real_capture.py`. Adding the
   new sweep as `BMS_<id>_sweep.txt` with parametrised assertions means
   CI catches a regression that breaks either firmware revision.

3. **If any deviations surface**, update `docs/FIELD_MATRIX.md` and
   `docs/specifications/README.md` to record the firmware-specific
   variation. No production code change is shipped unless we're sure the
   *spec'd* address has been wrong all along — and that requires either
   primary-source backing or both BMSes agreeing.

## What "clean" looks like

A clean verification looks like:

- Every numeric setting in the BLE export matches the decoded value in the
  sweep dump's `## Decoded settings` section. We expect millivolt /
  100 mA / 0.1 °C agreement.
- The packed-bit register decodes to the same toggles the Control tab
  shows.
- The real-time decoder values match the Status tab anchors within
  rounding and a few seconds of drift on `runtime_s`.
- The static-info block returns the same model/HW/SW/serial the app shows.

If all of those hold, the decoder is correct end-to-end for this firmware
variant and we lock it in.
