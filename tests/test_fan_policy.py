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
print("\n== temperature target, fixed curves: cut at target+2 for 5 s, fans irrelevant ==")
ctl, gov, store, state, run = rig(saved={"profile": "quiet", "target": "80"})
set_temp(81, 70)
tick(ctl, 6)
check("no cut at target+1", limits() == [600.0, 600.0], limits())
set_temp(82, 70)
tick(ctl, 2)     # 0 s, then 2 s
check("no cut before 5 s at target+2", limits() == [600.0, 600.0], limits())
tick(ctl, 2)     # 4 s, 6 s -> cut
check("cut after 5 s at target+2", max(limits()) < 600.0, limits())
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
check("power cut once fans >= max and card >= target+2 for 5 s", max(limits()) < 600.0, limits())

ctl, gov, store, state, run = rig(saved={"profile": "adaptive", "target": "80"})
set_temp(82, 60)
nv.DEVS[0].manual_fan = 30
tick(ctl, 3)       # fans still climbing (slew +10/tick), not yet at 100
check("adaptive (fan max 100): no cut while fans are below 100",
      limits() == [600.0, 600.0] or max(fans()) >= 100, (limits(), fans()))
tick(ctl, 12)
check("adaptive (fan max 100): cut once fans reach 100", max(limits()) < 600.0 and max(fans()) == 100,
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
check("trim saved with its target", doc["adaptive"] == {"target_c": 80, "trim_pct": round(learned, 2)},
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


print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    print("FAILED: " + "; ".join(FAIL))
sys.exit(1 if FAIL else 0)
