# -*- coding: utf-8 -*-
"""Contract tests for --power-ceiling, run against a fake NVML.

No GPU required. Run with:  python3 tests/test_power_ceiling.py

The fake in fake_pynvml.py models the parts of NVML the governor touches (power limit
constraints, current limit, draw, utilization). Tests assert on the value written to the
DEVICE, not on the daemon's applied_w cache — see
plans/decisions/006-lesson-applied-w-cache-blind-to-external-writes.md for why that
distinction matters.
"""
import sys, os, io, importlib.util, tempfile, logging

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fake_pynvml as fakenvml
sys.modules["pynvml"] = fakenvml

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
spec = importlib.util.spec_from_file_location("nfc", os.path.join(REPO, "nvidia-fan-control.py"))
nfc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(nfc)
logging.getLogger("nfc").setLevel(logging.CRITICAL)   # quiet; flip to INFO to debug

PASS, FAIL = [], []
def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ok   " if cond else "  FAIL ") + name + (("  -> " + detail) if detail and not cond else ""))

def make(n=2, budget=None, ceiling=None, path=None, persist=False, fallback=300.0,
         ceiling_source_default=False, **devkw):
    fakenvml.reset(n, **devkw)
    handles = [fakenvml.nvmlDeviceGetHandleByIndex(i) for i in range(n)]
    g = nfc.PowerGovernor(handles=handles, budget_w=budget, interval=0.0,
                          fallback_w=fallback, ceiling_request=ceiling,
                          ceiling_path=path, persist_startup_ceiling=persist)
    g.init()
    return g

def limits():
    return [d.limit for d in fakenvml.DEVS]

print("\n== parse_power_ceiling ==")
check("single value", nfc.parse_power_ceiling("300") == [300.0])
check("per-GPU list", nfc.parse_power_ceiling("300,400") == [300.0, 400.0])
check("whitespace tolerated", nfc.parse_power_ceiling(" 300 , 400 ") == [300.0, 400.0])
check("'none' -> [] (explicit unpin)", nfc.parse_power_ceiling("none") == [])
check("'off'/'0'/'' -> []", all(nfc.parse_power_ceiling(v) == [] for v in ("off", "0", "")))
for bad in ("abc", "-50", "300,abc"):
    try:
        nfc.parse_power_ceiling(bad); check(f"rejects {bad!r}", False)
    except Exception as e:
        check(f"rejects {bad!r}", type(e).__name__ == "ArgumentTypeError", type(e).__name__)

print("\n== ceiling bounds the actuator surface ==")
g = make(ceiling=[300.0])
check("max_w shrunk to ceiling", g.max_w == [300.0, 300.0], str(g.max_w))
check("hw_max_w preserved", g.hw_max_w == [600.0, 600.0], str(g.hw_max_w))
check("cards pushed down at init (reboot re-apply)", limits() == [300.0, 300.0], str(limits()))
check("restore target is the ceiling, not 600", max(g.max_w) == 300.0)
g._set_limit(0, 599.0)
check("_set_limit cannot exceed the ceiling", limits()[0] == 300.0, str(limits()))
g.clamp_all(500.0, "test")
check("clamp_all cannot exceed the ceiling", limits() == [300.0, 300.0], str(limits()))

print("\n== --power-fallback is bounded by the ceiling ==")
g = make(ceiling=[250.0], fallback=400.0)
g.clamp_all(g.fallback_w, "sensor blind")
check("fallback 400 W clamped to 250 W ceiling", limits() == [250.0, 250.0], str(limits()))

print("\n== hardware-range clamping ==")
g = make(ceiling=[50.0])          # below the 100 W hardware floor
check("ceiling below hw floor clamped up to min_w", g.ceiling_w == [100.0, 100.0], str(g.ceiling_w))
g = make(ceiling=[700.0])         # above the 600 W hardware max
check("ceiling above hw max clamped down", g.ceiling_w == [600.0, 600.0], str(g.ceiling_w))

print("\n== per-GPU ceilings ==")
g = make(ceiling=[300.0, 450.0])
check("per-GPU values applied independently", g.max_w == [300.0, 450.0], str(g.max_w))
check("limits follow per-GPU ceiling", limits() == [300.0, 450.0], str(limits()))
before = list(g.max_w)
g._apply_ceiling([1.0, 2.0, 3.0], "test")     # wrong length for 2 GPUs
check("length mismatch ignored, previous ceiling kept", g.max_w == before, str(g.max_w))

print("\n== sensor-less hold mode (no UPS) ==")
called = {"n": 0}
g = make(budget=None, ceiling=[300.0])
g.ups.read = lambda: called.__setitem__("n", called["n"] + 1) or None
fakenvml.DEVS[0].limit = 600.0                 # simulate an external nvidia-smi -pl 600
g.applied_w[0] = 600.0
g.update(force=True)
check("no UPS read in ceiling-only mode", called["n"] == 0, str(called["n"]))
check("hold re-pins a card that drifted up", limits() == [300.0, 300.0], str(limits()))

print("\n== budget mode: throttle below ceiling, restore up to it ==")
import time as _t
def busy(w=350.0, util=90):
    for d in fakenvml.DEVS: d.draw = w; d.util = util

g = make(budget=900.0, ceiling=[400.0])
busy(350.0)
g.ups.read = lambda: (1000.0, ("OL",))          # 100 W over budget
g.update(force=True)
check("throttles below the ceiling under UPS pressure", max(limits()) < 400.0, str(limits()))

g.ups.read = lambda: (500.0, ("OL",))           # lots of headroom
busy(100.0, 90)                                 # still active, but low draw
for _ in range(60):
    g._feedback_wait_total_w = None
    g._last_limit_change = _t.monotonic() - 999  # bypass the 30 s restore dwell
    g.update(force=True)
check("restores exactly to the ceiling, never 600", limits() == [400.0, 400.0], str(limits()))
check("learned cap lands on the ceiling", g.learned_cap_w == 400.0, str(g.learned_cap_w))

print("\n== idle reset respects the ceiling (new in the deployed law) ==")
g = make(budget=900.0, ceiling=[350.0])
for d in fakenvml.DEVS: d.limit = 200.0
g.applied_w = [200.0, 200.0]; g.learned_cap_w = 200.0
g.ups.read = lambda: (300.0, ("OL",))
for d in fakenvml.DEVS: d.draw = 16.0; d.util = 0   # idle
g._idle_since = _t.monotonic() - 999
g.update(force=True)
check("idle reset goes to the ceiling, not hw max", limits() == [350.0, 350.0], str(limits()))

print("\n== thermal derate coexists with the ceiling ==")
g = make(budget=None, ceiling=[400.0])          # ceiling-only + thermal
check("starts pinned at the ceiling", limits() == [400.0, 400.0], str(limits()))
# fans maxed and 2C over target for longer than the initial dwell -> derate
g._thermal_hot_since = _t.monotonic() - 999
g.observe_thermal(hottest_c=95, fan_pct=100, target_c=90)
derated = limits()[0]
check("thermal derates BELOW the ceiling", derated < 400.0, str(limits()))
check("thermal hold engaged", g._thermal_limited is True)
g.update(force=True)
check("hold does NOT fight the thermal derate", limits() == [derated, derated], str(limits()))
g.update(force=True)
check("still holding the derate on a second tick", limits() == [derated, derated], str(limits()))
# cool down -> thermal hold clears -> hold returns to the ceiling
g._thermal_cool_since = _t.monotonic() - 999
g.observe_thermal(hottest_c=80, fan_pct=60, target_c=90)
check("thermal hold cleared after cooling", g._thermal_limited is False)
g.update(force=True)
check("returns to the ceiling once thermal clears", limits() == [400.0, 400.0], str(limits()))

print("\n== ceiling at/under budget => constant power, no throttling ==")
g = make(budget=900.0, ceiling=[300.0])
g.ups.read = lambda: (760.0, ("OL",))
busy(300.0)
seen = set()
for _ in range(20):
    g._feedback_wait_total_w = None
    g.update(force=True)
    seen.add(tuple(limits()))
check("power stays pinned across ticks", seen == {(300.0, 300.0)}, str(seen))

print("\n== control file: live pin / unpin / persist ==")
tmpdir = tempfile.mkdtemp()
path = os.path.join(tmpdir, "sub", "power-ceiling")
g = make(budget=None, ceiling=[300.0], path=path, persist=True)
check("startup ceiling persisted to file", os.path.exists(path) and
      open(path).read().strip() == "300", repr(open(path).read() if os.path.exists(path) else None))

g2 = make(budget=None, ceiling=None, path=path)   # fresh daemon, e.g. after a reboot
ok, req = nfc.read_ceiling_file(path)
check("file re-read yields the persisted request", (ok, req) == (True, [300.0]), str((ok, req)))

g = make(budget=None, ceiling=[300.0], path=path, persist=True)
open(path, "w").write("450\n")
g.update(force=True)
check("live pin to 450 W picked up without restart", g.max_w == [450.0, 450.0], str(g.max_w))
check("hardware follows the live pin", limits() == [450.0, 450.0], str(limits()))

open(path, "w").write("none\n")
g.update(force=True)
check("live 'none' unpins back to hardware max", g.ceiling_w is None and g.max_w == [600.0, 600.0],
      str(g.max_w))

open(path, "w").write("  325 , 375   # benchmark run 7\n")
g.update(force=True)
check("per-GPU + comment + whitespace parsed live", g.max_w == [325.0, 375.0], str(g.max_w))

prev = list(g.max_w)
open(path, "w").write("garbage!!\n")
g.update(force=True)
check("malformed file ignored, cap retained", g.max_w == prev, str(g.max_w))

open(path, "w").write("410\n")
g._reload_requested = False
g._ceiling_stamp = g._ceiling_file_stamp()     # hide the mtime change
g.request_reload()
g.update(force=True)
check("SIGHUP forces a re-read past the mtime cache", g.max_w == [410.0, 410.0], str(g.max_w))

os.remove(path)
g.update(force=True)
check("removing the file unpins", g.ceiling_w is None and g.max_w == [600.0, 600.0], str(g.max_w))

print("\n== unpin releases the cards (ceiling-only mode has no restore loop) ==")
g = make(budget=None, ceiling=[300.0])
check("pinned at 300 W", limits() == [300.0, 300.0], str(limits()))
g._apply_ceiling(None, "test unpin")
check("unpin hands cards back to the default limit", limits() == [600.0, 600.0], str(limits()))
g = make(budget=None, ceiling=None, ceiling_source_default=True)
check("startup unpin with no budget releases too", limits() == [600.0, 600.0], str(limits()))
tmp2 = tempfile.mkdtemp(); p2 = os.path.join(tmp2, "ceil")
open(p2, "w").write("300\n")
g = make(budget=None, ceiling=[300.0], path=p2)
open(p2, "w").write("none\n")
g.update(force=True)
check("live unpin via file releases the cards", limits() == [600.0, 600.0], str(limits()))
g = make(budget=900.0, ceiling=[300.0])
g._apply_ceiling(None, "test unpin")
check("budget mode unpin does NOT jump (UPS-supervised restore instead)",
      limits() == [300.0, 300.0], str(limits()))


print("\n== ceiling is enforced against out-of-band changes ==")
g = make(budget=None, ceiling=[300.0])
check("pinned at 300 W", limits() == [300.0, 300.0], str(limits()))
fakenvml.DEVS[0].limit = 600.0        # simulate an external `nvidia-smi -pl 600`
g.update(force=True)
check("external raise above the ceiling is pulled back", limits() == [300.0, 300.0], str(limits()))
g2 = make(budget=900.0, ceiling=[350.0])
g2.ups.read = lambda: (400.0, ("OL",))
fakenvml.DEVS[1].limit = 600.0
g2.update(force=True)
check("enforced in budget mode too", fakenvml.DEVS[1].limit == 350.0, str(limits()))
g3 = make(budget=None, ceiling=None)
fakenvml.DEVS[0].limit = 600.0
g3.update(force=True)
check("no ceiling -> no enforcement, external value left alone",
      fakenvml.DEVS[0].limit == 600.0, str(limits()))

print("\n== shutdown semantics ==")
g = make(ceiling=[300.0])
g.restore_defaults()
check("ceiling outlives the process (nvidia-smi -pl semantics)", limits() == [300.0, 300.0], str(limits()))
g = make(ceiling=None, budget=900.0)
fakenvml.DEVS[0].limit = 200.0
g.restore_defaults()
check("no ceiling -> card defaults restored as before", limits() == [600.0, 600.0], str(limits()))

print("\n== unwritable state dir degrades gracefully ==")
g = make(budget=None, ceiling=[300.0], path="/proc/nope/power-ceiling", persist=True)
check("bad path does not crash init", g.max_w == [300.0, 300.0], str(g.max_w))
g.update(force=True)
check("bad path does not crash update", limits() == [300.0, 300.0], str(limits()))

print("\n== no ceiling => original behaviour ==")
g = make(budget=900.0, ceiling=None)
check("max_w still the hardware max", g.max_w == [600.0, 600.0], str(g.max_w))
check("nothing written at init", fakenvml.SET_CALLS == [], str(fakenvml.SET_CALLS))

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    print("FAILED: " + ", ".join(FAIL))
sys.exit(1 if FAIL else 0)
