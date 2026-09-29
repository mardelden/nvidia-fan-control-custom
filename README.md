# nvidia-fan-control

Fan control and a **GPU power governor** for headless NVIDIA hosts (built for 2× RTX PRO 6000
Blackwell, mounted back to back). It runs as one systemd service. On the Vulcandom fleet it's
deployed by the Ansible role `proxmox_host` (`--tags gpu-fan`), and operators drive it with
the `gpuguard` CLI.

> **GPU power governor.** Keeps GPU power within the operator's limits (power ceiling,
> temperature target) and the host's safety limits (UPS budget, emergency temperature). It
> reads the UPS through NUT as a **read-only input**: it polls `upsc` every few seconds and
> never sends commands to the UPS or changes its settings. It does not shut the host down;
> that's NUT's `upsmon`. The only things it changes are **GPU power limits and fan speeds**.

Forked from [zmarty/nvidia-fan-control](https://github.com/zmarty/nvidia-fan-control) and
since rewritten. The design history is in `plans/` and `plans/decisions/`.

## The settings

There are four **live** settings. Each one is picked up without a restart and survives
reboots:

| Setting | Values | No file means |
|---|---|---|
| **Fan profile** (whole host) | `native` · `quiet` · `aggressive` · `performance` · `max` · `adaptive[:FANMAX]` | `native` |
| **Mirror** | `on` · `off` | `off` |
| **Temperature target** | °C (50 … emergency − 3), or `none` | no target |
| **Power ceiling** (per GPU) | `W` · `W,W` · `none` | the hardware max |

The **safety limits** are set by the deployment as flags on the unit. They are never live
settings:

| Flag | Default | Meaning |
|---|---|---|
| `--temp-emergency C` | 92 | 2 s at or above it cuts every GPU to its minimum power |
| `--power-budget W` | off | keep TOTAL UPS load under W |
| `--ups NAME` | `cyberpower` | NUT UPS name (read-only) |
| `--power-floor-on FLAGS` | `OB,LB` | UPS statuses that clamp every GPU to its floor |
| `--power-fallback W` | 300 | per-GPU limit if the UPS becomes unreadable |
| `--power-interval S` | 5 | seconds between UPS reads |

### Fan profiles

| Profile | Fans |
|---|---|
| `native` | The card's **own factory curve**. The daemon leaves those fans alone |
| `quiet` | 30% up to 40 °C → 100% by 65 °C (quiet at idle, loud under load) |
| `aggressive` | 40% at 30 °C → 100% at 70 °C |
| `performance` | 50% at 25 °C → 100% at 60 °C |
| `max` | 100%, always |
| `adaptive[:FANMAX]` | Learns the quietest speed that holds the card **at** the temperature target, never above FANMAX% (default 100). **Needs a target** |

`adaptive` starts from a ramp (30% at `target − 20 °C`, 100% at the target) and learns a
trim that pulls the fans down to the quietest speed that still holds the target. The trim
learns downward slowly, holds at the target, and unwinds fast if the card gets hotter. What
it has learned survives a restart.

**Mirror** is for cards that share airflow (back to back): both fans follow the **hotter**
card. With `native`, the hotter card stays on its factory curve and the cooler card copies
its speed. With a fixed curve, both cards run the curve at the hotter card's temperature.
`adaptive` always treats both cards as one zone.

### How the temperature target is held

**Power is cut when the card reaches the target**, and harder the further over it is:

| Card vs target | Cut |
|---|---|
| at the target | −30 W |
| +1 °C | −60 W |
| +2 °C | −120 W |
| +3 °C | −240 W |
| +4 °C and up | −50% of current power (the largest single cut) |

- **Grace:** a card that's **steady** at the target gets 5 s before the first cut, so a brief
  touch isn't punished. A card that's **still climbing** (warmer than two readings ago) is cut
  at once, and at +4 °C or more there's never a grace.
- **Prediction:** if a card rises **≥ 2 °C in one reading** while within 10 °C of the target,
  the daemon projects two readings ahead and cuts **before** the target, sized by the predicted
  overshoot. On pve-ai this took the peak from 82 °C to exactly the 75 °C target.
- **Hold while falling:** after a cut, no further cut while the card is still cooling. It's cut
  again only if it stops falling while still at or over the target.
- **Release and recovery:** the hold is released at `target − 2 °C` for 30 s. Power then comes
  back in steps every 30 s, **+20 W × 2^(°C below target − 2)**, at most +50% per step. A card
  that has clearly cooled gets full power back in about a minute; one just under the line
  creeps up +20 W at a time.

| Profile | Power is cut when |
|---|---|
| fixed curves (`native`, `quiet`, …) | the rules above, **whatever the fans are doing** |
| `adaptive:N` | the rules above, **and** the fans are at their max N% (fans first, then power) |

So the profile decides **noise versus throughput**. With `native`, the target is held by
losing GPU power. With `max`, the fans take the heat and the GPU keeps its power.
`adaptive:50` means *"hold the target, never louder than 50%, and give up power first"*. For
reference, with the fans pinned at 30%, one RTX PRO 6000 sustains about 240–270 W at 75 °C
under gpu-burn.

### Emergency cutoff

It's **always armed**, for every profile: **2 s at the emergency temperature** (92 °C) cuts
every GPU to its **hardware minimum** (150 W on the RTX PRO 6000). With `native` and mirror
off the fans are left alone; every other profile, and mirror, forces them to 100%. It's
released once the card is **30 °C below** the emergency temperature for 30 s, and power then
walks back up in +20 W steps.

**If a card's temperature can't be read for 3 readings in a row (6 s)**, the daemon is blind and
fails safe: every GPU goes to its minimum power, and the fans it owns go to 100%. `native` fans
keep the factory curve, which reads the sensor itself. It's released after 30 s of good
readings, again with +20 W steps.

**Stopping the service never raises any card's power.** A ceiling, a hold, a UPS on-battery
floor or a budget trim all stay in place until the next start.

### The UPS budget

With `--power-budget`, the governor keeps **total** UPS load (CPU, board and disks included)
under the budget. It trims one shared cap by the measured excess on the first over-budget
reading, then restores slowly: three consecutive readings with headroom, +20 W, and at least
30 s between raises. An `OB` status clamps every GPU to its floor. If the UPS can't be read
3 times in a row, the cards go to `--power-fallback`.

The UPS is a **pull**, and a coarse one: `ups.load` is an integer percent of nominal (10 W
steps on a 1000 W unit) and it refreshes on NUT's full-poll cycle (`pollfreq`). The budget
protects against **sustained** load, not against a fast ramp. The ceiling is what bounds a
ramp.

## Where settings come from

Each setting is resolved in this order:

1. **A flag on the command line.** Only for hand-runs.
2. **A temporary override** in `/run/nvidia-fan-control/`. Cleared by a restart.
3. **The saved file** in `/var/lib/nvidia-fan-control/`: `fan-profile`, `fan-mirror`,
   `temp-target`, `power-ceiling`.
4. **The default.**

Changes to files are picked up within one fan tick (~2 s), and `systemctl reload`
(SIGHUP) re-reads them at once. **A malformed value is logged and ignored, and the previous
value stays in force.** Every change is acknowledged in the journal:

```
FAN: profile set to adaptive, fan max 50% (saved /var/lib/nvidia-fan-control/fan-profile)
FAN: mirror on (...)
TEMP: target set to 85C (...); power is cut at 87C held for 5s
POWER: ceiling set to 300/300 W per GPU (...)
FAN: ignoring profile adaptive (...) — it needs a temp target; keeping native
```

The daemon publishes what's actually in force to `/run/nvidia-fan-control/effective.json`.
It persists what it has learned (the adaptive trim, plus the thermal and emergency holds) to
`/var/lib/nvidia-fan-control/runtime-state.json`. The holds are restored only if they're
less than 5 minutes old.

## Operating it

On the fleet, use **`gpuguard`**, or the deploy team's `just gpu-*-set` recipes. By hand on
the host:

```bash
echo 300          > /var/lib/nvidia-fan-control/power-ceiling   # saved, survives reboots
echo adaptive:60  > /var/lib/nvidia-fan-control/fan-profile
echo 85           > /var/lib/nvidia-fan-control/temp-target
systemctl reload nvidia-fan-control                              # optional: re-read now
cat /run/nvidia-fan-control/effective.json                       # what's in force
journalctl -u nvidia-fan-control -f
```

### Running the script by hand

```bash
python3 nvidia-fan-control.py --mode max --temp-target 80    # try settings for this run only
python3 nvidia-fan-control.py --dry-run --mode quiet         # log what would change; touch nothing
python3 nvidia-fan-control.py --power-dry-run                # power governor logs only; fans run normally
python3 nvidia-fan-control.py --clear-override               # drop temporary overrides
python3 nvidia-fan-control.py --reset-fans                   # all fans back to the factory curve
```

Flags given by hand **never change the saved files**.

- **If the service is stopped**, the script drives the cards itself, and the next service
  start applies the saved settings again.
- **If the service is running**, the flags become a **temporary override**: the service
  applies them and acknowledges them, and a restart clears them. A value the service refuses
  is rolled back.
- `--temp-target` given without `--mode` implies `adaptive`, which is the old meaning of that
  flag.

## Testing

No GPU is needed; the tests run against a fake NVML:

```bash
python3 tests/test_fan_policy.py      # the settings, profiles, target, emergency, overrides, runtime state
python3 tests/test_power_ceiling.py   # the ceiling inside the UPS budget law
```

For real load, use the deploy team's `gpu-burn` in the `gpu-test` container (VMID 200) with
`/root/ceiling-load-test.sh`. The plan 002 hardware results are recorded in
`plans/002-layered-fan-policy.md`.

## Known behaviour worth knowing

- **NVML does not revert manual fans when the process dies.** It was measured on pve-ai: 150 s
  after `kill -9` with the fans at 100%, they were still manual at 100%. A clean stop hands
  them back to the factory curve. For crashes, the unit should run
  `nvidia-fan-control.py --reset-fans` as `ExecStopPost=`.
- **Raising a limit is supervised.** A lowered ceiling applies at once. After a hold, power
  returns in steps every 30 s, exponentially bigger the cooler the card (+20 W just under the
  line, up to +50% when clearly cool). After an emergency or blind hold it stays at +20 W.
- **Per-GPU ceilings must be equal for now** (`300`, not `600,300`). Cuts use one shared cap,
  so the first cut would drop the higher card to the lower card's level. It needs a per-card
  cap to fix.
- **The 2 s poll is the remaining limit on overshoot.** A hot card at 600 W with 30% fan climbs
  ~6 °C per reading, so one reading is the earliest reaction.
- **The thermal cut is one shared cap.** When the hottest card is over, every card's cap
  drops. That's right for back-to-back cards.
- **Changed defaults compared with older versions:** no file now means `native` with mirror
  off. The fixed curves used to sync both cards unless `--independent` was given. Now they're
  per card unless mirror is on.
