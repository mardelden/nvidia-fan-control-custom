# Plan: Live fan profile, mirror setting and temperature target

**Status:** Implemented in this repo and hardware-tested on pve-ai (2026-09-29). Awaiting the deploy team's review and deploy.
**Date:** 2026-09-29
**Driven by:** gpuguard ADRs 004–011 (`~/src/gpuguard/plans/decisions/`); Part D of proxmox
handover `gpuguard-infra-request-001`
**Revision:** rewritten three times on the same day as the operator settled the model.
Earlier drafts had a fan "ceiling defender", then retired the learning controller, then
aimed it at `ceiling − 2`. All are superseded by gpuguard ADRs 006–008.

## Context

Today every fan setting is a **startup flag**: `--mode`, `--mirror`, `--independent`,
`--temp-target`. Changing one means a playbook run and a restart. `--temp-target` runs a
*learning setpoint controller* that ignores `--mode` and `--mirror` entirely. pve-ai runs
`--mirror --temp-target 85`, so that controller is in charge.

The operator wants four independent live settings, each changed without a restart, each
surviving a reboot, and each set through gpuguard → `just` → a state file.

## The model

| Setting | Values | No file means |
|---|---|---|
| **Fan profile** | `native` \| `quiet` \| `aggressive` \| `performance` \| `max` \| `adaptive` \| `adaptive:<fan max %>` | `native` |
| **Mirror** | `on` \| `off` | `off` |
| **Temperature target** | °C 50..(emergency − 3) \| `none` | No target |
| **Power ceiling** | W \| W,W \| `none` | Hardware max (already built) |

All four are set by the operator through gpuguard. None of them affects the others except
as described below.

### The temperature target and power, for every profile

**Power is cut once the card is ≥ target + 2 °C for 5 s.** A 2 °C overshoot for up to 5 s
is accepted by design. The profiles differ only in whether the fans matter:

| Profile | Fans | Power is cut when |
|---|---|---|
| Fixed curves (`native`, `quiet`, `aggressive`, `performance`, `max`) | Follow the curve **exactly**. `native` is the card's factory curve, hands off | card ≥ target + 2 for 5 s, **whatever the fans are doing** |
| `adaptive` | The learning controller holds the card **at** the target, never above the **fan max** | fans **≥ the fan max** (default 100%) and card ≥ target + 2 for 5 s |

With the fixed curves the profile decides noise versus throughput: `native` is quiet, and
the target is held by losing GPU power; `max` is loud, and the GPU keeps its power. With no
target set, there's no temperature-driven power cut at all.

### `adaptive` (today's `--temp-target` controller, kept as a profile)

- **It requires a temperature target** and holds the card **at** it. In normal running the
  card sits at the target, not below it.
- The fans come from a feed-forward ramp (30% at `target − 20 °C` → 100% at the target) plus
  the **learned trim**, which finds the quietest fan speed that holds the target. The trim
  learns downward slowly within 8 °C below the target, holds at the target, unwinds 20×
  faster above it, and is clamped to −50..0 points.
- **Fan max** (ADR 009): part of the profile value, `adaptive` (100%) or `adaptive:50`. Range
  30..100; 30 is the controller's floor. The controller never commands above it, and power is
  cut when the fans are **≥ the fan max** and the card is ≥ target + 2 for 5 s. So `adaptive:50`
  means "hold the target, never louder than 50%, and give up GPU power first".
- The emergency fan override (at the emergency temperature, default 92 °C) still forces 100%,
  **above** the fan max.
- Changed from today: power is cut after **5 s** (was 15 s), and the fan threshold is the fan
  max (today it's hard-wired to 100%).

### Mirror (a setting, not a profile)

For cards mounted **back to back that share airflow**: both fans follow the hotter card.
Cards with separate airflow leave it off.

| Profile | mirror `on` | mirror `off` |
|---|---|---|
| `native` | The hotter card on its factory curve, the cooler card copies its speed (today's `--mirror`) | Factory curve on each card, fully hands off |
| fixed curve | Both cards follow the hotter card's temperature (today's default "sync") | Each card follows its own temperature (today's `--independent`) |
| `adaptive` | One thermal zone (the hottest card) | **v1: still one zone.** Per-card adaptive needs per-card trim and slew state; deferred |

**The default changes:** the fixed curves have always synced unless `--independent` was
given. With mirror defaulting to `off`, they become per-card. Back-to-back hosts (pve-ai)
are seeded with mirror `on`.

### Emergency cutoff (gpuguard ADR 010)

**Always armed**, for every profile and whatever else is set. It's separate from the target:

- **Trigger:** the hottest card is ≥ the emergency temperature (default **92 °C**) for **2 s**.
- **Action:** every GPU goes to the **hardware minimum** (150 W here, clamped by `min_w`),
  the same `clamp_all(min(min_w))` the UPS floor uses. Log a loud `⚠ EMERGENCY: …` line.
- **Fans:** `native` with mirror off is left alone (the factory curve handles it). Our
  profiles and mirror keep forcing the fans to 100% at the same threshold, which replaces the
  hard-wired `CRITICAL_TEMP`.
- **Release:** once the card is ≤ emergency − 30 °C (62 °C by default) for 30 s. Power comes back
  through the normal slow restore path, never as a jump. This is conservative by design: at
  150 W the hotter card may hover near 62 while a workload continues.
- **Set in inventory, not live:** a unit flag `--temp-emergency`, a safety limit like
  `--power-budget`. It's read-only in gpuguard.
- **The governor therefore always runs.** With no budget, no power ceiling and no target it
  holds nothing, but it stays armed for the emergency.

## Approach

### 1. Power derate (`PowerGovernor.observe_thermal`)

| | Fixed curves | `adaptive` | Today |
|---|---|---|---|
| Trigger | card ≥ target + 2 | fans ≥ **fan max** and card ≥ target + 2 | fans ≥ 100% and card ≥ target + 2 |
| Initial dwell | **5 s** | **5 s** | 15 s |
| Repeat dwell | 5 s | 5 s | 5 s |
| Step | `clamp(excess × 10 W, 20, 150)`, excess over target + 2 | same | same |
| Recovery | card ≤ target − 2 for 30 s | same | same |

- The trigger is today's condition with the fan requirement generalised. For the fixed curves
  it's dropped entirely; for adaptive it becomes the fan max (default 100, the same as today).
- **A temperature target on its own enables the governor**, the same way a power ceiling
  does, so a host with no UPS budget can still derate.
- The fresh-UPS-feedback gate after a thermal step stays.
- **The derate reaches the 150 W floor and the card is still over target + 2:** log a loud
  warning; nothing more can be done in software. gpuguard `status` shows it.

### 2. Files

**Operator settings.** These are written only through the deploy team's verbs. They're
picked up live, re-read on SIGHUP, re-applied at boot, and malformed input is ignored
(the previous value is kept). They have the same semantics as the existing `power-ceiling`.

| File | Journal ack |
|---|---|
| `/var/lib/nvidia-fan-control/fan-profile` | `FAN: profile set to …` (for example `adaptive:50` → `FAN: profile set to adaptive, fan max 50%`) |
| `/var/lib/nvidia-fan-control/fan-mirror` | `FAN: mirror on / off` |
| `/var/lib/nvidia-fan-control/temp-target` | `TEMP: target set to … / cleared` |
| `/var/lib/nvidia-fan-control/power-ceiling` | `POWER: ceiling set to … / cleared` (existing) |

**Daemon runtime state**, `/var/lib/nvidia-fan-control/runtime-state.json`. Only the daemon
writes it; operators and recipes never do. It's written every 60 s and on a clean exit,
atomically (tmp + rename). It holds only what a restart would otherwise lose. Everything
else is already read back from the hardware at start (the governor's `learned_cap_w` from
the current power limits, and the commanded fan speed from the current fan speed).

| Field | Restored when |
|---|---|
| `adaptive.trim_pct` + `adaptive.target_c` | The profile is still `adaptive` **and** the target is unchanged. A trim learned for a different temperature is discarded. It's restored across reboots too: it describes the chassis, and if it's too optimistic it unwinds fast |
| `emergency.active` + `emergency.since` | It was saved **< 5 min ago**, the same rule as the thermal hold. A restart during an emergency cutoff must not raise power on a card that's still hot |
| `thermal.limited` + `thermal.since` | It was saved **< 5 min ago**. A thermal hold describes the current load, so it's stale after a reboot or a long gap. Restoring it stops a restart during a derate from letting the UPS path raise power on a still-hot card |

### 3. Validation and precedence

- **`adaptive:<n>` with `n` outside 30..100:** refused as malformed; the previous profile is
  kept.
- **A target ≥ emergency − 2:** refused. The normal cut (target + 2) must come before the
  emergency.
- **`adaptive` with no temperature target:** the daemon refuses the profile
  (`FAN: ignoring adaptive — needs a temp target`) and keeps the previous profile.
- **The target is cleared while `adaptive` is active:** the daemon falls back to `native`
  and logs it loudly. Factory control is the safe side.
- gpuguard refuses both orderings **before** calling the recipe, so in normal use the daemon
  never sees them.

### 4. Live switching

- **Fixed curve → fixed curve:** the lookup changes on the next tick.
- **→ `native`:** every fan goes to factory (`NVML_FAN_POLICY_TEMPERATURE_CONTINOUS_SW`), and
  the daemon stops writing fan speeds (unless mirror is on, in which case the cooler card
  follows).
- **`native` → our curve or `adaptive`:** the fans go to manual, **starting from the speed
  they're running at now** (read back first), so there's no step down.
- **→ `adaptive`:** the trim comes from the runtime state if the target matches; otherwise it
  starts at 0.
- **Mirror on → off:** each card goes back to its own curve (with `native`, the cooler card
  returns to factory). **Off → on:** the cooler card starts following the hotter card.
- **An NVML error mid-switch:** log it, keep the previous setting, retry next tick. Never
  leave a card half-switched.

### 5. Where settings come from (gpuguard ADR 005, part 3)

- **The systemd unit passes safety limits, never operator settings.** It keeps
  `--power-budget`, `--power-floor-on`, `--power-fallback`, and adds `--temp-emergency`. It
  drops `--mode`, `--mirror`, `--independent`, `--temp-target` and `--power-ceiling`; the
  deploy team seeds those files from inventory with `force: false`. It adds
  `RuntimeDirectory=nvidia-fan-control` for the hand-run overrides (Part D).
- **The script keeps its flags for hand-run use**, with the same names: `--temp-target`
  (ADR 008 reversed the rename to ceiling, so no alias is needed), `--mirror` (now the
  mirror setting), and `--independent` (now an alias for mirror off). `--mode` gains
  `adaptive`.

### 6. Running the script by hand (gpuguard ADR 011)

It's never refused, never writes the saved settings, and resets on a service or machine
restart.

| Service | A hand-run with flags |
|---|---|
| Stopped | Drives the cards directly (foreground loop or `--once`) and writes no files. The next service start applies the saved settings |
| Running | Writes a **temporary override** to `/run/nvidia-fan-control/<same file names>`, SIGHUPs the service, waits for the journal ack, and exits. The service applies it **on top of** the saved settings. Only the service drives the cards |

- **Precedence:** override (`/run`) > saved (`/var/lib`) > default. Overrides are read with
  the same parser and ack lines, so they carry the same validation.
- **Reset:** `RuntimeDirectory=` removes `/run/nvidia-fan-control` when the service stops,
  and `/run` is tmpfs, so a reboot clears it as well. `--clear-override` removes it early.
- **Service detection:** `systemctl is-active nvidia-fan-control`, falling back to a PID file
  in the runtime directory for non-systemd use.
- **`--dry-run`** covers fans and power. It logs what would happen and touches nothing: no
  hardware, no override, no saved file.
- This **reverses** the August behaviour where a hand-run `--power-ceiling` wrote the saved
  `power-ceiling` file.

## Decisions (operator, 2026-09-29)

- **No file means not set.** The defaults are the "no file" column above.
- **One fan profile for the whole host; no per-GPU profiles** (operator: "the system works
  pretty well for any GPU"). A per-GPU value such as `quiet,aggressive` is refused as
  malformed in v1, which keeps the syntax free for later. If it's ever added: with mirror on,
  both fans run the louder of the two profiles' demands at the hotter card's temperature, and
  adaptive stays whole-host. The power ceiling stays per-GPU, as it already is.
- **When the daemon quits, the cards run on their factory curve.** This is existing
  behaviour on a clean stop: `restore_auto_control()`. The power ceiling stays in place (the
  `nvidia-smi -pl` semantics).
- **Restore the last state after a restart.** The settings files already survive; the
  runtime state file adds the learned trim and the thermal hold.
- **Unverified: a crash, not a clean stop.** Nobody has tested whether NVML returns manual
  fans to factory after the process dies. For the hardware test list: `kill -9` the daemon
  in a high-fan state and time what the fans do.

## pve-ai seed (proposed)

`adaptive` + mirror `on` + temperature target **85** + power ceiling **300** (the existing
file).

| | Today | After |
|---|---|---|
| Fan behaviour | Learning controller, one zone, holds 85 °C | Same |
| Power cut | fans 100% + card ≥ 87 °C for 15 s | fans ≥ 100% (default fan max) + card ≥ 87 °C for **5 s** |
| After a restart | The learned trim starts from 0 | The learned trim is **restored** |

That's the same behaviour, protecting a little sooner, and it remembers what it learned.

## Naming and the canonical description (operator, 2026-09-29)

The component is the **"GPU power governor"**, not a "UPS power governor". The old phrase,
and flags like `--power-budget 900 --ups cyberpower`, read as though we control the UPS.
We don't. Use this description verbatim, or close to it, in the README, the HANDOVER, the
`PowerGovernor` docstring and gpuguard's `describe`:

> **GPU power governor.** Keeps GPU power within the operator's limits (power ceiling,
> temperature target) and the host's safety limits (UPS budget, emergency temperature). It
> reads the UPS through NUT as a **read-only input**: it polls `upsc` every few seconds and
> never sends commands to the UPS or changes its settings. It does not shut the host down;
> that's NUT's `upsmon`. The only things it changes are **GPU power limits and fan speeds**.

The same applies to the systemd unit's `Description=`, which reads *"NVIDIA GPU fan control
+ UPS/thermal power governor"*. That's in the deploy team's template, so suggest the change
in the Part D handover and leave the wording to them.

**Deferred:** reading the UPS *status* on every fan tick (2 s) instead of every governor
interval (5 s), for faster reaction to "on battery". The pull model stays either way. Not in
v1 (operator: "fine for now").

## Files to modify

| File | Change |
|---|---|
| `~/src/proxmox/roles/proxmox_host/files/nvidia-fan-control.py` (golden, first) | Profiles incl. `adaptive`, the mirror setting, the two derate triggers, three new settings files, runtime state, live switching, flags + the `--independent` alias |
| `nvidia-fan-control.py` (the fork, a byte-copy after that) | Same, per ADR 007 |
| `tests/test_fan_policy.py` (new) | Fake-NVML: both derate triggers and their dwells; the 2 °C / 5 s tolerance (no cut at +1.9 °C, no cut before 5 s); the fan max (the command never exceeds it, the cut fires at it, the emergency temperature still forces 100%, out-of-range values refused); recovery; file semantics; the adaptive-requires-target refusal and fallback; runtime-state restore rules; switching (including the no-step-down rule); `native` + mirror `off` never writes a fan speed; the emergency cutoff (2 s trigger, 150 W, ≤ emergency − 30 for 30 s release, native fans untouched); override precedence and the hand-run paths (service stopped vs running; no saved-file writes); `--dry-run` touches nothing |
| README / HANDOVER | Rewrite the fan-control sections for this model |

The golden file lives in the deploy team's repo. We put the change there and **hand it over
for their review and deploy**. We don't deploy it ourselves.

## Open questions

- **Tuning on hardware:** the step size at the new 5 s dwell (3× more reactive than today's
  15 s; the UPS feedback gate may need a look), and the recovery band. The emergency release
  at emergency − 30 may, in practice, wait for the load to ease; watch how long it stays
  latched in the load test.
- **Resolved (2026-09-29):** the 92 °C override on `native` → the factory curve handles the
  fans, and the emergency *power* cutoff covers everyone (ADR 010). The hand-run fixes →
  temporary overrides, no saved-file writes, and a dry run that covers the fans (ADR 011).

## Outcome (2026-09-29)

**Implemented** in `nvidia-fan-control.py`. There are 116 fake-NVML assertions:
`tests/test_fan_policy.py` (96) and `tests/test_power_ceiling.py` (20, ported to the new API).

**Hardware test on pve-ai.** A hand-run with its own state and run dirs, so production state
was never touched. Production service stopped for about 35 minutes, with a systemd safety-net
timer. gpu-burn (deploy team, proxmox `134dc15`) in `gpu-test` provided the load.

| # | Scenario | Result |
|---|---|---|
| 1 | Settings loaded from the files, with ack lines | ✅ |
| 2 | Live profile / mirror switching; malformed input ignored; `native` hands off (policy read back from NVML) | ✅ |
| 3 | `native` + target 65 under load: cut at 68 °C for 6 s → 280/260/240 W; the card held at 68 °C; fans untouched (factory 30 → 40%) | ✅ |
| 4 | `adaptive:50` + target 65: fans never above 50%; cut at 67 °C with fans at 50% → 280 … 200 W; the S3 recovery walk visible (260 → 280 → 300) | ✅ |
| 5 | Emergency at a test threshold of 70 °C: 150 W within one sample; `native` fans untouched; a live switch to `max` during the hold; release at 40 °C for 30 s; walk 150 → 170 W | ✅ |
| 6 | A hand-run override while the daemon runs; a refused target rolled back; `--clear-override` | ✅ |
| 7 | A clean stop saves the runtime state and leaves the ceiling; the restart resumes every setting | ✅ |
| 8 | Crash (`kill -9`) with the fans at 100% | ❌ **the fans stay manual.** See `decisions/008`; fixed with `--reset-fans` as `ExecStopPost=` |

Observations:

- **At a 300 W ceiling, two cards at full load put total UPS load at ~870 W**, close to the
  900 W budget.
- **The thermal cut is a shared cap**, so both cards drop when the hottest one is over.
- **The emergency release (emergency − 30) is slow when it's close to idle temperature.** At the
  test threshold of 70 °C (release at 40 °C), the factory fans needed minutes; switching to `max`
  released it in about 90 s. At the real 92 °C (release at 62 °C) it's far easier.
- **The new script is a drop-in for the old unit.** Production's exact `ExecStart`
  (`--mirror --temp-target 85 …`) gives adaptive + mirror on + target 85, which is today's
  behaviour. So the deploy is safe in two steps: the daemon first, then the unit and the seed.
