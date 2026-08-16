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
import sys
import argparse
import logging
from typing import List, Tuple

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


class NvidiaFanController:
    def __init__(self, curve: List[Tuple[int, int]], poll_interval: float = 2.0,
                 sync: bool = True, mirror: bool = False):
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
                time.sleep(self.poll_interval)
        except KeyboardInterrupt:
            log.info("Interrupted by user")
        finally:
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

    controller = NvidiaFanController(curve, args.interval,
                                     sync=not args.independent, mirror=args.mirror)
    
    # Handle signals for clean shutdown
    def signal_handler(sig, frame):
        log.info(f"Received signal {sig}")
        controller.stop()
    
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)
    
    controller.init()
    
    if args.once:
        controller.update_fans_mirror() if args.mirror else controller.update_fans()
        log.info("Ran once. Fans will return to auto control after a few minutes.")
    else:
        controller.run()


if __name__ == "__main__":
    main()
