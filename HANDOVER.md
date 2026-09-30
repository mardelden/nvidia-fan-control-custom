# HANDOVER — nvidia-fan-control (fan control + GPU power governor)

For whoever deploys and operates this daemon. On the Vulcandom fleet that's the deploy team,
through the Ansible role `proxmox_host` (`--tags gpu-fan`) in `~/src/proxmox`. **The golden
copy of the script is `roles/proxmox_host/files/nvidia-fan-control.py` in that repo**, and
this repo is kept byte-identical to it (plans/decisions/007).

## What it is

One process, run as root under systemd. It does two jobs, and they're in the same loop on
purpose: cutting power lowers temperature, so two separate controllers would react to each
other.

- **Fan control:** a fan profile (`native` = the factory curve, left alone; `quiet`,
  `aggressive`, `performance`, `max`; or `adaptive`, the learning controller), plus an
  optional **mirror** for back-to-back cards.
- **The GPU power governor:** the only thing that writes GPU power limits. It enforces the
  operator's power ceiling and temperature target, and the host's UPS budget and emergency
  temperature. **The UPS is a read-only input** (`upsc`, polled). It never commands the UPS
  and never shuts the host down; that's NUT's `upsmon`.

## The contract: live settings vs safety limits

**Operator settings are files, not flags.** The unit must NOT pass them:

| File in `/var/lib/nvidia-fan-control/` | Values | Missing means |
|---|---|---|
| `fan-profile` | `native` `quiet` `aggressive` `performance` `max` `adaptive[:FANMAX]` | `native` |
| `fan-mirror` | `on` `off` | `off` |
| `temp-target` | °C, or `none` | no target |
| `power-ceiling` | `W`, `W,W`, or `none` | the hardware max |
| `power-ceiling-total` | `W` (all GPUs together), or `none` | the **last total** if one was in force (only `none` clears it); otherwise no total |

Changes are picked up live (≤ 2 s; `systemctl reload` re-reads at once), and each one is
acknowledged in the journal with a line starting `FAN:`, `TEMP:` or `POWER:`. **A line
containing `ignoring` means the value was refused and the previous one is still in force.**

**Why the unit must not pass them:** a flag beats the file at every start (flag > override >
saved). A flag in `ExecStart` would silently undo every live change at the next restart. That
already happened once with `--power-ceiling` (proxmox `87b4b9a` → `0f802b4`).

**Safety limits stay as unit flags**, set per host and read-only in gpuguard:

| Flag | pve-ai | Meaning |
|---|---|---|
| `--temp-emergency` | 92 | 2 s at or above it → every GPU to its hardware minimum |
| `--power-budget` | 900 | total UPS load budget; omit it on hosts without a UPS |
| `--ups` | `cyberpower` | NUT name |
| `--power-floor-on` | `OB` | statuses that clamp every GPU to its floor |
| `--power-fallback` | 150 | per-GPU limit if the UPS becomes unreadable |
| `--power-interval` | 5 | seconds between UPS reads |
| `--interval` | 2 | fan loop period |

**Seeding:** Ansible writes each settings file from inventory **only if it's absent**
(`force: false`), so inventory is a first-boot default, never an override. The proposed seed
for pve-ai is `adaptive`, mirror `on`, target `85`, ceiling `300` (the ceiling file already
exists).

## Unit requirements

```ini
ExecStart=/usr/bin/python3 /opt/nvidia-fan-control/nvidia-fan-control.py \
          --interval 2 --temp-emergency 92 --power-budget 900 --ups cyberpower \
          --power-interval 5 --power-fallback 150 --power-floor-on OB
ExecReload=/bin/kill -SIGHUP $MAINPID
ExecStopPost=/usr/bin/python3 /opt/nvidia-fan-control/nvidia-fan-control.py --reset-fans
StateDirectory=nvidia-fan-control     # /var/lib: saved settings + runtime-state.json
RuntimeDirectory=nvidia-fan-control   # /run: overrides, lock, effective.json; removed on stop
Restart=always
```

- **`ExecStopPost=… --reset-fans` is needed.** NVML does **not** revert manual fans when the
  process dies: measured on pve-ai on 2026-09-29, the fans were still manual at 100% 150 s
  after `kill -9`. A clean stop hands them back to the factory curve; a crash would leave them
  wherever they were. `Restart=always` covers most of that window, and `ExecStopPost=` covers
  the rest.
- **`RuntimeDirectory=`** is what makes a restart clear hand-run overrides.
- **Suggested description:** *"NVIDIA fan control + GPU power governor (UPS is a read-only
  input)"*.

**Migration is safe in two steps.** The new script accepts the old unit's flags unchanged
(`--mirror --interval 2 --temp-target 85 --power-budget 900 …`). `--temp-target` given without
`--mode` means `adaptive`, so behaviour stays as it is today. The only differences: the thermal
cut now triggers after 5 s instead of 15 s, and the emergency cutoff is armed. Then change the
unit and seed the files. Until the unit drops those flags, a saved change to profile, mirror
or target is acknowledged as *masked by a command-line flag*.

## Safety behaviour

| Condition | Action |
|---|---|
| Card ≥ `--temp-emergency` for 2 s | Every GPU to its hardware minimum (150 W). Fans: 100% unless the profile is `native` with mirror off. Released at emergency − 30 °C for 30 s, then walks up +20 W / 30 s |
| Card at the target (adaptive: and fans at its fan max) | Power cut on the hot card(s) only: 30 W × 2^(°C over, averaged over the last two readings), at most 25% per step, and no lower than the card's last proven level in one step. A 5 s grace only if the card is steady; none if it's still climbing; a bounded cut (≤ 120 W) before the target if it rises in two readings in a row averaging ≥ 2 °C/reading; once over the target, the measured excess sizes the cut. Released at target − 2 °C for 30 s (adaptive: below the target for 10 s), then exponential recovery per card, waiting while a card is still warming (fork decisions/009, plan 003) |
| A card's temperature unreadable in 3 of the last 5 readings | Blind: every GPU to its minimum power, owned fans 100% (`native` left alone); released after 30 s of good readings, +20 W steps. A single missed reading counts at the last known temperature |
| UPS status matches `--power-floor-on` | Every GPU to its hardware floor, immediately |
| UPS unreadable 3× | Clamp to `--power-fallback` (never raising a card that's in a hold) |
| Total ceiling set (soft) | Idle cards counted at their measured draw (10 s peak + 10 W, in 25 W steps; typically 50 W), their limit staying at the 150 W floor; the busy cards share the rest, each up to its ceiling and its own cut; idle cards also hold what the busy ones can't use; lowered before raised. The draw stays within the total, except for a tick or two when an idle card wakes (at most 150 W minus the idle card's count, per waking card). An idle, cool card's old cuts are cleared after 60 s |
| Total UPS load > budget | For 20 s: nothing (a short excursion is allowed; none above the UPS's rating). Then trim every card by the measured excess; restore slowly (+20 W). Only a thermal-hold recovery raises faster, bounded by headroom, and it waits for a fresh UPS sample |
| Service stops cleanly | Fans → factory curve. Power is **never raised**: the ceiling, holds, the on-battery floor, budget trims and an out-of-band `nvidia-smi -pl` lower all stay until the next start |
| Service crashes | Fans stay where they were unless `ExecStopPost=--reset-fans` runs (see above) |
| Restart / reboot | Settings come back from the files. The learned adaptive trim is restored; thermal and emergency holds are restored if < 5 min old |

## Operating it

`gpuguard` (`~/src/gpuguard`) is the operator CLI. Underneath it are the deploy team's verbs:
`just gpu-ceiling-set <host> <value>` exists today; the fan-profile, mirror and target verbs
follow the same contract. By hand:

```bash
cat /run/nvidia-fan-control/effective.json          # what's in force, and why
journalctl -u nvidia-fan-control -f | grep -E "^(FAN|TEMP|POWER|THERMAL|⚠)"
python3 /opt/nvidia-fan-control/nvidia-fan-control.py --mode max   # temporary override while the service runs
python3 /opt/nvidia-fan-control/nvidia-fan-control.py --clear-override
```

## Gotchas

- **The UPS signal is coarse and lagging.** `ups.load` is an integer percent of nominal (10 W
  on a 1000 W unit) and it refreshes on NUT's full-poll cycle (`pollfreq`, 5 s on pve-ai). The
  budget handles sustained load, not a fast ramp. The **power ceiling** is what bounds a ramp.
- **The budget usually binds before the ceiling does.** At a 600 W ceiling, one loaded card plus
  CPU load hit 990 W and the governor throttled to 510 W. At a 300 W ceiling, two cards at full
  load sat at ~870 W, which is close to the 900 W budget.
- **Raising a limit is supervised.** Lowering is immediate. After a hold, power returns every
  30 s in exponential steps (+20 W just under the line, up to +50% when clearly cool); after an
  emergency or blind hold, +20 W steps.
- **Per-GPU ceilings may differ** since plan 003 (per-card caps), so `gpu-ceiling-set` no
  longer needs to refuse unequal values.
- **The total ceiling is the ramp bound; the ceiling is per card.** To give one card 600 W
  safely, set the **total first** (e.g. 750), then raise the ceiling (600). The other order
  leaves 600/600 with no total for a moment. With a total, an idle card sits at 150 W, and it
  stays there after a stop until the next start.
- **`--power-dry-run` is power-only** (fans and the emergency still run); `--dry-run` touches
  nothing at all.
- **The thermal cut hits only the hot card(s)**; the 92 °C emergency is still host-wide. With a
  total, the cooler busy cards take the share a cut card can't use.
- **`adaptive` unlearns from power cuts.** Each thermal hold raises a floor under its quiet trim
  (+20%, up to 0 = the base curve); 10 cut-free minutes lower it 5%. The log shows
  `FAN: adaptive unlearns …`; `effective.json` has `adaptive_trim_floor_pct`.
- **`adaptive` needs a target.** Setting it without one is refused. Clearing the target while
  it's active falls back to `native`.
- **The maximum target is emergency − 3 (89 °C).** Our old docs said 90–91; the cut point is
  now the target itself, and that cap keeps room before the emergency.
- **A bad `runtime-state.json` is set aside as `.bad`** and the daemon starts fresh; it can't
  crash-loop.
- **Per-GPU fan profiles aren't supported** (refused); the power ceiling is per GPU.

## Validation

With no GPU: `python3 tests/test_fan_policy.py && python3 tests/test_power_ceiling.py`.

On hardware, the plan 002 run on pve-ai on 2026-09-29 covered: settings loaded from files;
live switching; `native` really hands off (policy read back from NVML); target held by power
alone under gpu-burn; `adaptive:50` capping the fans and then cutting power; the emergency
cutoff with a live profile switch, release and walk; hand-run overrides and rollback; restart;
and the crash test. The results are in `plans/002-layered-fan-policy.md`. For load, use
`gpu-burn` in the `gpu-test` container (VMID 200) with `/root/ceiling-load-test.sh`.

## Files

| File | What |
|---|---|
| `nvidia-fan-control.py` | The daemon (a byte-copy of the golden file in `~/src/proxmox`) |
| `nvidia-fan-control.service` | A reference unit; the canonical one is the Ansible template |
| `tests/` | Fake-NVML test suites |
| `plans/`, `plans/decisions/` | Design, decisions, lessons |
