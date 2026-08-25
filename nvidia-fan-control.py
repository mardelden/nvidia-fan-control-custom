#!/usr/bin/env python3
"""
Aggressive NVIDIA GPU Fan Control Daemon for Headless GPUs
Designed for high-power AI workloads on RTX PRO 6000 cards

Run as: sudo python3 nvidia-fan-control.py
Or install as a systemd service
"""

import pynvml
import os
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
CRITICAL_TEMP = 92

# Optional temperature-target mode. Unlike --mirror (which hands the hot card back
# and forth between factory auto and our emergency override), this owns both
# cards' fans continuously and treats them as one thermal zone. Demand rises along a
# target-relative approach band, rises quickly, and falls slowly to avoid fan hunting.
TARGET_FAN_MIN_PCT = 30
TARGET_FAN_APPROACH_BAND_C = 20
TARGET_FAN_SLEW_UP_PCT = 10
TARGET_FAN_SLEW_DOWN_PCT = 2

# The linear target-relative curve is a safe feed-forward starting point, not the
# final command. An integral trim learns how much less fan this particular chassis
# needs to sit AT the requested temperature. Learn downward slowly while below the
# target, freeze at the setpoint, and unwind quickly if temperature rises above it.
TARGET_TRACKING_BAND_C = 8
TARGET_TRIM_MIN_PCT = -50.0
TARGET_TRIM_DOWN_PCT_PER_C_S = 0.125
TARGET_TRIM_UP_PCT_PER_C_S = 2.5

# If maximum fans cannot hold the target, cooling has run out of actuator authority.
# Derate the existing common power ceiling instead of letting temperature oscillate
# at the emergency boundary. Recovery is deliberately slower than derating.
THERMAL_POWER_MARGIN_C = 2
THERMAL_POWER_INITIAL_DWELL_S = 15.0
THERMAL_POWER_REPEAT_DWELL_S = 5.0
THERMAL_POWER_MIN_STEP_W = 20.0
THERMAL_POWER_MAX_STEP_W = 150.0
THERMAL_POWER_W_PER_EXCESS_C = 10.0
THERMAL_POWER_RECOVER_MARGIN_C = 2
THERMAL_POWER_RECOVER_DWELL_S = 30.0


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
# its own time constant, so a naive proportional loop will hunt. Downward changes
# shed the measured whole-system excess (bounded for safety); upward changes require
# sustained headroom and are deliberately small.
POWER_DEADBAND_W = 15               # ignore changes smaller than this
POWER_SLEW_DOWN_W = 150             # max decrease per update (react fast)
POWER_SLEW_UP_W = 20                # max increase after a sustained-headroom dwell

# Reactive law: leave GPUs at MAX while the UPS has headroom; throttle once load goes over
# budget. Default reacts on the FIRST over-budget tick (grace=1, the minimum). A truly brief
# spike still passes: the UPS sensor is ~2 s coarse so a sub-2 s transient never registers, and
# the down-slew is bounded (150 W/tick), so the first throttle step is gentle regardless. Raise
# POWER_OVER_GRACE_TICKS to also ride out LONGER sustained overshoots before reacting.
POWER_OVER_GRACE_TICKS = 1          # throttle on the first over-budget tick (min; sub-2s spikes pass via sensor coarseness)
POWER_RESTORE_MARGIN_W = 50         # only restore toward MAX when this far under budget (hysteresis)
POWER_RESTORE_HEADROOM_TICKS = 3    # require repeated under-budget observations before raising a learned cap
POWER_RESTORE_DWELL_S = 30.0        # minimum time between upward cap changes
POWER_IDLE_DRAW_W = 75.0            # per-GPU board-power ceiling for considering a card idle
POWER_IDLE_UTIL_PCT = 5             # utilization ceiling for considering a card idle
POWER_IDLE_DWELL_S = 60.0           # all cards must stay idle this long before resetting caps to MAX

# Fail-safe: if the UPS can't be read this many times in a row we are flying blind,
# so clamp to a conservative per-GPU limit rather than assuming headroom.
POWER_MAX_READ_FAILURES = 3
DEFAULT_POWER_FALLBACK_W = 300      # per-GPU limit when the sensor is unavailable

# After a downward limit step, do not act again on the same cached UPS sample.
# pve-ai's CyberPower held a stale 1030 W value for 36 s after load disappeared;
# without this gate, falling GPU draw was misattributed as rising non-GPU load.
POWER_FEEDBACK_TIMEOUT_S = 45.0

# NUT ups.status flags that immediately clamp GPUs to their hardware floor.
# Conservative default preserves the original behavior; hosts can configure only
# OB when LB merely reflects low estimated runtime while the UPS remains OL.
DEFAULT_POWER_FLOOR_FLAGS = ("OB", "LB")

# ── POWER CEILING ──
# Operator-set upper actuation bound. Replaces the card's hardware max as the value
# the governor restores toward, so power can be pinned at a chosen wattage (e.g. for
# apples-to-apples benchmarking) WITHOUT stopping the daemon and losing fan control,
# thermal derating and the UPS safety loop. Works with or without a UPS: with a budget
# the governor still throttles BELOW the ceiling under UPS or thermal pressure and
# restores up to (never above) it; with no budget it simply holds the cap.
#
# NVML power limits do not survive a reboot, so persistence is re-application at
# startup from this file. The same file doubles as the live control file: it is polled
# every update() and SIGHUP forces an immediate re-read, so a benchmark can pin/unpin
# without restarting the service.
DEFAULT_POWER_CEILING_FILE = "/var/lib/nvidia-fan-control/power-ceiling"


def parse_power_floor_flags(value: str) -> Tuple[str, ...]:
    """Parse a comma-separated, non-empty set of NUT ups.status tokens."""
    flags = tuple(dict.fromkeys(part.strip().upper() for part in value.split(",")
                                if part.strip()))
    if not flags:
        raise argparse.ArgumentTypeError("expected at least one NUT status flag (for example: OB)")
    invalid = [flag for flag in flags if not flag.isalpha()]
    if invalid:
        raise argparse.ArgumentTypeError(
            "NUT status flags must contain letters only: " + ",".join(invalid))
    return flags


def parse_power_ceiling(value: str) -> List[float]:
    """Parse a power ceiling: `WATTS`, `WATTS,WATTS,...` (per-GPU), or `none` to unpin.

    Returns [] for an explicit unpin, so callers can tell "operator asked for no
    ceiling" apart from "flag absent" (argparse default None).
    """
    text = value.strip().lower()
    if text in ("", "none", "off", "0"):
        return []
    out: List[float] = []
    for part in text.split(","):
        part = part.strip()
        try:
            watts = float(part)
        except ValueError:
            raise argparse.ArgumentTypeError(
                f"expected watts or a comma-separated per-GPU list, got {part!r}")
        if watts <= 0:
            raise argparse.ArgumentTypeError(f"power ceiling must be > 0 W, got {watts:g}")
        out.append(watts)
    return out


def read_ceiling_file(path: str) -> Tuple[bool, Optional[List[float]]]:
    """Read the ceiling control/state file.

    Returns (ok, request). ok=False means "no usable value — keep whatever ceiling is
    already active": a missing file, an unreadable one, or garbage. A benchmark script
    typo must never silently un-pin the cards. ok=True with request=None is an
    explicit unpin; ok=True with a list is a ceiling in watts.
    """
    try:
        with open(path) as f:
            raw = f.read()
    except FileNotFoundError:
        return (False, None)
    except OSError as e:
        log.warning(f"⚠ POWER: cannot read ceiling file {path}: {e}")
        return (False, None)
    raw = raw.split("#", 1)[0].strip()   # allow operators to annotate the file
    try:
        request = parse_power_ceiling(raw)
    except argparse.ArgumentTypeError as e:
        log.error(f"POWER: ignoring malformed ceiling file {path}: {e}")
        return (False, None)
    return (True, request or None)


def parse_temp_target(value: str) -> int:
    """Parse a useful GPU temperature target below the hard safety boundary."""
    try:
        target = int(value)
    except ValueError as e:
        raise argparse.ArgumentTypeError("temperature target must be an integer") from e
    if not 50 <= target < CRITICAL_TEMP:
        raise argparse.ArgumentTypeError(
            f"temperature target must be 50..{CRITICAL_TEMP - 1} C")
    return target


class UpsReader:
    """Reads total system draw from NUT (`upsc <name>`).

    This UPS (CyberPower CP1500PFCLCDa) does NOT expose `ups.realpower`, only
    `ups.load` as an integer percent of `ups.realpower.nominal` — so watts are
    derived, with nominal/100 resolution. The raw `ups.status` tokens are returned
    to the governor, which decides which configured flags require an immediate floor.
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

    def read(self) -> Optional[Tuple[float, Tuple[str, ...]]]:
        """Return (total_watts, status_flags) or None if the UPS can't be read."""
        try:
            v = self._upsc()
            if self.nominal_w is None:
                self.nominal_w = int(float(v.get("ups.realpower.nominal", 0))) or None
                if self.nominal_w:
                    log.info(f"UPS '{self.ups_name}': {v.get('ups.model','?')} "
                             f"nominal={self.nominal_w} W")
            load_pct = float(v["ups.load"])
            status_flags = tuple(v.get("ups.status", "").split())
            if not self.nominal_w:
                raise ValueError("ups.realpower.nominal missing/zero")
            self.consecutive_failures = 0
            return (load_pct * self.nominal_w / 100.0, status_flags)
        except Exception as e:
            self.consecutive_failures += 1
            log.error(f"UPS read failed ({self.consecutive_failures}): {e}")
            return None


class PowerGovernor:
    """Keeps TOTAL UPS load under `budget` by capping GPU power limits — REACTIVELY.

    An operator-set POWER CEILING (see the ceiling machinery below) replaces the
    hardware max as the upper actuation bound, so "MAX" below means the ceiling whenever
    one is set. The control law is otherwise unchanged: UPS and thermal pressure still
    throttle BELOW the ceiling, and recovery restores up to — never above — it. With
    budget_w=None there is no UPS at all and the governor degenerates to holding the
    ceiling, while thermal derating keeps working.

    New workloads start at MAX. The ceiling is pulled down as soon as load goes over
    budget (on the FIRST over-budget tick by default), then retained as a learned cap
    while work remains active. A truly brief spike still passes: the ~2 s coarse UPS
    sensor cannot see it and the down-slew is bounded.

    Control law each tick:
        over budget for >= GRACE ticks  -> common_cap -= measured UPS excess
        near budget                     -> hold the learned common cap
        sustained headroom              -> common_cap += a small restore step
        all GPUs idle for a dwell        -> reset every cap directly to hardware MAX

    The excess-based adjustment matters when only one of several GPUs is active. The
    common-cap reduction is `UPS excess / active GPU count`; idle cards retain the same
    ceiling but do not dilute the corrective step. Remembering the resulting safe ceiling
    also prevents the old 600 -> 450 -> 600 limit wave.

    (Earlier revisions used a PROACTIVE law — per_gpu = (budget-non_gpu)/n EVERY tick —
    which pre-capped the GPUs even at idle. Changed to reactive per operator 2026-08-20:
    brief overshoots are acceptable, only sustained ones warrant a throttle.)

    `non_gpu` remains a diagnostic derived from the UPS and aggregate board draw; the
    control error itself comes directly from whole-system UPS watts.
    """

    def __init__(self, handles: List, budget_w: Optional[float] = None,
                 ups_name: str = DEFAULT_UPS_NAME,
                 interval: float = DEFAULT_POWER_INTERVAL,
                 fallback_w: float = DEFAULT_POWER_FALLBACK_W,
                 floor_on_flags: Tuple[str, ...] = DEFAULT_POWER_FLOOR_FLAGS,
                 dry_run: bool = False,
                 ceiling_request: Optional[List[float]] = None,
                 ceiling_path: Optional[str] = None,
                 persist_startup_ceiling: bool = False,
                 ceiling_source: str = "startup"):
        self.handles = handles
        # None => no UPS sensor available; ceiling-only ("hold") mode. Thermal derating
        # still runs, since it uses fan% and temperature rather than the UPS.
        self.budget_w = budget_w
        self.interval = interval
        self.fallback_w = fallback_w
        self.floor_on_flags = tuple(flag.upper() for flag in floor_on_flags)
        self.dry_run = dry_run
        self.ups = UpsReader(ups_name)
        self.min_w: List[float] = []
        # hw_max_w is the card's own constraint; max_w is the EFFECTIVE upper bound
        # (hardware max, or the ceiling when one is set). Every write clamps to max_w.
        self.hw_max_w: List[float] = []
        self.max_w: List[float] = []
        self.default_w: List[float] = []
        self.applied_w: List[float] = []
        self._last_run = 0.0
        self._was_power_floor = False
        self._over_ticks = 0
        self._feedback_wait_total_w: Optional[float] = None
        self._feedback_wait_status_flags: Tuple[str, ...] = ()
        self._feedback_wait_since = 0.0
        self._headroom_ticks = 0
        self._last_limit_change = 0.0
        self._idle_since: Optional[float] = None
        self.learned_cap_w: Optional[float] = None
        self._last_ups_reading: Optional[Tuple[float, Tuple[str, ...]]] = None
        self._thermal_hot_since: Optional[float] = None
        self._thermal_cool_since: Optional[float] = None
        self._last_thermal_step = 0.0
        self._thermal_limited = False
        # Ceiling. ceiling_request is what was ASKED for (one value = broadcast to all
        # GPUs, or one per GPU); ceiling_w is the per-GPU value actually in force after
        # clamping into the hardware range. Both None when unpinned.
        self.ceiling_request: Optional[List[float]] = ceiling_request or None
        self.ceiling_w: Optional[List[float]] = None
        self.ceiling_path = ceiling_path
        self.ceiling_source = ceiling_source
        self.persist_startup_ceiling = persist_startup_ceiling
        self._ceiling_stamp: Optional[Tuple[int, int]] = None
        self._reload_requested = False
        self._hold_log_pending = True

    def init(self):
        for i, h in enumerate(self.handles):
            lo, hi = pynvml.nvmlDeviceGetPowerManagementLimitConstraints(h)
            self.min_w.append(lo / 1000.0)
            self.hw_max_w.append(hi / 1000.0)
            self.default_w.append(
                pynvml.nvmlDeviceGetPowerManagementDefaultLimit(h) / 1000.0)
            self.applied_w.append(
                pynvml.nvmlDeviceGetPowerManagementLimit(h) / 1000.0)
            log.info(f"  GPU {i}: power limit range {self.min_w[i]:.0f}-{self.hw_max_w[i]:.0f} W "
                     f"(default {self.default_w[i]:.0f} W, now {self.applied_w[i]:.0f} W)")
        self.max_w = list(self.hw_max_w)

        dry = "  [DRY RUN — nothing will be set]" if self.dry_run else ""
        if self.budget_w is None:
            log.info("Power budget: DISABLED (no UPS sensor) — ceiling-only mode" + dry)
        else:
            log.info(f"Power budget: {self.budget_w:.0f} W total UPS load" + dry)
            log.info("Immediate power-floor UPS flags: " + ",".join(self.floor_on_flags))

        # Applied BEFORE learned_cap_w is seeded, so the learned common cap starts at the
        # ceiling rather than at the pre-ceiling hardware limit. This is also what makes a
        # ceiling survive a reboot: NVML limits reset to the card default at boot, so the
        # daemon re-applies the persisted request here.
        if self.ceiling_request is not None:
            self._apply_ceiling(self.ceiling_request, self.ceiling_source)
        else:
            log.info("Power ceiling: none — upper limit is the card hardware max "
                     + "/".join(f"{w:.0f}" for w in self.hw_max_w) + " W")
            if self.budget_w is None:
                self._release_to_default(self.ceiling_source)
        if self.persist_startup_ceiling:
            self._write_ceiling_file()
        self._ceiling_stamp = self._ceiling_file_stamp()
        if self.ceiling_path:
            log.info(f"Power ceiling control file: {self.ceiling_path} "
                     "(write watts or 'none'; SIGHUP re-reads immediately)")
        if self.budget_w is not None and self.ceiling_w and self.fallback_w > min(self.ceiling_w):
            log.info(f"  note: --power-fallback {self.fallback_w:.0f} W sits above the "
                     "ceiling and will be clamped to it when the UPS is unreadable")

        self.learned_cap_w = min(self.applied_w) if self.applied_w else None
        self._last_limit_change = time.monotonic()

        # Ceiling-only mode has no sensor to wait for — land on the cap immediately.
        if self.budget_w is None:
            self._hold_ceiling()

    # ───────────────────────── ceiling machinery ─────────────────────────
    # The ceiling is applied by SHRINKING max_w, never by clamping at the decision
    # sites: _set_limit() and every control path already bound writes to max_w, so one
    # assignment caps the whole actuator surface — the UPS restore branch, the idle
    # reset, the thermal derate, --power-fallback and the floor-flag clamp. Any future
    # code that sets a power limit MUST go through _set_limit() to inherit the cap.

    def _apply_ceiling(self, request: Optional[List[float]], source: str):
        """Make `request` the active ceiling. None unpins (back to the hardware max)."""
        n = len(self.handles)
        if request is None:
            if self.ceiling_request is None and self.ceiling_w is None:
                return
            self.ceiling_request = None
            self.ceiling_w = None
            self.max_w = list(self.hw_max_w)
            self._hold_log_pending = True
            log.info(f"POWER: ceiling cleared ({source}) — upper limit back to hardware max "
                     + "/".join(f"{w:.0f}" for w in self.hw_max_w) + " W")
            # Let the UPS restore branch reconsider promptly rather than sitting out a
            # 30 s dwell that was armed by the (now irrelevant) ceiling change.
            self._headroom_ticks = 0
            if self.budget_w is None:
                self._release_to_default(source)
            return

        if len(request) == 1:
            wanted = [request[0]] * n
        elif len(request) == n:
            wanted = list(request)
        else:
            log.error(f"POWER: ignoring ceiling from {source}: got {len(request)} values "
                      f"for {n} GPU(s) — pass one value or one per GPU. Keeping "
                      + (("/".join(f"{w:.0f}" for w in self.ceiling_w) + " W")
                         if self.ceiling_w else "no ceiling"))
            return

        effective: List[float] = []
        for i, want in enumerate(wanted):
            capped = max(self.min_w[i], min(self.hw_max_w[i], want))
            if abs(capped - want) >= 1.0:
                log.warning(f"⚠ POWER: GPU {i} ceiling {want:.0f} W is outside the hardware "
                            f"range {self.min_w[i]:.0f}-{self.hw_max_w[i]:.0f} W — using "
                            f"{capped:.0f} W")
            effective.append(capped)

        self.ceiling_request = list(request)
        self.ceiling_w = effective
        self.max_w = list(effective)
        self._hold_log_pending = True
        log.info("POWER: ceiling set to " + "/".join(f"{w:.0f}" for w in effective)
                 + f" W per GPU ({source}; hardware max "
                 + "/".join(f"{w:.0f}" for w in self.hw_max_w) + " W)")

        # A lowered ceiling takes effect NOW — it is a safety bound, not a control
        # target, so it is never slew-limited. Raising it is left to the normal restore
        # path so the UPS still supervises the way back up.
        for i in range(n):
            if self.applied_w[i] > self.max_w[i] + 0.5:
                self._set_limit(i, self.max_w[i])
        # The learned common cap must never sit above the ceiling, or the restore branch
        # would keep targeting a value the ceiling forbids.
        if self.learned_cap_w is not None:
            self.learned_cap_w = min(self.learned_cap_w, min(self.max_w))
        if self.budget_w is None:
            self._hold_ceiling()

    def _release_to_default(self, source: str):
        """Hand the cards back to their default limit.

        Only used in ceiling-only mode. With a UPS budget the restore branch walks the
        limits back up under supervision, but with no sensor nothing else ever raises a
        limit — so without this an unpin would silently leave the GPUs parked at the
        old cap forever, which is the opposite of what the operator asked for.
        """
        for i in range(len(self.handles)):
            if abs(self.applied_w[i] - self.default_w[i]) >= 1.0:
                self._set_limit(i, self.default_w[i])
        if self.applied_w:
            self.learned_cap_w = min(self.applied_w)
        log.info(f"POWER: no ceiling and no UPS budget ({source}) — GPUs returned to their "
                 "default limit " + "/".join(f"{w:.0f}" for w in self.applied_w) + "W")

    def _hold_ceiling(self):
        """Ceiling-only mode: park every GPU on its cap. Idempotent, logs on change."""
        if self.ceiling_w is None:
            return
        # Thermal derating owns the cap while it is engaged. Pushing back up to the
        # ceiling here would undo the derate every tick and oscillate against the
        # thermal loop; observe_thermal() clears the hold once the card has cooled.
        if self._thermal_limited and self.learned_cap_w is not None:
            targets = [min(self.ceiling_w[i], self.learned_cap_w) for i in range(len(self.handles))]
        else:
            targets = list(self.ceiling_w)
        before = tuple(self.applied_w)
        for i in range(len(self.handles)):
            self._set_limit(i, targets[i])
        if self.applied_w:
            self.learned_cap_w = min(self.applied_w)
        if tuple(self.applied_w) != before or self._hold_log_pending:
            log.info("POWER: holding ceiling (no UPS sensor), limits "
                     + "/".join(f"{w:.0f}" for w in self.applied_w) + "W"
                     + ("  [thermal derate active]" if self._thermal_limited else ""))
            self._hold_log_pending = False

    def _ceiling_file_stamp(self) -> Optional[Tuple[int, int]]:
        if not self.ceiling_path:
            return None
        try:
            st = os.stat(self.ceiling_path)
            return (st.st_mtime_ns, st.st_size)
        except FileNotFoundError:
            return None
        except OSError:
            return self._ceiling_stamp     # transient stat error: assume unchanged

    def _refresh_ceiling_from_file(self, force: bool = False):
        """Pick up live edits to the control file. Cheap enough to run every tick."""
        if not self.ceiling_path:
            return
        stamp = self._ceiling_file_stamp()
        if not force and stamp == self._ceiling_stamp:
            return
        self._ceiling_stamp = stamp
        if stamp is None:
            self._apply_ceiling(None, "control file removed")
            return
        ok, request = read_ceiling_file(self.ceiling_path)
        if not ok:
            return                          # garbage/unreadable: keep the active cap
        if request != self.ceiling_request:
            self._apply_ceiling(request, f"control file {self.ceiling_path}")

    def _write_ceiling_file(self):
        """Persist the active ceiling so it can be re-applied after a reboot."""
        if not self.ceiling_path or self.dry_run:
            return
        text = ("none" if self.ceiling_request is None
                else ",".join(f"{w:.0f}" for w in self.ceiling_request))
        tmp = self.ceiling_path + ".tmp"
        try:
            os.makedirs(os.path.dirname(self.ceiling_path) or ".", exist_ok=True)
            with open(tmp, "w") as f:
                f.write(text + "\n")
            os.replace(tmp, self.ceiling_path)
            self._ceiling_stamp = self._ceiling_file_stamp()
        except OSError as e:
            log.warning(f"⚠ POWER: could not persist ceiling to {self.ceiling_path}: {e} "
                        "— the ceiling is active but will NOT survive a restart")

    def _enforce_ceiling(self):
        """Re-assert the cap against out-of-band changes, e.g. a manual `nvidia-smi -pl`.

        applied_w is a cache of what THIS daemon last wrote, so _set_limit()'s
        "already there" early-return is blind to anyone else moving the limit. A ceiling
        is a guarantee, not a preference: if it is in force it has to hold regardless of
        who changed the limit, so re-read the hardware and correct upward violations.
        Downward external changes are left alone — the control loop already owns those.
        """
        if self.ceiling_w is None or self.dry_run:
            return
        for i, h in enumerate(self.handles):
            try:
                actual = pynvml.nvmlDeviceGetPowerManagementLimit(h) / 1000.0
            except pynvml.NVMLError as e:
                log.error(f"GPU {i}: power limit read failed: {e}")
                continue
            if actual > self.max_w[i] + 0.5:
                log.warning(f"⚠ POWER: GPU {i} limit is {actual:.0f} W, above the "
                            f"{self.max_w[i]:.0f} W ceiling — changed out of band; re-applying")
                self.applied_w[i] = actual        # resync so _set_limit actually writes
                self._set_limit(i, self.max_w[i])

    def request_reload(self):
        """SIGHUP: re-read the control file on the next loop iteration."""
        self._reload_requested = True

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
        self.learned_cap_w = min(self.applied_w) if self.applied_w else None
        self._last_limit_change = time.monotonic()
        self._headroom_ticks = 0
        self._idle_since = None

    def _read_gpu_power_and_activity(self) -> Optional[Tuple[float, int]]:
        """Return aggregate board draw and a conservative active-GPU count.

        A failed utilization read is treated as active: it is safer to retain a learned
        cap than to reset to hardware maximum when we cannot prove that every card is idle.
        """
        total_draw = 0.0
        active_count = 0
        for i, h in enumerate(self.handles):
            try:
                draw_w = pynvml.nvmlDeviceGetPowerUsage(h) / 1000.0
                util_pct = pynvml.nvmlDeviceGetUtilizationRates(h).gpu
            except pynvml.NVMLError as e:
                log.error(f"GPU {i}: power/utilization read failed: {e}")
                return None
            total_draw += draw_w
            if draw_w > POWER_IDLE_DRAW_W or util_pct > POWER_IDLE_UTIL_PCT:
                active_count += 1
        return total_draw, active_count

    def observe_thermal(self, hottest_c: int, fan_pct: int, target_c: int):
        """Derate the common cap when maximum cooling cannot hold the target.

        Fan control is the first actuator. Power only falls after the fan has reached
        100% and temperature reaches THERMAL_POWER_MARGIN_C above target for
        a dwell. A thermal reduction arms the same fresh-UPS-feedback gate as an UPS
        reduction, preventing the two control inputs from reacting twice to one stale
        whole-system reading.
        """
        if self.learned_cap_w is None or not self.min_w:
            return
        now = time.monotonic()
        thermally_over = (
            fan_pct >= 100
            and hottest_c >= target_c + THERMAL_POWER_MARGIN_C
        )

        if thermally_over:
            self._thermal_cool_since = None
            if self._thermal_hot_since is None:
                self._thermal_hot_since = now
                return
            hot_for = now - self._thermal_hot_since
            required_dwell = (
                THERMAL_POWER_REPEAT_DWELL_S
                if self._thermal_limited else THERMAL_POWER_INITIAL_DWELL_S
            )
            if (hot_for < required_dwell
                    or (self._thermal_limited
                        and now - self._last_thermal_step < required_dwell)):
                return

            before = tuple(self.applied_w)
            excess_c = max(
                0, hottest_c - (target_c + THERMAL_POWER_MARGIN_C))
            step_w = max(
                THERMAL_POWER_MIN_STEP_W,
                min(THERMAL_POWER_MAX_STEP_W,
                    excess_c * THERMAL_POWER_W_PER_EXCESS_C),
            )
            new_cap = max(min(self.min_w), self.learned_cap_w - step_w)
            self.learned_cap_w = new_cap
            for i in range(len(self.handles)):
                self._set_limit(i, new_cap)
            self._thermal_limited = True
            self._thermal_hot_since = now
            self._last_thermal_step = now
            self._last_limit_change = now
            if tuple(self.applied_w) != before and self._last_ups_reading is not None:
                total_w, status_flags = self._last_ups_reading
                self._feedback_wait_total_w = total_w
                self._feedback_wait_status_flags = status_flags
                self._feedback_wait_since = now
            log.warning(
                f"⚠ THERMAL: {hottest_c}C at {fan_pct}% fan for {hot_for:.0f}s "
                f"(target {target_c}C, excess {excess_c}C) -> "
                f"-{step_w:.0f} W, learned power cap {new_cap:.0f} W")
            return

        self._thermal_hot_since = None
        if not self._thermal_limited:
            self._thermal_cool_since = None
            return

        if hottest_c <= target_c - THERMAL_POWER_RECOVER_MARGIN_C:
            if self._thermal_cool_since is None:
                self._thermal_cool_since = now
                return
            cool_for = now - self._thermal_cool_since
            if cool_for >= THERMAL_POWER_RECOVER_DWELL_S:
                self._thermal_limited = False
                self._thermal_cool_since = None
                log.info(
                    f"THERMAL: {hottest_c}C <= {target_c - THERMAL_POWER_RECOVER_MARGIN_C}C "
                    f"for {cool_for:.0f}s -> thermal hold cleared; UPS recovery may resume")
        else:
            self._thermal_cool_since = None

    def update(self, force: bool = False):
        now = time.monotonic()

        # Polled every call rather than every governor interval: a stat() is far cheaper
        # than the UPS read, and it lets a benchmark pin/unpin within one fan poll
        # instead of waiting out --power-interval.
        if self._reload_requested:
            self._reload_requested = False
            self._refresh_ceiling_from_file(force=True)
        else:
            self._refresh_ceiling_from_file()

        if not force and (now - self._last_run) < self.interval:
            return
        self._last_run = now

        self._enforce_ceiling()

        # Ceiling-only mode: no UPS to read and no budget to defend, so there is nothing
        # to throttle against — just keep the cards parked on the cap. Thermal derating
        # still runs; it is driven from the fan loop via observe_thermal(), not here.
        if self.budget_w is None:
            self._hold_ceiling()
            return

        reading = self.ups.read()
        if reading is None:
            if self.ups.consecutive_failures >= POWER_MAX_READ_FAILURES:
                self.clamp_all(self.fallback_w,
                               f"UPS unreadable x{self.ups.consecutive_failures} (flying blind)")
            return
        total_w, status_flags = reading
        self._last_ups_reading = (total_w, status_flags)
        matched_floor_flags = [flag for flag in self.floor_on_flags if flag in status_flags]

        # ── configured UPS emergency: runtime beats throughput, floor immediately ──
        if matched_floor_flags:
            if not self._was_power_floor:
                status = " ".join(status_flags) or "(empty)"
                matched = ",".join(matched_floor_flags)
                self.clamp_all(min(self.min_w),
                               f"UPS status {status} matched floor-on {matched}")
                self._was_power_floor = True
            self._feedback_wait_total_w = None
            return
        if self._was_power_floor:
            status = " ".join(status_flags) or "(empty)"
            log.info(f"UPS power-floor condition cleared (status {status}) — "
                     "resuming normal power governing")
            self._was_power_floor = False

        # A power-limit change and the UPS reading are asynchronous. Hold after
        # every downward step until NUT publishes a different load/status sample;
        # otherwise a cached total plus falling GPU draw invents rising non-GPU load.
        if self._feedback_wait_total_w is not None:
            old_total = self._feedback_wait_total_w
            old_status = self._feedback_wait_status_flags
            elapsed = now - self._feedback_wait_since
            if total_w != old_total or status_flags != old_status:
                log.info(f"POWER: fresh UPS feedback after throttle: {old_total:.0f}W/"
                         f"{' '.join(old_status)} -> {total_w:.0f}W/"
                         f"{' '.join(status_flags)}")
                self._feedback_wait_total_w = None
            elif elapsed < POWER_FEEDBACK_TIMEOUT_S:
                log.info(f"POWER: ups={total_w:.0f}W status={' '.join(status_flags)} — "
                         f"awaiting fresh feedback after throttle "
                         f"({elapsed:.0f}/{POWER_FEEDBACK_TIMEOUT_S:.0f}s), limits held "
                         + "/".join(f"{w:.0f}" for w in self.applied_w) + "W")
                return
            else:
                log.warning(f"⚠ POWER: no fresh UPS feedback for {elapsed:.0f}s; "
                            "allowing another throttle decision")
                self._feedback_wait_total_w = None

        gpu_state = self._read_gpu_power_and_activity()
        if gpu_state is None:
            return
        gpu_draw, active_gpu_count = gpu_state

        non_gpu = max(0.0, total_w - gpu_draw)

        # A learned cap belongs to the current workload. Do not chase low UPS samples
        # upward while work is active; once every GPU has been genuinely idle for a
        # full dwell, clear the learned ceiling and make the next job start at MAX.
        if active_gpu_count == 0:
            self._over_ticks = 0
            self._headroom_ticks = 0
            if self._idle_since is None:
                self._idle_since = now
            idle_for = now - self._idle_since
            if idle_for >= POWER_IDLE_DWELL_S:
                changed = False
                for i in range(len(self.handles)):
                    if abs(self.applied_w[i] - self.max_w[i]) >= 1.0:
                        self._set_limit(i, self.max_w[i])
                        changed = True
                self.learned_cap_w = min(self.applied_w) if self.applied_w else None
                self._thermal_limited = False
                self._thermal_hot_since = None
                self._thermal_cool_since = None
                if changed:
                    self._last_limit_change = now
                    self._feedback_wait_total_w = None
                    log.info(f"POWER: all GPUs idle for {idle_for:.0f}s -> reset limits "
                             + "/".join(f"{w:.0f}" for w in self.applied_w) + "W")
            else:
                log.info(f"POWER: all GPUs idle ({idle_for:.0f}/{POWER_IDLE_DWELL_S:.0f}s) "
                         "-> learned limits held "
                         + "/".join(f"{w:.0f}" for w in self.applied_w) + "W")
            return
        self._idle_since = None

        if self.learned_cap_w is None:
            self.learned_cap_w = min(self.applied_w)

        # ── REACTIVE: trim the common cap by the measured whole-system excess ──
        over = total_w - self.budget_w
        if over > POWER_DEADBAND_W:
            self._headroom_ticks = 0
            self._over_ticks += 1
            if self._over_ticks < POWER_OVER_GRACE_TICKS:
                log.info(f"POWER: ups={total_w:.0f}W over budget {self.budget_w:.0f}W by "
                         f"{over:.0f}W (grace {self._over_ticks}/{POWER_OVER_GRACE_TICKS}) — "
                         "letting it pass, limits held "
                         + "/".join(f"{w:.0f}" for w in self.applied_w) + "W")
                return
            # Split the observed UPS excess only across cards that are actually drawing
            # power. All cards keep one common ceiling, but idle cards no longer dilute
            # the correction applied to the active workload.
            target = self.learned_cap_w - (over / active_gpu_count)
            mode = "throttle"
        else:
            self._over_ticks = 0
            max_common_cap = min(self.max_w)
            if total_w >= self.budget_w - POWER_RESTORE_MARGIN_W:
                self._headroom_ticks = 0
                log.info(f"POWER: ups={total_w:.0f}W gpu={gpu_draw:.0f}W other={non_gpu:.0f}W "
                         f"budget={self.budget_w:.0f}W -> learned ceiling steady, limits held "
                         + "/".join(f"{w:.0f}" for w in self.applied_w) + "W")
                return

            if self.learned_cap_w >= max_common_cap:
                self._headroom_ticks = 0
                log.info(f"POWER: ups={total_w:.0f}W gpu={gpu_draw:.0f}W other={non_gpu:.0f}W "
                         f"budget={self.budget_w:.0f}W -> headroom, already at MAX "
                         + "/".join(f"{w:.0f}" for w in self.applied_w) + "W")
                return

            if self._thermal_limited:
                self._headroom_ticks = 0
                log.info(f"POWER: ups={total_w:.0f}W gpu={gpu_draw:.0f}W other={non_gpu:.0f}W "
                         f"budget={self.budget_w:.0f}W -> thermal hold, learned limits held "
                         + "/".join(f"{w:.0f}" for w in self.applied_w) + "W")
                return

            self._headroom_ticks += 1
            since_change = now - self._last_limit_change
            if (self._headroom_ticks < POWER_RESTORE_HEADROOM_TICKS
                    or since_change < POWER_RESTORE_DWELL_S):
                log.info(f"POWER: ups={total_w:.0f}W gpu={gpu_draw:.0f}W other={non_gpu:.0f}W "
                         f"budget={self.budget_w:.0f}W -> headroom dwell "
                         f"{self._headroom_ticks}/{POWER_RESTORE_HEADROOM_TICKS}, "
                         f"{since_change:.0f}/{POWER_RESTORE_DWELL_S:.0f}s; learned limits held "
                         + "/".join(f"{w:.0f}" for w in self.applied_w) + "W")
                return

            target = min(max_common_cap, self.learned_cap_w + POWER_SLEW_UP_W)
            self._headroom_ticks = 0
            mode = "restore"

        limits_before = tuple(self.applied_w)
        for i in range(len(self.handles)):
            cur = self.applied_w[i]
            want = max(self.min_w[i], min(self.max_w[i], target))
            delta = want - cur
            if abs(delta) < POWER_DEADBAND_W:
                # The deadband applies to whole-system error. With multiple active GPUs,
                # the per-card share may be smaller while the aggregate correction is
                # still required (for example 20 W excess / 2 cards = 10 W each).
                if mode == "throttle" and delta < 0:
                    self._set_limit(i, want)
                    continue
                # MAX is an exact hardware state, not a noisy sensor target. Permit
                # the final in-deadband restoration step (for example 590 -> 600 W).
                if mode == "restore" and delta > 0 and want == self.max_w[i]:
                    self._set_limit(i, want)
                continue
            if delta < 0:
                want = cur - min(-delta, POWER_SLEW_DOWN_W)
            else:
                want = cur + min(delta, POWER_SLEW_UP_W)
            self._set_limit(i, want)

        if tuple(self.applied_w) != limits_before:
            self.learned_cap_w = min(self.applied_w)
            self._last_limit_change = now
            if mode == "throttle":
                self._feedback_wait_total_w = total_w
                self._feedback_wait_status_flags = status_flags
                self._feedback_wait_since = now
                log.info(f"POWER: throttle step applied; awaiting fresh UPS feedback "
                         f"(timeout {POWER_FEEDBACK_TIMEOUT_S:.0f}s)")

        log.info(f"POWER: ups={total_w:.0f}W gpu={gpu_draw:.0f}W other={non_gpu:.0f}W "
                 f"budget={self.budget_w:.0f}W -> {mode}, learned-cap={self.learned_cap_w:.0f}W "
                 + "/".join(f"{w:.0f}" for w in self.applied_w) + "W")

    def restore_defaults(self):
        if self.dry_run:
            return
        if self.ceiling_w is not None:
            # `nvidia-smi -pl` semantics: a pinned ceiling outlives the process, so
            # stopping the service must not silently un-pin a running benchmark.
            log.info("Power ceiling active — leaving GPUs pinned instead of restoring "
                     "card defaults (unpin with --power-ceiling none, or write 'none' "
                     "to the ceiling file)")
            for i, h in enumerate(self.handles):
                try:
                    pynvml.nvmlDeviceSetPowerManagementLimit(h, int(self.ceiling_w[i] * 1000))
                    log.info(f"  GPU {i}: held at {self.ceiling_w[i]:.0f} W")
                except pynvml.NVMLError as e:
                    log.error(f"  GPU {i}: could not hold ceiling: {e}")
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
                 sync: bool = True, mirror: bool = False, governor=None,
                 temp_target: Optional[int] = None):
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
        # A configured target supersedes mirror/factory policy. Both cards become one
        # manually controlled thermal zone with asymmetric fan slew.
        self.temp_target = temp_target
        self._commanded_fan_pct: Optional[int] = None
        self._target_trim_pct = 0.0
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

            # Target and explicit-curve modes own every fan. Plain mirror mode manages
            # policy per poll so the hotter card can remain on factory auto.
            if self.temp_target is not None or not self.mirror:
                for fan_idx in range(fan_count):
                    try:
                        pynvml.nvmlDeviceSetFanControlPolicy(
                            handle, fan_idx, pynvml.NVML_FAN_POLICY_MANUAL
                        )
                    except pynvml.NVMLError as e:
                        log.warning(f"    Could not set manual control for fan {fan_idx}: {e}")

        if self.temp_target is not None:
            log.info(f"Temperature target: {self.temp_target}C (manual sync, "
                     f"fan slew +{TARGET_FAN_SLEW_UP_PCT}/-{TARGET_FAN_SLEW_DOWN_PCT}% per poll)")
        else:
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

    def get_target_fan_demand(self, temp: int) -> int:
        """Map temperature to a safe feed-forward demand below the configured target."""
        if self.temp_target is None:
            raise RuntimeError("temperature target is not configured")
        if temp >= CRITICAL_TEMP:
            return 100
        approach_start = self.temp_target - TARGET_FAN_APPROACH_BAND_C
        if temp <= approach_start:
            return TARGET_FAN_MIN_PCT
        ratio = (temp - approach_start) / TARGET_FAN_APPROACH_BAND_C
        demand = TARGET_FAN_MIN_PCT + ratio * (100 - TARGET_FAN_MIN_PCT)
        return max(TARGET_FAN_MIN_PCT, min(100, round(demand)))

    def update_fans_target(self):
        """Hold one configurable temperature target with asymmetric fan slew.

        Both cards use the hottest temperature and the same command. Upward changes
        are fast; downward changes are intentionally slow. The absolute critical
        boundary still bypasses slew and commands 100% immediately.
        """
        temps = {}
        for gpu_idx, handle in enumerate(self.handles):
            try:
                temps[gpu_idx] = pynvml.nvmlDeviceGetTemperature(
                    handle, pynvml.NVML_TEMPERATURE_GPU)
            except pynvml.NVMLError as e:
                log.error(f"GPU {gpu_idx}: Error reading temperature: {e}")
        if not temps:
            return

        hottest = max(temps.values())
        base_demand = self.get_target_fan_demand(hottest)
        if self._commanded_fan_pct is None:
            measured = []
            for handle in self.handles:
                try:
                    measured.append(pynvml.nvmlDeviceGetFanSpeed_v2(handle, 0))
                except pynvml.NVMLError:
                    pass
            self._commanded_fan_pct = max(measured) if measured else base_demand

        # Convert the upper-bound-style feed-forward curve into a true temperature
        # setpoint. Below target, integrate a negative correction so fan speed keeps
        # falling until the temperature actually reaches the requested value. At the
        # target the learned correction is held. Above target it unwinds much faster.
        # Integration starts only near the target to avoid cold-start wind-up.
        error_c = hottest - self.temp_target
        if -TARGET_TRACKING_BAND_C <= error_c < 0:
            self._target_trim_pct -= (
                -error_c * TARGET_TRIM_DOWN_PCT_PER_C_S * self.poll_interval)
        elif error_c > 0:
            self._target_trim_pct += (
                error_c * TARGET_TRIM_UP_PCT_PER_C_S * self.poll_interval)
        self._target_trim_pct = max(
            TARGET_TRIM_MIN_PCT, min(0.0, self._target_trim_pct))
        demand = round(max(
            TARGET_FAN_MIN_PCT,
            min(100.0, base_demand + self._target_trim_pct),
        ))

        if hottest >= CRITICAL_TEMP:
            command = 100
        elif demand > self._commanded_fan_pct:
            command = min(demand, self._commanded_fan_pct + TARGET_FAN_SLEW_UP_PCT)
        else:
            command = max(demand, self._commanded_fan_pct - TARGET_FAN_SLEW_DOWN_PCT)
        self._commanded_fan_pct = command

        for gpu_idx, (handle, fan_count) in enumerate(zip(self.handles, self.fan_counts)):
            for fan_idx in range(fan_count):
                try:
                    pynvml.nvmlDeviceSetFanSpeed_v2(handle, fan_idx, command)
                except pynvml.NVMLError as e:
                    log.error(f"GPU {gpu_idx} Fan {fan_idx}: Error setting speed: {e}")

        if self.governor:
            self.governor.observe_thermal(hottest, command, self.temp_target)
        critical = " CRITICAL" if hottest >= CRITICAL_TEMP else ""
        log.info(f"target: hottest={hottest}C target={self.temp_target}C "
                 f"base={base_demand}% trim={self._target_trim_pct:+.1f}% "
                 f"demand={demand}% command={command}%{critical}")

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
                if self.temp_target is not None:
                    self.update_fans_target()
                elif self.mirror:
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
        "--temp-target",
        type=parse_temp_target,
        default=None,
        metavar="CELSIUS",
        help=f"Own all fans as one thermal zone around a configurable target "
             f"(50..{CRITICAL_TEMP - 1} C). Overrides --mirror/--mode; fan demand "
             "rises before the target, ramps up fast, and ramps down slowly. With "
             "--power-budget, sustained heat at 100%% fan also derates GPU power."
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
        "--power-ceiling",
        type=parse_power_ceiling,
        default=None,
        metavar="WATTS",
        help="Upper power limit the governor will never exceed, replacing the card "
             "hardware max (e.g. --power-ceiling 300). One value applies to every GPU; "
             "a comma-separated list sets each GPU (--power-ceiling 300,400). Use "
             "'none' to unpin. Enables the governor on its own — no UPS required — and "
             "also bounds --power-fallback. Persisted to --power-ceiling-file so it "
             "survives restarts and reboots"
    )
    parser.add_argument(
        "--power-ceiling-file",
        default=DEFAULT_POWER_CEILING_FILE,
        metavar="PATH",
        help=f"State/control file for the power ceiling (default: "
             f"{DEFAULT_POWER_CEILING_FILE}). Read at startup to restore a ceiling after "
             f"a reboot, and polled live so `echo 300 > PATH` pins and `echo none > PATH` "
             f"unpins without restarting the service. SIGHUP re-reads immediately"
    )
    parser.add_argument(
        "--no-power-ceiling-file",
        action="store_true",
        help="Do not read or write the ceiling state file; --power-ceiling then applies "
             "to this run only and nothing is persisted"
    )
    parser.add_argument(
        "--power-floor-on",
        type=parse_power_floor_flags,
        default=DEFAULT_POWER_FLOOR_FLAGS,
        metavar="FLAG[,FLAG...]",
        help="Comma-separated NUT ups.status flags that immediately clamp every GPU "
             "to its hardware floor (default: OB,LB; use OB to ignore LB while online)"
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
    mode_desc = (f"TARGET {args.temp_target}C (manual sync)" if args.temp_target is not None
                 else "MIRROR (hotter card's own curve, mirrored onto cooler)" if args.mirror
                 else "INDEPENDENT (per-card)" if args.independent
                 else "SYNC (all fans = hottest card)")
    log.info(f"NVIDIA Fan Control - Curve: {args.mode.upper()} - {mode_desc}")
    log.info("=" * 50)

    # Resolve the startup ceiling. An explicit --power-ceiling wins over the persisted
    # value and is written back, so the file always reflects what is actually in force.
    # The unit deliberately does NOT pass --power-ceiling, which leaves the state file as
    # the single source of truth and lets a live pin survive `systemctl restart`.
    ceiling_path = None if args.no_power_ceiling_file else args.power_ceiling_file
    persisted_ok, persisted_request = (read_ceiling_file(ceiling_path) if ceiling_path
                                       else (False, None))
    if args.power_ceiling is not None:
        ceiling_request = args.power_ceiling or None     # [] means an explicit unpin
        ceiling_source = "--power-ceiling"
    else:
        ceiling_request = persisted_request if persisted_ok else None
        ceiling_source = f"persisted in {ceiling_path}"

    # --power-budget is no longer the only enable switch: a ceiling runs the governor on
    # its own with no UPS sensor, which also makes thermal derating available on hosts
    # that have no NUT. An explicit `--power-ceiling none` starts it briefly so the unpin
    # is applied and persisted.
    governor = None
    if (args.power_budget is not None or args.power_ceiling is not None
            or ceiling_request is not None):
        governor = PowerGovernor(
            handles=[],                      # filled in by controller.init()
            budget_w=args.power_budget,
            ups_name=args.ups,
            interval=args.power_interval,
            fallback_w=args.power_fallback,
            floor_on_flags=args.power_floor_on,
            dry_run=args.power_dry_run,
            ceiling_request=ceiling_request,
            ceiling_path=ceiling_path,
            persist_startup_ceiling=args.power_ceiling is not None,
            ceiling_source=ceiling_source,
        )
        if args.power_budget is not None:
            log.info(f"Power governor ENABLED — budget {args.power_budget:.0f} W via UPS "
                     f"'{args.ups}', floor-on={','.join(args.power_floor_on)}")
        else:
            log.info("Power governor ENABLED in CEILING-ONLY mode — no UPS budget; "
                     "power cap plus thermal derating only")
    elif args.temp_target is not None:
        log.warning("Temperature target has no --power-budget or --power-ceiling; fan "
                    "control is active but thermal power derating is unavailable")

    controller = NvidiaFanController(curve, args.interval,
                                     sync=not args.independent, mirror=args.mirror,
                                     governor=governor, temp_target=args.temp_target)

    # Handle signals for clean shutdown
    def signal_handler(sig, frame):
        log.info(f"Received signal {sig}")
        controller.stop()

    def reload_handler(sig, frame):
        log.info("Received SIGHUP — re-reading the power ceiling control file")
        if governor:
            governor.request_reload()

    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGHUP, reload_handler)

    controller.init()

    if args.once:
        if args.temp_target is not None:
            controller.update_fans_target()
        elif args.mirror:
            controller.update_fans_mirror()
        else:
            controller.update_fans()
        if governor:
            governor.update(force=True)
        log.info("Ran once. Fans will return to auto control after a few minutes.")
    else:
        controller.run()


if __name__ == "__main__":
    main()
