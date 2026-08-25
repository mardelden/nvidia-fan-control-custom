# NVIDIA Aggressive Fan Control

A Python-based fan control daemon for headless NVIDIA GPUs, designed for high-power AI workloads.

## Features

- **Multiple fan curves** - From quiet idle to maximum cooling
- **Headless operation** - Works without X11/display (uses NVML directly)
- **Multiple modes** - Choose between quiet, aggressive, performance, or max cooling
- **Systemd service** - Runs automatically on boot
- **Graceful shutdown** - Restores automatic fan control when stopped

Fork additions (this repo):

- **Sync / mirror across cards** - back-to-back cards perform as one thermal zone, so an
  idle neighbour's slow fan can't choke the hot card's airflow
- **[Temperature target](#temperature-target--thermal-power-derating-fork-addition)** -
  `--temp-target 90` holds the cards at a chosen temperature and derates power when the
  fans run out of authority
- **[UPS power governor](#power-governor-fork-addition)** - `--power-budget` keeps TOTAL
  server draw under a watt budget so an undersized UPS can't trip
- **[Power ceiling](#power-ceiling--pinning-a-fixed-wattage-fork-addition)** -
  `--power-ceiling 300` pins a fixed wattage that sticks, for repeatable benchmarking.
  Works with no UPS, live-settable, survives restarts

## Fan Curves

### Quiet Mode (default)
Matches NVIDIA default at idle, ramps aggressively under load.

| Temperature | Fan Speed |
|-------------|-----------|
| ≤40°C | 30% |
| 45°C | 40% |
| 50°C | 55% |
| 55°C | 75% |
| 60°C | 90% |
| 65°C+ | 100% |

### Aggressive Mode
| Temperature | Fan Speed |
|-------------|-----------|
| 30°C | 40% |
| 40°C | 50% |
| 50°C | 65% |
| 55°C | 75% |
| 60°C | 85% |
| 65°C | 95% |
| 70°C+ | 100% |

### Performance Mode
| Temperature | Fan Speed |
|-------------|-----------|
| 25°C | 50% |
| 35°C | 60% |
| 45°C | 75% |
| 50°C | 85% |
| 55°C | 95% |
| 60°C+ | 100% |

### Max Mode
Always runs fans at 100%.

## Requirements

- NVIDIA GPU with fan control support
- Python 3.8+
- `pynvml` package
- Root/sudo access (required for fan control)

## Installation

### 1. Install pynvml

```bash
sudo apt install python3-pynvml
```

### 2. Copy the script to system location

```bash
sudo mkdir -p /opt/nvidia-fan-control
sudo cp nvidia-fan-control.py /opt/nvidia-fan-control/
sudo chmod +x /opt/nvidia-fan-control/nvidia-fan-control.py
```

### 3. Install the systemd service

```bash
sudo cp nvidia-fan-control.service /etc/systemd/system/
sudo systemctl daemon-reload
```

### 4. Enable and start the service

```bash
sudo systemctl enable nvidia-fan-control.service
sudo systemctl start nvidia-fan-control.service
```

## Usage

### Check service status

```bash
sudo systemctl status nvidia-fan-control
```

### View live logs

```bash
journalctl -u nvidia-fan-control -f
```

### Stop the service (restores automatic fan control)

```bash
sudo systemctl stop nvidia-fan-control
```

### Restart with different mode

Edit the service file to change the mode:

```bash
sudo nano /etc/systemd/system/nvidia-fan-control.service
```

Change the `ExecStart` line:
```ini
# For quiet mode (default - silent idle, aggressive ramp):
ExecStart=/usr/bin/python3 /opt/nvidia-fan-control/nvidia-fan-control.py --mode quiet --interval 1

# For aggressive mode (always audible):
ExecStart=/usr/bin/python3 /opt/nvidia-fan-control/nvidia-fan-control.py --mode aggressive --interval 1

# For performance mode (louder, cooler):
ExecStart=/usr/bin/python3 /opt/nvidia-fan-control/nvidia-fan-control.py --mode performance --interval 1

# For max cooling (100% always):
ExecStart=/usr/bin/python3 /opt/nvidia-fan-control/nvidia-fan-control.py --mode max --interval 1
```

Then reload and restart:
```bash
sudo systemctl daemon-reload
sudo systemctl restart nvidia-fan-control
```

## Manual Usage

You can also run the script manually:

```bash
# Run once and exit (fans return to auto after a few minutes)
sudo python3 nvidia-fan-control.py --once

# Run as daemon with custom interval
sudo python3 nvidia-fan-control.py --mode performance --interval 2

# Show help
python3 nvidia-fan-control.py --help
```

## Command Line Options

| Option | Description |
|--------|-------------|
| `--mode`, `-m` | Fan curve mode: `quiet` (default), `aggressive`, `performance`, or `max` |
| `--interval`, `-i` | Poll interval in seconds (default: 2.0) |
| `--independent` | Per-card fans instead of syncing to the hottest card |
| `--mirror` | Hotter card stays on its factory curve; cooler card mirrors it |
| `--once` | Set fans once and exit (don't run as daemon) |
| `--power-floor-on FLAG[,FLAG...]` | NUT statuses that immediately use the GPU hardware floor |

## Uninstall

```bash
sudo systemctl stop nvidia-fan-control
sudo systemctl disable nvidia-fan-control
sudo rm /etc/systemd/system/nvidia-fan-control.service
sudo rm -rf /opt/nvidia-fan-control
sudo systemctl daemon-reload
```

## Troubleshooting

### Service won't start

Check logs:
```bash
journalctl -u nvidia-fan-control -n 50 --no-pager
```

### Permission denied errors

The service must run as root. Check that the service file has `User=root`.

### Fans not responding

Ensure NVIDIA persistence daemon is running:
```bash
sudo systemctl status nvidia-persistenced
```

### Fans reset to default after stopping

This is expected behavior - the script restores automatic fan control when stopped.

## License

MIT

---

## Power governor (fork addition)

Closed-loop **whole-server** power cap: the UPS is the sensor, the GPU power limit is
the actuator. Keeps total UPS load under a budget so the UPS can actually carry the
machine instead of tripping on overload.

```bash
# see what it WOULD do, without touching anything
python3 nvidia-fan-control.py --power-budget 900 --power-dry-run --once

# run for real, alongside the fan curve
python3 nvidia-fan-control.py --mode quiet --interval 1 --power-budget 900
```

### Why it lives in this daemon

Capping power lowers temperature, which changes what the fan curve does. Two
independent daemons would be reacting to each other's output. The governor is ticked
from the fan loop and self-rate-limits to `--power-interval` (default 5 s).

### Control law

**Reactive, with a learned common cap** — GPUs stay at MAX while the UPS has headroom; one shared
cap is trimmed as soon as load goes over budget (the first over-budget tick by default —
`POWER_OVER_GRACE_TICKS` raises that to ride out longer overshoots). A truly brief spike still
passes: the ~2 s coarse UPS sensor can't see a sub-2 s transient. Earlier revisions capped
proactively even at idle; changed to reactive 2026-08-20.

```
non_gpu  = total_ups_watts − Σ(gpu power draw)
over budget >= GRACE ticks  ->  trim learned cap by (over / active_gpu_count)
sustained headroom          ->  raise learned cap by POWER_SLEW_UP_W, up to max_w
all GPUs idle for a dwell   ->  reset caps to max_w for the next job
brief spike / steady band   ->  hold
```

`max_w` is the hardware maximum, or the **power ceiling** when one is set (see below).

Three refinements over a naive loop:

- **The excess is split across *active* cards only.** An idle neighbour used to dilute the
  correction applied to the card actually drawing power.
- **Raising requires sustained evidence** — `POWER_RESTORE_HEADROOM_TICKS` (3) consecutive
  under-budget samples *and* `POWER_RESTORE_DWELL_S` (30 s) since the last change.
- **A learned cap belongs to its workload.** Rather than chasing low UPS samples upward mid-job,
  caps reset to MAX only once every card has been genuinely idle (< 75 W board draw and
  < 5% utilization) for 60 s — so the next job starts unrestricted.

Attributing the remainder to `non_gpu` instead of modelling the CPU means the loop
stays correct even if **other devices share the UPS** — which is what we want, since
the thing being protected is the UPS, not the server.

### Anti-oscillation

The NUT driver refreshes every ~2 s and NVML's own enforcement has its own time
constant, so a naive proportional loop hunts. Three guards:

| Guard | Value | Why |
|---|---|---|
| Deadband | 15 W | ignore jitter (GPU idle draw wobbles ~1 W) |
| Slew down | 150 W/step | react fast in the safe direction |
| Slew up | 20 W/step | recover gently, never overshoot the budget |
| Headroom ticks | 3 | repeated under-budget evidence before raising |
| Restore dwell | 30 s | minimum spacing between upward changes |
| Idle dwell | 60 s | all cards idle before caps reset to MAX |

Reactive behaviour at a 900 W budget: idle holds `600/600` (total ~190 W ≪ budget, no
throttle); a sustained overload trims down 150 W/step until total settles at ~budget, then
recovers 20 W/step as load falls. The final restore step snaps exactly to hardware max, so the
15 W deadband cannot strand a 600 W card at 590 W. A truly brief spike passes through — it's below
the ~2 s sensor's resolution.

After each downward step, the governor holds all GPU limits until NUT publishes a different
`ups.load` or `ups.status` sample. This prevents a cached UPS total from turning falling GPU draw
into falsely rising inferred non-GPU load. An unchanged sample is accepted after 45 seconds so a
stuck sensor cannot freeze protection indefinitely. Emergency `--power-floor-on` flags bypass the
wait. The same limit is still applied to every GPU.

### Safety behaviour

| Condition | Action |
|---|---|
| `ups.status` matches `--power-floor-on` | clamp both GPUs to the hardware floor (150 W); default `OB,LB` |
| 100% fan and still over `--temp-target` for a dwell | derate the learned cap (thermal governor, below) |
| UPS unreadable ×3 | clamp to `--power-fallback` (default 300 W) rather than assume headroom — itself bounded by the ceiling |
| Daemon exit, no ceiling | restore each GPU's factory default power limit |
| Daemon exit, ceiling set | leave the GPUs pinned at the ceiling (see below) |

### Sensor limitations

This UPS (CyberPower CP1500PFCLCDa) exposes **no** `ups.realpower`. Only
`ups.load` as an **integer percent** of `ups.realpower.nominal` (1000 W), so:

- watts are derived, at **10 W resolution**
- the driver polls every ~2 s, so **transients under ~2 s are invisible**

This governs *sustained* draw. It is not inrush protection.

### Flags

| Flag | Default | |
|---|---|---|
| `--temp-target CELSIUS` | *off* | own all fans as one thermal zone around a target (50..91 C); overrides `--mode`/`--mirror` |
| `--power-budget WATTS` | *off* | total UPS load budget; enables the governor |
| `--power-ceiling WATTS` | *off* | per-GPU upper limit; enables the governor **without a UPS** |
| `--power-ceiling-file PATH` | `/var/lib/nvidia-fan-control/power-ceiling` | ceiling state + live control file |
| `--no-power-ceiling-file` | off | this run only; persist nothing |
| `--ups NAME` | `cyberpower` | NUT name, see `upsc -l` |
| `--power-interval SEC` | `5.0` | below ~2 s buys nothing |
| `--power-fallback WATTS` | `300` | per-GPU clamp when the sensor dies |
| `--power-floor-on FLAG[,FLAG...]` | `OB,LB` | statuses that immediately use hardware floor |
| `--power-dry-run` | off | log only, change nothing |

---

## Temperature target + thermal power derating (fork addition)

`--temp-target 90` replaces curve/mirror mode with a single closed loop that owns **all** fans as
one thermal zone and holds the cards at a chosen temperature, instead of following a fixed curve
and handing off to the factory emergency behaviour.

```bash
python3 nvidia-fan-control.py --temp-target 90 --interval 2 --power-budget 900
```

Fan demand starts rising `TARGET_FAN_APPROACH_BAND_C` (20 C) *before* the target rather than
waiting for it, then a learned trim tracks the setpoint: it comes down slowly while below target
(`TARGET_TRIM_DOWN_PCT_PER_C_S`), freezes at the setpoint, and unwinds fast if temperature rises
(`TARGET_TRIM_UP_PCT_PER_C_S`). Fan slew is asymmetric — up 10%/poll, down 2%/poll — so the fans
ramp decisively and back off gently.

**Thermal power derating.** Fans are the first actuator; power is the second. If fans reach 100%
*and* the hottest card is still `THERMAL_POWER_MARGIN_C` (2 C) above target for a dwell (15 s the
first time, 5 s for repeats), cooling has run out of authority and the governor derates the shared
power cap rather than letting temperature oscillate at the emergency boundary:

```
step = clamp(excess_C × 10 W, 20 W, 150 W)      # bigger overshoot, bigger cut
```

Recovery is deliberately slower than derating: the thermal hold clears only after the card sits
`THERMAL_POWER_RECOVER_MARGIN_C` (2 C) *below* target for `THERMAL_POWER_RECOVER_DWELL_S` (30 s).
While the hold is engaged, UPS-driven recovery is suppressed too — the two loops must not fight.

A thermal reduction arms the same fresh-UPS-feedback gate as a UPS reduction, so one stale
whole-system reading cannot cause both control inputs to react to it.

Derating needs a governor, which means `--power-budget` **or** `--power-ceiling`. With neither, the
daemon warns and runs fan-only. A hard safety floor still forces 100% fan at `CRITICAL_TEMP` (92 C)
regardless of the loop.

---

## Power ceiling — pinning a fixed wattage (fork addition)

`--power-ceiling` sets the **upper** actuation bound the governor will never exceed,
replacing the card's hardware max (600 W on RTX PRO 6000) as the value it restores
toward. It is what `nvidia-smi -pl` should have been here: a cap that actually *sticks*,
because the governor honours it instead of fighting it.

Without this, a manual `nvidia-smi -pl 300` is re-raised to 600 W on the next governor
tick, and the only way to pin power was to stop the daemon — surrendering fan control
and the UPS safety loop with it.

### Pin, repin, unpin

```bash
# pin both cards at 300 W, no UPS needed at all
python3 nvidia-fan-control.py --mode quiet --power-ceiling 300

# per-GPU ceilings
python3 nvidia-fan-control.py --power-ceiling 300,450

# with the UPS governor as well: still protects the UPS, never exceeds 300 W
python3 nvidia-fan-control.py --mode quiet --power-budget 900 --power-ceiling 300

# live, against the running service — no restart, fans and UPS loop stay up
echo 300  | sudo tee /var/lib/nvidia-fan-control/power-ceiling
echo none | sudo tee /var/lib/nvidia-fan-control/power-ceiling   # unpin

# systemctl reload also works (sends SIGHUP; re-reads immediately)
sudo systemctl reload nvidia-fan-control
```

The control file accepts a single wattage, a comma-separated per-GPU list,
`none`/`off`/`0` to unpin, and `#` comments (`300,450  # bench run 7`). It is polled
every fan tick, so a change lands within ~1 s. A malformed write is logged and
**ignored** — a typo in a benchmark script never silently un-pins the cards.

### Interaction with the budget

The control law is unchanged. The ceiling only replaces `hw_max` as the restore target:

- **Ceiling above the UPS-safe wattage** — the governor still throttles *below* the
  ceiling under UPS pressure, then restores up to (never above) it.
- **Ceiling at or under the UPS-safe wattage** — the loop never finds a reason to
  throttle, so power is simply constant. This is the benchmarking state: a known,
  documented wattage, with fan control and UPS protection fully intact.

`--power-fallback` is bounded by the ceiling too, so nothing exceeds it even when the
sensor dies. **Thermal derating** also works below the ceiling: if a card cooks at 100% fan the
governor still cuts power, and the ceiling hold defers to that derate until the card cools rather
than fighting it every tick.

Because a ceiling is a guarantee rather than a preference, the daemon re-reads the hardware limit
each governor tick and re-applies the cap if anything changed it out of band — a manual
`nvidia-smi -pl 600` is pulled back within one tick. (`applied_w` is only a cache of what this
daemon last wrote, so a guarded write alone would not have held the invariant.)

**Lowering** a ceiling takes effect immediately — it is a safety bound. **Raising** one goes through
the normal supervised restore path, so expect the idle dwell (60 s) or a headroom dwell before the
cards actually move up.

### Persistence

NVML power limits reset to the card default on reboot — no NVML setting survives that.
Persistence is therefore *re-application*: the ceiling is written to
`/var/lib/nvidia-fan-control/power-ceiling` and re-applied when the daemon starts.

| Event | Result |
|---|---|
| `systemctl stop` | GPUs stay pinned at the ceiling (the cap outlives the process) |
| `systemctl restart` | ceiling re-applied from the file |
| Reboot | ceiling re-applied at service start; cards sit at the card default for the few seconds before that |
| `--power-ceiling` passed on the command line | wins over the file, and is written to it |

Because an explicit `--power-ceiling` **overrides** the persisted value at startup, the
recommended deployment leaves it out of the unit file (as the shipped
`nvidia-fan-control.service` does) and treats the state file as the single source of
truth — that way a live pin survives a restart. Rationale and alternatives:
`plans/decisions/001-power-ceiling-precedence.md`.

Check what is currently pinned:

```bash
cat /var/lib/nvidia-fan-control/power-ceiling
nvidia-smi --query-gpu=index,power.limit,power.max_limit --format=csv
```
