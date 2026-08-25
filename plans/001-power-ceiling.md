# Plan: Configurable Max Power Ceiling (`--power-ceiling`)

**Status:** Implemented
**Date:** 2026-08-24

## Context

The governor's upper actuation bound is hardwired to the card's hardware maximum
(600 W on RTX PRO 6000), read once at `init()` from
`nvmlDeviceGetPowerManagementLimitConstraints`. Whenever the UPS has headroom the
restore branch targets `max(self.max_w)` — so a manual `nvidia-smi -pl 300` is
re-raised to 600 W on the next governor tick. The only way to pin power today is
to stop the daemon, which also surrenders fan management and the UPS safety loop.

Operator use case: benchmarking DeepSeek-V4 across configs. Dynamic power makes
perf comparisons noisy — a lower number could be the config under test, or just a
mid-run throttle. A fixed, documented wattage gives apples-to-apples runs while
the governor keeps protecting the 1000 W UPS and managing fans.

## Approach

Introduce a **ceiling** that replaces the hardware max as the governor's upper
actuation limit. The insight that keeps the change small: the existing control law
already clamps every write through a single per-GPU `max_w` bound, in three places —
`_set_limit()`, the per-GPU clamp in `update()`, and the restore target
`max(self.max_w)`. Shrinking `max_w` at init therefore bounds the *entire*
actuator surface, including the sensor-blind `--power-fallback` path (which
writes through `_set_limit`), with no new clamp logic.

Three things are genuinely new:

1. **Sensor-less operation.** Today `--power-budget` is the enable switch for the
   governor. A ceiling must work with no UPS at all, so governor construction is
   gated on `budget OR ceiling`, and `update()` gains a hold-only branch that skips
   `UpsReader` entirely when there is no budget.
2. **Persistence across restarts.** NVML power limits reset to the card default on
   reboot/driver reload, so persistence is achieved by re-applying at daemon start
   from a small state file — not by the hardware retaining anything.
3. **Live set/unset.** The same state file doubles as the control file, polled each
   `update()` (a `stat()` is cheap), plus `SIGHUP` for an immediate re-read.

## Files to Modify

| File | Change |
|------|--------|
| `nvidia-fan-control.py` | Ceiling constants, `parse_power_ceiling()`, `PowerGovernor` ceiling state + file I/O + hold-only mode, `restore_defaults()` honouring the ceiling, `--power-ceiling` / `--power-ceiling-file` flags, governor gating, `SIGHUP` handler |
| `nvidia-fan-control.service` | `StateDirectory=nvidia-fan-control`, `ExecReload=` SIGHUP. `--power-ceiling` deliberately left OUT of `ExecStart` so the state file stays the source of truth |
| `README.md` | Ceiling section, flag table rows, benchmark pin/unpin recipe |
| `HANDOVER.md` | Operator contract: precedence, state file path, restart semantics |

## Implementation Details

### 1. Ceiling representation — per-GPU list

`--power-ceiling 300` (common) or `--power-ceiling 300,400` (per-GPU). Stored as
`self.ceiling_w: Optional[List[float]]`. A single value broadcasts to all GPUs; a
list must match GPU count. `max_w` becomes `min(hw_max, ceiling)` per GPU.

The ceiling is clamped into `[min_w, hw_max]` at apply time and warned about if it
falls outside — a 100 W ceiling on a card with a 200 W hardware floor is an operator
error worth logging loudly, not a crash.

### 2. Sensor-less hold mode

When `budget_w is None`, `update()` re-reads the control file, then holds every GPU
at its ceiling via `_set_limit` (already idempotent — returns early when the applied
value is within 1 W) and returns. No `upsc` call, no fallback logic, no floor flags.
Logging is on-change only; a steady hold must not spam the journal every tick.

### 3. Bounded restore in budget mode

No change to the control law. `target = max(self.max_w)` now resolves to the ceiling,
and the deadband "snap to max" escape at the restore branch snaps to the ceiling.
Throttling below the ceiling under UPS pressure is unchanged. If the ceiling sits at
or under the UPS-safe budget, the loop never finds a reason to throttle → constant
power, which is the desired benchmarking state.

### 4. State/control file

Default `/var/lib/nvidia-fan-control/power-ceiling`. Plain text: a number, a
comma-separated per-GPU list, or `none`/`off`/`0`/empty to unpin. Polled by `stat()`
on mtime+size each `update()`; `SIGHUP` forces a re-read.

**Precedence:** an explicit `--power-ceiling` on the command line wins at startup and
is written to the file; otherwise the file supplies the value. See
`plans/decisions/001-power-ceiling-precedence.md` for why config-beats-runtime was
chosen and what it costs.

### 5. Shutdown

`restore_defaults()` restores to the ceiling when one is active, not to the card
default. This is the `nvidia-smi -pl` semantic the operator asked for: the cap
outlives the process, so stopping the service does not silently un-pin a
benchmark. With no ceiling, behaviour is unchanged.

## Edge Cases

- **Ceiling below hardware floor** — clamped to `min_w`, warned once.
- **Ceiling above hardware max** — clamped to `hw_max`, warned once (a 700 W ceiling
  on a 600 W card is a no-op, not an error).
- **Per-GPU list length mismatch** — argparse-time error for the flag; for the control
  file, log and ignore the bad write, keeping the previous ceiling (a benchmark script
  typo must not un-pin the cards).
- **Unreadable/unwritable state dir** — warn and continue in memory. Persistence is a
  convenience; losing it must not take down fan control.
- **`--power-fallback` above the ceiling** — clamped by `_set_limit` automatically;
  also reported at init so the log shows the effective value.
- **`--power-dry-run`** — covers ceiling writes too; the file is still read but not
  written.
- **Ceiling cleared while throttled below it** — the normal restore path walks the
  limit back up under UPS supervision. No special case.

## Open Questions

- None blocking. Precedence resolved in ADR 001.

## Fork sync

After deployment the fork was re-synced from the proxmox golden file so the two match
byte-for-byte — see `plans/decisions/007-sync-fork-from-proxmox-golden-file.md`. This fork
now carries the thermal governor and learned-cap control law it had been missing, plus
`tests/test_power_ceiling.py`.

## Outcome (deployment)

**The real deployment target was NOT this fork.** See
`plans/decisions/005-lesson-golden-file-is-source-of-truth.md`. The feature was ported onto
`~/src/proxmox/roles/proxmox_host/files/nvidia-fan-control.py` (~345 lines ahead of this
fork: thermal governor, `learned_cap_w`, idle-card detection) and deployed with
`just hosts --limit pve-ai --tags gpu-fan`.

Port-specific adaptations beyond the plan:

| Concern | Handling |
|---|---|
| `learned_cap_w` seeded from `min(applied_w)` | Ceiling applied *before* the seed, and clamped into it on every change, so the learned cap can never sit above the ceiling |
| Idle reset writes `max_w[i]` directly | Bounded for free by the shrink; verified live (idle reset lands on the ceiling, not 600 W) |
| Thermal derate vs. ceiling hold | `_hold_ceiling()` defers to `learned_cap_w` while `_thermal_limited`, or the two loops oscillate against each other |
| Thermal derating previously required `--power-budget` | A ceiling now also enables the governor, so hosts with no UPS get thermal derating too |
| Out-of-band `nvidia-smi -pl` | `_enforce_ceiling()` — see `plans/decisions/006-lesson-applied-w-cache-blind-to-external-writes.md` |

Live-verified on pve-ai 2026-08-24 (idle GPUs, no workload): pin, hold across ticks,
idle-reset respecting the cap, out-of-band correction, per-GPU pin, supervised raise,
SIGHUP/`systemctl reload`, malformed-input rejection, restart persistence, pinned-on-stop,
and unpin. Box returned to its baseline (600 W, ceiling `none`, 0 restarts, no errors).

## Outcome

Implemented 2026-08-24. Verified against a fake-NVML harness (45 assertions) plus live
daemon runs covering pin, per-GPU pin, live repin, SIGHUP, unpin, and SIGTERM-with-cap.

One gap surfaced during testing that the plan had missed — see
`plans/decisions/004-lesson-unpin-needs-explicit-release.md`. Shrinking `max_w` bounds
everything downward but nothing in ceiling-only mode ever raises a limit, so unpinning
left the cards stranded at the old cap. Fixed with `_release_to_default()`.

Not implemented (deliberately, out of scope): no change to the fan curves, no change to
the control law itself, and `--power-budget` semantics are untouched for hosts that
never set a ceiling.
