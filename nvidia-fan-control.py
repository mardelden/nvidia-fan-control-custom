#!/usr/bin/env python3
"""
Aggressive NVIDIA GPU Fan Control Daemon for Headless GPUs
Designed for high-power AI workloads on RTX PRO 6000 cards

Run as: sudo python3 nvidia-fan-control.py
Or install as a systemd service
"""

import pynvml
import time
import signal
import subprocess
import sys
import argparse
import logging
from typing import Dict, List, Optional, Tuple

# Configure logging for systemd journal
logging.basicConfig(
    level=logging.INFO,
    format='%(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
log = logging.getLogger(__name__)

# Quiet idle, aggressive ramp curve (DEFAULT)
# Matches NVIDIA default at idle, ramps hard above 45°C
QUIET_CURVE = [
    (40, 30),   # ≤40°C -> 30% (match NVIDIA default idle)
    (45, 40),   # 45°C -> 40% (gentle start)
    (50, 55),   # 50°C -> 55% (starting to work)
    (55, 75),   # 55°C -> 75% (ramping hard)
    (60, 90),   # 60°C -> 90% (aggressive)
    (65, 100),  # 65°C -> 100% (full blast)
]

# Aggressive fan curve: (temp_threshold, fan_speed_percent)
# Fans ramp up much earlier and faster than default
AGGRESSIVE_FAN_CURVE = [
    (30, 40),   # 30°C -> 40% (never let fans be quiet)
    (40, 50),   # 40°C -> 50%
    (50, 65),   # 50°C -> 65%
    (55, 75),   # 55°C -> 75%
    (60, 85),   # 60°C -> 85%
    (65, 95),   # 65°C -> 95%
    (70, 100),  # 70°C -> 100% (full blast)
]

# Even more aggressive "performance" curve
PERFORMANCE_FAN_CURVE = [
    (25, 50),   # 25°C -> 50% (always loud, always cool)
    (35, 60),   # 35°C -> 60%
    (45, 75),   # 45°C -> 75%
    (50, 85),   # 50°C -> 85%
    (55, 95),   # 55°C -> 95%
    (60, 100),  # 60°C -> 100%
]

# Maximum cooling - just run at 100% always
MAX_COOLING_CURVE = [
    (0, 100),   # Always 100%
]

# STOCK-matched curve — approximates the card's OWN factory fan curve (measured on
# RTX PRO 6000: ~30% idle, ~44% @76°C, ~54% @88°C). Paired with sync it keeps the
# native quiet behaviour but ties both cards together, so the ONLY change vs stock is
# the cooler card's fan rising to match the hotter one — isolates the airflow/sync gain.
NATIVE_CURVE = [
    (40, 30),   # idle — matches stock
    (60, 35),
    (70, 41),
    (78, 46),
    (85, 52),
    (90, 58),
]

# HARD SAFETY FLOOR — regardless of the selected curve, force 100% fan at/above this
# temperature. Because this daemon OVERRIDES the card's own fan curve, a too-gentle
# custom curve (e.g. 'native' tops at 58%) could otherwise leave fans low while a card
# is dangerously hot. The GPU's own thermal throttle (~88-90°C, clocks drop) and
# emergency shutdown (~95°C+) are the hardware backstop above this.
CRITICAL_TEMP = 87


# ─────────────────────────── POWER GOVERNOR ───────────────────────────
# Closed-loop whole-server power cap. The UPS is the sensor (it is the only thing
# that sees TOTAL draw, including CPU/board/disks) and the GPU power limit is the
# actuator. Goal: keep total UPS load under budget so the UPS can actually carry
# the machine, instead of tripping on overload.
#
# Measured baseline on pve-ai (CyberPower CP1500PFCLCDa, ups.realpower.nominal=1000):
#   idle total ~220 W with GPUs at ~16 W each  ->  non-GPU floor ~190 W
#   Threadripper 7970X peaks ~355 W, so non-GPU can reach ~480 W under CPU load.
# With a 900 W budget that leaves 420-710 W to split across the GPUs.

# UPS load is reported as INTEGER PERCENT of ups.realpower.nominal, so resolution
# is nominal/100 (10 W on a 1000 W unit). Do not expect finer control than that.
DEFAULT_POWER_BUDGET = 900          # watts, total UPS load ceiling
DEFAULT_UPS_NAME = "cyberpower"     # `upsc -l` name
DEFAULT_POWER_INTERVAL = 5.0        # seconds between governor updates

# Anti-oscillation. The UPS driver polls every ~2 s and NVML's own enforcement has
# its own time constant, so a naive proportional loop will hunt. Asymmetric slew:
# come DOWN fast (safe direction), go UP slowly.
POWER_DEADBAND_W = 15               # ignore changes smaller than this
POWER_SLEW_DOWN_W = 150             # max decrease per update (react fast)
POWER_SLEW_UP_W = 40                # max increase per update (recover gently)

# Reactive law: leave GPUs at MAX while the UPS has headroom; only throttle once load
# SUSTAINS over budget. A brief spike (even above nominal — the UPS carries it on
# surge/battery for a few seconds) passes untouched until the overage persists this many
# consecutive updates. Restore toward MAX only once comfortably back under budget (hysteresis).
POWER_OVER_GRACE_TICKS = 2          # ~one update interval of overshoot allowed before throttling
POWER_RESTORE_MARGIN_W = 50         # only restore toward MAX when this far under budget

# Fail-safe: if the UPS can't be read this many times in a row we are flying blind,
# so clamp to a conservative per-GPU limit rather than assuming headroom.
POWER_MAX_READ_FAILURES = 3
DEFAULT_POWER_FALLBACK_W = 300      # per-GPU limit when the sensor is unavailable

# NUT ups.status flags that mean "not on mains". On battery we clamp to the floor:
# runtime matters far more than throughput during an outage.
ON_BATTERY_FLAGS = ("OB", "LB")


class UpsReader:
    """Reads total system draw from NUT (`upsc <name>`).

    This UPS (CyberPower CP1500PFCLCDa) does NOT expose `ups.realpower`, only
    `ups.load` as an integer percent of `ups.realpower.nominal` — so watts are
    derived, with nominal/100 resolution. `ups.status` is used to detect a mains
    outage (`OB` = on battery, `LB` = low battery).
    """

    def __init__(self, ups_name: str = DEFAULT_UPS_NAME):
        self.ups_name = ups_name
        self.nominal_w: Optional[int] = None
        self.consecutive_failures = 0

    def _upsc(self) -> Dict[str, str]:
        out = subprocess.run(
            ["upsc", self.ups_name],
            capture_output=True, text=True, timeout=5, check=True,
        ).stdout
        vars_: Dict[str, str] = {}
        for line in out.splitlines():
            if ":" in line:
                k, _, v = line.partition(":")
                vars_[k.strip()] = v.strip()
        return vars_

    def read(self) -> Optional[Tuple[float, bool]]:
        """Return (total_watts, on_battery) or None if the UPS can't be read."""
        try:
            v = self._upsc()
            if self.nominal_w is None:
                self.nominal_w = int(float(v.get("ups.realpower.nominal", 0))) or None
                if self.nominal_w:
                    log.info(f"UPS '{self.ups_name}': {v.get('ups.model','?')} "
                             f"nominal={self.nominal_w} W")
            load_pct = float(v["ups.load"])
            status = v.get("ups.status", "")
            on_battery = any(f in status.split() for f in ON_BATTERY_FLAGS)
            if not self.nominal_w:
                raise ValueError("ups.realpower.nominal missing/zero")
            self.consecutive_failures = 0
            return (load_pct * self.nominal_w / 100.0, on_battery)
        except Exception as e:
            self.consecutive_failures += 1
            log.error(f"UPS read failed ({self.consecutive_failures}): {e}")
            return None


class PowerGovernor:
    """Keeps TOTAL UPS load under `budget` by capping GPU power limits — REACTIVELY.

    The GPUs run at their MAX limit whenever the UPS has headroom; the ceiling is
    pulled down only once load actually SUSTAINS over budget (past a short grace, so a
    brief spike — which the UPS carries on surge/battery for a few seconds — passes
    through untouched). Trades a small bounded overshoot for full GPU throughput
    whenever the UPS isn't genuinely stressed.

    Control law each tick:
        non_gpu = total_ups_watts - sum(gpu draw)
        over budget for >= GRACE ticks  -> throttle toward (budget - non_gpu)/n_gpus
        comfortably under budget        -> restore toward hw_max
        brief overshoot / steady band   -> hold
    then deadband + asymmetric slew (down fast, up gently) before it is applied.

    (Earlier revisions used a PROACTIVE law — per_gpu = (budget-non_gpu)/n EVERY tick —
    which pre-capped the GPUs even at idle. Changed to reactive per operator 2026-08-20:
    brief overshoots are acceptable, only sustained ones warrant a throttle.)

    Attributing the remainder to `non_gpu` rather than modelling the CPU means the loop
    is correct even if OTHER devices share the UPS — the thing protected is the UPS.
    """

    def __init__(self, handles: List, budget_w: float = DEFAULT_POWER_BUDGET,
                 ups_name: str = DEFAULT_UPS_NAME,
                 interval: float = DEFAULT_POWER_INTERVAL,
                 fallback_w: float = DEFAULT_POWER_FALLBACK_W,
                 dry_run: bool = False):
        self.handles = handles
        self.budget_w = budget_w
        self.interval = interval
        self.fallback_w = fallback_w
        self.dry_run = dry_run
        self.ups = UpsReader(ups_name)
        self.min_w: List[float] = []
        self.max_w: List[float] = []
        self.default_w: List[float] = []
        self.applied_w: List[float] = []
        self._last_run = 0.0
        self._was_on_battery = False
        self._over_ticks = 0

    def init(self):
        for i, h in enumerate(self.handles):
            lo, hi = pynvml.nvmlDeviceGetPowerManagementLimitConstraints(h)
            self.min_w.append(lo / 1000.0)
            self.max_w.append(hi / 1000.0)
            self.default_w.append(
                pynvml.nvmlDeviceGetPowerManagementDefaultLimit(h) / 1000.0)
            self.applied_w.append(
                pynvml.nvmlDeviceGetPowerManagementLimit(h) / 1000.0)
            log.info(f"  GPU {i}: power limit range {self.min_w[i]:.0f}-{self.max_w[i]:.0f} W "
                     f"(default {self.default_w[i]:.0f} W, now {self.applied_w[i]:.0f} W)")
        log.info(f"Power budget: {self.budget_w:.0f} W total UPS load"
                 + ("  [DRY RUN — nothing will be set]" if self.dry_run else ""))

    def _set_limit(self, idx: int, watts: float):
        watts = max(self.min_w[idx], min(self.max_w[idx], watts))
        if abs(watts - self.applied_w[idx]) < 1.0:
            return
        if self.dry_run:
            log.info(f"  [dry-run] GPU {idx}: would set limit {watts:.0f} W")
            self.applied_w[idx] = watts
            return
        try:
            pynvml.nvmlDeviceSetPowerManagementLimit(self.handles[idx], int(watts * 1000))
            self.applied_w[idx] = watts
        except pynvml.NVMLError as e:
            log.error(f"GPU {idx}: could not set power limit {watts:.0f} W: {e}")

    def clamp_all(self, watts: float, reason: str):
        log.warning(f"⚠ POWER: clamping all GPUs to {watts:.0f} W — {reason}")
        for i in range(len(self.handles)):
            self._set_limit(i, watts)

    def update(self, force: bool = False):
        now = time.monotonic()
        if not force and (now - self._last_run) < self.interval:
            return
        self._last_run = now

        reading = self.ups.read()
        if reading is None:
            if self.ups.consecutive_failures >= POWER_MAX_READ_FAILURES:
                self.clamp_all(self.fallback_w,
                               f"UPS unreadable x{self.ups.consecutive_failures} (flying blind)")
            return
        total_w, on_battery = reading

        # ── mains lost: runtime beats throughput, floor the cards immediately ──
        if on_battery:
            if not self._was_on_battery:
                self.clamp_all(min(self.min_w), "ON BATTERY — maximising runtime")
                self._was_on_battery = True
            return
        if self._was_on_battery:
            log.info("Mains restored — resuming normal power governing")
            self._was_on_battery = False

        gpu_draw = 0.0
        for i, h in enumerate(self.handles):
            try:
                gpu_draw += pynvml.nvmlDeviceGetPowerUsage(h) / 1000.0
            except pynvml.NVMLError as e:
                log.error(f"GPU {i}: power read failed: {e}")
                return

        non_gpu = max(0.0, total_w - gpu_draw)

        # ── REACTIVE: only pull the ceiling down when load SUSTAINS over budget ──
        over = total_w - self.budget_w
        if over > POWER_DEADBAND_W:
            self._over_ticks += 1
            if self._over_ticks < POWER_OVER_GRACE_TICKS:
                log.info(f"POWER: ups={total_w:.0f}W over budget {self.budget_w:.0f}W by "
                         f"{over:.0f}W (grace {self._over_ticks}/{POWER_OVER_GRACE_TICKS}) — "
                         "letting it pass, limits held "
                         + "/".join(f"{w:.0f}" for w in self.applied_w) + "W")
                return
            # sustained over budget → shed the excess so total settles at ~budget
            target = (self.budget_w - non_gpu) / max(1, len(self.handles))
            mode = "throttle"
        else:
            self._over_ticks = 0
            if total_w < self.budget_w - POWER_RESTORE_MARGIN_W:
                target = max(self.max_w)          # headroom → run free (per-GPU clamp below)
                mode = "restore"
            else:
                log.info(f"POWER: ups={total_w:.0f}W gpu={gpu_draw:.0f}W other={non_gpu:.0f}W "
                         f"budget={self.budget_w:.0f}W -> steady, limits held "
                         + "/".join(f"{w:.0f}" for w in self.applied_w) + "W")
                return

        for i in range(len(self.handles)):
            cur = self.applied_w[i]
            want = max(self.min_w[i], min(self.max_w[i], target))
            delta = want - cur
            if abs(delta) < POWER_DEADBAND_W:
                continue
            if delta < 0:
                want = cur - min(-delta, POWER_SLEW_DOWN_W)
            else:
                want = cur + min(delta, POWER_SLEW_UP_W)
            self._set_limit(i, want)

        log.info(f"POWER: ups={total_w:.0f}W gpu={gpu_draw:.0f}W other={non_gpu:.0f}W "
                 f"budget={self.budget_w:.0f}W -> {mode} "
                 + "/".join(f"{w:.0f}" for w in self.applied_w) + "W")

    def restore_defaults(self):
        if self.dry_run:
            return
        log.info("Restoring default GPU power limits...")
        for i, h in enumerate(self.handles):
            try:
                pynvml.nvmlDeviceSetPowerManagementLimit(h, int(self.default_w[i] * 1000))
                log.info(f"  GPU {i}: restored to {self.default_w[i]:.0f} W")
            except pynvml.NVMLError as e:
                log.error(f"  GPU {i}: could not restore power limit: {e}")


class NvidiaFanController:
    def __init__(self, curve: List[Tuple[int, int]], poll_interval: float = 2.0,
                 sync: bool = True, mirror: bool = False, governor=None):
        self.curve = sorted(curve, key=lambda x: x[0])
        self.poll_interval = poll_interval
        # sync=True (default): ALL fans track the HOTTEST card ("perform as one card").
        # For back-to-back cards this stops an idle neighbour's slow fan from choking
        # the hot card's airflow. sync=False = upstream per-card independent behaviour.
        self.sync = sync
        # mirror=True: NO custom curve at all. Keep the HOTTER card on its own factory
        # (auto) curve, read the speed it chooses, and set the COOLER card to match.
        # Roles swap when the temps cross. The only curve in play is the card's own.
        self.mirror = mirror
        # Optional PowerGovernor. Deliberately driven from THIS loop rather than a
        # second daemon: capping power lowers temperature, so two independent
        # controllers would be reacting to each other's output.
        self.governor = governor
        self.running = False
        self.handles = []
        self.fan_counts = []
        
    def init(self):
        """Initialize NVML and get GPU handles"""
        pynvml.nvmlInit()
        count = pynvml.nvmlDeviceGetCount()
        
        log.info(f"Found {count} NVIDIA GPU(s)")
        
        for i in range(count):
            handle = pynvml.nvmlDeviceGetHandleByIndex(i)
            name = pynvml.nvmlDeviceGetName(handle)
            fan_count = pynvml.nvmlDeviceGetNumFans(handle)
            
            self.handles.append(handle)
            self.fan_counts.append(fan_count)
            
            log.info(f"  GPU {i}: {name} ({fan_count} fans)")
            
            # Enable manual fan control for all fans (mirror mode manages policy
            # per-poll instead — the hotter card stays on auto).
            if not self.mirror:
                for fan_idx in range(fan_count):
                    try:
                        pynvml.nvmlDeviceSetFanControlPolicy(
                            handle, fan_idx, pynvml.NVML_FAN_POLICY_MANUAL
                        )
                    except pynvml.NVMLError as e:
                        log.warning(f"    Could not set manual control for fan {fan_idx}: {e}")
        
        log.info(f"Fan curve: {self.curve}")
        log.info(f"Poll interval: {self.poll_interval}s")

        if self.governor:
            self.governor.handles = self.handles
            self.governor.init()
    
    def get_fan_speed_for_temp(self, temp: int) -> int:
        """Calculate fan speed based on temperature using the curve"""
        if temp <= self.curve[0][0]:
            return self.curve[0][1]
        
        if temp >= self.curve[-1][0]:
            return self.curve[-1][1]
        
        # Linear interpolation between curve points
        for i in range(len(self.curve) - 1):
            t1, s1 = self.curve[i]
            t2, s2 = self.curve[i + 1]
            
            if t1 <= temp <= t2:
                # Linear interpolation
                ratio = (temp - t1) / (t2 - t1)
                return int(s1 + ratio * (s2 - s1))
        
        return self.curve[-1][1]
    
    def update_fans(self):
        """Update fan speeds. sync=True (default): every fan on every GPU tracks the
        HOTTEST card — coordinated cooling so back-to-back cards behave 'as one'.
        sync=False: each card follows its own temperature (upstream behaviour)."""
        # 1. read every GPU's temperature
        temps = {}
        for gpu_idx, handle in enumerate(self.handles):
            try:
                temps[gpu_idx] = pynvml.nvmlDeviceGetTemperature(
                    handle, pynvml.NVML_TEMPERATURE_GPU)
            except pynvml.NVMLError as e:
                log.error(f"GPU {gpu_idx}: Error reading temperature: {e}")
        if not temps:
            return

        # 2. in sync mode, one shared target from the hottest card
        hottest = max(temps.values())
        shared_target = self.get_fan_speed_for_temp(hottest)
        # SAFETY FLOOR — never leave fans low when a card is dangerously hot, whatever
        # the curve says (protects a too-gentle curve like 'native').
        if hottest >= CRITICAL_TEMP:
            shared_target = 100
            log.warning(f"⚠ SAFETY: hottest={hottest}°C >= {CRITICAL_TEMP}°C -> forcing 100% fan")

        # 3. apply
        for gpu_idx, (handle, fan_count) in enumerate(zip(self.handles, self.fan_counts)):
            if gpu_idx not in temps:
                continue
            temp = temps[gpu_idx]
            target_speed = shared_target if self.sync else self.get_fan_speed_for_temp(temp)
            if not self.sync and temp >= CRITICAL_TEMP:
                target_speed = 100  # per-card safety floor in independent mode
            for fan_idx in range(fan_count):
                try:
                    pynvml.nvmlDeviceSetFanSpeed_v2(handle, fan_idx, target_speed)
                except pynvml.NVMLError as e:
                    log.error(f"GPU {gpu_idx} Fan {fan_idx}: Error setting speed: {e}")
            log.info(f"GPU {gpu_idx}: {temp}°C -> {target_speed}%"
                     + (f"  [sync: hottest={hottest}°C]" if self.sync else ""))
    
    def update_fans_mirror(self):
        """Mirror mode: keep the HOTTER card on its own factory (auto) curve, read the
        speed it chooses, and set the COOLER card to match — no custom curve of ours.
        Roles swap when the temps cross. Safety floor still forces 100% above CRITICAL."""
        temps = {}
        for gpu_idx, handle in enumerate(self.handles):
            try:
                temps[gpu_idx] = pynvml.nvmlDeviceGetTemperature(
                    handle, pynvml.NVML_TEMPERATURE_GPU)
            except pynvml.NVMLError as e:
                log.error(f"GPU {gpu_idx}: Error reading temperature: {e}")
        if len(temps) < 2:
            return  # nothing to mirror with fewer than 2 GPUs

        hotter = max(temps, key=temps.get)
        cooler = min(temps, key=temps.get)

        # SAFETY FLOOR: both cards to 100% (manual) if the hotter card is critically hot
        if temps[hotter] >= CRITICAL_TEMP:
            for gi in (hotter, cooler):
                for fi in range(self.fan_counts[gi]):
                    try:
                        pynvml.nvmlDeviceSetFanControlPolicy(self.handles[gi], fi, pynvml.NVML_FAN_POLICY_MANUAL)
                        pynvml.nvmlDeviceSetFanSpeed_v2(self.handles[gi], fi, 100)
                    except pynvml.NVMLError as e:
                        log.error(f"GPU {gi} Fan {fi}: {e}")
            log.warning(f"⚠ SAFETY: hottest={temps[hotter]}°C >= {CRITICAL_TEMP}°C -> both fans 100%")
            return

        # hotter card: back on its OWN factory curve (auto), so it picks its native speed
        for fi in range(self.fan_counts[hotter]):
            try:
                pynvml.nvmlDeviceSetFanControlPolicy(
                    self.handles[hotter], fi, pynvml.NVML_FAN_POLICY_TEMPERATURE_CONTINOUS_SW)
            except pynvml.NVMLError as e:
                log.error(f"GPU {hotter} Fan {fi}: {e}")

        try:
            native_fan = pynvml.nvmlDeviceGetFanSpeed_v2(self.handles[hotter], 0)
        except pynvml.NVMLError as e:
            log.error(f"GPU {hotter}: read fan failed: {e}")
            return

        # cooler card: manual, mirror the hotter card's native fan (never below its own —
        # identical cards + monotonic curve mean the hotter temp always demands >= fan)
        for fi in range(self.fan_counts[cooler]):
            try:
                pynvml.nvmlDeviceSetFanControlPolicy(self.handles[cooler], fi, pynvml.NVML_FAN_POLICY_MANUAL)
                pynvml.nvmlDeviceSetFanSpeed_v2(self.handles[cooler], fi, native_fan)
            except pynvml.NVMLError as e:
                log.error(f"GPU {cooler} Fan {fi}: {e}")

        log.info(f"mirror: GPU{hotter}(hot,auto) {temps[hotter]}°C fan={native_fan}% "
                 f"-> GPU{cooler}(cool) {temps[cooler]}°C set {native_fan}%")

    def restore_auto_control(self):
        """Restore automatic fan control on all GPUs"""
        log.info("Restoring automatic fan control...")
        for gpu_idx, (handle, fan_count) in enumerate(zip(self.handles, self.fan_counts)):
            for fan_idx in range(fan_count):
                try:
                    pynvml.nvmlDeviceSetFanControlPolicy(
                        handle, fan_idx, 
                        pynvml.NVML_FAN_POLICY_TEMPERATURE_CONTINOUS_SW
                    )
                    log.info(f"  GPU {gpu_idx} Fan {fan_idx}: Restored to auto")
                except pynvml.NVMLError as e:
                    log.error(f"  GPU {gpu_idx} Fan {fan_idx}: Could not restore: {e}")
    
    def run(self):
        """Main control loop"""
        self.running = True
        log.info("Fan control daemon started.")
        
        try:
            while self.running:
                if self.mirror:
                    self.update_fans_mirror()
                else:
                    self.update_fans()
                if self.governor:
                    self.governor.update()   # no-ops until its own interval elapses
                time.sleep(self.poll_interval)
        except KeyboardInterrupt:
            log.info("Interrupted by user")
        finally:
            if self.governor:
                self.governor.restore_defaults()
            self.restore_auto_control()
            pynvml.nvmlShutdown()
            log.info("Fan control daemon stopped.")
    
    def stop(self):
        """Stop the control loop"""
        self.running = False


def main():
    parser = argparse.ArgumentParser(
        description="Aggressive NVIDIA GPU Fan Control for Headless Systems"
    )
    parser.add_argument(
        "--mode", "-m",
        choices=["native", "quiet", "aggressive", "performance", "max"],
        default="quiet",
        help="Fan curve: native (match stock, just sync), quiet (default), aggressive, "
             "performance, or max (100%% always)"
    )
    parser.add_argument(
        "--interval", "-i",
        type=float,
        default=2.0,
        help="Poll interval in seconds (default: 2.0)"
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Set fans once and exit (don't run as daemon)"
    )
    parser.add_argument(
        "--independent",
        action="store_true",
        help="Each card follows its OWN temperature (upstream behaviour). Default is "
             "sync: every fan tracks the hottest card ('perform as one card')."
    )
    parser.add_argument(
        "--power-budget",
        type=float,
        default=None,
        metavar="WATTS",
        help=f"Enable the power governor: cap GPU power limits so TOTAL UPS load stays "
             f"under WATTS (e.g. --power-budget {DEFAULT_POWER_BUDGET:.0f}). Reads total "
             f"draw from NUT. Off unless specified."
    )
    parser.add_argument(
        "--ups",
        default=DEFAULT_UPS_NAME,
        help=f"NUT UPS name for the power governor (default: {DEFAULT_UPS_NAME}; see `upsc -l`)"
    )
    parser.add_argument(
        "--power-interval",
        type=float,
        default=DEFAULT_POWER_INTERVAL,
        help=f"Seconds between power-governor updates (default: {DEFAULT_POWER_INTERVAL}). "
             f"The NUT driver only refreshes every ~2 s, so going below that buys nothing."
    )
    parser.add_argument(
        "--power-fallback",
        type=float,
        default=DEFAULT_POWER_FALLBACK_W,
        metavar="WATTS",
        help=f"Per-GPU limit to clamp to if the UPS becomes unreadable "
             f"(default: {DEFAULT_POWER_FALLBACK_W:.0f} W)"
    )
    parser.add_argument(
        "--power-dry-run",
        action="store_true",
        help="Power governor logs what it WOULD set without touching the GPUs. "
             "Use this first to sanity-check the budget against real load."
    )
    parser.add_argument(
        "--mirror",
        action="store_true",
        help="No custom curve: keep the HOTTER card on its own factory (auto) curve, read "
             "the fan it picks, and mirror it onto the cooler card. Overrides --mode/--independent."
    )

    args = parser.parse_args()
    
    curves = {
        "native": NATIVE_CURVE,
        "quiet": QUIET_CURVE,
        "aggressive": AGGRESSIVE_FAN_CURVE,
        "performance": PERFORMANCE_FAN_CURVE,
        "max": MAX_COOLING_CURVE,
    }
    
    curve = curves[args.mode]
    mode_desc = ("MIRROR (hotter card's own curve, mirrored onto cooler)" if args.mirror
                 else "INDEPENDENT (per-card)" if args.independent
                 else "SYNC (all fans = hottest card)")
    log.info(f"NVIDIA Fan Control - Curve: {args.mode.upper()} - {mode_desc}")
    log.info("=" * 50)

    governor = None
    if args.power_budget is not None:
        governor = PowerGovernor(
            handles=[],                      # filled in by controller.init()
            budget_w=args.power_budget,
            ups_name=args.ups,
            interval=args.power_interval,
            fallback_w=args.power_fallback,
            dry_run=args.power_dry_run,
        )
        log.info(f"Power governor ENABLED — budget {args.power_budget:.0f} W via UPS '{args.ups}'")

    controller = NvidiaFanController(curve, args.interval,
                                     sync=not args.independent, mirror=args.mirror,
                                     governor=governor)
    
    # Handle signals for clean shutdown
    def signal_handler(sig, frame):
        log.info(f"Received signal {sig}")
        controller.stop()
    
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)
    
    controller.init()
    
    if args.once:
        controller.update_fans_mirror() if args.mirror else controller.update_fans()
        if governor:
            governor.update(force=True)
        log.info("Ran once. Fans will return to auto control after a few minutes.")
    else:
        controller.run()


if __name__ == "__main__":
    main()
