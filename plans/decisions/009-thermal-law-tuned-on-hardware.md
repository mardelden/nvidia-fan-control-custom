# Decision: The thermal power law, as tuned live on pve-ai

**Status:** Accepted
**Date:** 2026-09-29
**Supersedes:** the cut rules in plan 002 and gpuguard ADR 008 (cut at target + 2 °C, 5 s,
linear 10 W/°C steps)

## Context

The operator watched gpu-burn at 600 W on GPU1 (the hotter card, the one that takes GPU0's
exhaust) and tuned the law between runs. The first version let the card overshoot badly:
78 °C against a 72 °C cut point, while 10 W-per-degree steps took a minute to catch up.

## Decision (operator, 2026-09-29)

| Rule | Value |
|---|---|
| **Cut point** | **The target itself** (no +2 °C allowance) |
| **Cut size** | **30 W × 2^(°C over target)**, at most **50% of current power**: −30, −60, −120, −240, then −50% |
| **Grace** | 5 s, but **only while the temperature is steady.** A card that is warmer than it was two readings ago is cut without waiting. At target + 4 °C or more, never |
| **Prediction** | At the onset, if the card rose **≥ 2 °C since the last reading** and is within 10 °C below the target: `predicted = now + rise × 2 readings`. Cut when the prediction reaches the target, sized by the predicted overshoot. Measured **per reading**, not per second: readings are ~2.05 s apart, so a 1.0 °C/s threshold rejected a real +2 °C-per-reading climb |
| **Hold while falling** | After a cut, no further cut while the card is cooler than it was one repeat dwell (5 s) ago. Once it stops falling and is still at or over the target, cut again. (Comparing with the temperature *at* the cut let a card plateau over the target forever) |
| **Release** | At **target − 2 °C for 30 s** (a 2 °C band, so the power doesn't flip back and forth) |
| **Recovery** | **+20 W × 2^(°C below target − 2)** per 30 s, at most +50% of current power. With a UPS budget, also no more than the measured headroom. After an **emergency or blind** hold, a conservative +20 W |
| **Emergency** (unchanged) | 92 °C for 2 s → every GPU to its minimum. **`native` fans are not touched: power only** (operator, reaffirmed after the deploy team's review) |
| **Maximum target** | Emergency − 3 (89 °C), kept fixed even though the cut point moved |

## Evidence (GPU1, `adaptive:30`, target 75 °C, 600 W start)

| Version | Cold-start peak | Hot-start peak |
|---|---|---|
| Cut at target + 2, linear steps (targets 70/75) | +5 to +8 °C over | — |
| Cut at target, 30 W exponential, 5 s grace | 78 (+3) | 82 (+7) |
| + prediction per second | 78 (+3); didn't fire | 77 (+2) |
| **+ prediction per reading, grace only when steady** | **75 (0)** | **75 (0)** |

With the final law, the prediction fired at 66 °C (cold) and 69 °C (hot), 6–9 °C before the
target. Afterwards the card still warmed up to the target and needed small −30 W trims, so the
early cut wasn't bigger than necessary. Recovery after a burn: from the hold being released to
full power in ~60 s, against ~6 min with linear +20 W steps.

## Consequences

- **With the fans pinned at their 30% minimum,** GPU1 sustains about 240–270 W at 75 °C under
  gpu-burn. That's the useful number for anyone choosing a quiet profile.
- **The 2 s poll interval is now the remaining limit.** A hot card at 600 W / 30% fan climbs
  ~6 °C per reading, so the earliest possible reaction is one reading. A 1 s poll would halve
  that; it hasn't been done (it doubles NVML reads and log volume).
- **Known bug, not fixed here: per-GPU ceilings that differ (e.g. `600,300`).** The shared cap
  is applied to every card, so the first cut would drop the higher card to the lower card's
  level. Every production host uses equal ceilings; don't use unequal ones until this is fixed
  with a per-card cap.
