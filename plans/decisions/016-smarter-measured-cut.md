# Decision: a smarter measured cut (averaged, 25% at most, a proven-level floor)

**Status:** Accepted (operator, 2026-09-30). In the fork, not yet deployed.
**Amends:** the cut sizes in decisions/009.

## Context

Production on pve-ai (target 85, `adaptive:90`, total 700) under vLLM, 02:50:33: GPU1 was
steady at 83–84 °C at 560 W, and one burst read **90 °C**. The next reading was 80. The
measured law sized the cut from that one reading: +5 °C → 30 W × 2⁵ = 960 W, capped at 50% →
**560 → 280 W**.

GPU1 had held 420 W below the target just before (02:49:25–:55). It was walked up to 560 in
a vLLM lull. Request 007 (decisions/015) had bounded only the *predictive* cut; the measured
path was left large on purpose ("keep the large cuts for measured overshoot"). The operator
then asked for it to be smarter: "let's not half".

## Decision

For a **measured** cut:

1. **The size comes from the average of the last two readings** (the trigger is still the
   latest reading). A one-reading spike counts half: 84 → 90 is sized as +2 (−120 W).
2. **At most `THERMAL_CUT_MAX_FRACTION` = 25% of a card's power per step** (was 50%). A card
   still over is cut again after the 5 s repeat dwell.
3. **No lower than the card's last proven level in one step.** That's the most recent limit
   it held below the target for `THERMAL_PROVEN_DWELL_S` = 20 s while drawing at least
   `THERMAL_PROVEN_DRAW_FRACTION` = 90% of it (a light load proves nothing). It applies only
   when the card overheats *above* that level, and it's forgotten on the per-card idle reset.

The predictive cut (decisions/015, ≤ 120 W), recovery (quick, by the operator's choice) and
the 92 °C emergency are unchanged.

## Alternatives Considered

| Alternative | Why not |
|---|---|
| Keep 50% on measured overshoot | That's the 560 → 280 W case |
| Cut straight back to the proven level | A proven level from long ago could be far below: another way to halve |
| A recovery cap instead | The operator prefers quick recovery |

## Consequences

- The 02:50:33 replay goes 560 → 440 W (the deployed version: 280).
- A real, fast runaway gets smaller first cuts, 25% at most and averaged, so it's more likely
  to end in the 92 °C emergency than be caught by one big cut. The operator accepted the
  same trade-off for prediction.
- Tests: 256 + 23. Not hardware-tested.
