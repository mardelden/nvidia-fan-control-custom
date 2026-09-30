# Decision: adaptive unlearns its quiet trim from its own power cuts

**Status:** Accepted (operator, 2026-09-29). In the fork, not yet deployed.

## Context

This was seen on pve-ai in production (target 85, `adaptive:90`, total 700) under vLLM on
GPU1:

- At 83 °C adaptive had learned a −35.5% trim: base 93% → 58% fans. The card sat right at
  the target.
- The next burst took it to 88 °C at 90% fans → a −240 W cut. A minute later: 82 °C rising
  3 °C per reading → a predictive −232 W cut.
- After each recovery adaptive learned quiet again (0 → −35% in ~2–3 min), and the next
  burst was cut again.

The learned trim isn't new: the pre-plan-002 daemon had the same one, with the same numbers.
What changed is the context:

| | Before | Now |
|---|---|---|
| Power cut | target + 2 °C held 15 s | at the target (decisions/009) |
| Power per busy card | ceiling 300 W | up to 550–600 W (the total) |
| Fan max | 100% | 90% |

Together they removed the buffer between where adaptive parks the card and where power is
cut.

## Decision

Adaptive learns from its own power cuts:

- Each **thermal hold that starts** (a power cut for heat) raises a floor under the quiet trim
  by `TRIM_FLOOR_RAISE_PCT` = 20%: −50 → −30 → −10 → 0, where 0 means the fans follow the base
  curve.
- Every `TRIM_FLOOR_RELAX_S` = 10 min without a cut lowers it by `TRIM_FLOOR_RELAX_PCT` = 5%.
- The floor is saved with the trim (same target) and published as `adaptive_trim_floor_pct`.

## Alternatives Considered

| Alternative | Why not |
|---|---|
| A fan target below the power target (fans aim at 80–82 °C, cut at 85) | The operator chose unlearning only |
| No quiet trim within a few °C of the target | Louder, and less adaptive for steady loads |
| A fixed curve instead of adaptive | None has a 90% cap; no learning at all |
| Back to cutting at target + 2 for 15 s | Reverses the tuned law (decisions/009) and brings the overshoot back |

## Consequences

- A bursty workload converges within ~3 cuts to the base curve: ≈90% fans at 83 °C for an
  85 °C target, absorbing bursts before power is cut. A steady workload keeps its quiet trim.
- After a quiet stretch (e.g. overnight) the floor relaxes back to −50 in ~1.7 h, so the first
  bursts of the next heavy session can be cut again, once or twice, until it relearns.
- Tests: 238 + 23. Not hardware-tested (operator: prepare only).
