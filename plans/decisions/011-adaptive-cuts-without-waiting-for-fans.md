# Decision: adaptive cuts power without waiting for the fans to spin up

**Status:** Accepted
**Date:** 2026-09-29
**Context:** a live test on pve-ai of the limit cycle the deploy team modelled (decisions/010, #1)

## What the test showed

The test ran gpu-burn on GPU1 for 5 minutes with `adaptive` (fan max 100), mirror on, target
89 °C, ceiling 600 W and the production safety limits (UPS budget 900 W, emergency 92 °C).

- **No limit cycle.** Power settled at 510–530 W and the card at 87–89 °C. The UPS budget did
  most of the limiting (600 → 580, 520 → 510, then a +20 W restore to 530). The thermal law cut
  once (580 → 520) and released 30 s later.
- **The fans and the cut act at the same moment.** Adaptive had learned to hold 89 °C at only
  ~76–86% fan (a −14 to −24% trim). At 90 °C it dropped the trim and commanded 100%. Two
  seconds later the governor cut 60 W, "at 100% fan" by the command, while nvidia-smi still
  measured 93%. Since the cut moved to the target itself (decisions/009), adaptive's setpoint
  and the cut point are the same temperature.

## Decision (operator, 2026-09-29)

**Keep it.** "We shouldn't wait for fans." A card over the target loses power at once, even if
its fans are still spinning up.

**Rejected:** requiring the fans to have been at their max for ~15 s before an adaptive cut.
Proposed after this test; the operator preferred the immediate cut.

## Consequences

- Under adaptive, a 1 °C blip over the target costs a small power cut (30 W × 2^excess) as well
  as the fans. It's released at target − 2 °C for 30 s.
- The deploy team's #1 limit cycle wasn't reproduced with free fans and a binding UPS budget.
  The case it describes, fans with no headroom (`native` or `adaptive:30`) and a large gap
  between the ceiling and the power the card can sustain, hasn't had a long run yet. It stays
  parked.
- The logs are in the session scratchpad (`pve-ai-oscillation-test-2026-09-29.{log,watch}`).
