"""Contract tests for the governor and the live fan policy (plan 002), against a fake NVML.

No GPU needed:  python3 tests/test_fan_policy.py

Time is simulated (a fake clock), so the dwells (2 s, 5 s, 30 s) run instantly. Assertions
are on what reached the DEVICE (fake_pynvml.DEVS / *_CALLS), not on the daemon's caches:
see plans/decisions/006-lesson-applied-w-cache-blind-to-external-writes.md.
"""
import importlib.util
import json
import logging
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import fake_pynvml as nv  # noqa: E402
sys.modules["pynvml"] = nv

spec = importlib.util.spec_from_file_location(
    "nfc", os.path.join(os.path.dirname(HERE), "nvidia-fan-control.py"))
nfc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(nfc)
logging.getLogger("nfc").setLevel(logging.CRITICAL)


class Clock:
    def __init__(self):
        self.mono = 1000.0
        self.wall = 1_700_000_000.0

    def monotonic(self):
        return self.mono

    def time(self):
        return self.wall

    def sleep(self, s):
        self.advance(s)

    def advance(self, s):
        self.mono += s
        self.wall += s


CLOCK = Clock()
nfc.time = CLOCK

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ok   " if cond else "  FAIL ") + name + (("  -> " + str(detail)) if detail and not cond else ""))


def limits():
    return [d.limit for d in nv.DEVS]


def fans():
    return [d.fan for d in nv.DEVS]


def policies():
    return [d.policy for d in nv.DEVS]


MANUAL, FACTORY = nv.NVML_FAN_POLICY_MANUAL, nv.NVML_FAN_POLICY_TEMPERATURE_CONTINOUS_SW


def write(dirpath, name, text):
    os.makedirs(dirpath, exist_ok=True)
    with open(os.path.join(dirpath, nfc.SETTING_FILES[name]), "w") as f:
        f.write(text + "\n")
    CLOCK.advance(0.01)
    st = os.stat(os.path.join(dirpath, nfc.SETTING_FILES[name]))
    os.utime(os.path.join(dirpath, nfc.SETTING_FILES[name]),
             ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))


def rig(n=2, saved=None, cli=None, budget=None, emergency=92, **dev):
    """A controller + governor on a fresh fake machine. `saved` = {setting: text}."""
    nv.reset(n, **dev)
    state = tempfile.mkdtemp()
    run = tempfile.mkdtemp()
    for name, text in (saved or {}).items():
        write(state, name, text)
    store = nfc.SettingsStore(state, run, cli or {})
    gov = nfc.PowerGovernor(handles=[], budget_w=budget, interval=5.0)
    ctl = nfc.FanController(store, gov, poll_interval=2.0, emergency_c=emergency,
                            state_dir=state, run_dir=run)
    ctl.init()
    return ctl, gov, store, state, run


def tick(ctl, n=1, dt=2.0):
    for _ in range(n):
        ctl.update()
        ctl.governor.update()
        CLOCK.advance(dt)


def set_temp(*temps):
    for d, t in zip(nv.DEVS, temps):
        d.temp = t


# ───────────────────────────────────────────────────────────────────────────
print("\n== parsers ==")
P = nfc.parse_fan_profile
check("profile names", [P(x).name for x in ("native", "quiet", "aggressive", "performance", "max",
                                           "adaptive")] == list(nfc.PROFILE_NAMES))
check("adaptive fan max", P("adaptive:50") == nfc.FanProfile("adaptive", 50))
check("adaptive default fan max is 100", P("adaptive").fan_max == 100)
check("empty profile = native", P("").name == "native")
check("comments tolerated", P("quiet  # night").name == "quiet")
for bad, why in [("mirror", "mirror is a setting, not a profile"), ("quiet,max", "per-GPU refused"),
                 ("adaptive:20", "fan max below 30"), ("adaptive:101", "fan max above 100"),
                 ("quiet:5", "fixed curves take no parameter"), ("turbo", "unknown")]:
    try:
        P(bad)
        check(f"refuses {bad!r} ({why})", False)
    except ValueError:
        check(f"refuses {bad!r} ({why})", True)
check("mirror on/off", nfc.parse_mirror("on") is True and nfc.parse_mirror("off") is False
      and nfc.parse_mirror("") is False)
check("target none", nfc.parse_temp_target("none") is None)
check("target value", nfc.parse_temp_target("85") == 85)
try:
    nfc.parse_temp_target("40")
    check("target below 50 refused", False)
except ValueError:
    check("target below 50 refused", True)
check("ceiling list", nfc.parse_power_ceiling("250,450") == [250.0, 450.0])
check("ceiling none -> []", nfc.parse_power_ceiling("none") == [])


# ───────────────────────────────────────────────────────────────────────────
print("\n== defaults: no files = native, hands off ==")
ctl, gov, store, state, run = rig()
set_temp(70, 60)
nv.FAN_CALLS.clear()
tick(ctl, 5)
check("native: never writes a fan speed", nv.FAN_CALLS == [], nv.FAN_CALLS)
check("native: fans on the factory policy", policies() == [FACTORY, FACTORY], policies())
check("native: fans follow the factory curve", fans() == [nv.factory_curve(70), nv.factory_curve(60)],
      fans())
set_temp(93, 60)
nv.FAN_CALLS.clear()
tick(ctl, 3)
check("native + mirror off: even at the emergency temperature the fans are not touched",
      nv.FAN_CALLS == [] and policies() == [FACTORY, FACTORY], (nv.FAN_CALLS, policies()))
check("...but the emergency POWER cutoff still fires (150 W floor)", limits() == [150.0, 150.0],
      limits())


# ───────────────────────────────────────────────────────────────────────────
print("\n== fixed curves follow the curve exactly ==")
ctl, gov, store, state, run = rig(saved={"profile": "quiet"})
set_temp(50, 45)
tick(ctl, 3)
check("quiet, mirror off: each card on its own temperature",
      fans() == [nfc.FanController.curve_pct(nfc.QUIET_CURVE, 50),
                 nfc.FanController.curve_pct(nfc.QUIET_CURVE, 45)], fans())
write(state, "mirror", "on")
tick(ctl, 20)
check("quiet, mirror on: both cards follow the hotter card",
      fans() == [nfc.FanController.curve_pct(nfc.QUIET_CURVE, 50)] * 2, fans())
ctl, gov, store, state, run = rig(saved={"profile": "max"})
tick(ctl, 2)
check("max: 100% always", fans() == [100, 100], fans())


# ───────────────────────────────────────────────────────────────────────────
print("\n== native + mirror on = today's --mirror ==")
ctl, gov, store, state, run = rig(saved={"mirror": "on"})
set_temp(80, 50)
tick(ctl, 2)
check("hotter card stays on factory", policies()[0] == FACTORY, policies())
check("cooler card copies the hotter card's factory speed",
      policies()[1] == MANUAL and nv.DEVS[1].fan == nv.factory_curve(80), (policies(), fans()))
set_temp(50, 82)
tick(ctl, 2)
check("roles swap when the temperatures cross",
      policies() == [MANUAL, FACTORY] and nv.DEVS[0].fan == nv.factory_curve(82), (policies(), fans()))
set_temp(50, 93)
tick(ctl, 1)
check("mirror on + emergency: both fans 100%", fans() == [100, 100], fans())


# ───────────────────────────────────────────────────────────────────────────
print("\n== temperature target, fixed curves: cut AT the target after a 5 s grace, fans irrelevant ==")
ctl, gov, store, state, run = rig(saved={"profile": "quiet", "target": "80"})
set_temp(79, 70)
tick(ctl, 6)
check("no cut below the target", limits() == [600.0, 600.0], limits())
set_temp(80, 70)
tick(ctl, 1)
check("a card CLIMBING into the target (79 -> 80) is cut at once, no grace (-30 W); only that "
      "card: GPU1 at 70C keeps 600", limits() == [570.0, 600.0], limits())
check("thermal hold engaged", gov.thermal_limited)
cut = limits()[0]
set_temp(78, 70)       # = target-2
tick(ctl, 14)          # 28 s
check("hold stays before 30 s at target-2", limits()[0] == cut and gov.thermal_limited, limits())
tick(ctl, 2)
check("hold released after 30 s at target-2", not gov.thermal_limited)
check("released into a recovery walk", gov.recovery_walk)

print("\n== ...and the walk back up is stepped, never a jump ==")
before = limits()[0]
tick(ctl, 5)                 # 10 s: first step not due yet (30 s dwell)
check("no jump straight after release", limits()[0] == before, limits())
tick(ctl, 12)                # past the 30 s dwell
check("+20 W step after the dwell", limits()[0] == before + nfc.POWER_SLEW_UP_W, limits())


print("\n== exponential cut: 30 W x 2^(degrees over target), at most 25% of power per step ==")
def first_cut(temp, target=80, profile="quiet", wait=True):
    ctl, gov, *_ = rig(saved={"profile": profile, "target": str(target)})
    set_temp(temp, 50)
    ctl.update()                      # first reading over the cut point
    if wait:
        CLOCK.advance(5.0); ctl.update()
    return 600.0 - limits()[0]
check("at the target: -30 W after the 5 s grace", first_cut(80) == 30.0, first_cut(80))
check("+1C: -60 W after the grace", first_cut(81) == 60.0, first_cut(81))
check("+2C: -120 W after the grace", first_cut(82) == 120.0, first_cut(82))
check("+3C: 240 W capped at 25% of power: -150 W after the grace", first_cut(83) == 150.0,
      first_cut(83))
check("+3C does NOT skip the grace", first_cut(83, wait=False) == 0.0, first_cut(83, wait=False))
check("+4C: NO grace, capped at 25% of power (-150 W at 600 W)", first_cut(84, wait=False) == 150.0,
      first_cut(84, wait=False))

print("\n== a smarter measured cut (operator, 2026-09-30): averaged, 25% at most, proven floor ==")
def steady_card(ceiling="560", draw=540.0, temp=84, ticks=12):
    ctl, gov, *_ = rig(saved={"profile": "quiet", "ceiling": ceiling, "target": "85"})
    nv.DEVS[1].draw, nv.DEVS[1].util = draw, 95
    set_temp(45, temp)
    for _ in range(ticks):
        ctl.update(); gov.update(); CLOCK.advance(2.0)
    return ctl, gov
ctl, gov = steady_card()
check("GPU1 held 84C at 560 W for 24 s, drawing 540 W: proven at 560", gov._good_w.get(1) == 560.0,
      gov._good_w)
set_temp(45, 90)                             # production 02:50:33: one spike reading, 84 -> 90
ctl.update()
check("production replay: a one-reading spike 84 -> 90 is sized on the average (87C, +2): "
      "-120 W -> 440 W, not halved to 280", limits()[1] == 440.0, limits())
ctl, gov = steady_card(draw=100.0)
check("a light load (100 W at a 560 W limit) proves nothing", 1 not in gov._good_w, gov._good_w)
ctl, gov = steady_card()
gov._good_w[1] = 480.0                       # proven earlier at 480, since walked up to 560
set_temp(45, 90)
ctl.update()
check("overheating above its proven 480: one cut goes no lower than 480 (not 440)",
      limits()[1] == 480.0, limits())
CLOCK.advance(6.0); ctl.update()
check("...still at 90C at its proven level: the normal step (+5C averaged, 25%: 480 -> 360)",
      limits()[1] == 360.0, limits())

print("\n== hold while cooling: no second cut until the card stops cooling ==")
ctl, gov, *_ = rig(saved={"profile": "quiet", "target": "80"})
set_temp(86, 50)
ctl.update()
check("big overshoot cut at once, 25% (600 -> 450 W)", limits()[0] == 450.0, limits())
set_temp(85, 50)
CLOCK.advance(6.0); ctl.update()
check("falling 86 -> 85: power held, no second cut", limits()[0] == 450.0, limits())
set_temp(84, 50)
CLOCK.advance(6.0); ctl.update()
check("still falling 85 -> 84: still held", limits()[0] == 450.0, limits())
CLOCK.advance(6.0); ctl.update()
check("stopped falling at 84C (+4C over): cut again, 25% of 450 -> 337.5 W", limits()[0] == 337.5,
      limits())

print("\n== ...but a card that PLATEAUS over the target is cut again (pve-ai test B) ==")
ctl, gov, *_ = rig(saved={"profile": "quiet", "target": "60"})
set_temp(70, 50)
ctl.update()
check("+10C: one cut, capped at 25% (600 -> 450 W)", limits()[0] == 450.0, limits())
set_temp(63, 50)
CLOCK.advance(6.0); ctl.update()
check("falling 70 -> 63: held at 450 W", limits()[0] == 450.0, limits())
CLOCK.advance(6.0); ctl.update()
check("flat at 63C, still over the 60C target: cut again (+3C -> -240, capped 25% -> 337.5 W)",
      limits()[0] == 337.5, limits())

print("\n== exponential recovery: +20 W x 2^(degrees below target-2), at most +50% ==")
def recovery_step(cool_to, target=80):
    ctl, gov, *_ = rig(saved={"profile": "quiet", "target": str(target)})
    set_temp(target + 6, 50)
    ctl.update()                                   # one big cut: 600 -> 450 (25%)
    set_temp(cool_to, 50)
    tick(ctl, 17)                                  # 34 s at <= target-2: hold released
    assert not gov.thermal_limited, "hold should have released"
    before = limits()[0]
    tick(ctl, 16)                                  # past the 30 s raise dwell
    return limits()[0] - before
check("just under the release point (78C): +20 W", recovery_step(78) == 20.0, recovery_step(78))
check("2C below it (76C): +80 W", recovery_step(76) == 80.0, recovery_step(76))
check("well below (60C): a big step, here up to the 600 W max (+150 from 450)",
      recovery_step(60) == 150.0, recovery_step(60))

ctl, gov, *_ = rig(saved={"profile": "quiet", "target": "80"})
set_temp(93, 60)
ctl.update(); CLOCK.advance(2.0); ctl.update()
check("emergency hold at 150 W", limits() == [150.0, 150.0], limits())
set_temp(40, 38)
tick(ctl, 17)
check("emergency released", not gov.emergency_active)
before = limits()[0]
tick(ctl, 16)
check("after an EMERGENCY recovery stays +20 W even though the card is 38C below target",
      limits()[0] - before == 20.0, (before, limits()))

ctl, gov, *_ = rig(saved={"profile": "quiet", "target": "80"}, budget=900)
for d in nv.DEVS:
    d.draw, d.util = 300.0, 90
gov.ups.read = lambda: (500.0, ("OL",))
gov.update(force=True)
set_temp(86, 50)
ctl.update()                                       # cut 600 -> 450 (25%)
set_temp(60, 50)
tick(ctl, 17)
gov.ups.read = lambda: (800.0, ("OL",))            # only 50 W of headroom to 850 W
for _ in range(20):
    gov._feedback_wait_total_w = None
    gov.update(force=True); CLOCK.advance(10.0)
    ctl.update()
    if limits()[0] > 450.0:
        break
check("UPS budget mode: the raise is bounded by headroom (50 W over 2 cards -> +25 W, not +150)",
      450.0 < limits()[0] <= 475.0, limits())

print("\n== rate of rise: cut at the ONSET when the prediction reaches the target ==")
def rise(temps, target=75, profile="quiet", **kw):
    ctl, gov, *_ = rig(saved={"profile": profile, "target": str(target)}, **kw)
    for t in temps:
        set_temp(t, 40)
        ctl.update()
        CLOCK.advance(2.0)
    return ctl, gov
# smoothed and bounded (operator, 2026-09-30): production under vLLM saw one +4C reading at 83C
# predict 91-96C and halve the power
ctl, gov = rise([64, 70])          # ONE fast reading
check("one fast reading (64 -> 70) no longer predicts: no cut below the target",
      limits()[0] == 600.0, limits())
ctl, gov = rise([58, 64, 70])      # two rising readings, +6C/reading: predicted 82 >= 75
check("a sustained fast rise (58 -> 64 -> 70): cut BEFORE the 75C target, no grace",
      limits()[0] < 600.0, limits())
check("...bounded: sized for at most +2C (-120 W), not the predicted +7C (50%)",
      limits()[0] == 480.0, limits())
ctl, gov = rise([70, 71, 72, 73, 74])   # 0.5 C/s
check("slow climb (0.5 C/s): no prediction, nothing below the target", limits()[0] == 600.0, limits())
ctl, gov = rise([74, 73, 74, 73, 74])   # flicker
check("1 C flicker at 74C: no prediction, no cut", limits()[0] == 600.0, limits())
ctl, gov = rise([50, 55, 60])      # fast, but 15C below the target
check("fast rise more than 10C below the target: ignored", limits()[0] == 600.0, limits())
ctl, gov = rise([58, 64, 70, 76])  # predictive cut at 70, then 76 two seconds later
check("after a predictive cut, 76C is held for the repeat dwell, not cut again at once",
      limits()[0] == 480.0, limits())
ctl, gov = rise([76, 80, 84], target=85)   # production replay (vLLM): +4C/reading after a recovery
check("production replay: +4C/reading toward 85C -> one bounded cut at 84C (-120 W), not half "
      "the power", limits()[0] == 480.0, limits())
ctl, gov = rise([70, 74, 78])      # already over the target: measured, not predicted
check("a card measured over the target (78C after 74C) is cut by the measured average of the "
      "last two readings (+1C: -60 W), not a prediction", limits()[0] == 540.0, limits())
ctl, gov = rise([60, 66, 72], target=75, profile="adaptive:60")   # fans climb 10%/tick, not yet at 60
check("adaptive: prediction alone doesn't cut while the fans are below the fan max",
      limits()[0] == 600.0 or max(fans()) >= 60, (limits(), fans()))

print("\n== grace only when STEADY; prediction per reading (pve-ai cold-start replay) ==")
ctl, gov, *_ = rig(saved={"profile": "quiet", "target": "80"})
set_temp(80, 50)
ctl.update(); CLOCK.advance(2.0); ctl.update(); CLOCK.advance(2.0); ctl.update()
check("steady AT the target keeps the 5 s grace (no cut at 4 s)", limits()[0] == 600.0, limits())
CLOCK.advance(2.0); ctl.update()
check("...and is cut once the grace runs out (-30 W)", limits()[0] == 570.0, limits())
ctl, gov = rise([66, 67, 69, 70, 72])       # the real cold run: +1, then +2 per reading
check("cold-run replay: an average 1.5C/reading is below the fast threshold: no predictive cut",
      limits()[0] == 600.0, limits())
ctl, gov = rise([66, 67, 69, 70, 72, 75])
check("...the measured law cuts at the target instead (climbing, no grace: -30 W)",
      limits()[0] == 570.0, limits())
ctl, gov = rise([72, 73, 74, 75])           # slow climb, 1C per reading
check("slow climb 73 -> 74 -> 75: at the target and still climbing -> cut at once (-30 W)",
      limits()[0] == 570.0, limits())
ctl, gov = rise([75, 74, 75, 74, 75])       # flicker around the target
check("a 1C flicker around the target (75, 74, 75 ...) is steady -> no cut", limits()[0] == 600.0,
      limits())

# ───────────────────────────────────────────────────────────────────────────
print("\n== adaptive: holds AT the target, fan max, cut at fans >= max ==")
try:
    ctl, gov, store, state, run = rig(saved={"profile": "adaptive"})
    check("adaptive with no target is refused (stays native)", ctl.profile.name == "native",
          ctl.profile)
except Exception as e:
    check("adaptive with no target is refused (stays native)", False, e)
ctl, gov, store, state, run = rig(saved={"profile": "adaptive:50", "target": "80"})
check("adaptive:50 accepted with a target", ctl.profile == nfc.FanProfile("adaptive", 50))
set_temp(86, 60)
tick(ctl, 10)
check("fans never exceed the fan max", max(f for _, f in nv.FAN_CALLS) <= 50, nv.FAN_CALLS[-4:])
check("fans reach the fan max under heat", fans() == [50, 50], fans())
check("power cut on the hot card once fans >= max; the 60C card is not cut",
      limits()[0] < 600.0 and limits()[1] == 600.0, limits())

ctl, gov, store, state, run = rig(saved={"profile": "adaptive", "target": "80"})
set_temp(82, 60)
nv.DEVS[0].manual_fan = 30
tick(ctl, 3)       # fans still climbing (slew +10/tick), not yet at 100
check("adaptive (fan max 100): no cut while fans are below 100",
      limits() == [600.0, 600.0] or max(fans()) >= 100, (limits(), fans()))
tick(ctl, 12)
check("adaptive (fan max 100): cut once fans reach 100", limits()[0] < 600.0 and max(fans()) == 100,
      (limits(), fans()))
set_temp(93, 60)
ctl2, *_ = (ctl,)
ctl.profile = nfc.FanProfile("adaptive", 50)
tick(ctl, 1)
check("emergency forces 100% even above adaptive's fan max", fans() == [100, 100], fans())

print("\n== adaptive learns downward below the target, never below the floor ==")
ctl, gov, store, state, run = rig(saved={"profile": "adaptive", "target": "80"})
set_temp(76, 60)
tick(ctl, 60)
check("trim learned downward", ctl._target_trim_pct < 0, ctl._target_trim_pct)
check("never below the 30% floor", min(fans()) >= nfc.TARGET_FAN_MIN_PCT, fans())

print("\n== clearing the target while adaptive -> native ==")
os.remove(os.path.join(state, nfc.SETTING_FILES["target"]))
tick(ctl, 1)
check("falls back to native", ctl.profile.name == "native", ctl.profile)
tick(ctl, 1)
check("native fallback hands the fans back to the factory", policies() == [FACTORY, FACTORY],
      policies())

print("\n== target too close to the emergency is refused ==")
ctl, gov, store, state, run = rig(saved={"target": "91"}, emergency=92)
check("target 91 with emergency 92 refused", ctl.target is None, ctl.target)
ctl, gov, store, state, run = rig(saved={"target": "89"}, emergency=92)
check("target 89 (emergency-3) accepted", ctl.target == 89, ctl.target)


# ───────────────────────────────────────────────────────────────────────────
print("\n== emergency cutoff: 2 s at the emergency temperature -> 150 W ==")
ctl, gov, store, state, run = rig(saved={"profile": "quiet", "ceiling": "300"})
set_temp(92, 60)
ctl.update()
check("no cut on the first reading", limits() == [300.0, 300.0], limits())
CLOCK.advance(2.0)
ctl.update()
check("cut to the hardware floor after 2 s", limits() == [150.0, 150.0], limits())
check("mirror off: only the hot card's fans forced to 100% (cards are thermally separate)",
      fans() == [100, nfc.FanController.curve_pct(nfc.QUIET_CURVE, 60)], fans())
check("...power is cut on BOTH cards regardless", limits() == [150.0, 150.0], limits())
set_temp(70, 60)
tick(ctl, 20)
check("stays cut while above emergency-30 (62 C)", limits() == [150.0, 150.0] and gov.emergency_active,
      limits())
set_temp(62, 55)
tick(ctl, 14)
check("still held before 30 s at <= 62 C", gov.emergency_active)
tick(ctl, 2)
check("released after 30 s at <= 62 C", not gov.emergency_active)
tick(ctl, 3)
check("no jump on release", limits() == [150.0, 150.0], limits())
tick(ctl, 14)
check("walks up in +20 W steps", limits() == [170.0, 170.0], limits())

ctl_m, gov_m, *_ = rig(saved={"profile": "quiet", "mirror": "on"})
set_temp(92, 60)
ctl_m.update(); CLOCK.advance(2.0); ctl_m.update()
check("mirror on + emergency: both cards' fans forced to 100%", fans() == [100, 100], fans())

print("\n== emergency beats a UPS fallback that would raise power ==")
ctl, gov, store, state, run = rig(saved={"profile": "quiet"}, budget=900)
gov.ups.read = lambda: None
gov.ups.consecutive_failures = 5
set_temp(93, 60)
ctl.update(); CLOCK.advance(2.0); ctl.update(); gov.update(force=True)
check("UPS-blind fallback (300 W) does not lift an emergency 150 W", limits() == [150.0, 150.0], limits())


# ───────────────────────────────────────────────────────────────────────────
print("\n== live settings: files, SIGHUP, malformed input ==")
ctl, gov, store, state, run = rig(saved={"profile": "quiet"})
write(state, "profile", "performance")
tick(ctl, 1)
check("profile change picked up live", ctl.profile.name == "performance")
write(state, "profile", "turbo!!")
tick(ctl, 1)
check("malformed profile ignored, previous kept", ctl.profile.name == "performance")
write(state, "ceiling", "250")
tick(ctl, 1)
check("power ceiling change picked up live", limits() == [250.0, 250.0], limits())
os.remove(os.path.join(state, nfc.SETTING_FILES["profile"]))
tick(ctl, 1)
check("profile file removed -> native", ctl.profile.name == "native")


# ───────────────────────────────────────────────────────────────────────────
print("\n== precedence: flag > override > saved > default ==")
ctl, gov, store, state, run = rig(saved={"profile": "quiet", "ceiling": "300"})
write(run, "ceiling", "250")
tick(ctl, 1)
check("override beats saved", limits() == [250.0, 250.0], limits())
write(state, "ceiling", "280")
tick(ctl, 1)
check("a saved change under an override doesn't take effect", limits() == [250.0, 250.0], limits())
check("...but is acknowledged in the journal as masked",
      any("POWER: ceiling set to 280" in m and "masked" in m for m in ctl.messages), ctl.messages[-2:])
os.remove(os.path.join(run, nfc.SETTING_FILES["ceiling"]))
tick(ctl, 1)
check("override removed -> saved applies", limits() == [280.0, 280.0], limits())
ctl, gov, store, state, run = rig(saved={"profile": "quiet"}, cli={"profile": nfc.FanProfile("max")})
check("a flag beats the saved file", ctl.profile.name == "max")


# ───────────────────────────────────────────────────────────────────────────
print("\n== hand-runs never write the saved settings ==")
ctl, gov, store, state, run = rig(cli={"profile": nfc.FanProfile("max"), "ceiling": [250.0]})
tick(ctl, 2)
check("no saved setting files created by flags",
      not any(os.path.exists(os.path.join(state, f)) for f in nfc.SETTING_FILES.values()),
      os.listdir(state))
check("...but the flags are in force", ctl.profile.name == "max" and limits() == [250.0, 250.0])

print("\n== the legacy unit (--mirror --temp-target 85) keeps today's behaviour ==")
args = nfc.build_parser().parse_args(["--mirror", "--temp-target", "85"])
cli = nfc.cli_settings(args)
check("--temp-target without --mode implies adaptive", cli.get("profile") == nfc.FanProfile("adaptive"),
      cli)
check("--mirror -> mirror on", cli.get("mirror") is True)
args = nfc.build_parser().parse_args([])
check("no flags -> no CLI layer at all", nfc.cli_settings(args) == {}, nfc.cli_settings(args))


# ───────────────────────────────────────────────────────────────────────────
print("\n== runtime state survives a restart ==")
ctl, gov, store, state, run = rig(saved={"profile": "adaptive", "target": "80"})
set_temp(76, 60)
tick(ctl, 40)
learned = ctl._target_trim_pct
ctl.save_runtime_state(force=True)
doc = json.load(open(os.path.join(state, nfc.RUNTIME_STATE_FILE)))
check("trim saved with its target (and the unlearning floor)",
      doc["adaptive"] == {"target_c": 80, "trim_pct": round(learned, 2), "trim_floor_pct": -50.0},
      doc)
store2 = nfc.SettingsStore(state, run, {})
gov2 = nfc.PowerGovernor(handles=[], interval=5.0)
ctl2 = nfc.FanController(store2, gov2, emergency_c=92, state_dir=state, run_dir=run)
ctl2.init()
check("trim restored after restart", abs(ctl2._target_trim_pct - round(learned, 2)) < 0.01,
      ctl2._target_trim_pct)
write(state, "target", "78")
store3 = nfc.SettingsStore(state, run, {})
ctl3 = nfc.FanController(store3, nfc.PowerGovernor(handles=[], interval=5.0), emergency_c=92,
                         state_dir=state, run_dir=run)
ctl3.init()
check("trim for a different target is not restored", ctl3._target_trim_pct == 0.0, ctl3._target_trim_pct)

ctl, gov, store, state, run = rig(saved={"profile": "quiet"})
set_temp(93, 60)
ctl.update(); CLOCK.advance(2.0); ctl.update()
ctl.save_runtime_state(force=True)
CLOCK.advance(60)
nv.DEVS[0].limit = nv.DEVS[1].limit = 600.0      # a restart/reboot put the limits back
ctl2 = nfc.FanController(nfc.SettingsStore(state, run, {}), nfc.PowerGovernor(handles=[], interval=5.0),
                         emergency_c=92, state_dir=state, run_dir=run)
ctl2.init()
check("emergency hold (<5 min old) restored: limits back to 150 W", limits() == [150.0, 150.0], limits())
ctl.save_runtime_state(force=True)
CLOCK.advance(600)
nv.DEVS[0].limit = nv.DEVS[1].limit = 600.0
ctl3 = nfc.FanController(nfc.SettingsStore(state, run, {}), nfc.PowerGovernor(handles=[], interval=5.0),
                         emergency_c=92, state_dir=state, run_dir=run)
ctl3.init()
check("stale hold (>5 min) not restored", not ctl3.governor.emergency_active)


# ───────────────────────────────────────────────────────────────────────────
print("\n== exit: never raise power on a hot card ==")
ctl, gov, store, state, run = rig(saved={"profile": "quiet", "ceiling": "300"})
set_temp(93, 60)
ctl.update(); CLOCK.advance(2.0); ctl.update()
gov.restore_defaults()
check("exit during an emergency leaves 150 W (not the 300 W ceiling)", limits() == [150.0, 150.0],
      limits())
ctl, gov, store, state, run = rig(saved={"ceiling": "300"})
gov.restore_defaults()
check("exit with a ceiling holds the ceiling", limits() == [300.0, 300.0], limits())
ctl.restore_auto_control()
check("exit hands the fans back to the factory", policies() == [FACTORY, FACTORY])


print("\n== unreadable temperatures fail safe (deploy-team review #1) ==")
ctl, gov, *_ = rig(saved={"profile": "quiet", "ceiling": "300"})
set_temp(60, 60)
tick(ctl, 2)
nv.DEVS[1].temp_fail = True
tick(ctl, 2)
check("two unreadable readings: tolerated, power unchanged", limits() == [300.0, 300.0], limits())
tick(ctl, 1)
check("third unreadable reading: BLIND -> every GPU to minimum power", limits() == [150.0, 150.0],
      limits())
check("...and the fans we own go to 100%", fans() == [100, 100], fans())
nv.DEVS[1].temp_fail = False
tick(ctl, 10)
check("readable again: held for 30 s", limits() == [150.0, 150.0] and gov.blind_active, limits())
tick(ctl, 10)
check("released after 30 s of good readings", not gov.blind_active)
tick(ctl, 17)
check("...and power walks back up +20 W (conservative)", limits()[0] == 170.0, limits())
ctl, gov, *_ = rig()                       # native, mirror off
for d in nv.DEVS:
    d.temp_fail = True
nv.FAN_CALLS.clear()
tick(ctl, 4)
check("native + mirror off, blind: power cut, fans left to the factory curve",
      limits() == [150.0, 150.0] and nv.FAN_CALLS == [], (limits(), nv.FAN_CALLS))

print("\n== --power-dry-run is power-only again (review #4) ==")
nv.reset(2)
state, run = tempfile.mkdtemp(), tempfile.mkdtemp()
write(state, "profile", "max")
write(state, "ceiling", "250")
store = nfc.SettingsStore(state, run, {})
gov = nfc.PowerGovernor(handles=[], interval=5.0, dry_run=True)
ctl = nfc.FanController(store, gov, emergency_c=92, state_dir=state, run_dir=run, dry_run=False)
ctl.init()
tick(ctl, 2)
check("power-only dry run: no power writes", nv.SET_CALLS == [], nv.SET_CALLS)
check("...but the fans run normally", fans() == [100, 100], fans())
args = nfc.build_parser().parse_args(["--power-dry-run"])
check("--power-dry-run no longer sets the full --dry-run", args.power_dry_run and not args.dry_run)

print("\n== a bad runtime-state file can't crash-loop the daemon (review #5) ==")
for bad in ('[1, 2, 3]', '{"adaptive": {"target_c": "hot", "trim_pct": -5}}', '{"adaptive": "x"}',
            'not json'):
    nv.reset(2)
    state, run = tempfile.mkdtemp(), tempfile.mkdtemp()
    write(state, "profile", "adaptive")
    write(state, "target", "80")
    with open(os.path.join(state, nfc.RUNTIME_STATE_FILE), "w") as f:
        f.write(bad)
    try:
        c = nfc.FanController(nfc.SettingsStore(state, run, {}), nfc.PowerGovernor(handles=[], interval=5.0),
                              emergency_c=92, state_dir=state, run_dir=run)
        c.init()
        ok = (not os.path.exists(os.path.join(state, nfc.RUNTIME_STATE_FILE))
              and os.path.exists(os.path.join(state, nfc.RUNTIME_STATE_FILE + ".bad")))
        check(f"bad state {bad[:28]!r}: starts, file set aside as .bad", ok)
    except Exception as e:
        check(f"bad state {bad[:28]!r}: starts, file set aside as .bad", False, e)

print("\n== a stop requested during init() is honoured (review #6) ==")
ctl, gov, *_ = rig(saved={"ceiling": "300"})
ctl.stop()                                   # SIGTERM arrived while starting up
nv.FAN_CALLS.clear()
ctl.run()
check("run() returns at once instead of starting the loop", nv.FAN_CALLS == [] and not ctl.running)

# ───────────────────────────────────────────────────────────────────────────
print("\n== dry run touches nothing ==")
nv.reset(2)
state, run = tempfile.mkdtemp(), tempfile.mkdtemp()
write(state, "profile", "max")
write(state, "ceiling", "250")
store = nfc.SettingsStore(state, run, {})
gov = nfc.PowerGovernor(handles=[], interval=5.0, dry_run=True)
ctl = nfc.FanController(store, gov, emergency_c=92, state_dir=state, run_dir=run, dry_run=True)
ctl.init()
set_temp(93, 60)
tick(ctl, 4)
check("no power writes", nv.SET_CALLS == [], nv.SET_CALLS)
check("no fan writes", nv.FAN_CALLS == [], nv.FAN_CALLS)
check("no policy writes", nv.POLICY_CALLS == [], nv.POLICY_CALLS)
check("no files written", os.listdir(run) == [] and sorted(os.listdir(state)) ==
      sorted([nfc.SETTING_FILES["profile"], nfc.SETTING_FILES["ceiling"]]), (os.listdir(run), os.listdir(state)))


# ───────────────────────────────────────────────────────────────────────────
print("\n== the power ceiling (carried over from plan 001) ==")
ctl, gov, store, state, run = rig(saved={"ceiling": "300"})
check("ceiling bounds max_w", gov.max_w == [300.0, 300.0], gov.max_w)
check("cards pushed down at start", limits() == [300.0, 300.0], limits())
nv.DEVS[0].limit = 600.0
gov.update(force=True)
check("out-of-band raise corrected", limits() == [300.0, 300.0], limits())
write(state, "ceiling", "250,450")
tick(ctl, 1)
check("per-GPU ceiling", gov.max_w == [250.0, 450.0], gov.max_w)
write(state, "ceiling", "none")
tick(ctl, 1)
check("unpin with no UPS budget returns the cards to default", limits() == [600.0, 600.0], limits())
ctl, gov, store, state, run = rig(saved={"ceiling": "50"})
check("ceiling below the hardware floor clamped up", gov.ceiling_w == [150.0, 150.0], gov.ceiling_w)
ctl, gov, store, state, run = rig(saved={"ceiling": "300"}, budget=900)
gov.ups.read = lambda: (500.0, ("OL",))
for d in nv.DEVS:
    d.draw, d.util = 300.0, 90
gov.update(force=True)
check("UPS budget mode still respects the ceiling", max(limits()) <= 300.0, limits())


print("\n== a flaky sensor can't hide a hot card (re-review #5) ==")
ctl, gov, *_ = rig(saved={"profile": "quiet"})
set_temp(60, 95)
tick(ctl, 1)                                 # 95C seen: the emergency timer starts
nv.DEVS[1].temp_fail = True
tick(ctl, 1)                                 # unreadable: counted at its last known 95C
check("unreadable every other reading: the emergency still trips (last known temperature)",
      gov.emergency_active and limits() == [150.0, 150.0], (gov.emergency_active, limits()))
check("...and one bad reading alone isn't blind", not gov.blind_active)
ctl, gov, *_ = rig(saved={"profile": "quiet", "ceiling": "300"})
set_temp(60, 60)
tripped = None
for n, bad in enumerate([True, True, False, True, True, False], 1):
    nv.DEVS[1].temp_fail = bad
    tick(ctl, 1)
    if gov.blind_active and tripped is None:
        tripped = n
check("unreadable 2 readings in 3: blind by the 4th reading (3 of the last 5)", tripped == 4, tripped)
ctl, gov, *_ = rig(saved={"profile": "quiet", "target": "85"})
set_temp(70, 80)
tick(ctl, 3)
nv.DEVS[1].temp_fail = True
tick(ctl, 1)
nv.DEVS[1].temp_fail = False
tick(ctl, 2)
check("a card reading again after a gap is no sudden rise: no predictive cut",
      limits() == [600.0, 600.0] and not gov.thermal_limited, limits())

print("\n== UPS-budget restores stay slow; only a thermal recovery is fast (re-review #2) ==")
ctl, gov, *_ = rig(saved={"profile": "quiet", "target": "80"}, budget=900)
set_temp(50, 50)                             # cool: 28C under the release point
for d in nv.DEVS:
    d.draw, d.util = 450.0, 90
gov.ups.read = lambda: (1100.0, ("OL",))
for _ in range(4):
    gov._feedback_wait_total_w = None
    gov.update(force=True); CLOCK.advance(10.0)
trimmed = limits()[0]
check("UPS over budget: trimmed", trimmed < 600.0, limits())
gov.ups.read = lambda: (500.0, ("OL",))
steps = []
for _ in range(12):
    before = limits()[0]
    gov._feedback_wait_total_w = None
    ctl.update(); gov.update(force=True); CLOCK.advance(10.0)
    if limits()[0] != before:
        steps.append(limits()[0] - before)
check("a UPS trim restores +20 W at a time, even with a target set and the cards cool",
      steps and set(steps) == {20.0}, steps)

ctl, gov, *_ = rig(saved={"profile": "quiet", "target": "80"}, budget=900)
for d in nv.DEVS:
    d.draw, d.util = 300.0, 90
gov.ups.read = lambda: (500.0, ("OL",))
gov.update(force=True)
set_temp(86, 50)
ctl.update()                                 # thermal cut 600 -> 450 (25%)
set_temp(60, 50)
tick(ctl, 17)                                # hold released
raised = False
for _ in range(20):
    gov._feedback_wait_total_w = None
    gov.update(force=True); CLOCK.advance(10.0)
    ctl.update()
    if limits()[0] > 450.0:
        raised = True
        break
step = limits()[0] - 450.0
check("after a thermal hold the raise is still fast (bounded by headroom)", raised and step > 20.0,
      limits())
check("...and the next decision waits for a fresh UPS sample",
      gov._feedback_wait_total_w is not None)
held = limits()
gov.update(force=True)                       # the same (stale) UPS reading
check("...so a stale reading can't stack a second fast raise", limits() == held, (held, limits()))

print("\n== no GPUs reported: no crash (re-review #4) ==")
try:
    ctl, gov, *_ = rig(n=0)
    tick(ctl, 2)
    check("0 GPUs: a tick doesn't raise", True)
except Exception as e:
    check("0 GPUs: a tick doesn't raise", False, repr(e))

print("\n== total ceiling: split between the cards, never exceeded (plan 003) ==")
P = nfc.parse_power_total
check("total parser", P("750") == 750.0 and P("none") == 0.0 and P("off") == 0.0)
for bad in ("abc", "-5", "300,300"):
    try:
        P(bad); check(f"total {bad!r} refused", False)
    except ValueError:
        check(f"total {bad!r} refused", True)


def busy_cards(*flags):
    for d, b in zip(nv.DEVS, flags):
        d.draw, d.util = (300.0, 90) if b else (20.0, 0)


def sum_ok(total=750.0, gov=None):
    """The sum of the limits within the (soft) total: an idle card sits at its 150 W minimum
    but is counted at 75 W, so the bound is the total + 75 W per idle card."""
    bound = gov.limit_sum_bound_w() if gov is not None and gov.total_w is not None else total
    return sum(limits()) <= bound + 0.5


def run(ctl, n, dt=2.0, total=750.0, trace=None):
    ok = True
    for _ in range(n):
        ctl.update(); ctl.governor.update(); CLOCK.advance(dt)
        ok = ok and sum_ok(total, ctl.governor)
        if trace is not None:
            trace.append(tuple(limits()))
    return ok


# no UPS budget, ceiling 600, total 750
ctl, gov, *_ = rig(saved={"ceiling": "600", "total": "750"})
busy_cards(False, False)
check("start: every card assumed busy, equal split 375/375", limits() == [375.0, 375.0], limits())
ctl._write_effective()
doc = json.load(open(os.path.join(ctl.run_dir, "effective.json")))
check("...and effective.json shows the total and the shares",
      doc["in_force"]["total"] == "750" and doc["total_shares_w"] == [375, 375],
      (doc["in_force"], doc.get("total_shares_w")))
ok = run(ctl, 8)
check("both idle: still an equal split", limits() == [375.0, 375.0] and ok, limits())
busy_cards(False, True)
trace = []
ok = run(ctl, 8, trace=trace)
check("GPU1 busy, GPU0 idle for 10 s: 150 / 600", limits() == [150.0, 600.0], limits())
check("...and the sum never went over 750 W on any tick", ok, trace)
check("lower before raise: GPU0 went to 150 a tick before GPU1 went up",
      any(t == (150.0, 375.0) for t in trace), trace)
busy_cards(True, True)
trace = []
ok = run(ctl, 3, trace=trace)
check("GPU0 wakes: back to 375 / 375 within two ticks", limits() == [375.0, 375.0], limits())
check("...lowering GPU1 first, the sum never over 750 W", ok and (375.0, 150.0) not in trace
      and any(t == (150.0, 375.0) for t in trace), trace)
busy_cards(True, False)
ok = run(ctl, 3)
check("a pause under 10 s doesn't move power", limits() == [375.0, 375.0] and ok, limits())
ok = run(ctl, 5)
check("GPU1 idle for 10 s: 600 / 150", limits() == [600.0, 150.0] and ok, limits())

ctl, gov, *_ = rig(saved={"ceiling": "300", "total": "750"})
busy_cards(True, False)
run(ctl, 8)
check("a per-GPU ceiling still bounds the busy card (300, not 600); the idle card holds the "
      "rest ahead of time, up to its own 300 ceiling", limits() == [300.0, 300.0], limits())
ctl, gov, *_ = rig(saved={"ceiling": "500,300", "total": "750"})
busy_cards(True, True)
run(ctl, 3)
check("unequal ceilings: water-filled 450/300 (GPU1 capped, GPU0 takes the rest)",
      limits() == [450.0, 300.0], limits())
ctl, gov, *_ = rig(saved={"total": "250"})
check("a total below the GPUs' minimums (300 W) is refused", gov.total_w is None and
      limits() == [600.0, 600.0], (gov.total_w, limits()))
ctl, gov, *_ = rig(saved={"ceiling": "600", "total": "750"})
busy_cards(True, False)
run(ctl, 8)
write(ctl.state_dir, "total", "none")
run(ctl, 2)
check("total cleared: each card back to its own ceiling", limits() == [600.0, 600.0], limits())

print("\n== total ceiling with the UPS budget ==")
ctl, gov, *_ = rig(saved={"ceiling": "600", "total": "750"}, budget=900)
gov.ups.read = lambda: (400.0, ("OL",))
busy_cards(False, True)
ok = run(ctl, 8)
gov._last_run = 0
gov.update(force=True)
check("idle GPU0 at 150, busy GPU1 at 600 under the budget", limits() == [150.0, 600.0] and ok,
      limits())
gov.ups.read = lambda: (1000.0, ("OL",))
gov._feedback_wait_total_w = None
gov.update(force=True)
check("UPS over by 100 W: the busy card is cut 100 W, the idle one stays at its minimum",
      limits() == [150.0, 500.0], limits())
busy_cards(True, True)
gov.ups.read = lambda: (850.0, ("OL",))
ok = run(ctl, 3)
check("GPU0 wakes during the trim: 375/375 shares (within GPU1's 500 W cap), sum within 750",
      limits() == [375.0, 375.0] and ok, limits())
ctl, gov, *_ = rig(saved={"ceiling": "300"}, budget=900)
for d in nv.DEVS:
    d.draw, d.util = 300.0, 90
gov.ups.read = lambda: (1000.0, ("OL",))
gov.update(force=True)
check("no total, equal cards: the UPS cut is unchanged (both 300 -> 250)",
      limits() == [250.0, 250.0], limits())

print("\n== a waking card gets its share at once, even after a cut while it was idle ==")
# pve-ai 2026-09-29: a thermal cut while GPU0 idled at its 150 W share recorded 150 W as
# GPU0's own cap, so when it woke it stayed at 150 and crawled back +20 W per 30 s
ctl, gov, *_ = rig(saved={"profile": "quiet", "ceiling": "600", "total": "600", "target": "80"})
busy_cards(False, True)
set_temp(50, 70)
run(ctl, 8, total=600.0)
check("GPU1 busy, GPU0 idle: 150 / 550 (soft total: idle GPU0 at 20 W counted at 50 W)",
      limits() == [150.0, 550.0], limits())
set_temp(50, 81)
for _ in range(4):
    ctl.update(); gov.update(); CLOCK.advance(2.0)
check("a thermal cut on the busy card (the jump averaged -30 W, then +1C -60 W: 550 -> 460); "
      "the idle card isn't cut", limits()[1] == 460.0 and limits()[0] <= 175.0, limits())
check("...the cold idle card isn't cut at all (only the hot card is)", gov.cap_w[0] == 600.0,
      gov.cap_w)
busy_cards(True, True)
ok = run(ctl, 2, total=600.0)
check("GPU0 wakes: at once it gets its 300 W share (GPU1 lowered first), the sum 600",
      limits() == [300.0, 300.0] and ok, limits())

# the original bug, when the idle card is itself over the target (warmed by its neighbour)
ctl, gov, *_ = rig(saved={"profile": "quiet", "ceiling": "600", "total": "600", "target": "80"})
busy_cards(False, True)
set_temp(70, 70)
run(ctl, 8, total=600.0)
set_temp(81, 81)
for _ in range(4):
    ctl.update(); gov.update(); CLOCK.advance(2.0)
check("a hot IDLE card's cap is cut from its cap (600 -> 510), not recorded as its share",
      gov.cap_w[0] == 510.0, (gov.cap_w, limits()))
busy_cards(True, True)
ok = run(ctl, 2, total=600.0)
check("...so when it wakes it gets 300 W at once, not 150", limits()[0] == 300.0 and ok, limits())

ctl, gov, *_ = rig(saved={"ceiling": "600", "total": "600"}, budget=900)
gov.ups.read = lambda: (400.0, ("OL",))
busy_cards(False, True)
run(ctl, 8, total=600.0)
gov.ups.read = lambda: (1000.0, ("OL",))
gov._feedback_wait_total_w = None
gov.update(force=True)
check("UPS over by 100 W: busy GPU1 550 -> 450, idle GPU0 stays at 150",
      limits() == [150.0, 450.0], limits())
check("...and GPU0's own cap is trimmed from its cap (600 -> 500), not set to 150",
      gov.cap_w[0] == 500.0, gov.cap_w)
busy_cards(True, True)
gov.ups.read = lambda: (800.0, ("OL",))
ok = run(ctl, 2, total=600.0)
check("GPU0 wakes: its 300 W share at once", limits()[0] == 300.0 and ok, limits())

ctl, gov, *_ = rig(saved={"profile": "quiet", "ceiling": "600", "total": "750", "target": "80"})
busy_cards(True, True)
set_temp(81, 81)
run(ctl, 5)
check("both busy at their 375 W shares and hot: the cut really lowers them (315/315)",
      limits() == [315.0, 315.0], limits())

print("\n== a card that starts working and is cut in the same tick loses real power ==")
# pve-ai run 2: GPU1 started, and the very first reading was a fast rise; the cut came before
# the allocator had marked GPU1 busy, so it trimmed GPU1's cap and left its 300 W in place
ctl, gov, *_ = rig(saved={"profile": "max", "ceiling": "600", "total": "600", "target": "65"})
busy_cards(False, False)
set_temp(45, 49)
run(ctl, 7, total=600.0)
check("both idle: 300 / 300", limits() == [300.0, 300.0] and not any(gov._busy), (limits(), gov._busy))
busy_cards(False, True)
set_temp(45, 67)                              # busy, and 2C over the target in the same reading
ctl.update()
check("the cut lowers the card that just got busy (300 -> 270), in that same tick",
      limits() == [300.0, 270.0], limits())

print("\n== only the hot card is cut; the cold busy card takes the share it frees ==")
ctl, gov, *_ = rig(saved={"profile": "quiet", "ceiling": "600", "total": "750", "target": "80"})
busy_cards(True, True)
set_temp(60, 78)
ok = run(ctl, 4)
check("both busy: 375 / 375", limits() == [375.0, 375.0] and ok, limits())
set_temp(60, 81)
trace = []
ok = run(ctl, 6, trace=trace)
check("GPU1 over the target: only GPU1 is cut", limits()[1] < 375.0, limits())
check("...and cold GPU0 takes what GPU1 can't use, the sum within 750 on every tick",
      limits()[0] > 375.0 and abs(sum(limits()) - 750.0) < 1.0 and ok, trace)
set_temp(60, 70)
ok = run(ctl, 60, trace=trace)
check("GPU1 cools and recovers: back to 375 / 375, the sum within 750 throughout",
      limits() == [375.0, 375.0] and ok, limits())

print("\n== budget the busy card can't use waits on the idle card; taken back when needed ==")
ctl, gov, *_ = rig(saved={"profile": "quiet", "ceiling": "600", "total": "750", "target": "80"})
busy_cards(True, False)
set_temp(78, 50)
run(ctl, 7)
check("GPU0 busy, GPU1 idle: 600 / 150", limits() == [600.0, 150.0], limits())
set_temp(81, 50)
trace = []
ok = run(ctl, 6, trace=trace)
check("GPU0 cut for heat: idle GPU1 is handed what GPU0 can't use, ahead of any load",
      limits()[0] < 600.0 and limits()[1] > 150.0 and abs(sum(limits()) - 750.0) < 1.0, trace)
set_temp(60, 50)
ok = run(ctl, 80, trace=trace) and ok
check("GPU0 cools: GPU1 is lowered first and GPU0 takes it back, 600 / 150, the sum within 750 "
      "on every tick", limits() == [600.0, 150.0] and ok, trace[-6:])

print("\n== adaptive: power comes back below the target; the fans just follow temperature ==")
# pve-ai 2026-09-29: during a hold adaptive learned a -26% trim (fans ~75%) because the card
# sat just under the target, so it never reached target-2 and power never came back.
# Operator's rule: the fans follow temperature only, and power comes back whenever the card
# is below the target (the fans then aren't at their max), pausing at the target.
ctl, gov, *_ = rig(saved={"profile": "adaptive", "ceiling": "600", "total": "750", "target": "60"})
busy_cards(True, False)
set_temp(58, 45)
run(ctl, 3)
set_temp(63, 45)
run(ctl, 15)
check("hot: fans at 100, then power cut (fans first)", max(fans()) == 100 and gov.thermal_limited
      and limits()[0] < 600.0, (fans(), limits()))
cut_w = limits()[0]
set_temp(59, 45)                             # 1C under the target, not target-2
run(ctl, 3)
check("just under the target: the fans follow the curve (96%, not pinned), no quiet trim",
      fans() == [96, 96] and ctl._target_trim_pct == 0.0, (fans(), ctl._target_trim_pct))
run(ctl, 3)
check("below the target for 10 s: the hold releases (no target-2 / 30 s wait for adaptive)",
      not gov.thermal_limited and gov.recovery_walk, (gov.thermal_limited, gov.recovery_walk))
run(ctl, 22)                                 # one 30 s step, then the idle card lowered first
check("...and power walks back up while the card stays below the target",
      limits()[0] > cut_w, (cut_w, limits()))
set_temp(60, 45)                             # at the target: the walk pauses
held = limits()[0]
run(ctl, 20)
check("at the target the walk pauses (no cut either: fans at 100 only just now)",
      limits()[0] == held or gov.thermal_limited, (held, limits(), gov.thermal_limited))
set_temp(50, 45)
run(ctl, 150)
check("cool: power fully back (600), then adaptive quiets the fans again",
      limits()[0] == 600.0 and not gov.recovery_walk and max(fans()) < 100, (limits(), fans()))

# the fixed curves keep the 2C / 30 s release
ctl, gov, *_ = rig(saved={"profile": "quiet", "ceiling": "600", "target": "60"})
busy_cards(True, False)
set_temp(58, 45)
run(ctl, 3, total=1e9)
set_temp(63, 45)
run(ctl, 5, total=1e9)
set_temp(59, 45)
run(ctl, 20, total=1e9)
check("fixed curve: 59C (above target-2) keeps the hold", gov.thermal_limited)

# once the load stops the fans come down
ctl, gov, *_ = rig(saved={"profile": "adaptive", "ceiling": "600", "total": "750", "target": "60"})
busy_cards(True, False)
set_temp(58, 45)
run(ctl, 3)
set_temp(63, 45)
run(ctl, 15)
busy_cards(False, False)
set_temp(50, 45)
run(ctl, 12)
check("the load stops: the fans come down with the temperature", max(fans()) < 100,
      (fans(), gov.recovery_walk, gov._busy))

print("\n== recovery waits while a card is still warming up (the cut's own 'climbing') ==")
ctl, gov, *_ = rig(saved={"profile": "adaptive", "ceiling": "600", "total": "750", "target": "60"})
busy_cards(True, False)
set_temp(58, 45)
run(ctl, 3)
set_temp(63, 45)
run(ctl, 15)
set_temp(55, 45)
run(ctl, 25)                                 # released, and at least one step taken
check("released and walking back", not gov.thermal_limited and gov.recovery_walk)
# wait for a step, then climb 1C per reading for longer than a 30 s step interval
b = gov.cap_w[0]
for _ in range(40):
    run(ctl, 1)
    if gov.cap_w[0] > b:
        break
before = gov.cap_w[0]
climbing_steps = 0
for t in range(40, 60):                      # 40 -> 59C over 40 s: always warmer than 2 readings ago
    set_temp(t, 45)
    b = gov.cap_w[0]
    run(ctl, 1)
    if gov.cap_w[0] > b:
        climbing_steps += 1
check("no step while the card keeps warming (40 s of climbing)", climbing_steps == 0,
      climbing_steps)
set_temp(58, 45)
run(ctl, 20)
check("settled (58C, below the target): the walk resumes", gov.cap_w[0] > before, (before, gov.cap_w))

print("\n== adaptive unlearns: each power cut for heat raises the quiet-trim floor ==")
# pve-ai 2026-09-29 under vLLM: a -35% trim parked GPU1 at 83C with the fans at 58%, and each
# burst became a 230-240 W cut. Operator: unlearn from the cuts (no fan-target offset).
ctl, gov, *_ = rig(saved={"profile": "adaptive:90", "ceiling": "600", "total": "700", "target": "85"})
busy_cards(True, False)
set_temp(83, 45)
run(ctl, 200)                                # a steady load just under the target: learns quiet
check("steady just under the target: adaptive learns a deep quiet trim (floor -50)",
      ctl._target_trim_pct < -20.0 and ctl._trim_floor_pct == -50.0,
      (ctl._target_trim_pct, ctl._trim_floor_pct))

def burst(ctl):
    """a load step: the card jumps over the target (a cut), then cools and recovers"""
    set_temp(86, 45); run(ctl, 1)
    set_temp(89, 45); run(ctl, 4)
    set_temp(80, 45); run(ctl, 10)

burst(ctl)
check("first cut: the floor rises -50 -> -30", ctl._trim_floor_pct == -30.0 and gov.thermal_holds == 1,
      (ctl._trim_floor_pct, gov.thermal_holds))
burst(ctl)
burst(ctl)
check("two more cuts: -30 -> -10 -> 0 (the base curve)", ctl._trim_floor_pct == 0.0,
      ctl._trim_floor_pct)
set_temp(83, 45)
run(ctl, 150)                                # recovered, steady again just under the target
check("at floor 0 no quiet trim is learned: the fans follow the base curve (90% at 83C)",
      ctl._target_trim_pct == 0.0 and fans() == [90, 90], (ctl._target_trim_pct, fans()))
CLOCK.advance(nfc.TRIM_FLOOR_RELAX_S)
run(ctl, 1)
check("10 cut-free minutes: the floor relaxes 0 -> -5", ctl._trim_floor_pct == -5.0,
      ctl._trim_floor_pct)
ctl.save_runtime_state(force=True)
doc = json.load(open(os.path.join(ctl.state_dir, nfc.RUNTIME_STATE_FILE)))
check("the floor is saved with the trim", doc["adaptive"]["trim_floor_pct"] == -5.0, doc["adaptive"])
ctl._write_effective()
doc = json.load(open(os.path.join(ctl.run_dir, "effective.json")))
check("...and published in effective.json", doc["adaptive_trim_floor_pct"] == -5.0,
      doc.get("adaptive_trim_floor_pct"))

print("\n== recovery per card: a cool card comes back fast while the other sits near the target ==")
ctl, gov, *_ = rig(saved={"profile": "quiet", "ceiling": "600", "target": "60"}, budget=900)
gov.ups.read = lambda: (500.0, ("OL",))
busy_cards(True, True)
set_temp(59, 59)
run(ctl, 3, total=1e9)
set_temp(61, 61)
run(ctl, 4, total=1e9)
check("both cut for heat", max(limits()) < 600.0 and gov.thermal_limited, limits())
set_temp(45, 58)                             # GPU0 cool, GPU1 at the release point
run(ctl, 18, total=1e9)
before = limits()
steps = []
for _ in range(20):
    b = limits(); gov._feedback_wait_total_w = None
    ctl.update(); gov.update(); CLOCK.advance(2.0)
    if limits() != b:
        steps.append((limits()[0] - b[0], limits()[1] - b[1]))
check("GPU0 (45C) recovers in big steps, GPU1 (58C) in +20 W steps",
      steps and steps[0][0] > 40.0 and steps[0][1] == 20.0, (before, steps))

print("\n== per-card idle reset (with a total): old cuts cleared once a card idles and cools ==")
ctl, gov, *_ = rig(saved={"profile": "quiet", "ceiling": "600", "total": "750", "target": "60"},
                   budget=900)
gov.ups.read = lambda: (1100.0, ("OL",))
busy_cards(True, True)
set_temp(50, 50)
run(ctl, 6)
check("both busy, the UPS over budget: both trimmed", max(gov.cap_w) < 375.0, gov.cap_w)
gov.ups.read = lambda: (400.0, ("OL",))
busy_cards(False, True)
run(ctl, 20)
check("GPU0 idle 40 s: still its old cut", gov.cap_w[0] < 600.0, gov.cap_w)
run(ctl, 12)
check("GPU0 idle 60 s and cool: its cap is back to 600", gov.cap_w[0] == 600.0, gov.cap_w)
run(ctl, 2)
check("...and it holds the budget busy GPU1 can't use (not stuck at its old trim)",
      limits()[0] > 300.0 and sum(limits()) <= 750.5, limits())
ctl, gov, *_ = rig(saved={"profile": "quiet", "ceiling": "600"}, budget=900)
gov.ups.read = lambda: (1100.0, ("OL",))
busy_cards(True, True)
run(ctl, 6, total=1e9)
gov.ups.read = lambda: (400.0, ("OL",))
busy_cards(False, True)
run(ctl, 35, total=1e9)
check("without a total, an idle card keeps its UPS trim (UPS behaviour unchanged)",
      gov.cap_w[0] < 600.0, gov.cap_w)

print("\n== a restart with a total starts each card at its full share ==")
ctl, gov, *_ = rig(saved={"ceiling": "600", "total": "600"}, budget=900, now=300.0)
gov.ups.read = lambda: (400.0, ("OL",))
busy_cards(False, True)
ok = run(ctl, 7, total=600.0)
check("cards left at 300 W by the last run: GPU1 gets its 550 W share (soft), no crawl",
      limits() == [150.0, 550.0] and ok, limits())
ctl, gov, *_ = rig(saved={"ceiling": "600"}, budget=900, now=300.0)
check("without a total, a restart still starts from the current limits (unchanged)",
      gov.cap_w == [300.0, 300.0], gov.cap_w)

print("\n== the soft total (operator): a lone busy card gets total - 75, not total - 150 ==")
ctl, gov, *_ = rig(saved={"ceiling": "600", "total": "700"})
busy_cards(False, True)
run(ctl, 8, total=700.0)
check("total 700: busy GPU1 gets 600 (700 - 50, capped at 600), idle GPU0 sits at its 150 floor",
      limits() == [150.0, 600.0], limits())
check("...the limits add up to 750, within the soft bound (700 + 100 for the idle card)",
      sum(limits()) <= gov.limit_sum_bound_w() + 0.5, (limits(), gov.limit_sum_bound_w()))

busy_cards(True, True)
trace = []
run(ctl, 3, total=700.0, trace=trace)
check("GPU0 wakes: GPU1 lowered first, then an equal split within the hard total (350 / 350)",
      limits() == [350.0, 350.0] and trace[0][1] == 350.0, trace)

# counted at what it really draws: the 10 s peak + 10 W, in 25 W steps
ctl, gov, *_ = rig(saved={"ceiling": "600", "total": "600"})
busy_cards(False, True)
run(ctl, 8, total=600.0)
check("idle GPU0 at 20 W is counted at 50 W: busy GPU1 gets 550", limits() == [150.0, 550.0]
      and gov._idle_reserve_w(0) == 50.0, (limits(), gov._idle_reserve_w(0)))
seen = set()
for w in (15.0, 25.0, 18.0, 22.0, 16.0, 24.0):
    nv.DEVS[0].draw = w
    run(ctl, 1, total=600.0)
    seen.add(tuple(limits()))
check("the idle wobble (15-25 W) doesn't move the split", seen == {(150.0, 550.0)}, seen)
nv.DEVS[0].draw = 60.0                       # still idle (<= 75 W, util 0), drawing more
trace = []
ok = run(ctl, 3, total=600.0, trace=trace)
check("idle GPU0 creeps to 60 W: counted at 75 W, busy GPU1 lowered to 525", limits() == [150.0, 525.0]
      and gov._idle_reserve_w(0) == 75.0, (limits(), gov._idle_reserve_w(0)))
nv.DEVS[0].draw = 20.0
run(ctl, 3, total=600.0)
check("...back to 20 W: still counted at 75 W until the 60 W reading is 10 s old",
      limits() == [150.0, 525.0], limits())
run(ctl, 5, total=600.0)
check("...then 50 W again: busy GPU1 back to 550", limits() == [150.0, 550.0], limits())

print("\n== deploy-team review of plan 003 ==")
# effective.json follows the governor, not just settings changes
ctl, gov, *_ = rig(saved={"ceiling": "600", "total": "700"})
busy_cards(False, False)
run(ctl, 2, total=700.0)
ctl.refresh_effective()
busy_cards(False, True)
run(ctl, 8, total=700.0)
ctl.refresh_effective()
doc = json.load(open(os.path.join(ctl.run_dir, "effective.json")))
check("effective.json shows the limits and shares in force after the split moved: limits "
      "150/600, shares 100/600 (the idle card's share is below its 150 W floor)",
      doc["power_limits_w"] == [150, 600] and doc["total_shares_w"] == [100, 600],
      (doc["power_limits_w"], doc["total_shares_w"]))

# (1) a lowering that fails must hold every raise
ctl, gov, *_ = rig(saved={"ceiling": "600", "total": "700"})
busy_cards(True, True)
run(ctl, 3, total=700.0)
check("both busy: 350 / 350", limits() == [350.0, 350.0], limits())
busy_cards(False, True)
nv.DEVS[0].set_fail = 3                      # GPU0's next three limit writes fail
trace = []
ok = run(ctl, 10, total=700.0, trace=trace)
check("GPU0's lowering fails 3 times: GPU1 is not raised meanwhile (the sum within the soft "
      "bound)", ok and all(t[1] == 350.0 for t in trace if t[0] > 150.0), trace)
run(ctl, 3, total=700.0)
check("...GPU0 lowered on a retry, then GPU1 raised: 150 / 600 (soft: 700 - 75, capped at the "
      "600 ceiling)", limits() == [150.0, 600.0], limits())

# (3) after a restart with a UPS budget, full shares only once the first UPS reading is fine
ctl, gov, *_ = rig(saved={"ceiling": "600", "total": "700"}, budget=900, now=300.0)
check("restart: shares not raised before the first UPS reading", max(limits()) <= 300.0, limits())
gov.ups.read = lambda: (400.0, ("OB", "DISCHRG"))
busy_cards(True, True)
nv.SET_CALLS.clear()
run(ctl, 4, total=700.0)
check("first UPS reading on battery: straight to the floor (150), never a write above 300 first",
      limits() == [150.0, 150.0] and all(w <= 300.0 for _, w in nv.SET_CALLS),
      (limits(), nv.SET_CALLS))
ctl, gov, *_ = rig(saved={"ceiling": "600", "total": "700"}, budget=900, now=300.0)
gov.ups.read = lambda: (400.0, ("OL",))
busy_cards(True, True)
run(ctl, 4, total=700.0)
check("first UPS reading fine: full shares (350 / 350)", limits() == [350.0, 350.0], limits())

print("\n== UPS grace (operator): 20 s over the budget is fine, above the UPS rating isn't ==")
ctl, gov, *_ = rig(saved={"ceiling": "300"}, budget=900)
gov.ups.nominal_w = 1000
for d in nv.DEVS:
    d.draw, d.util = 300.0, 90
gov.ups.read = lambda: (950.0, ("OL",))
for _ in range(3):                           # 15 s over the budget, within the rating
    gov._feedback_wait_total_w = None
    gov.update(force=True); CLOCK.advance(5.0)
check("950 W for 15 s (budget 900, rating 1000): no trim yet", limits() == [300.0, 300.0], limits())
gov.update(force=True); CLOCK.advance(5.0)
gov._feedback_wait_total_w = None
gov.update(force=True)
check("...still over after 20 s: trimmed", max(limits()) < 300.0, limits())
ctl, gov, *_ = rig(saved={"ceiling": "300"}, budget=900)
gov.ups.nominal_w = 1000
for d in nv.DEVS:
    d.draw, d.util = 300.0, 90
gov.ups.read = lambda: (1050.0, ("OL",))
gov.update(force=True)
check("1050 W, above the UPS's 1000 W rating: trimmed at once", max(limits()) < 300.0, limits())
ctl, gov, *_ = rig(saved={"ceiling": "300"}, budget=900)
gov.ups.nominal_w = 1000
for d in nv.DEVS:
    d.draw, d.util = 300.0, 90
gov.ups.read = lambda: (950.0, ("OL",))
gov.update(force=True); CLOCK.advance(10.0)
gov.ups.read = lambda: (850.0, ("OL",))
gov.update(force=True); CLOCK.advance(5.0)
gov.ups.read = lambda: (950.0, ("OL",))
for _ in range(3):
    gov._feedback_wait_total_w = None
    gov.update(force=True); CLOCK.advance(5.0)
check("a dip under the budget restarts the grace", limits() == [300.0, 300.0], limits())

print("\n== a missing or unreadable total file keeps the last total (review #2, option B) ==")
ctl, gov, store, state, run_dir = rig(saved={"ceiling": "600", "total": "700"})
run(ctl, 2, total=700.0)
ctl.save_runtime_state(force=True)
os.remove(os.path.join(state, nfc.SETTING_FILES["total"]))
CLOCK.advance(0.01)
run(ctl, 2, total=700.0)
check("the file deleted: the 700 W total stays in force", gov.total_w == 700.0, gov.total_w)
nv.reset(2)
store2 = nfc.SettingsStore(state, run_dir, {})
gov2 = nfc.PowerGovernor(handles=[], interval=5.0)
ctl2 = nfc.FanController(store2, gov2, poll_interval=2.0, emergency_c=92, state_dir=state,
                         run_dir=run_dir)
ctl2.init()
check("a restart with the file still missing: the last total (700) from the runtime state",
      gov2.total_w == 700.0, gov2.total_w)
with open(os.path.join(state, nfc.SETTING_FILES["total"]), "w") as f:
    f.write("seven hundred\n")
nv.reset(2)
store3 = nfc.SettingsStore(state, run_dir, {})
gov3 = nfc.PowerGovernor(handles=[], interval=5.0)
ctl3 = nfc.FanController(store3, gov3, poll_interval=2.0, emergency_c=92, state_dir=state,
                         run_dir=run_dir)
ctl3.init()
check("a restart with a malformed file: still the last total (fails closed)", gov3.total_w == 700.0,
      gov3.total_w)
write(state, "total", "none")
run(ctl3, 2, total=1e9)
check("an explicit `none` clears it", gov3.total_w is None, gov3.total_w)
ctl3.save_runtime_state(force=True)
os.remove(os.path.join(state, nfc.SETTING_FILES["total"]))
nv.reset(2)
store4 = nfc.SettingsStore(state, run_dir, {})
gov4 = nfc.PowerGovernor(handles=[], interval=5.0)
ctl4 = nfc.FanController(store4, gov4, poll_interval=2.0, emergency_c=92, state_dir=state,
                         run_dir=run_dir)
ctl4.init()
check("...and once cleared, a later missing file means no total", gov4.total_w is None, gov4.total_w)

print("\n== unequal per-GPU ceilings no longer collapse on a cut (the shared-cap bug) ==")
ctl, gov, *_ = rig(saved={"profile": "quiet", "ceiling": "600,300", "target": "80"})
set_temp(81, 50)
for _ in range(4):                               # steady +1C: past the 5 s grace, one cut
    ctl.update(); CLOCK.advance(2.0)
check("a thermal cut takes the hot card down from its own level (-60 W: 540), the 50C card "
      "keeps its 300", limits() == [540.0, 300.0], limits())
before = limits()
set_temp(60, 50)
trace = []
for _ in range(40):
    ctl.update(); gov.update(); CLOCK.advance(2.0)
    trace.append(tuple(limits()))
check("recovery never raises a card by more than one step at a time",
      all(b - a <= 150.0 + 0.5 for prev, cur in zip([tuple(before)] + trace, trace)
          for a, b in zip(prev, cur)), trace)
check("...and ends back at 600/300", limits() == [600.0, 300.0], limits())

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    print("FAILED: " + "; ".join(FAIL))
sys.exit(1 if FAIL else 0)
