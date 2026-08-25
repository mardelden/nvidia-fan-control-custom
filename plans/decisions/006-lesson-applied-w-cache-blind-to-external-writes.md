# Lesson: `applied_w` is a write cache, so a cap is not a cap without reconciliation

**Date:** 2026-08-24
**Area:** power-governor

## What We Were Trying to Do

Guarantee that `--power-ceiling 300` means no GPU ever sits above 300 W.

## What We Tried

| Approach | Result | Why it failed/worked |
|----------|--------|---------------------|
| Shrink `max_w` so every write clamps to the ceiling | **Insufficient alone** | It bounds writes *this daemon makes*. `nvidia-smi -pl 600` from another shell raised GPU0 to 600 W and it stayed there |
| Re-read the hardware limit each governor tick and correct upward violations (`_enforce_ceiling`) | **Worked** | Verified live on pve-ai: an external `nvidia-smi -pl 550` was pulled back to 300 W within one 5 s tick |

## Root Cause

`_set_limit()` early-returns when `abs(watts - self.applied_w[idx]) < 1.0`. `applied_w`
is a cache of what *this process last wrote*, not a reading of the device. Anything that
changes the limit out of band leaves the cache stale, and the early-return then makes the
daemon actively refuse to correct it — the cache says "already there".

This was invisible in unit tests because the fake NVML test set `g.applied_w[0]` by hand
to simulate drift, which is exactly the state the real bug prevents. Only the live
hardware test surfaced it.

## Solution

`_enforce_ceiling()` runs each governor tick when a ceiling is active: read
`nvmlDeviceGetPowerManagementLimit`, and if it exceeds `max_w[i]`, resync `applied_w[i]`
to the true value and re-apply the cap. Only *upward* violations are corrected — external
downward moves are left to the normal control law.

## How to Avoid in Future

- A write-through cache in front of a device makes "we already set it" a claim about the
  process, not the world. Any invariant that must hold *against other writers* needs a
  read-back, not just a guarded write.
- When simulating drift in tests, mutate the **device** (`fakenvml.DEVS[i].limit`), never
  the daemon's cache — mutating the cache tests the wrong thing and hides this class of bug.
- Live-test any "guarantee" feature against a real out-of-band mutation before calling it done.
