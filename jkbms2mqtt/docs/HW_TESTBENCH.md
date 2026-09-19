# Hardware test bench — runbook

`scripts/hw_testbench.py` reads every writable setting from a real pack,
perturbs each one by a single step, verifies the change, checks that nothing
else moved, and restores the original value. It exists to validate the issue
 #32 addressing fix against hardware.

## Preconditions

- The `jkbms2mqtt` add-on is **stopped**. Nothing else may hold the RS485 bus.
- Loads and chargers are disconnected. The bench enforces this with a current
  interlock (it refuses to write if more than 1 A flows).
- Run every command from the `jkbms2mqtt/` directory.

## What it changes, and for how long

For each parameter: one write of `value ± one step`, one verification read,
then a write of the **captured raw words** back. Not a re-encode — the exact
words that were read, so a restore cannot drift.

Booleans (`charging_switch`, `discharging_switch`, `balance_switch`, and the
packed bits) are flipped and flipped back. MOSFET switches are tested last so
an abort cannot leave a pack mid-run with an output disabled.

Excluded by default because they reconfigure the pack rather than tune it:
`cell_count`, `pack_capacity_setting`. Add `--include-structural` to test them.

## Step 0 — see where the bug lands (no hardware)

```
.venv/bin/python -m scripts.hw_testbench --collision-report
```

Prints, for every parameter, the address the current table holds, the byte that
address denotes to the firmware, and which parameter actually owns that byte.
Every parameter except the one at byte 0 lands on a different parameter's
storage.

## Step 1 — read-only, one pack

```
.venv/bin/python -m scripts.hw_testbench \
    --gateway 192.168.8.153 --port 502 --slave-id 1
```

Writes a safety snapshot to `scripts/captures/hw_snapshots/` and prints every
decoded setting. No writes. Confirm the values match the BMS app before going
further.

## Step 2 — read-only, whole bank

```
.venv/bin/python -m scripts.hw_testbench \
    --gateway 192.168.8.153 --port 502 --slave-ids 1,2,3,4,5,6
```

Six snapshot files, one per pack. **Keep these.** They are the restore source
if anything goes wrong later.

## Step 3 — write test, one pack

Start with a single harmless parameter before the full sweep:

```
.venv/bin/python -m scripts.hw_testbench \
    --gateway 192.168.8.153 --port 502 --slave-id 1 \
    --only cell_request_charge_voltage \
    --write --confirm-writes
```

Then the full sweep on that one pack:

```
.venv/bin/python -m scripts.hw_testbench \
    --gateway 192.168.8.153 --port 502 --slave-id 1 \
    --write --confirm-writes \
    --report scripts/captures/hw_snapshots/BMS_1_report.json
```

## Step 4 — write test, whole bank

Only after step 3 is clean:

```
.venv/bin/python -m scripts.hw_testbench \
    --gateway 192.168.8.153 --port 502 --slave-ids 1,2,3,4,5,6 \
    --write --confirm-writes \
    --report scripts/captures/hw_snapshots/bank_report.json
```

The run stops at the first pack that aborts, so a problem on pack 2 never
touches packs 3-6.

## Reading the results

| Status | Meaning |
| --- | --- |
| `pass` | Written, verified, no collateral change, restored |
| `fail_no_change` | BMS ACKed the write but the words did not change |
| `fail_wrong_value` | Words changed to something other than what was sent (clamping or rounding) |
| `fail_side_effect` | Another word — or the packed-bit register — also changed |
| `fail_write` | BMS rejected the write or the bus dropped |
| `skipped` | Stored value sits outside the declared range, so no safe perturbation exists |

`fail_no_change` on **every** parameter means the address rule is still wrong.
`pass` on every parameter is the result that confirms the #32 fix.

Any failed **restore** aborts the whole run immediately and prints the snapshot
path. That is the one case needing manual action — see below.

## Firmware quirks found on PB2A16S20P 15.41

The first full BMS 1 sweep returned 30 `pass` and 5 `fail_write`, with the pack
fully restored. Both causes were firmware behaviour, not addressing faults, and
the bench now handles each.

**FC06 (write single register) is not supported.** All three packed bits were
rejected at `0x1114` with exception 2 (illegal data address) — while FC03 reads
that same register fine and returns `0x3200`, matching the app. Every one of the
30 successful writes used FC16. The bench now retries a single-register write as
a one-register FC16 write (`write_one_register`), on both the write and the
restore path. `write_executor._safe_write_register` carries the same fallback,
so the shipped bridge handles this firmware too.

**Some parameters already sit at their firmware ceiling.** `max_discharge_current`
(200.0 A) and `max_balance_current` (2.0 A) were rejected with exception 3
(illegal data value) when perturbed upward. The model designation `PB2A16S20P`
encodes a 2 A balancer, so 2.000 A is the hardware maximum. The bench now retries
once in the opposite direction on exception 3, and only reports `fail_write` if
both directions are refused.

Neither quirk is evidence against the byte-offset addressing rule: exception 3
means the address was accepted and the value refused, and the packed-bit
register is demonstrably readable at the address the bench wrote to.

**Both fixes confirmed on hardware.** A targeted re-test of the five failures on
BMS 1 returned 5/5 `pass`: the two currents wrote downward (200.0 → 199.999 A,
2.0 → 1.999 A) and all three packed bits wrote `via FC16`. The pack verified back
at baseline on a fresh connection. Together with the earlier runs this puts
BMS 1 at 35/35 parameters written, verified, and restored.

## Emergency restore

```
.venv/bin/python -m scripts.hw_testbench \
    --gateway 192.168.8.153 --port 502 \
    --restore-from scripts/captures/hw_snapshots/BMS_1_settings_<stamp>.json \
    --confirm-writes
```

Writes every setting from the snapshot back and verifies the pack matches it.

## Safety mechanisms

- Read-only is the default; writes need both `--write` and `--confirm-writes`.
- A snapshot is written to disk **before** the first write is attempted.
- The pack-current interlock refuses to write while current flows
  (`--force` overrides, only if you know the reading is wrong).
- Values are range-checked by the production encoder before reaching the bus.
- Restore is verified against the full snapshot window, not just the parameter
  that was touched.
- The bench never writes at the un-corrected address. That address lands on an
  unidentified register, so the bug is demonstrated arithmetically
  (`--collision-report`) rather than by corrupting an unknown setting.
