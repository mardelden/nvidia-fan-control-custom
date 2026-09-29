"""Regression tests for the power ceiling inside the UPS budget law (plan 001), on a fake NVML.

No GPU needed:  python3 tests/test_power_ceiling.py

Since plan 002 the settings files belong to SettingsStore (see test_fan_policy.py); these
tests drive PowerGovernor directly. Assertions are on what reached the DEVICE, not on the
governor's applied_w cache (plans/decisions/006).
"""
import importlib.util
import logging
import os
import sys
import time as _time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import fake_pynvml as nv  # noqa: E402
sys.modules["pynvml"] = nv

spec = importlib.util.spec_from_file_location(
    "nfc", os.path.join(os.path.dirname(HERE), "nvidia-fan-control.py"))
nfc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(nfc)
logging.getLogger("nfc").setLevel(logging.CRITICAL)

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ok   " if cond else "  FAIL ") + name + (("  -> " + str(detail)) if detail and not cond else ""))


def make(n=2, budget=None, ceiling=None, fallback=300.0, **dev):
    nv.reset(n, **dev)
    g = nfc.PowerGovernor(handles=[nv.nvmlDeviceGetHandleByIndex(i) for i in range(n)],
                          budget_w=budget, interval=0.0, fallback_w=fallback)
    g.init(ceiling, "test")
    return g


def limits():
    return [d.limit for d in nv.DEVS]


def busy(w=350.0, util=90):
    for d in nv.DEVS:
        d.draw, d.util = w, util


def fresh(g):
    g._feedback_wait_total_w = None
    g._last_limit_change = _time.monotonic() - 999


print("\n== the ceiling bounds the whole actuator surface ==")
g = make(ceiling=[300.0])
check("max_w shrunk to the ceiling", g.max_w == [300.0, 300.0], g.max_w)
check("hardware max preserved", g.hw_max_w == [600.0, 600.0], g.hw_max_w)
check("cards pushed down at start", limits() == [300.0, 300.0], limits())
g._set_limit(0, 599.0)
check("_set_limit cannot exceed the ceiling", limits()[0] == 300.0, limits())
g.clamp_all(500.0, "test")
check("clamp_all cannot exceed the ceiling", limits() == [300.0, 300.0], limits())
g = make(ceiling=[250.0], fallback=400.0)
g.clamp_all(g.fallback_w, "blind")
check("--power-fallback bounded by the ceiling", limits() == [250.0, 250.0], limits())
g = make(ceiling=[700.0])
check("ceiling above the hardware max clamped down", g.ceiling_w == [600.0, 600.0], g.ceiling_w)
g = make(ceiling=[300.0, 450.0])
g.apply_ceiling([1.0, 2.0, 3.0], "test")
check("wrong per-GPU length ignored, previous kept", g.max_w == [300.0, 450.0], g.max_w)

print("\n== UPS budget: throttle below the ceiling, restore up to it ==")
g = make(budget=900.0, ceiling=[400.0])
busy(350.0)
g.ups.read = lambda: (1000.0, ("OL",))
g.update(force=True)
check("throttles below the ceiling under UPS pressure", max(limits()) < 400.0, limits())
g.ups.read = lambda: (500.0, ("OL",))
busy(100.0, 90)
for _ in range(60):
    fresh(g)
    g.update(force=True)
check("restores exactly to the ceiling, never 600", limits() == [400.0, 400.0], limits())

print("\n== idle reset respects the ceiling ==")
g = make(budget=900.0, ceiling=[350.0])
for d in nv.DEVS:
    d.limit = 200.0
g.applied_w = [200.0, 200.0]
g.learned_cap_w = 200.0
g.ups.read = lambda: (300.0, ("OL",))
busy(16.0, 0)
g._idle_since = _time.monotonic() - 999
g.update(force=True)
check("idle reset goes to the ceiling, not the hardware max", limits() == [350.0, 350.0], limits())

print("\n== ceiling at/under the budget => constant power ==")
g = make(budget=900.0, ceiling=[300.0])
g.ups.read = lambda: (760.0, ("OL",))
busy(300.0)
seen = set()
for _ in range(20):
    g._feedback_wait_total_w = None
    g.update(force=True)
    seen.add(tuple(limits()))
check("power stays pinned across ticks", seen == {(300.0, 300.0)}, seen)

print("\n== out-of-band changes ==")
g = make(ceiling=[300.0])
nv.DEVS[0].limit = 600.0
g.update(force=True)
check("an external raise above the ceiling is pulled back", limits() == [300.0, 300.0], limits())
g = make(ceiling=None)
nv.DEVS[0].limit = 250.0
g.applied_w[0] = 250.0
g.update(force=True)
check("no ceiling: nothing enforced, the external value left alone", nv.DEVS[0].limit == 250.0, limits())

print("\n== UPS floor flag ==")
g = make(budget=900.0, ceiling=[300.0])
g.ups.read = lambda: (400.0, ("OB", "DISCHRG"))
busy(200.0)
g.update(force=True)
check("OB clamps every GPU to the hardware floor", limits() == [150.0, 150.0], limits())

print("\n== shutdown ==")
g = make(ceiling=[300.0])
g.restore_defaults()
check("a ceiling outlives the process", limits() == [300.0, 300.0], limits())
g = make(budget=900.0)
g.restore_defaults()
check("no ceiling, nothing lowered: cards left at their default", limits() == [600.0, 600.0], limits())
g = make(budget=900.0, ceiling=[300.0])
g.ups.read = lambda: (400.0, ("OB", "DISCHRG"))
busy(200.0)
g.update(force=True)
g.restore_defaults()
check("stop during a UPS on-battery floor keeps 150 W (never raised to the 300 W ceiling)",
      limits() == [150.0, 150.0], limits())
g = make(budget=900.0)
g.ups.read = lambda: (1000.0, ("OL",))
busy(350.0)
g.update(force=True)
trimmed = limits()[0]
g.restore_defaults()
check("stop after a UPS-budget trim keeps the trimmed limit (not raised to 600)",
      limits()[0] == trimmed and trimmed < 600.0, limits())
g = make(ceiling=[300.0])
g.thermal_limited = True
for d in nv.DEVS:
    d.limit = 220.0
g.applied_w = [220.0, 220.0]
g.restore_defaults()
check("a thermal hold on exit keeps the lowered limit (never raise a hot card)",
      limits() == [220.0, 220.0], limits())

print("\n== no ceiling => original behaviour ==")
g = make(budget=900.0)
check("max_w is the hardware max", g.max_w == [600.0, 600.0], g.max_w)
check("nothing written at start", nv.SET_CALLS == [], nv.SET_CALLS)

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    print("FAILED: " + "; ".join(FAIL))
sys.exit(1 if FAIL else 0)
