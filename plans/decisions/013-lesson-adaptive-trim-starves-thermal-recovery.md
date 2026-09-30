# Lesson: adaptive's quiet trim starves the thermal recovery

**Date:** 2026-09-29
**Area:** fan control × power governor

## What We Were Trying to Do

Hold a 60 °C target under gpu-burn with `adaptive` (fan max 100) and get power back once the
card cools.

## What We Tried

| Approach | Result | Why it failed/worked |
|---|---|---|
| Adaptive learns its quiet trim at all times | Failed | During a hold the card sat just under the target, so adaptive trimmed the fans to ~75% and held it there. The hold releases only at target − 2 for 30 s, so power stayed at 150–235 W with 300+ W of budget unused |
| Pin the fans at max during the hold and the walk back | Worked, but rejected | The hold released, but the fans stayed at 100% long after it (the walk can take minutes), including after the load stopped |
| **Fans follow the base curve (no trim) while power is held; adaptive releases below the target for 10 s; the walk pauses at the target and while climbing** | **Worked** | The fans track temperature only, and power comes back while there's room. The card settles at the most power it can hold at the target |

## Root Cause

Two controllers shared one setpoint and each assumed the other would move first. The fan
trim holds the card *at* the target; the power hold waited for it to go *below* it.

## How to Avoid in Future

- When two loops act on the same temperature, give them an explicit order in both
  directions: fans first on the way up, power first on the way back.
- Any rule that pins an actuator needs an end condition tied to the need, not to a slow
  state such as "recovery walk complete".
