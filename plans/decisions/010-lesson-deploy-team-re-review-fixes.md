# Lesson: flaky sensors, fast raises and stale caches (the deploy team's re-review of 249beb6)

**Status:** Accepted
**Date:** 2026-09-29
**Found by:** the deploy team's re-review of proxmox `bd9d17e` (= fork `249beb6`), in code and a
fake-NVML simulation. Each fix has a regression test that fails on `249beb6`.

## What was wrong

| # | Bug | Fix |
|---|---|---|
| 5 | **A flaky sensor escaped both fail-safes.** Blind needed 3 failures *in a row*, so a sensor failing 2 readings in 3 never tripped it. And a partial reading left the hot card out of `hottest`: the cooler card's temperature reset the emergency timer (a card at 95 °C, unreadable every other reading, never tripped the emergency), and when the hot card read again, the jump looked like a fast rise and the prediction cut 50% at once | Blind counts failures in a **window: 3 of the last 5 readings**. Below that, a missing card counts at its **last known temperature** |
| 2 | **The exponential raise leaked into the UPS governor.** With any target set, a UPS-budget trim restored with the thermal law's exponential step, up to +50%, on a UPS sample NUT may have held for ~36 s | UPS-budget restores are +20 W again. Only a recovery from a thermal hold (`recovery_walk`) takes the fast step, bounded by headroom, and **a raise over +20 W arms the fresh-UPS-sample wait**, like a cut. A UPS throttle ends the fast recovery |
| 3 | **Stop could still raise after an out-of-band change.** `restore_defaults` took `min(applied_w, ceiling)`, but `applied_w` is our cache: after `nvidia-smi -pl 200`, stop put the card back to 300 | `min(current, applied_w, ceiling or default)`, reading the device. Stop can now only lower (decisions/006 again: trust the device, not the cache) |
| 4 | `max()` on an empty temperature list crashed the loop if NVML reported 0 GPUs | Guard restored |

## The rule to keep

**A fail-safe that counts consecutive failures can be dodged by an intermittent fault.** Count
over a window. **A missing input must not quietly become "the other inputs"**: substitute the
last known value, or treat the whole reading as bad.

## Not fixed here: the thermal limit cycle (re-review #1)

The deploy team's first-order model of a card that needs ~270 W to hold 85 °C cycled between
180 and 300 W and between 64 and 87 °C, every 3–4 minutes. The cause is in `raise_step_w`: the
recovery step is sized from the current temperature, which lags the power change, so a deep
cut followed by release jumps straight back to the power that overheated the card.

It's safe (it peaks at 87–89 °C), and it can't happen on pve-ai today, where the cards sit
around 64 °C at 300 W with the fans at 100%. It changes the operator-tuned law, so it went to
the operator. The proposed fix is to remember the power level that overheated the card:
recover fast up to just below it, and +20 W per step above it.
