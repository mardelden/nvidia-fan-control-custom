# Decision: cool-first adaptive is a fan target below the power target

**Status:** Accepted (design call by gpuguard; the owner asked for the mode, 2026-11-07)
**Date:** 2026-11-07

## Context

The owner asked, through the deploy team, for a **cool-first** adaptive mode as a toggle beside
today's adaptive:

- below the target, the fans push the temperature as low as they can, up to the fan cap,
  instead of settling at the target;
- power is cut at the target exactly as today.

The deploy team offered two shapes and left the call to us:

- **two thresholds:** a fan setpoint the fans hold using up to the cap, plus the existing power
  target;
- **a mode flag** (e.g. `adaptive-cool:FANMAX`) that drops the quiet trim.

The first settings the owner wants: power target 85 °C, fan cap 90%, cool-first.

Today's adaptive is **quiet-first**. One target drives both actuators: the ramp (30% at
target − 20 → 100% at the target) plus a learned quiet trim parks the card near the target with
the least fan. Power is cut once the fans are at the cap and the card still reaches the target.

## Decision

**Two thresholds: a new live setting `fan-target`** (°C or `none`, file
`/var/lib/nvidia-fan-control/fan-target`, flag `--fan-target` for hand-runs).

- **The aim:** adaptive's ramp and quiet trim aim at `min(fan-target, temp-target)`.
- **Above the aim:** the ramp is at 100%, so the fans run at the fan max and the trim unwinds.
- **Below the aim:** the trim still learns quiet (within 8 °C), so an idle or lightly loaded card
  stays quiet. It's still adaptive.
- **Power: nothing changes.** It's cut at `temp-target` with the same law: the grace, the
  prediction, the measured cut, recovery, and the fan-max gate. The fan-max gate is already met
  in cool-first, because the fans are at the cap well before the cut point.
- **`none` (the default) = aim at `temp-target`,** which is exactly today's quiet-first adaptive.
  No existing test changed.
- **The bounds:**
  - the minimum is 50 °C, like the target;
  - a value above emergency − 3 is refused;
  - a value at or above `temp-target` clamps to it, so a night schedule that lowers the target
    pulls the aim down with it.
- **The learned trim is saved with the aim** (`adaptive.target_c` = the aim). A trim learned
  for another aim isn't restored.
- **`effective.json`** gains `in_force.fan_target` and `adaptive_fan_aim_c`.

## Alternatives Considered

| Alternative | Why rejected |
|---|---|
| A mode flag `adaptive-cool:FANMAX` that drops the quiet trim | Without a lower aim, the plain ramp still settles a loaded card close to the target (82% fans at 80 °C under an 85 °C target), which isn't "as cool as they can". It would need a hidden aim constant anyway. A new profile name also changes what the schedule, the verbs and readers of `in_force.profile` see. |
| Cool-first = fans pinned at the cap whenever a card is busy | That's a fixed curve; the owner asked for adaptive. |
| Decisions/014's alternative, "a fan target below the power target" (rejected 2026-09-29) | The same mechanism. The operator chose unlearning then to fix vLLM bursts under quiet-first. That stays the default, and the fan target is opt-in. |

## Consequences

- **The deploy team needs a verb,** `just gpu-fan-target-set <host> <C|none>`, and the
  thermal schedule can write `fan-target` like the profile and target files.
- **The owner's first settings:** `temp-target 85`, `fan-profile adaptive:90`, and a
  `fan-target` of their choice. **70 °C** is our suggested start: the fans run at 90% under real
  load and are quiet at idle. Lower is cooler and louder at moderate load.
- **Unlearning** still runs; with cool-first, cuts are rarer, so its floor matters less.
- **Tests:** 294 + 23. The new cool-first tests cover:
  - the parser and the CLI flag;
  - 80 °C at 90% (cool-first) vs below the cap (quiet-first);
  - no cut below the power target, and a cut at it;
  - quiet below the aim;
  - the trim keyed by the aim;
  - live changes;
  - the refusal above emergency − 3;
  - the clamp to the target.
