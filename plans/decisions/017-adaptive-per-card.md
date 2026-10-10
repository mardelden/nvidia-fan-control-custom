# Decision: adaptive drives each card on its own; mirror runs the fastest card's speed

**Status:** Accepted
**Date:** 2026-10-10
**Context:** pve-ai, after the operator turned mirror off at 16:33:28

## Context

The operator turned mirror off and the governor acknowledged it ("FAN: mirror off"), but both
cards kept the same fan speed: GPU0 at 58 °C and GPU1 at 69 °C, both at 84%. Mirror was only
read by `native` and the fixed curves. `adaptive` was written as one thermal zone (v1): one
ramp from the hottest card's temperature, one learned trim, one unlearning floor, one command
sent to every card. The README did say so ("`adaptive` always treats both cards as one zone").

The operator: "let's make adaptive control each card separately, and even learned quiet trim
should be separate. I use fan mirroring only when cards are back to back to guarantee
exhaust."

## Decision

**Adaptive's state is per card, always:**

- the ramp is from the card's own temperature;
- the learned quiet trim and its unlearning floor belong to that card;
- the fan command, with its 2%-a-reading ease down, is that card's own;
- the emergency override (100%) applies to the card at or above the emergency temperature.

**A floor rises only for the card that was cut.** The governor counts holds per card
(`thermal_holds_by_card`): a card counts once per hold, when it's first cut in it.

**A trim freezes only on a card whose own power is held back for heat.** `heat_state(card)`
reads `heat_cards`, the cards held back for heat:

- **added:** a card when a thermal cut hits it; every card on an emergency or blind clamp;
- **pruned:** when a new hold starts, a card that has recovered to its ceiling is dropped;
- **saved:** in the runtime state as `thermal.cards`, so a restored hold covers the same cards.

A cooler card that was never cut keeps learning its quiet trim.

**Mirror on:** each card still computes its own demand and trim, and every card runs the
**fastest** card's command. That's the exhaust guarantee for back-to-back cards. While the
hotter card stays the hotter one, this matches the old one-zone behaviour. Mirror off: each
card runs its own command. When mirror is turned off, a cool card eases down from the shared
speed 2% a reading.

**The fan gate on an adaptive cut** (cut once the fans reach the fan max) now looks at the
hottest card's own command. That's the fastest one if two cards tie.

## Alternatives Considered

| Alternative | Why rejected |
|---|---|
| Per-card fans, one shared trim | The operator asked for separate trims. A trim learned on one card's thermals is wrong for the other. |
| Keep the one-zone path for mirror on, add a per-card path for mirror off | Two sets of state that disagree at every mirror toggle; the max-of-demands rule gives the same guarantee with one state. |
| Every fan to 100% in an emergency even with mirror off | The fixed curves already treat the emergency per card without mirror; independent cards don't share heat. The power clamp is still host-wide. |

## Consequences

- **The runtime state format:**
  - per card under `adaptive.by_card` (`trim_pct`, `trim_floor_pct`) and `thermal.cards`;
  - the single `trim_pct` / `trim_floor_pct` keys remain (the least quiet card's) so a rollback
    to the previous version still reads the file;
  - an older file's single values restore to every card.
- **`effective.json`:** `adaptive_trim_pct` and `adaptive_trim_floor_pct` became lists (one per
  card). `fan_pct_by_card` is new. `fan_pct` stays a number (adaptive: the hottest card's
  command). No deploy-team verb parsed them; their vendored tests did.
- **The `target:` log line** is now one line per reading with a `GPU<n> …` part per card, plus
  `mirror -> N%` when mirror is on.
- **Tests:** 276 + 23. The old one-zone expectations were changed:
  - "fans reach the fan max": `[50, 30]`, the cool card on its own;
  - "emergency 100% on adaptive": the hot card only, with mirror off.
