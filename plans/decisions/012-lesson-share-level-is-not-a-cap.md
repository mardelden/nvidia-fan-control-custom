# Lesson: a card's share of the total is not a limit of its own

**Date:** 2026-09-29
**Area:** power governor (plan 003)

## What We Were Trying to Do

Split a total GPU ceiling between the cards, while keeping the UPS budget and thermal cuts
working per card.

## What We Tried

| Approach | Result | Why it failed/worked |
|---|---|---|
| Shared cap, re-derived as `min(applied)` | Failed (earlier) | An idle card at 150 W drags every card's cap to 150 W |
| Shared cap as `max(applied)` | Failed (earlier, reverted) | Cuts and walks then *raised* the lower card (300 → 580 W) |
| Per-card caps, every cut from `min(applied, cap)` | Failed on pve-ai | An idle card's level IS its share (150 W). A cut saved 150 W as its cap, so on waking it crawled +20 W / 30 s |
| ...with the idle card's cut taken from its cap | Failed on pve-ai, in one case | A card that started working in the same 2 s tick was still flagged idle, so its real 300 W draw wasn't cut |
| **...plus busy flags re-read at the moment of a cut** | **Worked** | Replay tests for both cases fail without the fix and pass with it |

## Root Cause

A card's limit can be held down by two different things: a **constraint** (UPS trim,
thermal cut, hold, ceiling) or its **share** of the total. Only a constraint belongs in the
cap. Any code that derives a cap from the applied limit, or cuts from it, mixes the two
whenever the share is what binds.

## Solution

- Caps are set explicitly by each operation, never derived from applied limits.
- A cut starts from the card's level, except for an idle card held down only by its share:
  its cap is cut instead (`_cut_base`).
- Busy status is refreshed from NVML right before a cut (`_refresh_busy`), and in the UPS
  step from the reading it just took.

## How to Avoid in Future

- For every new limit source, decide: **constraint** (goes in the cap) or **bound** (goes in
  `max_w`)? Never let a bound leak into the cap.
- Test the *wake-up* after every kind of cut, not just the cut itself. On pve-ai the cut
  looked right, and the damage only showed when the idle card woke.
