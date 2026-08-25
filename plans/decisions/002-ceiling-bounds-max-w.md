# Decision: Implement the ceiling by shrinking `max_w`, not by adding a clamp

**Status:** Accepted
**Date:** 2026-08-24

## Context

`--power-ceiling` must bound every path that writes a GPU power limit: the reactive
restore branch, the per-GPU clamp in the control law, the deadband "snap to max"
escape, the sensor-blind `--power-fallback`, and the UPS floor-flag clamp. Missing
any one of them means the cards silently exceed the operator's cap in some corner —
exactly the class of bug this feature exists to prevent.

## Decision

Apply the ceiling **once, at `init()`, by shrinking the per-GPU `max_w`** to
`min(hw_max, ceiling)`. Do not add ceiling checks at the call sites.

Every write already funnels through `_set_limit()`, which clamps to
`[min_w[idx], max_w[idx]]`, and the control law's own clamp uses the same bound. So
one assignment bounds the whole actuator surface, and the restore target
`max(self.max_w)` becomes the ceiling for free.

## Alternatives Considered

| Alternative | Pros | Cons | Why rejected |
|-------------|------|------|--------------|
| **Explicit `min(target, ceiling)` at each decision site** | Reads literally; ceiling is visible where power is decided | Five call sites today, and any future write path silently escapes the cap. The `--power-fallback` and floor-flag paths would each need their own clamp | Correctness depends on remembering to clamp; the invariant should be structural, not repeated |
| **A `ceiling` property wrapping `max_w` reads** | Keeps hardware max available for logging | Same number of touch points, plus indirection | No benefit over just storing both `hw_max_w` and a shrunk `max_w` |

## Consequences

- The hardware maximum is preserved separately as `hw_max_w` so logs can report
  "600 W hardware / 300 W ceiling" and so clearing the ceiling can restore `max_w`.
- `--power-fallback` is bounded by the ceiling automatically, satisfying the
  requirement with no dedicated code.
- Any future code that writes a power limit must go through `_set_limit()` to inherit
  the cap. This is now an invariant of `PowerGovernor`, noted in its docstring.
