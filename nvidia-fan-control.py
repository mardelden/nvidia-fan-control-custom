#!/usr/bin/env python3
"""
NVIDIA fan control + GPU power governor for headless GPU hosts (RTX PRO 6000).

GPU power governor. Keeps GPU power within the operator's limits (power ceiling,
temperature target) and the host's safety limits (UPS budget, emergency temperature).
It reads the UPS through NUT as a read-only input: it polls `upsc` every few seconds
and never sends commands to the UPS or changes its settings. It does not shut the host
down; that's NUT's `upsmon`. The only things it changes are GPU power limits and fan
speeds.

Settings (all live: picked up without a restart, and surviving reboots):

    fan profile        native | quiet | aggressive | performance | max | adaptive[:FANMAX]
    mirror             on | off         (both fans follow the hotter card; back-to-back cards)
    temperature target degrees C | none
    power ceiling      W | W,W | none   (per GPU)
    total ceiling      W | none         (all GPUs together, moved to the busy cards)

Each setting is resolved in this order: a command-line flag (hand-run only), then a
temporary override in the run dir (/run/nvidia-fan-control, cleared on restart), then
the saved file in the state dir (/var/lib/nvidia-fan-control), then the default.
Safety limits (UPS budget, emergency temperature, floor flags, fallback) are
command-line flags set by the deployment, never live settings.

Run as root: `sudo python3 nvidia-fan-control.py`, or as the systemd service. The
design is recorded in plans/002-layered-fan-policy.md and plans/003-total-gpu-ceiling.md.
"""

import argparse
import errno
import fcntl
import json
import logging
import math
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import pynvml

logging.basicConfig(
    level=logging.INFO,
    format='%(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
log = logging.getLogger(__name__)


# ─────────────────────────── FAN PROFILES ───────────────────────────
# (temperature C, fan %) points, linearly interpolated. `native` is not a table: it is
# the card's own factory curve, which the daemon leaves alone. `adaptive` is not a table
# either: it is the learning controller below.

# Quiet at idle, but ramps hard: full blast by 65 C.
QUIET_CURVE = [
    (40, 30),
    (45, 40),
    (50, 55),
    (55, 75),
    (60, 90),
    (65, 100),
]

AGGRESSIVE_FAN_CURVE = [
    (30, 40),
    (40, 50),
    (50, 65),
    (55, 75),
    (60, 85),
    (65, 95),
    (70, 100),
]

PERFORMANCE_FAN_CURVE = [
    (25, 50),
    (35, 60),
    (45, 75),
    (50, 85),
    (55, 95),
    (60, 100),
]

MAX_COOLING_CURVE = [
    (0, 100),
]

CURVES = {
    "quiet": QUIET_CURVE,
    "aggressive": AGGRESSIVE_FAN_CURVE,
    "performance": PERFORMANCE_FAN_CURVE,
    "max": MAX_COOLING_CURVE,
}
PROFILE_NAMES = ("native", "quiet", "aggressive", "performance", "max", "adaptive")
DEFAULT_PROFILE = "native"

# ── adaptive: the learning controller ──
# A feed-forward ramp from TARGET_FAN_MIN_PCT at (target - APPROACH_BAND) to 100% at the
# target, plus a learned trim that finds the quietest fan speed holding the card AT the
# target. The trim learns downward slowly within TRACKING_BAND below the target, holds at
# the target, and unwinds fast above it. It only ever makes the fans quieter than the ramp.
TARGET_FAN_MIN_PCT = 30
TARGET_FAN_APPROACH_BAND_C = 20
TARGET_FAN_SLEW_UP_PCT = 10
TARGET_FAN_SLEW_DOWN_PCT = 2
TARGET_TRACKING_BAND_C = 8
TARGET_TRIM_MIN_PCT = -50.0
TARGET_TRIM_DOWN_PCT_PER_C_S = 0.125
TARGET_TRIM_UP_PCT_PER_C_S = 2.5
# Unlearning (operator, 2026-09-29): adaptive learns from its own power cuts. Each thermal hold
# (a power cut for heat) raises a floor under the quiet trim by TRIM_FLOOR_RAISE_PCT, so a
# bursty workload that keeps getting cut ends up on the base curve (fans ahead of the burst);
# every TRIM_FLOOR_RELAX_S without a cut lowers it again by TRIM_FLOOR_RELAX_PCT, so a steady
# workload drifts back to quiet. Seen on pve-ai under vLLM: a -35% trim parked GPU1 at 83 C
# with fans at 58%, and each burst became a 230-240 W cut.
TRIM_FLOOR_RAISE_PCT = 20.0
TRIM_FLOOR_RELAX_PCT = 5.0
TRIM_FLOOR_RELAX_S = 600.0
ADAPTIVE_FAN_MAX_MIN_PCT = TARGET_FAN_MIN_PCT   # a fan max below the floor is contradictory
DEFAULT_ADAPTIVE_FAN_MAX_PCT = 100

TEMP_TARGET_MIN_C = 50


# ─────────────────────────── THERMAL POWER ───────────────────────────
# The temperature target is held by cutting GPU power once the card is AT the target for a
# short grace. With the fixed curves the fan speed is irrelevant; with adaptive the fans
# must also be at its fan max (fans first, then power).
#
# The cut is EXPONENTIAL in degrees over the target (operator, 2026-09-29):
#     step = MIN_STEP x 2^(degrees over target), at most MAX_STEP_FRACTION of power
#     at target: -30 W   +1 C: -60 W   +2 C: -120 W   +3 C: -240 W   +4 C and up: -50% of power
# The first cut waits INITIAL_DWELL_S (the grace), except at SKIP_DWELL_EXCESS_C or more
# over the target, where it cuts on that reading. While the card is still FALLING (cooler
# than one repeat dwell ago), power is HELD: temperature lags a cut by a few seconds, and
# cutting again before the last cut has taken effect overshoots downwards. Once it stops
# falling and is still at or over the target, it's cut again. (Comparing with the
# temperature AT the cut let a card plateau over the cut point forever; seen on pve-ai.)
#
# History: the cut point was target+2 C with 20 W linear steps. On pve-ai at 600 W a card
# overshot to 78 C against a 72 C cut point; the operator moved the cut to the target, made
# it exponential, and raised the base step to 30 W.
THERMAL_POWER_MARGIN_C = 0            # the cut point is the target itself
THERMAL_POWER_INITIAL_DWELL_S = 5.0
THERMAL_POWER_REPEAT_DWELL_S = 5.0
THERMAL_POWER_MIN_STEP_W = 30.0
THERMAL_POWER_MAX_STEP_FRACTION = 0.5
THERMAL_POWER_SKIP_DWELL_EXCESS_C = 4 # target+4 C or more: no grace
# Rate of rise (operator, 2026-09-29). Measured PER READING, not per second: readings are
# nominally 2 s apart but really ~2.05 s, so a per-second threshold of 1.0 C/s silently
# rejected a real +2 C-per-reading climb (seen on pve-ai).
#   * Prediction, at the ONSET of a hold: if the card rose >= RATE_FAST_C_PER_READING since
#     the last reading and is within RATE_WINDOW_C of the target, the decision uses
#         predicted = now + (rise per reading) x RATE_LOOKAHEAD_READINGS
#     and cuts as soon as the prediction reaches the target, sized by the predicted
#     overshoot. Only for the first cut: afterwards the real-temperature rules run.
#   * Grace only when STEADY: at or over the target, if the card is warmer than it was
#     two readings ago (still climbing), it's cut without waiting. The 5 s grace is kept
#     for a card that has reached the target and stopped climbing. Two readings, not one,
#     so a 1 C flicker (74, 75, 74, 75) still counts as steady.
THERMAL_RATE_FAST_C_PER_READING = 2
THERMAL_RATE_LOOKAHEAD_READINGS = 2
THERMAL_RATE_WINDOW_C = 10
# Prediction, smoothed and bounded (operator, 2026-09-30, from production under vLLM): one
# +4 C reading at 83 C predicted 91-96 C and halved the power (-202 W, -288 W). Now it takes
# two rising readings in a row, their average rise, and only while the card is still below
# the cut point; the prediction can add at most THERMAL_PREDICT_MAX_EXCESS_C (a cut of at
# most 30 W x 2^2 = 120 W). Once the card is measured over the target, the measured excess
# alone sizes the cut: the large cuts are for real overshoot.
THERMAL_PREDICT_MAX_EXCESS_C = 2
# The target must leave room below the emergency temperature, so the normal cut always
# comes first. Independent of the margin above.
TEMP_TARGET_EMERGENCY_GAP_C = 3
THERMAL_POWER_RECOVER_MARGIN_C = 2
# Recovery mirrors the cut (operator, 2026-09-29): once a hold is released, each raise is
#     step = POWER_SLEW_UP_W x 2^(degrees below target - RECOVER_MARGIN), at most MAX_STEP_FRACTION
# of current power, one raise per POWER_RESTORE_DWELL_S. A card sitting just under the
# release point creeps up +20 W; one that has clearly cooled gets its power back fast. With
# a UPS budget, a raise is also bounded by the measured headroom. After an EMERGENCY,
# recovery stays at the conservative +20 W: that hold means something went badly wrong.
THERMAL_POWER_RECOVER_DWELL_S = 30.0
# adaptive (operator, plan 003): power comes back whenever the card is below the target, after
# THERMAL_ADAPTIVE_RELEASE_DWELL_S there, and restoring pauses at the target. The fans just
# follow temperature and rise as power returns; with them on the curve, "below the target" is
# "the fans aren't at their max yet". The fixed curves keep the 2 C / 30 s release.
THERMAL_ADAPTIVE_RELEASE_DWELL_S = 10.0

# ── emergency cutoff: always armed, for every profile ──
# At the emergency temperature for EMERGENCY_DWELL_S, every GPU drops to its hardware
# minimum power. Released once the card is EMERGENCY_RELEASE_DROP_C below the emergency
# temperature for EMERGENCY_RELEASE_DWELL_S; power then walks back up in steps.
DEFAULT_TEMP_EMERGENCY_C = 92
TEMP_EMERGENCY_MIN_C = 60
TEMP_EMERGENCY_MAX_C = 95
EMERGENCY_DWELL_S = 2.0
EMERGENCY_RELEASE_DROP_C = 30
EMERGENCY_RELEASE_DWELL_S = 30.0
# If any card's temperature is unreadable in BLIND_READ_FAILURES of the last
# BLIND_WINDOW_READINGS readings, the governor is blind: every GPU goes to its minimum power
# and the fans we own go to 100% (native fans stay with the factory curve, which reads the
# sensor itself). A window, not a run: a flaky sensor that fails 2 readings in 3 never makes
# 3 in a row. Released once the window is clear and every card has read for
# BLIND_RELEASE_DWELL_S; power then walks back up in +20 W steps. Below the threshold, a
# missing card counts at its last known temperature, so it can't hide from the emergency.
BLIND_READ_FAILURES = 3
BLIND_WINDOW_READINGS = 5
BLIND_RELEASE_DWELL_S = 30.0


# ─────────────────────────── UPS BUDGET ───────────────────────────
# The UPS is the only sensor that sees TOTAL draw (CPU, board, disks), and the GPU power
# limit is the actuator. Goal: keep total UPS load under budget so the UPS can actually
# carry the machine.
#
# Measured baseline on pve-ai (CyberPower CP1500PFCLCDa, ups.realpower.nominal=1000):
#   idle total ~220 W with GPUs at ~16 W each  ->  non-GPU floor ~190 W
#   Threadripper 7970X peaks ~355 W, so non-GPU can reach ~480 W under CPU load.
#
# UPS load is reported as INTEGER PERCENT of ups.realpower.nominal, so resolution is
# nominal/100 (10 W on a 1000 W unit). Do not expect finer control than that.
DEFAULT_POWER_BUDGET = 900          # watts, total UPS load
DEFAULT_UPS_NAME = "cyberpower"     # `upsc -l` name
DEFAULT_POWER_INTERVAL = 5.0        # seconds between governor updates

# Anti-oscillation. The UPS driver refreshes on its own cycle and NVML enforcement has
# its own time constant, so a naive proportional loop hunts. Downward changes shed the
# measured whole-system excess (bounded); upward changes need sustained headroom and are
# deliberately small.
POWER_DEADBAND_W = 15
POWER_SLEW_DOWN_W = 150
POWER_SLEW_UP_W = 20
POWER_OVER_GRACE_TICKS = 1
# Operator (2026-09-30): crossing the budget for 20-30 s is fine, so a short burst isn't
# trimmed. Only a load that stays over the budget for POWER_OVER_GRACE_S is. Above the UPS's
# own rating (ups.realpower.nominal) there's no grace; with the rating unknown, none either.
# On battery the floor still clamps at once (a separate path).
POWER_OVER_GRACE_S = 20.0
POWER_RESTORE_MARGIN_W = 50
POWER_RESTORE_HEADROOM_TICKS = 3
POWER_RESTORE_DWELL_S = 30.0
POWER_IDLE_DRAW_W = 75.0
POWER_IDLE_UTIL_PCT = 5
POWER_IDLE_DWELL_S = 60.0

# Total GPU ceiling (plan 003): the power limits never add up to more than it, and the
# governor moves it to the busy cards. A card counts as busy at once (over POWER_IDLE_DRAW_W
# or POWER_IDLE_UTIL_PCT), and as idle only after ALLOC_IDLE_DWELL_S below both, so a pause
# between requests doesn't move power around.
ALLOC_IDLE_DWELL_S = 10.0
# A SOFT total (operator, 2026-09-29): an idle card is counted at what it actually draws, not
# at its 150 W minimum limit: its highest draw over the last ALLOC_DRAW_WINDOW_S, plus
# ALLOC_IDLE_RESERVE_MARGIN_W, rounded up to ALLOC_IDLE_RESERVE_STEP_W (so the idle wobble
# doesn't move the split every tick), never above its minimum. Until there are readings it's
# counted at ALLOC_IDLE_RESERVE_W, the most an idle card can draw (above it, it counts as
# busy). The sum of the LIMITS may exceed the total by (minimum - reserve) per idle card; the
# DRAW stays within it, except for a tick or two when an idle card wakes, until the busy card
# is lowered.
ALLOC_IDLE_RESERVE_W = POWER_IDLE_DRAW_W
ALLOC_DRAW_WINDOW_S = 10.0
ALLOC_IDLE_RESERVE_MARGIN_W = 10.0
ALLOC_IDLE_RESERVE_STEP_W = 25.0

# Fail-safe: if the UPS can't be read this many times in a row we are flying blind, so
# clamp to a conservative per-GPU limit rather than assume headroom.
POWER_MAX_READ_FAILURES = 3
DEFAULT_POWER_FALLBACK_W = 300

# After a downward step, do not act again on the same cached UPS sample. pve-ai's
# CyberPower held a stale 1030 W value for 36 s after load disappeared; without this gate,
# falling GPU draw was misattributed as rising non-GPU load.
POWER_FEEDBACK_TIMEOUT_S = 45.0

# NUT ups.status flags that immediately clamp every GPU to its hardware floor.
DEFAULT_POWER_FLOOR_FLAGS = ("OB", "LB")


# ─────────────────────────── FILES ───────────────────────────
DEFAULT_STATE_DIR = "/var/lib/nvidia-fan-control"   # saved settings + runtime state
DEFAULT_RUN_DIR = "/run/nvidia-fan-control"         # overrides, lock, effective state
SETTING_FILES = {
    "profile": "fan-profile",
    "mirror": "fan-mirror",
    "target": "temp-target",
    "ceiling": "power-ceiling",
    "total": "power-ceiling-total",
}
SETTING_NAMES = ("profile", "mirror", "target", "ceiling", "total")
LOG_PREFIX = {"profile": "FAN", "mirror": "FAN", "target": "TEMP", "ceiling": "POWER",
              "total": "POWER"}
RUNTIME_STATE_FILE = "runtime-state.json"
EFFECTIVE_FILE = "effective.json"
LOCK_FILE = "daemon.lock"
RUNTIME_SAVE_INTERVAL_S = 60.0
HOLD_RESTORE_MAX_AGE_S = 300.0
OVERRIDE_ACK_TIMEOUT_S = 15.0
EFFECTIVE_REFRESH_S = 10.0      # effective.json: at least this fresh (temps, fans)


# ─────────────────────────── PARSERS ───────────────────────────

@dataclass(frozen=True)
class FanProfile:
    name: str
    fan_max: int = DEFAULT_ADAPTIVE_FAN_MAX_PCT

    def text(self) -> str:
        if self.name == "adaptive" and self.fan_max != DEFAULT_ADAPTIVE_FAN_MAX_PCT:
            return f"adaptive:{self.fan_max}"
        return self.name

    def describe(self) -> str:
        if self.name == "adaptive":
            return f"adaptive, fan max {self.fan_max}%"
        return self.name


def _strip(text: str) -> str:
    return text.split("#", 1)[0].strip().lower()


def parse_fan_profile(value: str) -> FanProfile:
    """`native` | `quiet` | `aggressive` | `performance` | `max` | `adaptive[:FANMAX]`."""
    text = _strip(value)
    if text == "":
        return FanProfile(DEFAULT_PROFILE)
    if "," in text:
        raise ValueError("per-GPU fan profiles are not supported; use one profile per host")
    name, _, arg = text.partition(":")
    if name not in PROFILE_NAMES:
        raise ValueError(f"unknown profile {name!r}; expected one of {', '.join(PROFILE_NAMES)}")
    if name != "adaptive":
        if arg:
            raise ValueError(f"profile {name!r} takes no parameter")
        return FanProfile(name)
    if not arg:
        return FanProfile("adaptive")
    try:
        fan_max = int(arg.rstrip("%"))
    except ValueError:
        raise ValueError(f"adaptive fan max must be a percentage, got {arg!r}")
    if not ADAPTIVE_FAN_MAX_MIN_PCT <= fan_max <= 100:
        raise ValueError(f"adaptive fan max must be {ADAPTIVE_FAN_MAX_MIN_PCT}..100, got {fan_max}")
    return FanProfile("adaptive", fan_max)


def parse_mirror(value: str) -> bool:
    text = _strip(value)
    if text in ("", "off", "false", "no", "0"):
        return False
    if text in ("on", "true", "yes", "1"):
        return True
    raise ValueError(f"expected on or off, got {text!r}")


def parse_temp_target(value: str) -> Optional[int]:
    """Degrees C, or `none` / `off` / empty for no target. The upper bound depends on the
    emergency temperature and is checked by the controller."""
    text = _strip(value)
    if text in ("", "none", "off"):
        return None
    try:
        celsius = int(text.rstrip("c"))
    except ValueError:
        raise ValueError(f"expected degrees C or none, got {text!r}")
    if celsius < TEMP_TARGET_MIN_C:
        raise ValueError(f"target must be at least {TEMP_TARGET_MIN_C}C, got {celsius}C")
    return celsius


def parse_power_ceiling(value: str) -> List[float]:
    """`WATTS`, `WATTS,WATTS,...` (per GPU), or `none` to unpin.

    Returns [] for an explicit unpin, so callers can tell it apart from "not given".
    """
    text = _strip(value)
    if text in ("", "none", "off", "0"):
        return []
    out: List[float] = []
    for part in text.split(","):
        part = part.strip()
        try:
            watts = float(part)
        except ValueError:
            raise ValueError(f"expected watts or a comma-separated per-GPU list, got {part!r}")
        if watts <= 0:
            raise ValueError(f"power ceiling must be > 0 W, got {watts:g}")
        out.append(watts)
    return out


def parse_power_total(value: str) -> float:
    """`WATTS` for all GPUs together, or `none`. Returns 0.0 for an explicit `none`."""
    text = _strip(value)
    if text in ("", "none", "off", "0"):
        return 0.0
    try:
        watts = float(text)
    except ValueError:
        raise ValueError(f"expected watts (one number for all GPUs together), got {text!r}")
    if watts <= 0:
        raise ValueError(f"total ceiling must be > 0 W, got {watts:g}")
    return watts


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


def _parse_setting(name: str, text: str):
    """Parse a setting's file text into its value. Raises ValueError."""
    if name == "profile":
        return parse_fan_profile(text)
    if name == "mirror":
        return parse_mirror(text)
    if name == "target":
        return parse_temp_target(text)
    if name == "total":
        return parse_power_total(text) or None
    request = parse_power_ceiling(text)
    return request or None


def format_setting(name: str, value) -> str:
    if name == "profile":
        return value.text()
    if name == "mirror":
        return "on" if value else "off"
    if name == "target":
        return "none" if value is None else str(value)
    if name == "total":
        return "none" if not value else f"{value:g}"
    return "none" if not value else ",".join(f"{w:g}" for w in value)


SETTING_DEFAULTS = {"profile": FanProfile(DEFAULT_PROFILE), "mirror": False,
                    "target": None, "ceiling": None, "total": None}


def _argtype(parser_fn):
    def wrap(value):
        try:
            return parser_fn(value)
        except ValueError as e:
            raise argparse.ArgumentTypeError(str(e))
    return wrap


def _write_atomic(path: str, text: str):
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "w") as f:
        f.write(text)
    os.replace(tmp, path)


# ─────────────────────────── SETTINGS ───────────────────────────

class SettingsStore:
    """Resolves the five live settings from their layers, and notices changes.

    Precedence: `cli` (hand-run flags, in memory) > override (run dir) > saved (state dir)
    > default. Files are polled by stat() on every call (cheap), and SIGHUP forces a full
    re-read. A malformed file is logged and ignored: that layer keeps its previous value, so
    a typo can never silently change what is in force.
    """

    def __init__(self, state_dir: str, run_dir: str, cli: Optional[Dict[str, object]] = None):
        self.dirs = {"saved": state_dir, "override": run_dir}
        self.cli = dict(cli or {})
        self.layers: Dict[str, Dict[str, object]] = {"saved": {}, "override": {}}
        self.stamps: Dict[str, Dict[str, Optional[Tuple[int, int]]]] = {"saved": {}, "override": {}}
        self.effective: Dict[str, Tuple[object, str]] = {}
        self._reload = True
        self.generation = 0

    def path(self, layer: str, name: str) -> str:
        return os.path.join(self.dirs[layer], SETTING_FILES[name])

    def request_reload(self):
        self._reload = True

    def _stamp(self, path: str) -> Optional[Tuple[int, int]]:
        try:
            st = os.stat(path)
            return (st.st_mtime_ns, st.st_size)
        except FileNotFoundError:
            return None
        except OSError:
            return ("error", 0)   # unreadable dir etc.: treat as a change and retry later

    def _read_layer(self, layer: str, name: str, force: bool) -> bool:
        path = self.path(layer, name)
        stamp = self._stamp(path)
        if not force and name in self.stamps[layer] and self.stamps[layer][name] == stamp:
            return False
        self.stamps[layer][name] = stamp
        had = name in self.layers[layer]
        if stamp is None:
            self.layers[layer].pop(name, None)
            return had
        try:
            with open(path) as f:
                text = f.read()
        except OSError as e:
            log.warning(f"⚠ {LOG_PREFIX[name]}: cannot read {path}: {e}")
            return False
        try:
            value = _parse_setting(name, text)
        except ValueError as e:
            what = "ceiling file" if name == "ceiling" else f"{SETTING_FILES[name]} file"
            log.error(f"{LOG_PREFIX[name]}: ignoring malformed {what} {path}: {e}")
            return False
        old = self.layers[layer].get(name, None)
        self.layers[layer][name] = value
        return (not had) or old != value

    def resolve(self, name: str) -> Tuple[object, str]:
        if name in self.cli:
            return self.cli[name], "flag"
        if name in self.layers["override"]:
            return self.layers["override"][name], "override"
        if name in self.layers["saved"]:
            return self.layers["saved"][name], "saved"
        return SETTING_DEFAULTS[name], "default"

    def refresh(self) -> Tuple[List[Tuple[str, object, str]], List[Tuple[str, object, str]]]:
        """Re-read what changed. Returns (effective changes, masked saved changes).

        A masked change is a saved file that changed while a higher layer hides it. It is
        reported so the deploy team's verbs still see an acknowledgement in the journal.
        """
        force = self._reload
        self._reload = False
        masked = []
        for name in SETTING_NAMES:
            self._read_layer("override", name, force)
            if self._read_layer("saved", name, force):
                value, source = self.resolve(name)
                if source in ("flag", "override") and name in self.layers["saved"]:
                    masked.append((name, self.layers["saved"][name], source))
        changes = []
        for name in SETTING_NAMES:
            value, source = self.resolve(name)
            if self.effective.get(name) != (value, source):
                prev = self.effective.get(name)
                self.effective[name] = (value, source)
                # the total also on a source change: "no file" (keep the last total) and an
                # explicit `none` (clear it) are the same value but mean different things
                if prev is None or prev[0] != value or (name == "total" and prev[1] != source):
                    changes.append((name, value, source))
        if force:
            self.generation += 1
        return changes, masked


def describe_source(store: "SettingsStore", name: str, source: str) -> str:
    if source == "flag":
        return "command-line flag"
    if source == "default":
        return "default"
    return f"{'temporary override' if source == 'override' else 'saved'} {store.path(source, name)}"


# ─────────────────────────── UPS ───────────────────────────

class UpsReader:
    """Reads total system draw from NUT (`upsc <name>`), read-only.

    This UPS (CyberPower CP1500PFCLCDa) does NOT expose `ups.realpower`, only `ups.load`
    as an integer percent of `ups.realpower.nominal`, so watts are derived with nominal/100
    resolution. The raw `ups.status` tokens are returned to the governor.
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


# ─────────────────────────── POWER GOVERNOR ───────────────────────────

class PowerGovernor:
    """The GPU power governor: the only thing that writes GPU power limits.

    Limits it enforces, from the operator and from the host:

    - **Power ceiling** (operator, per GPU): the upper actuation bound. It's applied by
      SHRINKING max_w, never by clamping at the decision sites. _set_limit() and every
      control path already bound writes to max_w, so one assignment caps the whole
      actuator surface. Any code that sets a power limit MUST go through _set_limit().
    - **Total ceiling** (operator, all GPUs together, plan 003): _allocate() splits it
      between the cards every tick, idle cards at their minimum and the busy ones sharing
      the rest. It also works by shrinking max_w, so the limits never add up to more than
      it. A lowered share applies at once; a raised one waits a tick (lower before raise).
    - **Temperature target** (operator): observe_thermal() cuts power at the target
      (decisions/009), stepping by the excess, and releases at target - 2 C for 30 s.
    - **Emergency temperature** (host safety): observe_emergency() drops every GPU to its
      hardware minimum after 2 s at the emergency temperature. Always armed.
    - **UPS budget** (host safety, optional): update() keeps TOTAL UPS load under budget
      by trimming the busy cards' caps by the measured excess, reacting on the first
      over-budget tick, and restoring slowly with sustained headroom. With budget_w=None
      there is no UPS at all and the governor holds the ceiling.

    **Per-card caps (plan 003).** cap_w[i] is what the UPS budget, the thermal law and the
    holds allow card i; its limit is max(min_w, min(max_w, cap_w)). A cap is set explicitly
    by each operation and never re-derived from the applied limits (a shared cap re-derived
    as min(applied) dragged every card down to the lowest one). Cuts start from the card's
    current level and never raise it. Raises happen only in the supervised restore and
    recovery walk, the idle reset, and an allocation raise, which the total bounds.

    After a thermal or emergency hold is released, power walks back up in steps every
    POWER_RESTORE_DWELL_S, never as a jump.
    """

    def __init__(self, handles: List, budget_w: Optional[float] = None,
                 ups_name: str = DEFAULT_UPS_NAME,
                 interval: float = DEFAULT_POWER_INTERVAL,
                 fallback_w: float = DEFAULT_POWER_FALLBACK_W,
                 floor_on_flags: Tuple[str, ...] = DEFAULT_POWER_FLOOR_FLAGS,
                 dry_run: bool = False):
        self.handles = handles
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
        self._over_since: Optional[float] = None
        self._feedback_wait_total_w: Optional[float] = None
        self._feedback_wait_status_flags: Tuple[str, ...] = ()
        self._feedback_wait_since = 0.0
        self._headroom_ticks = 0
        self._last_limit_change = 0.0
        self._idle_since: Optional[float] = None
        self.cap_w: List[float] = []            # per card: see the class docstring
        self._last_ups_reading: Optional[Tuple[float, Tuple[str, ...]]] = None
        # thermal hold (temperature target)
        self._thermal_hot_since: Optional[float] = None
        self._thermal_cool_since: Optional[float] = None
        self._last_thermal_step = 0.0
        self._last_cut_temp_c: Optional[int] = None
        self._last_hottest_c: Optional[int] = None
        self._recent_c: List[int] = []                          # last 3 readings, oldest first
        self._target_c: Optional[int] = None
        self._conservative_recovery = False     # after an emergency: +20 W steps only
        self.thermal_limited = False
        self.thermal_holds = 0          # holds started (power cuts for heat), for adaptive
        # temperature unreadable
        self.blind_active = False
        self._blind_ok_since: Optional[float] = None
        # emergency cutoff
        self.emergency_active = False
        self._emergency_hot_since: Optional[float] = None
        self._emergency_cool_since: Optional[float] = None
        # after a hold is released, raise power in steps rather than jumping
        self.recovery_walk = False
        self.ceiling_request: Optional[List[float]] = None
        self.ceiling_w: Optional[List[float]] = None
        # total ceiling (plan 003)
        self.total_w: Optional[float] = None
        self.alloc_w: Optional[List[float]] = None
        self._busy: List[bool] = []
        self._quiet_since: List[Optional[float]] = []
        self._last_busy_now: Optional[List[bool]] = None
        self._draw_hist: List[List[Tuple[float, float]]] = []   # per card: (time, W), last 10 s
        self._last_busy_at: List[float] = []
        self._last_temps: Optional[Dict[int, int]] = None
        self._fan_threshold: Optional[int] = None      # set while adaptive drives the fans
        self._settle_pending = False    # full shares after a restart, once the UPS reads fine
        self._settle_now = False
        self._card_recent: Dict[int, List[int]] = {}   # per card: last 3 readings, oldest first
        self._hold_log_pending = True

    @property
    def learned_cap_w(self) -> Optional[float]:
        """The highest card's cap (for logs). Setting it sets every card's cap."""
        return max(self.cap_w) if self.cap_w else None

    @learned_cap_w.setter
    def learned_cap_w(self, value: Optional[float]):
        self.cap_w = [] if value is None else [float(value)] * len(self.handles)

    # ── setup ──
    def init(self, ceiling_request: Optional[List[float]] = None, source: str = "startup"):
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
        self._busy = [True] * len(self.handles)          # assume busy until measured
        self._quiet_since = [None] * len(self.handles)
        self._last_busy_at = [time.monotonic()] * len(self.handles)
        self._draw_hist = [[] for _ in self.handles]

        dry = "  [DRY RUN — nothing will be set]" if self.dry_run else ""
        if self.budget_w is None:
            log.info("Power budget: none (no UPS sensor)" + dry)
        else:
            log.info(f"Power budget: {self.budget_w:.0f} W total UPS load (read-only input)" + dry)
            log.info("Immediate power-floor UPS flags: " + ",".join(self.floor_on_flags))

        # Applied BEFORE the caps are seeded, so they start at the ceiling rather than at
        # the pre-ceiling hardware limit. NVML limits reset to the
        # card default at boot, so this is also how a ceiling survives a reboot.
        if ceiling_request is not None:
            self.apply_ceiling(ceiling_request, source)
        else:
            log.info("Power ceiling: none — upper limit is the card hardware max "
                     + "/".join(f"{w:.0f}" for w in self.hw_max_w) + " W")
            if self.budget_w is None:
                self._release_to_default(source)
        if self.budget_w is not None and self.ceiling_w and self.fallback_w > min(self.ceiling_w):
            log.info(f"  note: --power-fallback {self.fallback_w:.0f} W sits above the "
                     "ceiling and will be clamped to it when the UPS is unreadable")

        # each card starts from where it is and walks up under supervision
        self.cap_w = list(self.applied_w)
        self._last_limit_change = time.monotonic()
        if self.budget_w is None:
            self._hold_ceiling()

    # ── bounds ──
    def _ceil_w(self, i: int) -> float:
        """Card i's own upper bound, before the total's share: its ceiling or hardware max."""
        return self.ceiling_w[i] if self.ceiling_w is not None else self.hw_max_w[i]

    def _base_w(self, i: int) -> float:
        """Where card i's cap settles when nothing holds it down: its ceiling; with no
        ceiling, its hardware max, or its default when there is no UPS budget either."""
        if self.ceiling_w is None and self.budget_w is None:
            return self.default_w[i]
        return self._ceil_w(i)

    def _recompute_max_w(self):
        self.max_w = [min(self._ceil_w(i), self.alloc_w[i]) if self.alloc_w is not None
                      else self._ceil_w(i) for i in range(len(self.handles))]

    def _limit_for(self, i: int) -> float:
        """The limit card i should sit at, given its bounds and its cap."""
        return max(self.min_w[i], min(self.max_w[i], self.cap_w[i]))

    def heat_state(self) -> Optional[str]:
        """What adaptive's fans should know about power held back for heat:
        "hold"       - a thermal hold while a card is busy;
        "recovering" - walking back up while a busy card is still below its ceiling;
        None         - normal.
        In both held states adaptive's fans follow the base curve (temperature only) and
        learn no quieter trim, which would otherwise keep the card at the target."""
        n = len(self.handles)
        busy = [i for i in range(n) if not self._busy or self._busy[i]]
        if not busy:
            return None
        if self.thermal_limited:
            return "hold"
        if self.recovery_walk and self.cap_w and any(
                self.cap_w[i] < self._ceil_w(i) - 0.5 for i in busy):
            return "recovering"
        return None

    def _reset_idle_caps(self, now: float):
        """With a total: a card idle for POWER_IDLE_DWELL_S and cooled below the release
        point gets its cap back to its ceiling. The cuts it collected while busy belong to a
        workload that's gone, and the total bounds it when it wakes. (Without a total the
        UPS budget's behaviour is unchanged: only the all-idle reset.)"""
        if (self.total_w is None or not self.cap_w or self.emergency_active
                or self.blind_active or self._was_power_floor):
            return
        release_c = (self._target_c - THERMAL_POWER_RECOVER_MARGIN_C
                     if self._target_c is not None else None)
        for i in range(len(self.handles)):
            if self._busy[i] or now - self._last_busy_at[i] < POWER_IDLE_DWELL_S:
                continue
            if self.cap_w[i] >= self._ceil_w(i) - 0.5:
                continue
            temp_c = self._card_temp(i)
            if release_c is not None and temp_c is not None and temp_c > release_c:
                continue
            self.cap_w[i] = self._ceil_w(i)
            log.info(f"POWER: GPU {i} idle for {now - self._last_busy_at[i]:.0f}s"
                     + (f" at {temp_c}C" if temp_c is not None else "")
                     + f" -> its earlier cuts cleared (cap {self.cap_w[i]:.0f} W)")

    def _hot_cards(self, temps: Optional[Dict[int, int]], trigger_c: int) -> List[int]:
        """The cards a thermal cut applies to: every card at or over the cut point, and the
        hottest one (a predictive cut comes before it's over). Without per-card
        temperatures, every card."""
        n = len(self.handles)
        if not temps:
            return list(range(n))
        hottest = max(temps, key=lambda g: temps[g])
        return [i for i in range(n) if i == hottest or temps.get(i, -1) >= trigger_c]

    def _cut_base(self, i: int) -> float:
        """The level a cut on card i starts from: its current level, except for an IDLE
        card held down only by its share of the total. That card's cap is cut instead, so
        the cut doesn't record its idle share as a limit of its own. Otherwise it would be
        stuck at 150 W when it wakes up and crawl back +20 W per 30 s (seen on pve-ai)."""
        if (self.alloc_w is not None and not self._busy[i]
                and self.max_w[i] < self.cap_w[i] - 0.5):
            return self.cap_w[i]
        return min(self.applied_w[i], self.cap_w[i])

    def _lower_to(self, i: int, watts: float):
        """A cut: set card i to `watts` or leave it where it is if that's lower."""
        self._set_limit(i, min(self.applied_w[i], watts))

    # ── the ceiling ──
    def apply_ceiling(self, request: Optional[List[float]], source: str):
        """Make `request` the active ceiling. None unpins (back to the hardware max)."""
        n = len(self.handles)
        if request is None:
            if self.ceiling_request is None and self.ceiling_w is None:
                return
            self.ceiling_request = None
            self.ceiling_w = None
            self._recompute_max_w()
            self._hold_log_pending = True
            log.info(f"POWER: ceiling cleared ({source}) — upper limit back to hardware max "
                     + "/".join(f"{w:.0f}" for w in self.hw_max_w) + " W")
            self._headroom_ticks = 0
            if self.budget_w is None and not self._holding():
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
        self._recompute_max_w()
        self._hold_log_pending = True
        log.info("POWER: ceiling set to " + "/".join(f"{w:.0f}" for w in effective)
                 + f" W per GPU ({source}; hardware max "
                 + "/".join(f"{w:.0f}" for w in self.hw_max_w) + " W)")

        # A lowered ceiling takes effect NOW: it's a safety bound, not a control target.
        # Raising it is left to the normal restore path so the UPS supervises the way up.
        for i in range(n):
            if self.applied_w[i] > self.max_w[i] + 0.5:
                self._set_limit(i, self.max_w[i])
        if self.cap_w:
            # a raised ceiling is walked up to, under supervision: the cap stays below it
            self.cap_w = [min(c, self._ceil_w(i)) for i, c in enumerate(self.cap_w)]
        if self.total_w is not None:
            self._allocate(time.monotonic(), refresh=False)
        if self.budget_w is None:
            self._hold_ceiling()

    def _holding(self) -> bool:
        return self.emergency_active or self.thermal_limited or self.blind_active

    def _release_to_default(self, source: str):
        """Hand the cards back to their default limit (no ceiling, no UPS budget).

        With a budget the restore branch walks limits up under supervision, but with no
        sensor nothing else ever raises a limit, so an unpin would otherwise leave the
        GPUs parked at the old cap forever.
        """
        for i in range(len(self.handles)):
            if abs(self.applied_w[i] - self.default_w[i]) >= 1.0:
                self._set_limit(i, self.default_w[i])
        self.cap_w = list(self.default_w)
        log.info(f"POWER: no ceiling and no UPS budget ({source}) — GPUs at their "
                 "default limit " + "/".join(f"{w:.0f}" for w in self.applied_w) + "W")

    def _card_climbing(self, i: int) -> bool:
        """Card i is warmer than two readings ago: the same "climbing" the cut uses."""
        recent = self._card_recent.get(i, [])
        return len(recent) >= 3 and recent[-1] > recent[-3]

    def _thermal_room(self, i: int) -> bool:
        """May card i take power back now? Not while it's still warming up from the last
        step (climbing): wait for it to settle. With adaptive, also only while it's below
        the target (its fans aren't at their max yet); at the target the walk pauses. The
        fixed curves' release already waited for target - 2."""
        if self._card_climbing(i):
            return False
        if self._fan_threshold is None or self._target_c is None:
            return True
        temp_c = self._card_temp(i)
        return temp_c is None or temp_c < self._target_c + THERMAL_POWER_MARGIN_C

    def _card_temp(self, i: int) -> Optional[int]:
        """Card i's last temperature; the hottest card's when per-card readings are missing."""
        if self._last_temps and i in self._last_temps:
            return self._last_temps[i]
        return self._last_hottest_c

    def raise_step_w(self, level_w: Optional[float] = None,
                     temp_c: Optional[int] = None) -> float:
        """How much one upward step may add to a card at `level_w` and `temp_c` (default:
        the hottest card): exponential in degrees below the release point, at most
        THERMAL_POWER_MAX_STEP_FRACTION of its level. Per card, so a cool card comes back
        fast even while the other one sits near the target."""
        if level_w is None:
            level_w = self.learned_cap_w
        hot_c = temp_c if temp_c is not None else self._last_hottest_c
        if (self._conservative_recovery or self._target_c is None
                or hot_c is None or level_w is None):
            return POWER_SLEW_UP_W
        below_c = (self._target_c - THERMAL_POWER_RECOVER_MARGIN_C) - hot_c
        if below_c <= 0:
            return POWER_SLEW_UP_W
        step = POWER_SLEW_UP_W * (2 ** min(below_c, 16))
        return max(POWER_SLEW_UP_W, min(step, THERMAL_POWER_MAX_STEP_FRACTION * level_w))

    def _walk_step(self, now: float) -> bool:
        """One recovery-walk step: each card's cap towards its base. True when done."""
        if not self.cap_w:
            return True
        n = len(self.handles)
        if all(self.cap_w[i] >= self._base_w(i) - 0.5 for i in range(n)):
            return True
        if now - self._last_limit_change < POWER_RESTORE_DWELL_S:
            return False
        steps = []
        for i in range(n):
            if self.cap_w[i] >= self._base_w(i) - 0.5 or not self._thermal_room(i):
                continue
            step = self.raise_step_w(self.cap_w[i], self._card_temp(i))
            steps.append(step)
            self.cap_w[i] = min(self._base_w(i), self.cap_w[i] + step)
            self._set_limit(i, self._limit_for(i))
        if not steps:
            return False                # every card still short of it sits at the target
        self._last_limit_change = now
        log.info(f"POWER: recovering after a hold, +{max(steps):.0f} W -> "
                 + "/".join(f"{w:.0f}" for w in self.applied_w) + "W")
        done = all(self.cap_w[i] >= self._base_w(i) - 0.5 for i in range(n))
        if done:
            self._conservative_recovery = False
        return done

    def _hold_ceiling(self):
        """No UPS budget: park every GPU on its cap. Idempotent; logs on change."""
        now = time.monotonic()
        n = len(self.handles)
        if self.emergency_active or self.blind_active:
            targets = list(self.min_w)
        elif self.thermal_limited and self.cap_w:
            targets = [self._limit_for(i) for i in range(n)]
        elif self.recovery_walk:
            if self._walk_step(now):
                self.recovery_walk = False
                log.info("POWER: recovery complete")
            return
        elif self.ceiling_w is not None or self.total_w is not None:
            self.cap_w = [self._base_w(i) for i in range(n)]
            targets = [self._limit_for(i) for i in range(n)]
        else:
            return
        before = tuple(self.applied_w)
        for i in range(n):
            self._set_limit(i, targets[i])
        if tuple(self.applied_w) != before or self._hold_log_pending:
            log.info("POWER: holding (no UPS budget), limits "
                     + "/".join(f"{w:.0f}" for w in self.applied_w) + "W"
                     + ("  [EMERGENCY]" if self.emergency_active else
                        "  [thermal derate active]" if self.thermal_limited else ""))
            self._hold_log_pending = False

    def enforce_ceiling(self):
        """Re-assert the cap against out-of-band changes, e.g. a manual `nvidia-smi -pl`.

        applied_w is a cache of what THIS daemon last wrote, so _set_limit()'s "already
        there" early-return is blind to anyone else moving the limit. A ceiling is a
        guarantee, so re-read the hardware and correct upward violations.
        """
        if (self.ceiling_w is None and self.alloc_w is None) or self.dry_run:
            return
        for i, h in enumerate(self.handles):
            try:
                actual = pynvml.nvmlDeviceGetPowerManagementLimit(h) / 1000.0
            except pynvml.NVMLError as e:
                log.error(f"GPU {i}: power limit read failed: {e}")
                continue
            if actual > max(self.min_w[i], self.max_w[i]) + 0.5:
                bound = "share of the total" if self.max_w[i] < self._ceil_w(i) - 0.5 else "ceiling"
                log.warning(f"⚠ POWER: GPU {i} limit is {actual:.0f} W, above its "
                            f"{max(self.min_w[i], self.max_w[i]):.0f} W {bound} — changed out "
                            "of band; re-applying")
                self.applied_w[i] = actual
                self._set_limit(i, self.max_w[i])

    def _set_limit(self, idx: int, watts: float):
        watts = max(self.min_w[idx], min(self.max_w[idx], watts))
        if abs(watts - self.applied_w[idx]) < 1.0:
            return
        if self.dry_run:
            log.info(f"  [dry-run] GPU {idx}: would set power limit {watts:.0f} W")
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
        self.cap_w = [max(self.min_w[i], min(self._ceil_w(i), watts))
                      for i in range(len(self.handles))]
        self._last_limit_change = time.monotonic()
        self._headroom_ticks = 0
        self._idle_since = None

    def _read_activity(self) -> Optional[Tuple[List[float], List[bool]]]:
        """Per card: board draw, and whether it's busy right now. None if any read fails."""
        draws: List[float] = []
        busy: List[bool] = []
        for i, h in enumerate(self.handles):
            try:
                draw_w = pynvml.nvmlDeviceGetPowerUsage(h) / 1000.0
                util_pct = pynvml.nvmlDeviceGetUtilizationRates(h).gpu
            except pynvml.NVMLError as e:
                log.error(f"GPU {i}: power/utilization read failed: {e}")
                return None
            draws.append(draw_w)
            busy.append(draw_w > POWER_IDLE_DRAW_W or util_pct > POWER_IDLE_UTIL_PCT)
        return draws, busy

    def _read_gpu_power_and_activity(self) -> Optional[Tuple[float, int]]:
        """Aggregate board draw and a conservative active-GPU count (a failed read counts
        as active: safer to keep a learned cap than reset to hardware max blind)."""
        reading = self._read_activity()
        if reading is None:
            self._last_busy_now = None
            return None
        draws, busy = reading
        self._last_busy_now = busy
        return sum(draws), sum(busy)

    # ── the total ceiling (plan 003) ──
    def apply_total(self, total: Optional[float], source: str):
        """Make `total` (W, all GPUs together) the active total ceiling. None removes it."""
        n = len(self.handles)
        if total is None:
            if self.total_w is None:
                return
            self.total_w = None
            self.alloc_w = None
            self._recompute_max_w()
            self._hold_log_pending = True
            log.info(f"POWER: total ceiling cleared ({source}) — each GPU bounded by its own "
                     "ceiling only")
            if self.budget_w is None:
                self._hold_ceiling()
            return
        floor = sum(self.min_w)
        if total < floor - 0.5:
            log.error(f"POWER: ignoring total ceiling {total:g} W from {source} — below the "
                      f"{floor:.0f} W the GPUs need at their minimum; keeping "
                      + (f"{self.total_w:g} W" if self.total_w is not None else "no total"))
            return
        if self.total_w is not None and abs(total - self.total_w) < 0.5:
            return
        self.total_w = float(total)
        self._hold_log_pending = True
        top = sum(self._ceil_w(i) for i in range(n))
        note = (f"; above the {top:.0f} W sum of the GPUs' ceilings, so it doesn't bind"
                if total >= top - 0.5 else "")
        log.info(f"POWER: total ceiling set to {total:g} W across {n} GPU(s) ({source}); "
                 f"soft: idle GPUs counted at their measured draw (10 s peak + "
                 f"{ALLOC_IDLE_RESERVE_MARGIN_W:.0f} W, in {ALLOC_IDLE_RESERVE_STEP_W:.0f} W "
                 f"steps), the busy ones share the rest{note}")
        self._allocate(time.monotonic(), refresh=True)
        if self.budget_w is None:
            self._hold_ceiling()

    def settle_after_start(self):
        """With a total and no hold to restore, start every card at its full share.

        The caps normally start from the cards' current limits, so a restart walks up under
        supervision. With a total, those limits are just the previous run's shares, and the
        total already bounds what the cards can draw, so a busy card needn't crawl back.
        """
        if (self.total_w is None or not self.cap_w or self._holding()
                or self._was_power_floor):
            return
        if self.budget_w is not None and not self._settle_now:
            self._settle_pending = True     # after the first UPS reading (deploy-team review)
            return
        self._settle_pending = False
        self.cap_w = [self._ceil_w(i) for i in range(len(self.handles))]
        for i in range(len(self.handles)):
            self._set_limit(i, self._limit_for(i))
        log.info("POWER: started with a total: every card at its full share "
                 + "/".join(f"{w:.0f}" for w in self.applied_w) + "W")

    def _share_high(self, i: int) -> float:
        """The most card i can take: its own ceiling, and below that its cap (a thermal or
        UPS cut). What it can't use goes to the other cards."""
        high = min(self._ceil_w(i), self.cap_w[i]) if self.cap_w else self._ceil_w(i)
        return max(self.min_w[i], high)

    @staticmethod
    def _water_fill(amount: float, lows: List[float], highs: List[float]) -> List[float]:
        """Share `amount` equally, each part between its low and high; whole watts, down."""
        def filled(level: float) -> float:
            return sum(min(h, max(lo, level)) for lo, h in zip(lows, highs))

        lo_level, hi_level = 0.0, max(highs)
        if filled(hi_level) <= amount:
            level = hi_level
        else:
            for _ in range(60):
                mid = (lo_level + hi_level) / 2
                if filled(mid) <= amount:
                    lo_level = mid
                else:
                    hi_level = mid
            level = lo_level
        return [float(int(min(h, max(lo, level)))) for lo, h in zip(lows, highs)]

    def _note_draws(self, draws: List[float], now: float):
        for i, w in enumerate(draws):
            if i < len(self._draw_hist):
                hist = self._draw_hist[i]
                hist.append((now, w))
                while hist and now - hist[0][0] > ALLOC_DRAW_WINDOW_S:
                    hist.pop(0)

    def _idle_reserve_w(self, i: int) -> float:
        """What an idle card is counted at in the (soft) total: what it actually draws (its
        highest draw over the last 10 s, plus a margin, rounded up to 25 W steps), never
        above its minimum limit. Until there are readings: the most an idle card can draw."""
        hist = self._draw_hist[i] if i < len(self._draw_hist) else []
        if not hist:
            return min(ALLOC_IDLE_RESERVE_W, self.min_w[i])
        peak = max(w for _, w in hist) + ALLOC_IDLE_RESERVE_MARGIN_W
        step = ALLOC_IDLE_RESERVE_STEP_W
        return min(self.min_w[i], step * max(1, math.ceil(peak / step)))

    def _shares(self, busy: List[bool]) -> List[float]:
        """Split the total (soft). The busy cards share it first, each up to what it can
        take, with every idle card counted at its idle reserve (75 W) rather than its 150 W
        minimum. What the busy cards can't use goes to the idle cards ahead of time, so a
        card that wakes up already has it (the busy cards take it back, lower before raise,
        when they can use it again). All busy or all idle: everyone shares.

        An idle card's share can be below its minimum limit; its limit then stays at the
        minimum (the hardware floor), which is the "soft" part."""
        n = len(self.handles)
        sharers = [i for i in range(n) if busy[i]]
        if not sharers or len(sharers) == n:
            sharers = list(range(n))
        idle = [i for i in range(n) if i not in sharers]
        shares = list(self.min_w)
        reserve = {i: self._idle_reserve_w(i) for i in idle}
        amount = self.total_w - sum(reserve.values())
        for i, w in zip(sharers, self._water_fill(amount, [self.min_w[i] for i in sharers],
                                                  [self._share_high(i) for i in sharers])):
            shares[i] = w
        for i in idle:
            shares[i] = reserve[i]
        leftover = self.total_w - sum(shares)
        if idle and leftover >= 1.0:
            amount = sum(reserve.values()) + leftover
            for i, w in zip(idle, self._water_fill(amount, [reserve[i] for i in idle],
                                                   [self._share_high(i) for i in idle])):
                shares[i] = w
        return shares

    def limit_sum_bound_w(self) -> Optional[float]:
        """The most the power LIMITS may add up to under the soft total: the total, plus
        (minimum - reserve) for each idle card, whose limit can't go below its minimum."""
        if self.total_w is None:
            return None
        return self.total_w + sum(max(0.0, self.min_w[i] - self._idle_reserve_w(i))
                                  for i in range(len(self.handles)) if not self._busy[i])

    def _note_activity(self, busy_now: List[bool], now: float):
        """Busy at once; idle only after ALLOC_IDLE_DWELL_S. An unreadable card is busy."""
        for i in range(len(self.handles)):
            if busy_now[i]:
                self._busy[i] = True
                self._quiet_since[i] = None
                if i < len(self._last_busy_at):
                    self._last_busy_at[i] = now
            elif self._busy[i]:
                if self._quiet_since[i] is None:
                    self._quiet_since[i] = now
                elif now - self._quiet_since[i] >= ALLOC_IDLE_DWELL_S:
                    self._busy[i] = False
                    self._quiet_since[i] = None

    def _refresh_busy(self, now: float):
        """Before a cut: a card that just started working must count as busy in THIS tick,
        or the cut would trim its cap instead of the power it's drawing (seen on pve-ai)."""
        if self.total_w is None:
            return
        reading = self._read_activity()
        self._note_activity(reading[1] if reading is not None else [True] * len(self.handles),
                            now)

    def _allocate(self, now: float, refresh: bool = True):
        """Move the total to the busy cards. Lower before raise: in a tick that lowers any
        card's share, raises wait for the next tick, so the limits never add up to more
        than the total, even while a card is still obeying a lower limit."""
        if not self.handles:
            return
        n = len(self.handles)
        if refresh:
            reading = self._read_activity()
            self._note_activity(reading[1] if reading is not None else [True] * n, now)
            if reading is not None:
                self._note_draws(reading[0], now)
        if self.total_w is None:
            return
        want = self._shares(self._busy)
        old = self.alloc_w if self.alloc_w is not None else list(self.max_w)
        lowering = [i for i in range(n) if want[i] < old[i] - 0.5]
        raising = [i for i in range(n) if want[i] > old[i] + 0.5]
        if lowering:
            new = [want[i] if i in lowering else old[i] for i in range(n)]
            raising = []
        else:
            new = want
        stuck = [i for i in range(n) if self.alloc_w is not None
                 and self.applied_w[i] > max(self.min_w[i], self.max_w[i]) + 0.5]
        if self.alloc_w is not None and not lowering and not raising and not stuck:
            return
        self.alloc_w = new
        self._recompute_max_w()
        for i in set(lowering) | set(stuck):
            self._lower_to(i, self.max_w[i])
        # a lowering that didn't take (an NVML write error) leaves a card above its share:
        # no card is raised until every card is within its share, or the sum could exceed
        # the total (deploy-team review of plan 003)
        stuck = [i for i in range(n)
                 if self.applied_w[i] > max(self.min_w[i], self.max_w[i]) + 0.5]
        if stuck:
            log.warning("⚠ POWER: GPU " + ",".join(str(i) for i in stuck) + " still above its "
                        "share of the total (a limit write failed); raises held until it's down")
            raising = []
        if raising and not (self.emergency_active or self.blind_active or self._was_power_floor):
            for i in raising:
                if self.cap_w:
                    self._set_limit(i, self._limit_for(i))
        log.info(f"POWER: total {self.total_w:g} W -> "
                 + " / ".join(f"GPU {i} {new[i]:.0f} W ("
                              + ("busy" if self._busy[i]
                                 else f"idle, counted at {self._idle_reserve_w(i):.0f} W")
                              + ")" for i in range(n))
                 + (" (raises next tick)" if lowering and any(want[i] > old[i] + 0.5
                                                               for i in range(n)) else ""))

    # ── temperature ──
    def observe_emergency(self, hottest_c: int, emergency_c: int):
        """Always armed. Emergency temperature for EMERGENCY_DWELL_S -> every GPU to min."""
        if not self.min_w:
            return
        now = time.monotonic()
        if not self.emergency_active:
            self._emergency_cool_since = None
            if hottest_c < emergency_c:
                self._emergency_hot_since = None
                return
            if self._emergency_hot_since is None:
                self._emergency_hot_since = now
                log.warning(f"⚠ EMERGENCY: hottest {hottest_c}C >= {emergency_c}C; cutting "
                            f"power if it holds for {EMERGENCY_DWELL_S:.0f}s")
                return
            hot_for = now - self._emergency_hot_since
            if hot_for < EMERGENCY_DWELL_S:
                return
            self.emergency_active = True
            self.recovery_walk = False
            self._conservative_recovery = True
            self.clamp_all(min(self.min_w),
                           f"EMERGENCY: {hottest_c}C >= {emergency_c}C for {hot_for:.0f}s")
            log.warning(f"⚠ EMERGENCY: all GPUs at minimum power; released once <= "
                        f"{emergency_c - EMERGENCY_RELEASE_DROP_C}C for "
                        f"{EMERGENCY_RELEASE_DWELL_S:.0f}s")
            return

        # active: keep every card on the floor, and watch for the release
        for i in range(len(self.handles)):
            self._set_limit(i, self.min_w[i])
        release_c = emergency_c - EMERGENCY_RELEASE_DROP_C
        if hottest_c > release_c:
            self._emergency_cool_since = None
            return
        if self._emergency_cool_since is None:
            self._emergency_cool_since = now
            return
        cool_for = now - self._emergency_cool_since
        if cool_for >= EMERGENCY_RELEASE_DWELL_S:
            self.emergency_active = False
            self._emergency_hot_since = None
            self._emergency_cool_since = None
            self.thermal_limited = False
            self.recovery_walk = True
            self._last_limit_change = now
            self._hold_log_pending = True
            log.info(f"EMERGENCY: {hottest_c}C <= {release_c}C for {cool_for:.0f}s -> cleared; "
                     f"power walks back up +{POWER_SLEW_UP_W:.0f} W every "
                     f"{POWER_RESTORE_DWELL_S:.0f}s")

    def observe_thermal(self, hottest_c: int, target_c: Optional[int],
                        fan_pct: Optional[int] = None, fan_threshold: Optional[int] = None,
                        temps: Optional[Dict[int, int]] = None):
        """Hold the temperature target by cutting power.

        Cuts once the card is >= target + THERMAL_POWER_MARGIN_C for the dwell. With
        `fan_threshold` (adaptive), the fans must also be at or above it; without it (the
        fixed curves), the fan speed is irrelevant. A thermal step arms the same
        fresh-UPS-feedback gate as a UPS step, so one stale whole-system reading can't make
        both inputs react.
        """
        self._last_hottest_c = hottest_c
        self._last_temps = dict(temps) if temps else None
        for i, t in (temps or {}).items():
            self._card_recent[i] = (self._card_recent.get(i, []) + [t])[-3:]
        self._fan_threshold = fan_threshold
        self._target_c = target_c
        now = time.monotonic()
        self._recent_c = (self._recent_c + [hottest_c])[-3:]
        rise_c = hottest_c - self._recent_c[-2] if len(self._recent_c) >= 2 else 0
        climbing = len(self._recent_c) >= 3 and hottest_c > self._recent_c[-3]
        if not self.cap_w or not self.min_w or self.emergency_active:
            return
        if target_c is None:
            if self.thermal_limited:
                self.thermal_limited = False
                self.recovery_walk = True
                self._last_limit_change = now
                log.info("THERMAL: temperature target cleared -> thermal hold released")
            self._thermal_hot_since = None
            self._thermal_cool_since = None
            return

        trigger_c = target_c + THERMAL_POWER_MARGIN_C
        fans_ok = fan_threshold is None or (fan_pct is not None and fan_pct >= fan_threshold)
        # rate of rise: only at the onset (not already holding), only while the card is
        # still below the cut point, and only on a sustained rise (two rising readings in a
        # row, averaged), so a single jump doesn't extrapolate into a big cut
        predicted_c = None
        recent = self._recent_c
        sustained = (len(recent) >= 3 and recent[-1] > recent[-2] > recent[-3])
        avg_rise_c = (recent[-1] - recent[-3]) / 2.0 if len(recent) >= 3 else 0.0
        if (not self.thermal_limited and sustained and hottest_c < trigger_c
                and avg_rise_c >= THERMAL_RATE_FAST_C_PER_READING
                and hottest_c >= trigger_c - THERMAL_RATE_WINDOW_C):
            predicted_c = hottest_c + int(round(avg_rise_c * THERMAL_RATE_LOOKAHEAD_READINGS))
        predictive = predicted_c is not None and predicted_c >= trigger_c
        # bounded: a prediction adds at most THERMAL_PREDICT_MAX_EXCESS_C beyond the target
        effective_c = (min(predicted_c, trigger_c + THERMAL_PREDICT_MAX_EXCESS_C) if predictive
                       else hottest_c)
        thermally_over = effective_c >= trigger_c and fans_ok

        if thermally_over:
            self._thermal_cool_since = None
            excess_c = max(0, effective_c - trigger_c)
            no_grace = predictive or climbing or excess_c >= THERMAL_POWER_SKIP_DWELL_EXCESS_C
            if self._thermal_hot_since is None:
                self._thermal_hot_since = now
                if not no_grace:
                    return          # steady at the target: wait the grace
            hot_for = now - self._thermal_hot_since
            if self.thermal_limited:
                if now - self._last_thermal_step < THERMAL_POWER_REPEAT_DWELL_S:
                    return
                if self._last_cut_temp_c is not None and hottest_c < self._last_cut_temp_c:
                    # still falling: hold, and compare with THIS reading next time, so a
                    # card that stops falling above the cut point gets cut again
                    self._last_cut_temp_c = hottest_c
                    self._last_thermal_step = now
                    return
            elif hot_for < THERMAL_POWER_INITIAL_DWELL_S and not no_grace:
                return
            before = tuple(self.applied_w)
            # only the hot cards are cut, each from its own level, never raised; with a total,
            # the share a cut card can't use goes to the cooler busy cards (plan 003)
            self._refresh_busy(now)
            hot = self._hot_cards(temps, trigger_c)
            levels = [self._cut_base(i) for i in range(len(self.handles))]
            step_w = max(THERMAL_POWER_MIN_STEP_W,
                         min(THERMAL_POWER_MIN_STEP_W * (2 ** excess_c),
                             THERMAL_POWER_MAX_STEP_FRACTION * max(levels[i] for i in hot)))
            for i in hot:
                cut = min(step_w, THERMAL_POWER_MAX_STEP_FRACTION * levels[i])
                self.cap_w[i] = max(self.min_w[i], levels[i] - max(THERMAL_POWER_MIN_STEP_W, cut))
                self._lower_to(i, self._limit_for(i))
            if not self.thermal_limited:
                self.thermal_holds += 1     # a new hold: adaptive's unlearning counts these
            self.thermal_limited = True
            self.recovery_walk = False
            self._thermal_hot_since = now
            self._last_thermal_step = now
            self._last_cut_temp_c = effective_c   # the predicted peak, when it was predictive
            self._last_limit_change = now
            if tuple(self.applied_w) != before and self._last_ups_reading is not None:
                total_w, status_flags = self._last_ups_reading
                self._feedback_wait_total_w = total_w
                self._feedback_wait_status_flags = status_flags
                self._feedback_wait_since = now
            fans = f" at {fan_pct}% fan" if fan_pct is not None else ""
            why = (f"rising {avg_rise_c:.1f}C/reading over 2 readings, predicted {predicted_c}C "
                   f"(cut sized for at most +{THERMAL_PREDICT_MAX_EXCESS_C}C)"
                   if predictive and predicted_c > hottest_c
                   else "still climbing, no grace" if climbing and hot_for < 1.0
                   else f"for {hot_for:.0f}s")
            which = ("" if len(hot) == len(self.handles)
                     else " on GPU " + ",".join(str(i) for i in hot))
            log.warning(f"⚠ THERMAL: {hottest_c}C{fans}, {why} (target {target_c}C, "
                        f"+{excess_c}C over) -> -{step_w:.0f} W{which}, power cap "
                        + "/".join(f"{w:.0f}" for w in self.applied_w) + " W")
            if (all(self.cap_w[i] <= self.min_w[i] + 0.5 for i in hot)
                    and hottest_c >= trigger_c):
                log.warning("⚠ THERMAL: already at the hardware power floor and still over the "
                            "target — only the fan profile or the card's own limits can help now")
            return

        self._thermal_hot_since = None
        if not self.thermal_limited:
            self._thermal_cool_since = None
            return
        adaptive = fan_threshold is not None
        if adaptive:            # any degree below the target: the fans have room again
            release_c = trigger_c - 1
            dwell_s = THERMAL_ADAPTIVE_RELEASE_DWELL_S
        else:
            release_c = target_c - THERMAL_POWER_RECOVER_MARGIN_C
            dwell_s = THERMAL_POWER_RECOVER_DWELL_S
        if hottest_c <= release_c:
            if self._thermal_cool_since is None:
                self._thermal_cool_since = now
                return
            cool_for = now - self._thermal_cool_since
            if cool_for >= dwell_s:
                self.thermal_limited = False
                self._thermal_cool_since = None
                self._last_cut_temp_c = None
                self.recovery_walk = True
                self._last_limit_change = now
                log.info(f"THERMAL: {hottest_c}C <= {release_c}C for {cool_for:.0f}s"
                         + (" (adaptive: below the target, the fans have room)" if adaptive
                            else "") + " -> thermal hold cleared; power walks back up")
        else:
            self._thermal_cool_since = None

    def observe_blind(self, blind: bool):
        """Temperatures unreadable: fail safe to minimum power; release after a good dwell."""
        if not self.min_w:
            return
        now = time.monotonic()
        if blind:
            self._blind_ok_since = None
            if not self.blind_active:
                self.blind_active = True
                self.recovery_walk = False
                self._conservative_recovery = True
                self.clamp_all(min(self.min_w), "BLIND: GPU temperature unreadable")
            for i in range(len(self.handles)):
                self._set_limit(i, self.min_w[i])
            return
        if not self.blind_active:
            return
        if self._blind_ok_since is None:
            self._blind_ok_since = now
            return
        if now - self._blind_ok_since >= BLIND_RELEASE_DWELL_S:
            self.blind_active = False
            self._blind_ok_since = None
            self.recovery_walk = True
            self._last_limit_change = now
            log.info("BLIND: temperatures readable again for "
                     f"{BLIND_RELEASE_DWELL_S:.0f}s -> cleared; power walks back up")

    def restore_holds(self, thermal: bool, emergency: bool):
        """Re-arm holds saved in the runtime state (a restart must not lift them)."""
        if emergency:
            self.emergency_active = True
            self._conservative_recovery = True
            self.clamp_all(min(self.min_w), "EMERGENCY hold restored from runtime state")
        if thermal:
            self.thermal_limited = True
            log.info("THERMAL: thermal hold restored from runtime state")

    # ── the UPS loop ──
    def update(self, force: bool = False):
        now = time.monotonic()
        # the total is moved every fan tick from NVML (no lag), not on the UPS interval
        self._allocate(now)
        self._reset_idle_caps(now)
        if not force and (now - self._last_run) < self.interval:
            return
        self._last_run = now

        self.enforce_ceiling()

        if self.emergency_active or self.blind_active:
            log.info(f"POWER: {'EMERGENCY' if self.emergency_active else 'BLIND'} hold, limits "
                     + "/".join(f"{w:.0f}" for w in self.applied_w) + "W")
            return

        if self.budget_w is None:
            self._hold_ceiling()
            return

        reading = self.ups.read()
        if reading is None:
            if self.ups.consecutive_failures >= POWER_MAX_READ_FAILURES:
                if not self._holding() or self.fallback_w < min(self.applied_w):
                    self.clamp_all(self.fallback_w,
                                   f"UPS unreadable x{self.ups.consecutive_failures} (flying blind)")
            return
        total_w, status_flags = reading
        self._last_ups_reading = (total_w, status_flags)
        matched_floor_flags = [flag for flag in self.floor_on_flags if flag in status_flags]

        if matched_floor_flags:
            self._settle_pending = False        # on battery at start: no full shares
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

        # A power-limit change and the UPS reading are asynchronous. Hold after every
        # downward step, and after a fast raise, until NUT publishes a different sample;
        # otherwise a cached total plus changing GPU draw invents non-GPU load or headroom.
        if self._feedback_wait_total_w is not None:
            old_total = self._feedback_wait_total_w
            old_status = self._feedback_wait_status_flags
            elapsed = now - self._feedback_wait_since
            if total_w != old_total or status_flags != old_status:
                log.info(f"POWER: fresh UPS feedback after the last step: {old_total:.0f}W/"
                         f"{' '.join(old_status)} -> {total_w:.0f}W/{' '.join(status_flags)}")
                self._feedback_wait_total_w = None
            elif elapsed < POWER_FEEDBACK_TIMEOUT_S:
                log.info(f"POWER: ups={total_w:.0f}W status={' '.join(status_flags)} — "
                         f"awaiting fresh feedback after the last step "
                         f"({elapsed:.0f}/{POWER_FEEDBACK_TIMEOUT_S:.0f}s), limits held "
                         + "/".join(f"{w:.0f}" for w in self.applied_w) + "W")
                return
            else:
                log.warning(f"⚠ POWER: no fresh UPS feedback for {elapsed:.0f}s; "
                            "allowing another decision")
                self._feedback_wait_total_w = None

        if self._settle_pending:
            # first UPS reading after a restart: no on-battery floor (handled above) and not
            # over budget, so the cards can take their full shares
            self._settle_pending = False
            if total_w <= self.budget_w and not self._holding():
                self._settle_now = True
                self.settle_after_start()
                self._settle_now = False
            else:
                log.info(f"POWER: first UPS reading {total_w:.0f} W is over the "
                         f"{self.budget_w:.0f} W budget: the cards walk up to their shares instead")

        gpu_state = self._read_gpu_power_and_activity()
        if gpu_state is None:
            return
        gpu_draw, active_gpu_count = gpu_state
        non_gpu = max(0.0, total_w - gpu_draw)

        # A learned cap belongs to the current workload. Once every GPU has been idle for
        # a full dwell, reset to MAX (in steps, if recovering from a hold).
        if active_gpu_count == 0:
            self._over_ticks = 0
            self._over_since = None
            self._headroom_ticks = 0
            if self._idle_since is None:
                self._idle_since = now
            idle_for = now - self._idle_since
            if idle_for >= POWER_IDLE_DWELL_S and not self.thermal_limited:
                if self.recovery_walk:
                    if self._walk_step(now):
                        self.recovery_walk = False
                        log.info("POWER: recovery complete")
                    return
                changed = False
                self.cap_w = [self._ceil_w(i) for i in range(len(self.handles))]
                for i in range(len(self.handles)):
                    if abs(self.applied_w[i] - self._limit_for(i)) >= 1.0:
                        self._set_limit(i, self._limit_for(i))
                        changed = True
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

        if not self.cap_w:
            self.cap_w = list(self.applied_w)
        n = len(self.handles)
        if self.total_w is not None and self._last_busy_now is not None:
            self._note_activity(self._last_busy_now, now)
        levels = [self._cut_base(i) for i in range(n)]

        over = total_w - self.budget_w
        if over > POWER_DEADBAND_W:
            self._headroom_ticks = 0
            self._over_ticks += 1
            if self._over_since is None:
                self._over_since = now
            over_for = now - self._over_since
            rating_w = self.ups.nominal_w
            if (rating_w is not None and total_w <= rating_w
                    and over_for < POWER_OVER_GRACE_S):
                log.info(f"POWER: ups={total_w:.0f}W over budget {self.budget_w:.0f}W by "
                         f"{over:.0f}W for {over_for:.0f}/{POWER_OVER_GRACE_S:.0f}s (grace; "
                         f"within the UPS's {rating_w} W rating) — limits held "
                         + "/".join(f"{w:.0f}" for w in self.applied_w) + "W")
                return
            # as before: every card is cut by the excess over the busy count (the idle ones
            # too, so a card that wakes up doesn't start above the trim), each from its own
            # level rather than from one shared cap
            cut_w = min(over / active_gpu_count, POWER_SLEW_DOWN_W)
            targets = [max(self.min_w[i], levels[i] - cut_w) for i in range(n)]
            raise_w = 0.0
            mode = "throttle"
        else:
            self._over_ticks = 0
            self._over_since = None
            if total_w >= self.budget_w - POWER_RESTORE_MARGIN_W:
                self._headroom_ticks = 0
                log.info(f"POWER: ups={total_w:.0f}W gpu={gpu_draw:.0f}W other={non_gpu:.0f}W "
                         f"budget={self.budget_w:.0f}W -> learned ceiling steady, limits held "
                         + "/".join(f"{w:.0f}" for w in self.applied_w) + "W")
                return
            if all(self.cap_w[i] >= self._ceil_w(i) - 0.5 for i in range(n)):
                self._headroom_ticks = 0
                self.recovery_walk = False
                self._conservative_recovery = False
                log.info(f"POWER: ups={total_w:.0f}W gpu={gpu_draw:.0f}W other={non_gpu:.0f}W "
                         f"budget={self.budget_w:.0f}W -> headroom, already at MAX "
                         + "/".join(f"{w:.0f}" for w in self.applied_w) + "W")
                return
            if self.thermal_limited:
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
            headroom_w = (self.budget_w - POWER_RESTORE_MARGIN_W) - total_w
            if self.recovery_walk:
                # recovering after a thermal hold: each card's exponential step (from its own
                # temperature), bounded by the measured headroom
                per_card_w = headroom_w / max(1, active_gpu_count)
                raises = [max(POWER_SLEW_UP_W,
                              min(self.raise_step_w(levels[i], self._card_temp(i)), per_card_w))
                          if self._thermal_room(i) else 0.0
                          for i in range(n)]
            else:
                raises = [POWER_SLEW_UP_W] * n  # a UPS-budget trim restores slowly (#2)
            raise_w = max(raises)
            # each card below its own bound raises its cap by its step
            targets = [min(self._ceil_w(i), self.cap_w[i] + raises[i])
                       if self.cap_w[i] < self._ceil_w(i) - 0.5 else self.cap_w[i]
                       for i in range(n)]
            self._headroom_ticks = 0
            mode = "restore"

        limits_before = tuple(self.applied_w)
        for i in range(n):
            cur = self.applied_w[i]
            want = max(self.min_w[i], min(self.max_w[i], targets[i]))
            delta = want - cur
            if mode == "throttle":
                if targets[i] < levels[i] - 0.5:
                    self.cap_w[i] = targets[i]
                    self._lower_to(i, want)
                continue
            # restore: the cap moves by the step; the limit follows within the card's bound
            if targets[i] > self.cap_w[i] + 0.5:
                if (abs(delta) < POWER_DEADBAND_W and want < self.max_w[i]
                        and targets[i] < self._ceil_w(i) - 0.5):
                    continue            # too small to bother, and not the last step
                self.cap_w[i] = targets[i]
                self._set_limit(i, self._limit_for(i))

        if tuple(self.applied_w) != limits_before:
            self._last_limit_change = now
            if mode == "throttle":
                # the budget binds now: any further restore is a UPS restore, +20 W (#2)
                self.recovery_walk = False
            # after a cut, or a raise bigger than the slow step, the next decision waits for
            # a UPS sample that has seen it (NUT can hold a value for ~36 s)
            if mode == "throttle" or (mode == "restore" and raise_w > POWER_SLEW_UP_W):
                self._feedback_wait_total_w = total_w
                self._feedback_wait_status_flags = status_flags
                self._feedback_wait_since = now
                log.info(f"POWER: {mode} step applied; awaiting fresh UPS feedback "
                         f"(timeout {POWER_FEEDBACK_TIMEOUT_S:.0f}s)")

        log.info(f"POWER: ups={total_w:.0f}W gpu={gpu_draw:.0f}W other={non_gpu:.0f}W "
                 f"budget={self.budget_w:.0f}W -> {mode}, caps="
                 + "/".join(f"{w:.0f}" for w in self.cap_w) + "W "
                 + "/".join(f"{w:.0f}" for w in self.applied_w) + "W")

    def restore_defaults(self):
        """On exit: never RAISE any card's power.

        A pinned ceiling outlives the process (`nvidia-smi -pl` semantics), and so does
        anything that lowered a card: a thermal or emergency hold, the blind fail-safe, a UPS
        on-battery floor, the fallback clamp, a UPS-budget trim. Stopping the service must not
        undo those; the next start re-evaluates. Each card ends at the lower of its current
        limit and what it would otherwise be restored to (its ceiling, or its default).
        """
        if self.dry_run:
            return
        for i, h in enumerate(self.handles):
            restore_to = self.ceiling_w[i] if self.ceiling_w is not None else self.default_w[i]
            try:
                # the device, not just our cache: an out-of-band `nvidia-smi -pl` lower is
                # kept too (#3)
                current = pynvml.nvmlDeviceGetPowerManagementLimit(h) / 1000.0
                final = min(current, self.applied_w[i], restore_to)
                why = ("ceiling" if self.ceiling_w is not None and final >= restore_to - 0.5
                       else "default" if final >= restore_to - 0.5 else "kept lowered")
                if current - final >= 1.0:
                    pynvml.nvmlDeviceSetPowerManagementLimit(h, int(final * 1000))
                log.info(f"  GPU {i}: power left at {final:.0f} W ({why})")
            except pynvml.NVMLError as e:
                log.error(f"  GPU {i}: could not settle power limit: {e}")


# ─────────────────────────── FAN CONTROLLER ───────────────────────────

class FanController:
    """Drives the fans from the live settings, and feeds temperatures to the governor.

    Deliberately one loop with the governor rather than a second daemon: cutting power
    lowers temperature, so two independent controllers would react to each other.
    """

    def __init__(self, store: SettingsStore, governor: PowerGovernor,
                 poll_interval: float = 2.0, emergency_c: int = DEFAULT_TEMP_EMERGENCY_C,
                 state_dir: str = DEFAULT_STATE_DIR, run_dir: str = DEFAULT_RUN_DIR,
                 dry_run: bool = False):
        self.store = store
        self.governor = governor
        self.poll_interval = poll_interval
        self.emergency_c = emergency_c
        self.state_dir = state_dir
        self.run_dir = run_dir
        self.dry_run = dry_run
        self.handles: List = []
        self.fan_counts: List[int] = []
        # what is in force (after validation), vs what the settings ask for
        self.profile = FanProfile(DEFAULT_PROFILE)
        self.mirror = False
        self.target: Optional[int] = None
        self.requested_profile = FanProfile(DEFAULT_PROFILE)
        # fan state
        self._manual: Dict[int, Optional[bool]] = {}     # gpu -> policy we last set
        self._speed: Dict[int, int] = {}                  # gpu -> % we last set
        self._settle: Dict[int, bool] = {}                # gpu -> easing down after a switch
        self._commanded_fan_pct: Optional[int] = None     # adaptive (one zone)
        self._target_trim_pct = 0.0
        self._pending_trim: Optional[Tuple[int, float]] = None
        self._trim_floor_pct = TARGET_TRIM_MIN_PCT      # unlearning: raised by power cuts
        self._seen_holds = 0
        self._floor_changed_at = time.monotonic()
        self.last_fan_pct: Optional[int] = None
        self.last_temps: Dict[int, int] = {}
        self.messages: List[str] = []
        self._written_generation = -1
        self._written_snapshot: Optional[tuple] = None
        self._written_at = 0.0
        self._read_history: List[bool] = []     # True = a card was unreadable (blind window)
        self._blind_reported = False
        # set here, not in run(): a stop requested during init() must be honoured (#6)
        self.running = True
        self._last_runtime_save = 0.0

    # ── setup ──
    def init(self):
        pynvml.nvmlInit()
        count = pynvml.nvmlDeviceGetCount()
        log.info(f"Found {count} NVIDIA GPU(s)")
        for i in range(count):
            handle = pynvml.nvmlDeviceGetHandleByIndex(i)
            name = pynvml.nvmlDeviceGetName(handle)
            fan_count = pynvml.nvmlDeviceGetNumFans(handle)
            self.handles.append(handle)
            self.fan_counts.append(fan_count)
            self._manual[i] = None
            log.info(f"  GPU {i}: {name} ({fan_count} fans)")
        log.info(f"Poll interval: {self.poll_interval}s; emergency temperature "
                 f"{self.emergency_c}C (fixed, set by the deployment)")

        changes, _ = self.store.refresh()
        ceiling, csource = self.store.resolve("ceiling")
        self.governor.handles = self.handles
        self.governor.init(ceiling, describe_source(self.store, "ceiling", csource))
        self._apply([c for c in changes if c[0] != "ceiling"], [], startup=True)
        self._restore_runtime_state()
        self.governor.settle_after_start()
        self._write_effective()

    # ── settings ──
    def _note(self, message: str, warning: bool = False):
        (log.warning if warning else log.info)(message)
        self.messages = (self.messages + [message])[-10:]

    def _apply(self, changes, masked, startup: bool = False):
        by_name = {name: (value, source) for name, value, source in changes}
        for name, value, source in masked:
            text = value.describe() if name == "profile" else format_setting(name, value)
            live, _ = self.store.resolve(name)
            what = {"profile": "FAN: profile", "mirror": "FAN: mirror",
                    "target": "TEMP: target", "ceiling": "POWER: ceiling",
                    "total": "POWER: total ceiling"}[name]
            if source == "override":
                hidden = (f"masked until restart by a temporary override "
                          f"({format_setting(name, live)})")
            else:
                hidden = (f"masked by a command-line flag ({format_setting(name, live)}); remove "
                          f"the flag from the unit for the saved setting to apply")
            self._note(f"{what} set to {text} in the saved settings — {hidden}")
        if "ceiling" in by_name:
            value, source = by_name["ceiling"]
            self.governor.apply_ceiling(value, describe_source(self.store, "ceiling", source))
        if "total" in by_name:
            value, source = by_name["total"]
            described = describe_source(self.store, "total", source)
            if value is None and source == "default":
                # the file is gone or unreadable: fail closed (deploy-team review #2, the
                # operator's choice B). Only an explicit `none` clears the total.
                keep = (self.governor.total_w if self.governor.total_w is not None
                        else self._remembered_total())
                if keep is not None:
                    self._note(f"⚠ POWER: no readable total ceiling file "
                               f"({self.store.path('saved', 'total')}) — keeping the last total "
                               f"{keep:g} W; write `none` to clear it", True)
                    value, described = keep, "kept: the file is missing or unreadable"
            self.governor.apply_total(value, described)
        if "target" in by_name:
            value, source = by_name["target"]
            self._set_target(value, describe_source(self.store, "target", source))
        if "mirror" in by_name:
            value, source = by_name["mirror"]
            if value != self.mirror or startup:
                self.mirror = value
                self._note(f"FAN: mirror {'on' if value else 'off'} "
                           f"({describe_source(self.store, 'mirror', source)})")
                self._begin_switch()
        if "profile" in by_name:
            value, source = by_name["profile"]
            self.requested_profile = value
        if "profile" in by_name or "target" in by_name or startup:
            source = self.store.resolve("profile")[1]
            self._resolve_profile(describe_source(self.store, "profile", source), startup)

    def _set_target(self, value: Optional[int], source: str):
        highest = self.emergency_c - TEMP_TARGET_EMERGENCY_GAP_C
        if value is not None and value > highest:
            self._note(f"TEMP: ignoring target {value}C ({source}) — it must be at most "
                       f"{highest}C, so the power cut comes well before the "
                       f"{self.emergency_c}C emergency; keeping "
                       + (f"{self.target}C" if self.target is not None else "no target"), True)
            return
        if value == self.target:
            return
        self.target = value
        if value is None:
            self._note(f"TEMP: target cleared ({source})")
        else:
            self._note(f"TEMP: target set to {value}C ({source}); power is cut at "
                       f"{value + THERMAL_POWER_MARGIN_C}C after a "
                       f"{THERMAL_POWER_INITIAL_DWELL_S:.0f}s grace (none at "
                       f"{value + THERMAL_POWER_SKIP_DWELL_EXCESS_C}C+)")

    def _resolve_profile(self, source: str, startup: bool):
        want = self.requested_profile
        if want.name == "adaptive" and self.target is None:
            if self.profile.name == "adaptive":
                self._note(f"⚠ FAN: temp target cleared while the profile is adaptive -> falling "
                           f"back to native (adaptive needs a target)", True)
                self._switch_profile(FanProfile("native"), "fallback")
            else:
                self._note(f"FAN: ignoring profile adaptive ({source}) — it needs a temp target; "
                           f"keeping {self.profile.describe()}", True)
            return
        if want == self.profile and not startup:
            return
        self._switch_profile(want, source)

    def _switch_profile(self, profile: FanProfile, source: str):
        leaving_adaptive = self.profile.name == "adaptive" and profile.name != "adaptive"
        self.profile = profile
        self._note(f"FAN: profile set to {profile.describe()} ({source})")
        if profile.name == "adaptive":
            self._commanded_fan_pct = None     # re-seeded from the measured speed
            if self._pending_trim and self._pending_trim[0] == self.target:
                self._target_trim_pct = self._pending_trim[1]
                log.info(f"FAN: adaptive trim restored from runtime state "
                         f"({self._target_trim_pct:+.1f}%)")
            self._pending_trim = None
        elif leaving_adaptive:
            self._target_trim_pct = 0.0
        self._begin_switch()

    def _begin_switch(self):
        # our tables start from the speed the fans are running at, so there's no step down
        for gpu in range(len(self.handles)):
            self._settle[gpu] = True
            self._speed.pop(gpu, None)

    # ── fan I/O ──
    def _read_temps(self) -> Dict[int, int]:
        temps = {}
        for gpu, handle in enumerate(self.handles):
            try:
                temps[gpu] = pynvml.nvmlDeviceGetTemperature(handle, pynvml.NVML_TEMPERATURE_GPU)
            except pynvml.NVMLError as e:
                log.error(f"GPU {gpu}: Error reading temperature: {e}")
        return temps

    def _fans_owned(self) -> bool:
        return not (self.profile.name == "native" and not self.mirror)

    def _read_speed(self, gpu: int) -> Optional[int]:
        try:
            return pynvml.nvmlDeviceGetFanSpeed_v2(self.handles[gpu], 0)
        except pynvml.NVMLError:
            return None

    def _set_policy(self, gpu: int, manual: bool):
        if self._manual.get(gpu) == manual:
            return
        if self.dry_run:
            self._manual[gpu] = manual
            log.info(f"  [dry-run] GPU {gpu}: would set fan policy "
                     + ("manual" if manual else "factory"))
            return
        policy = (pynvml.NVML_FAN_POLICY_MANUAL if manual
                  else pynvml.NVML_FAN_POLICY_TEMPERATURE_CONTINOUS_SW)
        try:
            for fan in range(self.fan_counts[gpu]):
                pynvml.nvmlDeviceSetFanControlPolicy(self.handles[gpu], fan, policy)
            self._manual[gpu] = manual
            if not manual:
                self._speed.pop(gpu, None)
        except pynvml.NVMLError as e:
            self._manual[gpu] = None       # unknown: retry next tick
            log.error(f"GPU {gpu}: could not set fan policy: {e}")

    def _set_speed(self, gpu: int, pct: int):
        pct = max(0, min(100, int(pct)))
        self._set_policy(gpu, True)
        if self._speed.get(gpu) == pct and not self.dry_run:
            return
        if self.dry_run:
            if self._speed.get(gpu) != pct:
                log.info(f"  [dry-run] GPU {gpu}: would set fans {pct}%")
            self._speed[gpu] = pct
            return
        try:
            for fan in range(self.fan_counts[gpu]):
                pynvml.nvmlDeviceSetFanSpeed_v2(self.handles[gpu], fan, pct)
            self._speed[gpu] = pct
        except pynvml.NVMLError as e:
            self._speed.pop(gpu, None)
            log.error(f"GPU {gpu}: could not set fan speed {pct}%: {e}")

    @staticmethod
    def curve_pct(curve: List[Tuple[int, int]], temp: int) -> int:
        points = sorted(curve)
        if temp <= points[0][0]:
            return points[0][1]
        if temp >= points[-1][0]:
            return points[-1][1]
        for (t1, s1), (t2, s2) in zip(points, points[1:]):
            if t1 <= temp <= t2:
                return int(s1 + (temp - t1) / (t2 - t1) * (s2 - s1))
        return points[-1][1]

    def _eased(self, gpu: int, want: int) -> int:
        """After a switch, come down from the current speed gently instead of stepping."""
        if not self._settle.get(gpu):
            return want
        current = self._speed.get(gpu)
        if current is None:
            current = self._read_speed(gpu)
        if current is None or want >= current:
            self._settle[gpu] = False
            return want
        eased = max(want, current - TARGET_FAN_SLEW_DOWN_PCT)
        if eased == want:
            self._settle[gpu] = False
        return eased

    # ── one tick ──
    def update(self):
        changes, masked = self.store.refresh()
        if changes or masked:
            self._apply(changes, masked)
        if changes or masked or self.store.generation != self._written_generation:
            self._write_effective()
        temps = self._read_temps()
        missing = len(self.handles) - len(temps)
        # Fail safe (#1, #5): a card we can't read might be the hot one. The odd bad reading
        # is tolerated; BLIND_READ_FAILURES in the last BLIND_WINDOW_READINGS make it blind.
        self._read_history = (self._read_history + [missing > 0])[-BLIND_WINDOW_READINGS:]
        failures = sum(self._read_history)
        if failures >= BLIND_READ_FAILURES:
            if not self._blind_reported:
                self._blind_reported = True
                log.warning(f"⚠ BLIND: GPU temperature unreadable in {failures} of the last "
                            f"{len(self._read_history)} readings -> minimum power"
                            + ("; fans 100%" if self._fans_owned() else
                               "; native fans stay on the factory curve"))
            self.governor.observe_blind(True)
            if self._fans_owned():
                for gpu in range(len(self.handles)):
                    self._set_speed(gpu, 100)
            return
        if missing:
            # tolerated: count the missing card at its last known temperature, so it can't
            # reset the emergency timer or fake a sudden rise when it reads again
            for gpu in range(len(self.handles)):
                if gpu not in temps and gpu in self.last_temps:
                    temps[gpu] = self.last_temps[gpu]
        else:
            if self._blind_reported:
                self._blind_reported = False
                log.info("BLIND: all GPU temperatures readable again")
            self.governor.observe_blind(False)
        if not temps:
            return
        self.last_temps = temps
        hottest = max(temps.values())
        self.governor.observe_emergency(hottest, self.emergency_c)
        fan_pct, fan_threshold = self._update_fans(temps, hottest)
        self.last_fan_pct = fan_pct
        self.governor.observe_thermal(hottest, self.target, fan_pct, fan_threshold, temps)

    def _update_fans(self, temps: Dict[int, int], hottest: int) -> Tuple[Optional[int], Optional[int]]:
        """Drive the fans. Returns (the fan % we command, if any; adaptive's fan max)."""
        emergency = hottest >= self.emergency_c
        name = self.profile.name

        if name == "adaptive":
            return self._update_adaptive(hottest), self.profile.fan_max

        if name == "native":
            if not self.mirror:
                # hands off: the card's own factory curve, even in an emergency
                for gpu in temps:
                    self._set_policy(gpu, False)
                log.info(f"fans: native (factory, hands off) hottest={hottest}C")
                return None, None
            return self._update_native_mirror(temps, emergency), None

        curve = CURVES[name]
        if self.mirror:
            shared = 100 if emergency else self.curve_pct(curve, hottest)
            for gpu in temps:
                self._set_speed(gpu, 100 if emergency else self._eased(gpu, shared))
            label = "EMERGENCY 100%" if emergency else f"{self._speed.get(next(iter(temps)), shared)}%"
            log.info(f"fans: {name} mirror hottest={hottest}C -> {label}")
            return max(self._speed.get(g, shared) for g in temps), None
        commanded = []
        for gpu, temp in temps.items():
            want = 100 if temp >= self.emergency_c else self._eased(gpu, self.curve_pct(curve, temp))
            self._set_speed(gpu, want)
            commanded.append(want)
        log.info(f"fans: {name} per-card " + " ".join(
            f"GPU{g} {temps[g]}C->{self._speed.get(g, '?')}%" for g in temps))
        return max(commanded), None

    def _update_native_mirror(self, temps: Dict[int, int], emergency: bool) -> Optional[int]:
        if len(temps) < 2:
            for gpu in temps:
                self._set_policy(gpu, False)
            return None
        hotter = max(temps, key=temps.get)
        cooler = min(temps, key=temps.get)
        if emergency:
            for gpu in (hotter, cooler):
                self._set_speed(gpu, 100)
            log.warning(f"⚠ fans: native mirror EMERGENCY hottest={temps[hotter]}C -> both 100%")
            return 100
        self._set_policy(hotter, False)
        native_fan = self._read_speed(hotter)
        if native_fan is None:
            return None
        self._set_speed(cooler, native_fan)
        log.info(f"fans: native mirror GPU{hotter}(hot,factory) {temps[hotter]}C fan={native_fan}% "
                 f"-> GPU{cooler} {temps[cooler]}C set {native_fan}%")
        return native_fan

    def get_target_fan_demand(self, temp: int) -> int:
        """adaptive's feed-forward ramp: 30% at target-20C, rising to 100% at the target."""
        approach_start = self.target - TARGET_FAN_APPROACH_BAND_C
        if temp <= approach_start:
            return TARGET_FAN_MIN_PCT
        ratio = (temp - approach_start) / TARGET_FAN_APPROACH_BAND_C
        return max(TARGET_FAN_MIN_PCT, min(100, round(TARGET_FAN_MIN_PCT + ratio * (100 - TARGET_FAN_MIN_PCT))))

    def _unlearn(self):
        """Adaptive learns from its own power cuts: each new thermal hold raises the floor
        under the quiet trim; a cut-free TRIM_FLOOR_RELAX_S lowers it again."""
        now = time.monotonic()
        holds = self.governor.thermal_holds
        if holds > self._seen_holds:
            before = self._trim_floor_pct
            self._trim_floor_pct = min(0.0, self._trim_floor_pct
                                       + TRIM_FLOOR_RAISE_PCT * (holds - self._seen_holds))
            self._seen_holds = holds
            self._floor_changed_at = now
            log.info(f"FAN: adaptive unlearns after a power cut for heat: quiet trim floor "
                     f"{before:+.0f}% -> {self._trim_floor_pct:+.0f}%"
                     + (" (the fans now follow the base curve)"
                        if self._trim_floor_pct >= 0.0 else ""))
        elif (self._trim_floor_pct > TARGET_TRIM_MIN_PCT
              and now - self._floor_changed_at >= TRIM_FLOOR_RELAX_S):
            before = self._trim_floor_pct
            self._trim_floor_pct = max(TARGET_TRIM_MIN_PCT,
                                       self._trim_floor_pct - TRIM_FLOOR_RELAX_PCT)
            self._floor_changed_at = now
            log.info(f"FAN: adaptive, {TRIM_FLOOR_RELAX_S / 60:.0f} min without a power cut: "
                     f"quiet trim floor {before:+.0f}% -> {self._trim_floor_pct:+.0f}%")

    def _update_adaptive(self, hottest: int) -> int:
        """The learning controller: hold the card AT the target, never above the fan max.
        Both cards are one thermal zone (v1)."""
        self._unlearn()
        fan_max = self.profile.fan_max
        base = self.get_target_fan_demand(hottest)
        if self._commanded_fan_pct is None:
            measured = [s for s in (self._read_speed(g) for g in range(len(self.handles)))
                        if s is not None]
            self._commanded_fan_pct = min(fan_max, max(measured)) if measured else min(fan_max, base)

        # While power is held back for heat (the hold, and the walk back up), the fans just
        # follow temperature (the base curve) and no quieter trim is learned: a trim would
        # keep the card sitting at the target and starve the recovery (seen on pve-ai: trim
        # -26%, fans ~75%, power stuck at 150-235 W). Power comes back while the card is
        # below the target (adaptive release, see observe_thermal), and the fans rise with it. Otherwise the fans
        # quiet down just enough to keep the card near the target, and the hold, released
        # at target - 2 for 30 s, never lets go (seen on pve-ai: trim -26%, fans ~75%, the
        # card at 58-60 C, power stuck at 150-235 W with budget to spare).
        heat = self.governor.heat_state()
        error_c = hottest - self.target
        if heat is not None:
            self._target_trim_pct = 0.0
            demand = round(max(TARGET_FAN_MIN_PCT, min(fan_max, base)))
        else:
            if -TARGET_TRACKING_BAND_C <= error_c < 0:
                self._target_trim_pct -= (-error_c * TARGET_TRIM_DOWN_PCT_PER_C_S
                                          * self.poll_interval)
            elif error_c > 0:
                self._target_trim_pct += (error_c * TARGET_TRIM_UP_PCT_PER_C_S
                                          * self.poll_interval)
            self._target_trim_pct = max(self._trim_floor_pct, min(0.0, self._target_trim_pct))
            demand = round(max(TARGET_FAN_MIN_PCT, min(fan_max, base + self._target_trim_pct)))

        emergency = hottest >= self.emergency_c
        if emergency:
            command = 100
        elif demand > self._commanded_fan_pct:
            command = min(demand, self._commanded_fan_pct + TARGET_FAN_SLEW_UP_PCT)
        else:
            command = max(demand, self._commanded_fan_pct - TARGET_FAN_SLEW_DOWN_PCT)
        command = command if emergency else min(fan_max, command)
        self._commanded_fan_pct = command
        for gpu in range(len(self.handles)):
            self._set_speed(gpu, command)
        log.info(f"target: hottest={hottest}C target={self.target}C "
                 f"base={base}% trim={self._target_trim_pct:+.1f}% "
                 f"(floor {self._trim_floor_pct:+.0f}%) demand={demand}% "
                 f"command={command}% max={fan_max}%" + (" EMERGENCY" if emergency else "")
                 + ({"hold": " HOLD: no quiet trim", "recovering": " RECOVERING: no quiet trim"}
                    .get(heat, "") if not emergency else ""))
        return command

    # ── runtime state ──
    def _runtime_path(self) -> str:
        return os.path.join(self.state_dir, RUNTIME_STATE_FILE)

    def _restore_runtime_state(self):
        """Restore learned state. A bad file must never stop the daemon starting (#5): it is
        set aside as <file>.bad and the daemon starts fresh."""
        path = self._runtime_path()
        try:
            with open(path) as f:
                state = json.load(f)
            if not isinstance(state, dict):
                raise ValueError("not a JSON object")
            self._apply_runtime_state(state)
        except FileNotFoundError:
            return
        except (OSError, ValueError, TypeError, KeyError, AttributeError) as e:
            log.warning(f"⚠ runtime state {path} unusable ({e}); set aside, starting fresh")
            try:
                os.replace(path, path + ".bad")
            except OSError:
                pass

    def _remembered_total(self) -> Optional[float]:
        """The last total in force, from the runtime state (None if it was cleared)."""
        try:
            with open(self._runtime_path()) as f:
                state = json.load(f)
            w = (state.get("power") or {}).get("total_w") if isinstance(state, dict) else None
            return float(w) if w else None
        except (OSError, ValueError, TypeError, AttributeError):
            return None

    def _apply_runtime_state(self, state: dict):
        age = time.time() - float(state.get("saved_at", 0))
        adaptive = state.get("adaptive") or {}
        if adaptive.get("target_c") is not None and adaptive.get("trim_pct") is not None:
            trim = (int(adaptive["target_c"]), float(adaptive["trim_pct"]))
            if self.profile.name == "adaptive" and self.target == trim[0]:
                floor = adaptive.get("trim_floor_pct")
                if floor is not None:
                    self._trim_floor_pct = max(TARGET_TRIM_MIN_PCT, min(0.0, float(floor)))
                    self._floor_changed_at = time.monotonic()
                self._target_trim_pct = max(self._trim_floor_pct, min(0.0, trim[1]))
                log.info(f"FAN: adaptive trim restored from runtime state "
                         f"({self._target_trim_pct:+.1f}% for {trim[0]}C)")
            else:
                self._pending_trim = trim
        if age <= HOLD_RESTORE_MAX_AGE_S:
            self.governor.restore_holds(
                thermal=bool((state.get("thermal") or {}).get("limited")),
                emergency=bool((state.get("emergency") or {}).get("active")))
        elif (state.get("thermal") or {}).get("limited") or (state.get("emergency") or {}).get("active"):
            log.info(f"runtime state holds are {age:.0f}s old (> {HOLD_RESTORE_MAX_AGE_S:.0f}s) "
                     "— not restored")

    def save_runtime_state(self, force: bool = False):
        now = time.monotonic()
        if self.dry_run or (not force and now - self._last_runtime_save < RUNTIME_SAVE_INTERVAL_S):
            return
        self._last_runtime_save = now
        adaptive = ({"target_c": self.target, "trim_pct": round(self._target_trim_pct, 2),
                     "trim_floor_pct": round(self._trim_floor_pct, 1)}
                    if self.profile.name == "adaptive" and self.target is not None
                    else ({"target_c": self._pending_trim[0], "trim_pct": self._pending_trim[1]}
                          if self._pending_trim else None))
        state = {
            "saved_at": time.time(),
            "adaptive": adaptive,
            "thermal": {"limited": self.governor.thermal_limited},
            "emergency": {"active": self.governor.emergency_active},
            "power": {"total_w": self.governor.total_w},
        }
        try:
            os.makedirs(self.state_dir, exist_ok=True)
            _write_atomic(self._runtime_path(), json.dumps(state, indent=1) + "\n")
        except OSError as e:
            log.warning(f"⚠ could not save runtime state: {e}")
        self._write_effective()

    def _write_effective(self):
        if self.dry_run:
            return
        settings = {}
        for name in SETTING_NAMES:
            value, source = self.store.resolve(name)
            settings[name] = {"value": format_setting(name, value), "source": source}
        doc = {
            "generation": self.store.generation,
            "pid": os.getpid(),
            "at": time.time(),
            "settings": settings,
            "in_force": {"profile": self.profile.text(), "mirror": "on" if self.mirror else "off",
                         "target": format_setting("target", self.target),
                         "ceiling": format_setting("ceiling", self.governor.ceiling_request),
                         "total": format_setting("total", self.governor.total_w)},
            "emergency_c": self.emergency_c,
            "holds": {"thermal": self.governor.thermal_limited,
                      "emergency": self.governor.emergency_active,
                      "recovering": self.governor.recovery_walk},
            "power_limits_w": [round(w) for w in self.governor.applied_w],
            "total_shares_w": ([round(w) for w in self.governor.alloc_w]
                               if self.governor.alloc_w is not None else None),
            "temps_c": [self.last_temps.get(g) for g in range(len(self.handles))],
            "fan_pct": self.last_fan_pct,
            "adaptive_trim_floor_pct": (round(self._trim_floor_pct, 1)
                                        if self.profile.name == "adaptive" else None),
            "adaptive_trim_pct": (round(self._target_trim_pct, 1)
                                  if self.profile.name == "adaptive" else None),
            "messages": self.messages,
        }
        try:
            os.makedirs(self.run_dir, exist_ok=True)
            _write_atomic(os.path.join(self.run_dir, EFFECTIVE_FILE), json.dumps(doc, indent=1) + "\n")
            self._written_generation = self.store.generation
            self._written_snapshot = self._power_snapshot()
            self._written_at = time.monotonic()
        except OSError as e:
            log.warning(f"⚠ could not write {EFFECTIVE_FILE}: {e}")

    def _power_snapshot(self) -> tuple:
        g = self.governor
        return (tuple(round(w) for w in g.applied_w),
                tuple(round(w) for w in g.alloc_w) if g.alloc_w is not None else None,
                g.thermal_limited, g.emergency_active, g.blind_active, g.recovery_walk)

    def refresh_effective(self):
        """effective.json was rewritten only on a settings change, so the limits and shares
        in it went stale as soon as the governor moved them (deploy-team review): rewrite it
        when they change, and every EFFECTIVE_REFRESH_S for temperatures and fans."""
        if (self._power_snapshot() != self._written_snapshot
                or time.monotonic() - self._written_at >= EFFECTIVE_REFRESH_S):
            self._write_effective()

    # ── lifecycle ──
    def restore_auto_control(self):
        if self.dry_run:
            return
        log.info("Restoring factory fan control...")
        for gpu, (handle, fan_count) in enumerate(zip(self.handles, self.fan_counts)):
            for fan in range(fan_count):
                try:
                    pynvml.nvmlDeviceSetFanControlPolicy(
                        handle, fan, pynvml.NVML_FAN_POLICY_TEMPERATURE_CONTINOUS_SW)
                    log.info(f"  GPU {gpu} Fan {fan}: factory curve")
                except pynvml.NVMLError as e:
                    log.error(f"  GPU {gpu} Fan {fan}: could not restore: {e}")

    def run(self):
        log.info("Fan control + GPU power governor running.")
        try:
            while self.running:
                self.update()
                self.governor.update()
                self.refresh_effective()
                self.save_runtime_state()
                time.sleep(self.poll_interval)
        except KeyboardInterrupt:
            log.info("Interrupted by user")
        finally:
            self.save_runtime_state(force=True)
            self.governor.restore_defaults()
            self.restore_auto_control()
            pynvml.nvmlShutdown()
            log.info("Stopped.")

    def stop(self):
        self.running = False


# ─────────────────────────── HAND-RUN ───────────────────────────

def acquire_daemon_lock(run_dir: str):
    """Return an open, locked file if this process may drive the cards, else None."""
    os.makedirs(run_dir, exist_ok=True)
    fd = os.open(os.path.join(run_dir, LOCK_FILE), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as e:
        os.close(fd)
        if e.errno in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK):
            return None
        raise
    os.ftruncate(fd, 0)
    os.write(fd, f"{os.getpid()}\n".encode())
    return fd


def daemon_running(run_dir: str) -> bool:
    """True if another instance holds the daemon lock. Creates nothing."""
    try:
        fd = os.open(os.path.join(run_dir, LOCK_FILE), os.O_RDONLY)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    except OSError:
        return True
    finally:
        os.close(fd)


def lock_holder_pid(run_dir: str) -> Optional[int]:
    try:
        with open(os.path.join(run_dir, LOCK_FILE)) as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


def read_effective(run_dir: str) -> Optional[dict]:
    try:
        with open(os.path.join(run_dir, EFFECTIVE_FILE)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def clear_overrides(run_dir: str) -> List[str]:
    removed = []
    for name in SETTING_NAMES:
        path = os.path.join(run_dir, SETTING_FILES[name])
        try:
            os.remove(path)
            removed.append(SETTING_FILES[name])
        except FileNotFoundError:
            pass
    return removed


def signal_and_wait(run_dir: str, expect: Dict[str, str]) -> Tuple[bool, dict]:
    """SIGHUP the running daemon and wait for effective.json to show `expect`."""
    before = read_effective(run_dir) or {}
    pid = lock_holder_pid(run_dir)
    if pid is None:
        return False, {"messages": ["could not find the running daemon's PID"]}
    os.kill(pid, signal.SIGHUP)
    deadline = time.monotonic() + OVERRIDE_ACK_TIMEOUT_S
    while time.monotonic() < deadline:
        time.sleep(0.5)
        doc = read_effective(run_dir) or {}
        if doc.get("generation", -1) == before.get("generation", -1):
            continue
        in_force = doc.get("in_force", {})
        ok = all(in_force.get(k) == v for k, v in expect.items())
        return ok, doc
    return False, {"messages": [f"no acknowledgement from PID {pid} within "
                                f"{OVERRIDE_ACK_TIMEOUT_S:.0f}s"]}


def run_as_override(args, cli: Dict[str, object]) -> int:
    """Another instance (normally the service) drives the cards: hand our settings to it
    as a temporary override. Nothing saved is touched; a restart clears it."""
    run_dir = args.run_dir
    if args.clear_override:
        if args.dry_run:
            print("[dry-run] would remove the temporary overrides in " + run_dir)
            return 0
        try:
            removed = clear_overrides(run_dir)
        except OSError as e:
            print(f"cannot clear overrides in {run_dir}: {e} (run as root)", file=sys.stderr)
            return 3
        ok, doc = signal_and_wait(run_dir, {})
        print("Cleared temporary overrides: " + (", ".join(removed) or "none were set"))
        return 0 if ok or not removed else 2
    if not cli:
        doc = read_effective(run_dir)
        print(json.dumps(doc, indent=1) if doc else "The daemon is running; no state published yet.")
        return 0
    expect = {name: format_setting(name, value) for name, value in cli.items()}
    if args.dry_run:
        for name, text in expect.items():
            print(f"[dry-run] would set a temporary override {SETTING_FILES[name]} = {text} "
                  f"in {run_dir} (the running daemon keeps control; cleared on restart)")
        return 0
    previous: Dict[str, Optional[str]] = {}
    try:
        for name, text in expect.items():
            path = os.path.join(run_dir, SETTING_FILES[name])
            try:
                with open(path) as f:
                    previous[name] = f.read()
            except FileNotFoundError:
                previous[name] = None
            _write_atomic(path, text + "\n")
    except OSError as e:
        print(f"cannot write the override in {run_dir}: {e} (run as root)", file=sys.stderr)
        return 3
    ok, doc = signal_and_wait(run_dir, expect)
    if ok:
        print("Temporary override applied by the running daemon (until it restarts): "
              + ", ".join(f"{k}={v}" for k, v in expect.items()))
        return 0
    # Roll back, so a refused value can't linger in the override until the next restart.
    for name, text in previous.items():
        path = os.path.join(run_dir, SETTING_FILES[name])
        if text is None:
            try:
                os.remove(path)
            except FileNotFoundError:
                pass
        else:
            _write_atomic(path, text)
    signal_and_wait(run_dir, {})
    print("The running daemon did not take the override as given (rolled back):", file=sys.stderr)
    for message in doc.get("messages", [])[-3:]:
        print("  " + message, file=sys.stderr)
    return 2


# ─────────────────────────── MAIN ───────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="NVIDIA fan control and GPU power governor for headless hosts. Live "
                    "settings come from the state dir; flags given by hand apply to this run "
                    "only (or, if the service is running, become a temporary override).")
    live = parser.add_argument_group("live settings (hand-run: this run only, never saved)")
    live.add_argument("--mode", "-m", type=_argtype(parse_fan_profile), metavar="PROFILE",
                      help="Fan profile: native (factory curve, hands off), quiet, aggressive, "
                           "performance, max, or adaptive[:FANMAX] (hold the temp target, never "
                           "louder than FANMAX%%, default 100)")
    group = live.add_mutually_exclusive_group()
    group.add_argument("--mirror", dest="mirror", action="store_const", const=True,
                       help="Mirror on: both cards' fans follow the hotter card (back-to-back cards)")
    group.add_argument("--independent", dest="mirror", action="store_const", const=False,
                       help="Mirror off: each card follows its own temperature")
    live.add_argument("--temp-target", type=_argtype(parse_temp_target), metavar="C",
                      default=argparse.SUPPRESS,
                      help="Temperature target in C, or 'none'. Power is cut at the target (a 5s "
                           "grace while steady). Given without --mode, implies --mode adaptive "
                           "(the old behaviour)")
    live.add_argument("--power-ceiling", type=_argtype(parse_power_ceiling), metavar="WATTS",
                      help="Per-GPU power ceiling: W, W,W, or 'none'")
    live.add_argument("--power-ceiling-total", type=_argtype(parse_power_total), metavar="WATTS",
                      help="Total power ceiling for all GPUs together, moved to the busy cards: "
                           "W, or 'none'")

    safety = parser.add_argument_group("safety limits (set by the deployment)")
    safety.add_argument("--temp-emergency", type=int, default=DEFAULT_TEMP_EMERGENCY_C, metavar="C",
                        help=f"Emergency temperature: {EMERGENCY_DWELL_S:.0f}s at or above it cuts "
                             f"every GPU to minimum power (default {DEFAULT_TEMP_EMERGENCY_C})")
    safety.add_argument("--power-budget", type=float, metavar="WATTS",
                        help="Keep TOTAL UPS load under WATTS (the UPS is a read-only input)")
    safety.add_argument("--ups", default=DEFAULT_UPS_NAME,
                        help=f"NUT UPS name (default {DEFAULT_UPS_NAME}; see `upsc -l`)")
    safety.add_argument("--power-interval", type=float, default=DEFAULT_POWER_INTERVAL,
                        help=f"Seconds between UPS reads (default {DEFAULT_POWER_INTERVAL})")
    safety.add_argument("--power-fallback", type=float, default=DEFAULT_POWER_FALLBACK_W,
                        metavar="WATTS", help="Per-GPU limit if the UPS becomes unreadable")
    safety.add_argument("--power-floor-on", type=parse_power_floor_flags,
                        default=DEFAULT_POWER_FLOOR_FLAGS, metavar="FLAG[,FLAG...]",
                        help="NUT statuses that clamp every GPU to its floor (default OB,LB)")

    run = parser.add_argument_group("running")
    run.add_argument("--interval", "-i", type=float, default=2.0, help="Fan poll interval (s)")
    run.add_argument("--once", action="store_true", help="One tick, then exit")
    run.add_argument("--dry-run", action="store_true",
                     help="Log what would change (fans and power); touch nothing")
    run.add_argument("--power-dry-run", action="store_true",
                     help="Power governor only: log the power limits it WOULD set, while fans, "
                          "and everything else, run normally")
    run.add_argument("--clear-override", action="store_true",
                     help="Remove temporary overrides held by the running daemon")
    run.add_argument("--reset-fans", action="store_true",
                     help="Hand every fan back to its factory curve and exit. For the unit's "
                          "ExecStopPost=: NVML does NOT revert manual fans when the daemon dies "
                          "(measured 2026-09-29), so a crash would otherwise leave them stuck")
    run.add_argument("--state-dir", default=DEFAULT_STATE_DIR,
                     help=f"Saved settings + runtime state (default {DEFAULT_STATE_DIR})")
    run.add_argument("--run-dir", default=DEFAULT_RUN_DIR,
                     help=f"Overrides, lock, effective state (default {DEFAULT_RUN_DIR})")
    return parser


def cli_settings(args) -> Dict[str, object]:
    cli: Dict[str, object] = {}
    if args.mode is not None:
        cli["profile"] = args.mode
    if args.mirror is not None:
        cli["mirror"] = args.mirror
    if hasattr(args, "temp_target"):
        cli["target"] = args.temp_target
        if args.mode is None and args.temp_target is not None:
            cli["profile"] = FanProfile("adaptive")     # legacy: --temp-target meant adaptive
    if args.power_ceiling is not None:
        cli["ceiling"] = args.power_ceiling or None
    if args.power_ceiling_total is not None:
        cli["total"] = args.power_ceiling_total or None
    return cli


def reset_fans() -> int:
    """Put every fan on every GPU back on its factory curve. Safe to run any time."""
    pynvml.nvmlInit()
    failed = 0
    for gpu in range(pynvml.nvmlDeviceGetCount()):
        handle = pynvml.nvmlDeviceGetHandleByIndex(gpu)
        for fan in range(pynvml.nvmlDeviceGetNumFans(handle)):
            try:
                pynvml.nvmlDeviceSetFanControlPolicy(
                    handle, fan, pynvml.NVML_FAN_POLICY_TEMPERATURE_CONTINOUS_SW)
            except pynvml.NVMLError as e:
                failed += 1
                log.error(f"GPU {gpu} Fan {fan}: could not reset: {e}")
    pynvml.nvmlShutdown()
    log.info("Fans handed back to the factory curve" + (f" ({failed} failed)" if failed else ""))
    return 2 if failed else 0


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.reset_fans:
        return reset_fans()
    if not TEMP_EMERGENCY_MIN_C <= args.temp_emergency <= TEMP_EMERGENCY_MAX_C:
        print(f"--temp-emergency must be {TEMP_EMERGENCY_MIN_C}..{TEMP_EMERGENCY_MAX_C}", file=sys.stderr)
        return 3
    cli = cli_settings(args)

    if args.dry_run:
        # A dry run drives nothing, so it takes no lock and creates no files.
        if daemon_running(args.run_dir):
            return run_as_override(args, cli)
        if args.clear_override:
            print("No daemon is running, so there are no temporary overrides to clear.")
            return 0
    else:
        try:
            lock = acquire_daemon_lock(args.run_dir)
        except OSError as e:
            print(f"cannot use {args.run_dir}: {e} (run as root, or pass --run-dir)", file=sys.stderr)
            return 3
        if lock is None:
            return run_as_override(args, cli)
        if args.clear_override:
            print("No daemon is running, so there are no temporary overrides to clear.")
            return 0
        # We drive the cards. A fresh controlling instance starts with no overrides:
        # that is what makes a service restart reset them.
        stale = clear_overrides(args.run_dir)
        if stale:
            log.info("Cleared stale temporary overrides: " + ", ".join(stale))

    log.info("NVIDIA fan control + GPU power governor")
    log.info("=" * 50)
    store = SettingsStore(args.state_dir, args.run_dir, cli)
    governor = PowerGovernor(handles=[], budget_w=args.power_budget, ups_name=args.ups,
                             interval=args.power_interval, fallback_w=args.power_fallback,
                             floor_on_flags=args.power_floor_on,
                             dry_run=args.dry_run or args.power_dry_run)
    controller = FanController(store, governor, poll_interval=args.interval,
                               emergency_c=args.temp_emergency, state_dir=args.state_dir,
                               run_dir=args.run_dir, dry_run=args.dry_run)

    def on_stop(sig, frame):
        log.info(f"Received signal {sig}")
        controller.stop()

    def on_reload(sig, frame):
        log.info("Received SIGHUP — re-reading settings")
        store.request_reload()

    signal.signal(signal.SIGTERM, on_stop)
    signal.signal(signal.SIGINT, on_stop)
    signal.signal(signal.SIGHUP, on_reload)

    controller.init()
    if args.once:
        controller.update()
        governor.update(force=True)
        log.info("Ran once.")
        return 0
    controller.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
