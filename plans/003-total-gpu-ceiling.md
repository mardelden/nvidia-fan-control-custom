# Plan: A total GPU power ceiling, split between the cards (per-card caps)

**Status:** Implemented (fork; hardware-tested on pve-ai 2026-09-29, not yet deployed; see addenda)
**Date:** 2026-09-29

## Context

The operator wants **one busy card to run at 600 W while the other idles**. The per-GPU ceiling
can't do that safely:

- **The UPS budget reacts after the fact.** Its sensor lags (≥ 5 s, a 10 W resolution, once
  stuck for 36 s), and a GPU goes from idle to full power in under a second. With 600/600, two
  cards waking together draw 2×600 W + 190–480 W of host on a 1,000 W UPS for 5–40 s before
  the budget cuts. On 2026-08-24 a breaker tripped exactly that way.
  See memory note *GPU governor UPS lag is structural*.
- A ceiling is sensor-independent: it bounds what the cards *can* draw, so it holds however
  fast the load arrives. Today's 300/300 is that bound, but it also caps a lone busy card at
  300 W.

**A total ceiling keeps the bound while moving it between the cards.** The sum of the power
limits never exceeds the total, and the governor shifts the total to whichever card is busy,
reading each card's draw and utilization from NVML every 2 s, with no lag.

Operator decisions (2026-09-29): **750 W** on pve-ai, and a **live setting** like the ceiling.

| Total | One busy card | Two busy cards | Worst case with a CPU peak (480 W) |
|---|---|---|---|
| 750 W (chosen) | 600 W (other 150) | ~375 W each | ~1,230 W, until the UPS budget trims |
| 600 W | 450 W | 300 W each | ~1,080 W (= today's 300/300) |

The UPS budget stays. It covers what the total can't see (CPU, disks) and trims sustained load.

## Approach

### 1. Per-card caps replace the shared cap (the prerequisite)

Today `learned_cap_w` is ONE number applied to every card, and it's repeatedly re-derived as
`min(applied_w)`. Any card that is lower for its own reason drags every other card down with
it. That's the known "unequal per-GPU ceilings" bug, and a total ceiling makes unequal limits
the normal case (600/150). An earlier quick fix, taking the max instead, was reverted because
cuts and recovery walks could then *raise* a card (300 → 580 W).

**The new model.** Each card has its own cap `cap_w[i]`, the level the UPS budget, the thermal
law and the holds allow it. The card's limit is `max(min_w[i], min(max_w[i], cap_w[i]))`, where
`max_w[i]` = min(hardware max, per-GPU ceiling, allocation).

Rules that remove the bug class:

- **A cap is never derived from the applied limits.** Every operation sets it explicitly.
- **Cuts never raise.** Every cut starts from the card's current level:
  `min(applied_i, cap_i) − step`.
- **Raises happen in exactly three places:** the supervised restore/recovery walk (per card,
  per step), the idle reset, and an allocation raise (item 2).

| Operation | Old (shared cap) | New (per card) |
|---|---|---|
| UPS throttle | cap − excess/active, on every card | each **active** card: its level − excess/active (slew ≤ 150 W) |
| UPS restore | cap + step, on every card | each card below its bound: cap + step |
| Thermal cut | cap − step, on every card | every card: its level − step (step capped at 50% of *that card's* level) |
| Recovery walk | cap + step until min(targets) | each card: cap + step until its own target |
| Idle reset | cap = max | cap = its bound |
| Clamps (OB, fallback, emergency, blind) | every card = w | cap = w, every card |

For equal cards all of these reduce to today's behaviour. The existing 175 tests are the
regression net for that.

### 2. The total ceiling and the allocator

- **Setting `total`**, file `power-ceiling-total`, values `W | none` (`off`/`0` = none).
  Hand-run flag `--power-ceiling-total`. Acks:
  - `POWER: total ceiling set to 750 W across 2 GPUs (…)`
  - `POWER: total ceiling cleared (…)`
  - `POWER: ignoring total ceiling … — below the 300 W the GPUs need at their minimum; keeping …`
- **Activity** per card: active at once when the draw is over 75 W or the utilization over 5%;
  idle only after **10 s** of neither, so a pause between requests doesn't move power.
- **Allocation** (every fan tick, ~2 s, before the UPS interval gate):
  - **Some cards busy, some idle:** idle cards get their minimum (150 W). The busy cards
    share the rest equally, water-filled up to each card's own ceiling.
  - **All busy or all idle:** equal split, water-filled. So a card waking from all-idle starts
    at 375 W, not 150 W.
- **Lower before raise.** In a tick that lowers any card's allocation, only the lowering is
  applied. Raises wait for the next tick, 2 s later. So the sum of limits never exceeds the
  total, even for the milliseconds a card takes to obey a lower limit.
- **An allocation raise is applied at once**, up to the card's cap. It's safe because the
  total bounds it. It is not a supervised +20 W walk; that would leave a woken card at 150 W
  for minutes.
- **Holds win.** Emergency, blind, the `OB` floor and the fallback still clamp every card.
  The allocation only ever lowers `max_w`, it never lifts a hold.
- **Stop never raises** (unchanged). An idle card stays at its 150 W allocation after a stop,
  until the next start. That's what keeps the sum within the total with no daemon running.

### Alternatives considered

| Alternative | Why not |
|---|---|
| 600/600 with the UPS budget alone | Reactive and lagging: the 2026-08-24 breaker trip |
| A total as a unit flag (safety limit) | The operator chose a live setting, like the ceiling |
| Keep the shared cap and add the allocation on top | The shared cap collapses to the idle card's 150 W; `min(applied)` is the root bug |
| Idle cards at 0 margin above the minimum | 150 W is the hardware minimum on the RTX PRO 6000; nothing lower exists |
| Raise and lower in the same tick | A card needs time to obey a lower limit; one tick of delay makes the invariant hold strictly |

## Files to Modify

| File | Change |
|---|---|
| `nvidia-fan-control.py` | per-card `cap_w`; `total` setting (parser, store, flag, ack, effective.json); allocator; `enforce_ceiling` also when a total is set |
| `tests/test_power_ceiling.py`, `tests/test_fan_policy.py` | allocation, the sum invariant on every tick, lower-before-raise, unequal ceilings no longer collapsing, the UPS acting on active cards only |
| `README.md`, `HANDOVER.md` | the total ceiling; unequal per-GPU ceilings now allowed |
| `nvidia-fan-control.service` | unchanged (the total is a live setting) |

## Edge Cases

- **A total below the sum of minimums** (e.g. 250 W for two 150 W cards) is refused, and the
  previous value kept.
- **A total above the sum of ceilings** is accepted and simply never binds.
- **A card that can't be read** (draw/util): it counts as active, the conservative choice. It
  keeps its share.
- **One GPU:** the total acts as a second ceiling.
- **A per-GPU ceiling below a card's share:** the water-fill gives the difference to the
  other busy cards.
- **Runtime state:** the caps aren't persisted, as today. A restart starts from the cards'
  current limits and walks up.

## Open Questions

- Should an idle card get more than its minimum, to cut first-request latency (e.g. 200 W)?
  Start at the minimum and measure the wake-up on pve-ai.
- The deploy team names the new verb (suggested: `just gpu-total-ceiling-set <host> <W|none>`),
  and can lift the unequal-values refusal in `gpu-ceiling-set` once this ships.

## Outcome (2026-09-29)

Implemented as planned, plus three changes that the hardware test forced:

1. **An idle card's cut comes off its cap, not its share-level.** A thermal cut on GPU1 while
   GPU0 idled at 150 W saved 150 W as GPU0's own cap. When GPU0 woke, it stayed at 150 W and
   crawled back +20 W per 30 s instead of taking its 300 W share. See `_cut_base`
   (decisions/012).
2. **Busy flags are refreshed at the moment of a cut.** A card that started working and got
   a predictive cut in the same tick was still flagged idle, so the cut trimmed its cap and
   left its 300 W draw in place. See `_refresh_busy`.
3. **With a total and no hold, a restart starts each card at its full share**
   (`settle_after_start`). Otherwise a busy card crawls back from the previous run's share.
   Without a total, a restart still starts from the current limits.

**The UPS budget loop is unchanged in behaviour** (operator's requirement). Its arithmetic is
now per card: every card is still cut on an over-budget reading, each from its own level.

Hardware (pve-ai, gpu-burn in `gpu-test`, production stopped behind a safety timer):

| Run | Settings | Result |
|---|---|---|
| 1 | total 750 → 600 live, ceiling 600, adaptive/85 | GPU1 alone: 150/600 within one tick, lowered first. Live change to 600: 150/450 at once. GPU0 waking **crawled** (bug 1). Sum of limits ≤ the total on every sample |
| 2 | total 600, ceiling 600, `max` fans, target 65 | GPU0 waking got its 300 W share within 5 s, GPU1 lowered first; the sum of draws peaked at 600/600 W. The same-tick cut exposed bug 2 |

Tests: 189 + 23. Each fix has a replay test that fails without it.

**Not tested on hardware:** both cards busy under a 750 W total. That's ~1,050 W at the wall
until the UPS budget trims, and it was deliberately not provoked.

## Addendum: the thermal cut per card (operator, 2026-09-29)

**Operator:** with a total, cutting every card when one is hot wastes budget. So cut only the
hot card, and let the cooler busy cards use what it frees, still within the total.

- **The cut applies only to the hot card(s).** The law, its timings and its state still run
  on the hottest card (decisions/009). The cards cut are those at or over the target, plus
  the hottest one for a predictive cut. `_hot_cards`.
- **The unused share goes to the others.** The allocator's water-fill caps each busy card at
  `min(ceiling, cap)`, so a card capped below its fair share leaves the rest to the others.
  Lower before raise still holds.
- **Unchanged:** the 92 °C emergency (host-wide), and the UPS budget (every card).
- **The accepted trade-off:** back to back, GPU0's exhaust heats GPU1, so feeding GPU0 more
  pushes GPU1 down. The 92 °C emergency is the backstop.

Hardware (pve-ai, 17:16): both cards burning, total 600, target 62, fans at max. GPU1 reached
62 °C and **only GPU1 was cut** (300 → 270 → 240 W). GPU0 took the freed share a tick later
(330 → 360 W), with the sum at exactly 600 W. When GPU0 reached 62 °C in turn, only GPU0 was
cut. The coupling is visible: with GPU0 at 300–360 W, GPU1 held 61–62 °C even at 180 W. It
was at 63–64 °C at 300 W alone. Tests: 195 + 23.

## Addendum 2: sharing, fans and recovery (operator, 2026-09-29, from the hardware runs)

The operator watched four-phase runs (GPU0, then both, then GPU1 alone, then both; total 750,
ceiling 600, `adaptive`, target 60) and asked for five changes, each seen on the hardware:

| Seen | Change |
|---|---|
| An idle card at 150 W while the busy card was held at 300 W for heat: 300 W of the total unused | **Idle cards hold what the busy cards can't use, ahead of time** (`_shares`: busy cards first, then the leftover to the idle ones); the busy card takes it back, lower before raise |
| During a hold, adaptive learned a −26% trim, the fans sat at ~75%, the card at 58–60 °C: never target − 2 for 30 s, so power never came back | **While power is held for heat (the hold and the walk back), adaptive's fans follow the base curve and learn no quieter trim** (`heat_state`). An interim version pinned the fans at max; the operator rejected it (fans stuck at 100% long after the need) |
| Power restored only at target − 2 for 30 s, even with the fans below their max | **Adaptive: released once the card is below the target for 10 s; the walk pauses at the target** (`_thermal_room`). With the fans on the curve, "below the target" is "the fans have room". The fixed curves keep 2 °C / 30 s |
| A cool card crawled +20 W because the other card sat at the release point | **The recovery step is sized by each card's own temperature** (`raise_step_w(level, temp)`) |
| An idle, cool card kept the cuts it got while busy (215 W with 570 W free) | **Per-card idle reset, with a total only:** idle 60 s and below the release point → cap back to its ceiling (`_reset_idle_caps`). Without a total the UPS behaviour is unchanged |
| (Operator's question) keep the predictive rule on the way back? | The predictive cut is unchanged; **a recovery step also waits while the card is still climbing** (the cut's own definition) (`_card_climbing`) |

The final hardware run (all but the last row) showed: the fans at 93–97% near 60 °C, never
pinned, and 95 → 60% within 40 s of the load stopping. Holds released 10 s after the card went
below the target. The per-card idle reset gave GPU0 510–540 W of spare budget. The predictive
cut fired on a waking card. At 60 °C both cards together sustain only ~390–480 W with the
fans near max, so the temperature bound there, not the total. GPU1 (in GPU0's exhaust) took
the smaller share. Tests: 216 + 23.
