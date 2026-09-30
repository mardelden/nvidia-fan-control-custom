# Decision: the predictive cut is smoothed and bounded

**Status:** Accepted (operator, 2026-09-30). In the fork, not yet deployed.
**Amends:** the prediction in decisions/009.

## Context

In production on pve-ai (target 85, `adaptive:90`, total 700) under vLLM on GPU1, 02:17–02:27:

| Time | GPU1 limit | What happened |
|---|---|---|
| 02:23:11 | 405 → 202 | 83 °C, "+4 °C/reading, predicted 91 °C" → −202 W |
| 02:24:00–02:25:13 | 303 → 575 | fast recovery; the card had cooled to ~72 °C |
| 02:25:35 | 575 → 288 | 88 °C, "+4 °C/reading, predicted 96 °C" → −288 W |

After a cut the card cools, and the recovery overshoots the power it can sustain at 85 °C with
the fans at 90% (~450–500 W). The card then heats 4 °C per reading, and the prediction
extrapolated that **single** reading to 91–96 °C and sized the cut from it, halving the power.
That's far more than needed.

## Decision

- **Smoothed:** predict only after **two rising readings in a row**, using their **average**
  rise (≥ 2 °C per reading).
- **Only below the target:** once the card is measured at or over the target, the **measured**
  excess alone sizes the cut, so large cuts are for real overshoot.
- **Bounded:** a prediction adds at most `THERMAL_PREDICT_MAX_EXCESS_C` = +2 °C beyond the
  target: a cut of at most 30 W × 2² = 120 W.

The replay of the production case (76 → 80 → 84 °C toward 85) now gives one −120 W cut at
84 °C, where the deployed version gave −240 W at 80 °C and more after.

## Consequences

- The tuning-day cold start (+1, then +2 per reading) no longer predicts. The measured law
  cuts at the target (−30 W, climbing, no grace), so a hard ramp may overshoot by 1–2 °C more
  than on the tuning day. The 92 °C emergency is unchanged.
- **Not addressed here:** the recovery overshooting the sustainable power, walking back
  202 → 575 W in about 2 minutes. That was the other half of the cycle, and a candidate
  follow-up is to recover fast only up to the power level that last overheated the card.
