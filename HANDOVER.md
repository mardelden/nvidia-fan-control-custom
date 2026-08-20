# HANDOVER — nvidia-fan-control + UPS power governor

Deployment contract for the fleet/infra team. **Facts to adapt, not an implementation
to run** — the paths, unit name and install method below are what works on the dev box;
port them to fleet conventions rather than copying them.

Source: `github.com/mardelden/nvidia-fan-control-custom` (fork of `zmarty/nvidia-fan-control`)

---

## What it is

A single Python daemon that does two related things on a multi-GPU host:

1. **Fan control** — overrides the cards' factory fan curve; can sync all fans to the
   hottest card (matters for back-to-back cards where an idle neighbour's slow fan
   chokes the hot card's airflow).
2. **Power governor** *(fork addition, optional)* — closed-loop cap on **total UPS
   load** by adjusting GPU power limits.

They share one process deliberately: capping power lowers temperature, so two
independent controllers would react to each other's output.

## How it runs

| | |
|---|---|
| Shape | long-running systemd service, `Type=simple`, `Restart=on-failure` |
| User | **root** (required — NVML fan + power limit writes) |
| Listener | **none** — no ports, no sockets, no network |
| Health | liveness only: `systemctl is-active`. Journal lines are the real signal. |
| Shutdown | handles `SIGTERM`; restores factory fan policy **and** default power limits on exit |
| Resource | negligible — the fan-only version measured **49 ms CPU** total; no meaningful RAM |

## Dependencies

| Dependency | Why | Note |
|---|---|---|
| NVIDIA driver + NVML | fan + power control | must match the host driver, e.g. `580.126.18` |
| `nvidia-persistenced` | keeps the driver loaded | daemon must start **after** it |
| `python3-pynvml` | NVML bindings | `apt install python3-pynvml` |
| **NUT client (`upsc`)** | power governor sensor only | not needed if `--power-budget` is omitted |
| `nut-monitor.service` | provides `upsc` data | governor must start **after** it |

Python: stdlib only besides `pynvml`. No venv, no pip install, no lockfile.

## Startup ordering

```
nvidia-persistenced.service ─┐
nut-monitor.service ─────────┴─→ nvidia-fan-control.service
```

Both are `After=` **and** `Wants=`. If NUT is missing the governor fails safe (see
below) rather than crashing, but the ordering avoids a noisy first minute.

## Configuration contract

**All config is CLI flags in `ExecStart`.** No config file, no env vars, no secrets —
nothing to put in OpenBao.

| Flag | Default | Meaning |
|---|---|---|
| `--mode` | `quiet` | fan curve: `native`, `quiet`, `aggressive`, `performance`, `max` |
| `--interval` | `2.0` | fan loop period (seconds) |
| `--independent` | off | per-card fans instead of syncing to the hottest |
| `--mirror` | off | hotter card stays on its factory curve; cooler card mirrors it |
| **`--power-budget WATTS`** | **off** | **enables the governor.** Total UPS load ceiling |
| `--ups NAME` | `cyberpower` | NUT UPS name — `upsc -l` |
| `--power-interval` | `5.0` | governor period; below ~2 s buys nothing |
| `--power-fallback` | `300` | per-GPU clamp when the UPS is unreadable |
| `--power-dry-run` | off | log only, change nothing |

**The governor is off unless `--power-budget` is passed.** Hosts without a UPS run
exactly as before.

Reference invocation (dev box, pve-ai):

```
--mode quiet --interval 1 --power-budget 900
```

## Per-host values that must NOT be copied blindly

`--power-budget` is a function of **that host's UPS**, not a global. Derive it:

```
budget ≈ 0.85 × ups.realpower.nominal      # upsc <name> | grep realpower.nominal
```

On the dev box: nominal 1000 W → 900 W budget (chosen by the operator; 850 would be
more conservative for an SLA unit). A host on a 3 kVA UPS should get a very different
number, and a host with no UPS should not enable the governor at all.

## Safety behaviour

| Condition | Action |
|---|---|
| `ups.status` contains `OB` or `LB` (**on battery**) | clamp all GPUs to the hardware floor (150 W on RTX PRO 6000) — runtime beats throughput during an outage |
| UPS unreadable 3× consecutively | clamp to `--power-fallback` rather than assume headroom |
| Daemon exits / restarts | restores each GPU's **factory default** power limit and auto fan policy |
| GPU ≥ 87 °C | fan safety floor forces 100%, independent of the curve |

## Gotchas

- **Power limit ≠ power draw.** Idle cards draw ~16 W while their limit sits at 600 W,
  even holding 166 GB of model weights. Do not read an idle wattage as evidence the
  governor is or isn't needed.
- **The sensor is coarse and slow.** This UPS exposes no `ups.realpower` — only
  `ups.load` as an **integer percent** of nominal, refreshed every ~2 s. Watts are
  derived at **10 W resolution**, and **sub-2 s transients are invisible**. This
  governs sustained draw; it is *not* inrush protection.
- **The governor is a permanent ceiling, not an emergency brake.** On a host whose
  peak draw exceeds the budget, it is active whenever the GPUs work. On the dev box a
  900 W budget yields ~355 W/GPU at CPU idle and ~210 W/GPU under full CPU load,
  against a 600 W rating — a real throughput cost, chosen deliberately because the
  1000 W UPS cannot carry the machine at full tilt.
- **Only validated at idle so far.** Convergence (600 → 450 → 366, then steady) was
  measured with GPUs at 0% utilization. Behaviour under sustained inference load is
  modelled, not observed. **Validate with `--power-dry-run` under real load before
  enabling for real on any host.**
- **Other devices on the same UPS are included in the budget.** This is intentional —
  the thing being protected is the UPS. But it means unrelated load silently reduces
  GPU headroom, which can look like an unexplained throttle.
- **Fan control overrides the factory curve**, so a too-gentle custom curve can leave a
  hot card under-cooled. The 87 °C floor is the backstop; the GPU's own thermal
  throttle (~88–90 °C) and shutdown (~95 °C) are the hardware ones.

## Suggested validation before enabling

```bash
# 1. sensor present and sane
upsc <name> | grep -E 'ups.load|ups.status|realpower.nominal'

# 2. what would it do, right now, changing nothing
python3 nvidia-fan-control.py --power-budget <W> --power-dry-run --once

# 3. watch it converge under REAL load, still changing nothing
python3 nvidia-fan-control.py --power-budget <W> --power-dry-run --power-interval 3
```

Only then put `--power-budget` in the unit.

## Files

| File | |
|---|---|
| `nvidia-fan-control.py` | the daemon (single file, stdlib + pynvml) |
| `nvidia-fan-control.service` | **reference** unit — dev-box paths, adapt for the fleet |
| `stress_vllm.py` | load generator used to exercise the fan curve |
| `README.md` | user-facing docs incl. control law and anti-oscillation values |
