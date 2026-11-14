# Decision: a host request file caps all GPUs together, for heatguard

**Status:** Accepted (the owner ruled the input in Arcwright `heatguard/7`; the shape is gpuguard's call)
**Date:** 2026-11-14

## Context

heatguard is the new host-heat tool: a live view, events to SigNoz, and a guard that boosts the
board fans and caps the CPU. On pve-ai, GPU heat drives the board zone (SYSTIN; proxmox
ADR 066). When SYSTIN stays at its limit (50 °C) with the board fans at 100%, heatguard needs
less GPU power.

It must never set a power limit itself, and it must not share the operator's override files.
Its proposal: write `/run/nvidia-fan-control/host-request` with
`{"ceiling_w", "reason", "from"}`. The governor applies the lowest of its bounds with its
normal slew, acks it in `effective.json`, and ignores a stale request.

## Decision

- **The file:** `/run/nvidia-fan-control/host-request`, JSON,
  `{"total_w": W, "reason": str, "from": str}`. `total_w` is required, and the extra keys are
  ignored.
- **A total, not a per-card ceiling.** The board zone cares about total GPU heat. The soft total
  already moves a total to the busy cards, and "ceiling" means per card in this daemon.
- **The total in force** = min(the operator's total, the request's cap). The operator's total
  is kept separately (`operator_total_w`). Its acks, `in_force.total` and the remembered total
  are unchanged, and a change to it is acked even while the request binds.
- **Freshness:** the request counts while the file's mtime is ≤ 120 s old, so heatguard rewrites
  it at least every minute. A stale request is ignored, so a dead requester can't hold power
  down.
- **The slew:**
  - lower = at once;
  - higher or lifted = the cap walks up +50 W every 30 s after one 30-s dwell, until it reaches
    the request or, once lifted, the operator's total (or the ceilings' sum); then it's gone.
- **Edge cases:**
  - empty or absent = no request;
  - malformed = logged, ignored, `status: malformed`;
  - below the cards' minimums = held at the minimums.
- **The acks:**
  - `effective.json` `host_request` {`status` applied | stale | malformed | lifted, `total_w`,
    `from`, `reason`, `age_s`, `in_force_w`} and `total_in_force_w`;
  - the journal: `POWER: host request from … : all GPUs together at most N W (reason)`,
    `… raised to N W` / `lifted`, `… is Ns old … -> ignored`.
- **Not touched:** the per-card ceilings, the fans, the settings files, and `--clear-override`.
  heatguard never asks for more GPU fan (it would dump heat into the board, ADR 066).

## Alternatives Considered

| Alternative | Why rejected |
|---|---|
| A per-card ceiling (`ceiling_w`, the proposal) | The water-filled total already decides which card gives power back; a per-card cap from outside would fight it. A per-card form can be added later. |
| heatguard writes the operator's `power-ceiling-total` or an override | The ruling: never share the operator's files. The operator's setting would also be lost. |
| An `expires_at` field instead of the mtime | The mtime needs no clock agreement and no field to forget; rewriting is the heartbeat. |
| No raise slew (heatguard ramps its own requests) | A stale or removed request would jump straight back to full power and refill the board with heat. |

## Consequences

- **No unit, flag or setting change.** The deploy team ships the golden file as usual. heatguard
  needs write access to `/run/nvidia-fan-control` (root).
- **Tests:** 323 + 23. The new tests cover:
  - applied at once and acked;
  - the operator's total kept;
  - raised: the walk up;
  - lowered again at once;
  - an operator change while the request binds;
  - stale → ignored → walks back;
  - malformed ×4;
  - empty;
  - below the minimum;
  - `--clear-override`;
  - no operator total.
