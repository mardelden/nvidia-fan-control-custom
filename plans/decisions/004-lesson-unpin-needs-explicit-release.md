# Lesson: A bound that only clamps downward cannot undo itself

**Date:** 2026-08-24
**Area:** power-governor

## What We Were Trying to Do

Add `--power-ceiling` as an upper actuation bound that also works with no UPS sensor,
and make unpinning (`--power-ceiling none`, or `echo none > <ceiling-file>`) hand the
cards back to their normal limit.

## What We Tried

| Approach | Result | Why it failed/worked |
|----------|--------|---------------------|
| Shrink per-GPU `max_w` to the ceiling; let `_set_limit()`'s existing clamp do the rest | **Worked for pinning** | Every write already clamps to `max_w`, so one assignment bounded the restore target, `--power-fallback`, and the floor-flag clamp at once — no new clamp logic |
| Assume the same mechanism undoes a pin — clear the ceiling, restore `max_w = hw_max` | **Failed** | Raising `max_w` only *permits* a higher limit; it does not *request* one. In ceiling-only mode nothing ever raises a limit, so the cards stayed parked at the old cap indefinitely |
| Add `_release_to_default()`: on unpin with no budget, actively write each GPU's default limit | **Worked** | Makes the unpin an explicit actuation instead of relying on a control loop that isn't running |

## Root Cause

The ceiling is a **bound**, but "unpin" is an **action**. In budget mode the two are
easy to confuse because the reactive restore branch (`target = max(self.max_w)`) walks
limits back up on its own, so clearing the bound looks like it undoes the pin. That
restore branch only exists inside the UPS control loop. With `budget_w=None` there is
no loop, so relaxing the bound is a no-op at the hardware level.

Caught only because the end-to-end CLI test asserted on the *hardware* state after an
unpin rather than on `max_w`. Asserting on the internal bound alone would have passed.

## Solution

`_apply_ceiling(None, source)` calls `_release_to_default(source)` when
`self.budget_w is None`, writing `default_w[i]` to each GPU. `init()` does the same when
it starts with no ceiling and no budget (the `--power-ceiling none` unpin invocation).
In budget mode the release is deliberately skipped so the UPS still supervises the way
back up.

## How to Avoid in Future

- When adding a constraint that can be **removed**, write the removal path first and
  ask "what actively moves the system back?" A permissive bound moves nothing.
- Test actuator state, not controller state. Assert on the value written to the device
  (here: `nvmlDeviceSetPowerManagementLimit` calls), not on the internal limit variable.
- In this daemon specifically: `max_w` is a ceiling on writes, never a target. The only
  things that raise a limit are the budget-mode restore branch, `_hold_ceiling()`, and
  `_release_to_default()`. Any new mode needs one of those or it can only ratchet down.
