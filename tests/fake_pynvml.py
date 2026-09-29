"""In-memory stand-in for pynvml: enough to drive the fan controller and power governor.

Models what the daemon touches: power limit constraints and the current limit, board draw,
utilization, temperature, fan control policy and fan speed. Every write is recorded, so tests
can assert on what reached the DEVICE rather than on the daemon's own caches.

With the factory policy (TEMPERATURE_CONTINOUS_SW) a card's fan follows `factory_curve`
(a stock-like curve), which is what "native, hands off" means on real hardware.
"""
# Same numbering as the real pynvml (MANUAL = 1, factory = 0). The fake once had them
# reversed, which made a correct hardware readback look like a bug.
NVML_FAN_POLICY_TEMPERATURE_CONTINOUS_SW = 0
NVML_FAN_POLICY_MANUAL = 1
NVML_TEMPERATURE_GPU = 0


class NVMLError(Exception):
    pass


def factory_curve(temp):
    """Roughly the RTX PRO 6000's own curve: ~30% idle, ~52% at 85 C."""
    if temp <= 40:
        return 30
    return min(100, int(30 + (temp - 40) * 0.5))


class _Dev:
    def __init__(self, i, lo=150.0, hi=600.0, default=600.0, now=600.0):
        self.i = i
        self.lo = lo
        self.hi = hi
        self.default = default
        self.limit = now
        self.draw = 20.0
        self.util = 0
        self.temp = 40
        self.policy = NVML_FAN_POLICY_TEMPERATURE_CONTINOUS_SW
        self.manual_fan = 30
        self.temp_fail = False

    @property
    def fan(self):
        if self.policy == NVML_FAN_POLICY_MANUAL:
            return self.manual_fan
        return factory_curve(self.temp)


DEVS = []
SET_CALLS = []      # (gpu, watts) power-limit writes
FAN_CALLS = []      # (gpu, pct) fan-speed writes
POLICY_CALLS = []   # (gpu, policy) fan-policy writes


def reset(n=2, **kw):
    global DEVS, SET_CALLS, FAN_CALLS, POLICY_CALLS
    DEVS = [_Dev(i, **kw) for i in range(n)]
    SET_CALLS = []
    FAN_CALLS = []
    POLICY_CALLS = []


def nvmlInit(): pass
def nvmlShutdown(): pass
def nvmlDeviceGetCount(): return len(DEVS)
def nvmlDeviceGetHandleByIndex(i): return DEVS[i]
def nvmlDeviceGetName(h): return "FAKE RTX PRO 6000"
def nvmlDeviceGetNumFans(h): return 2
def nvmlDeviceGetTemperature(h, sensor):
    if h.temp_fail:
        raise NVMLError("temperature read failed")
    return h.temp
def nvmlDeviceGetFanSpeed_v2(h, fan): return h.fan


def nvmlDeviceSetFanControlPolicy(h, fan, policy):
    h.policy = policy
    POLICY_CALLS.append((h.i, policy))


def nvmlDeviceSetFanSpeed_v2(h, fan, pct):
    if h.policy != NVML_FAN_POLICY_MANUAL:
        raise NVMLError("fan speed set without manual policy")
    h.manual_fan = pct
    FAN_CALLS.append((h.i, pct))


def nvmlDeviceGetPowerManagementLimitConstraints(h): return (h.lo * 1000, h.hi * 1000)
def nvmlDeviceGetPowerManagementDefaultLimit(h): return h.default * 1000
def nvmlDeviceGetPowerManagementLimit(h): return h.limit * 1000


def nvmlDeviceSetPowerManagementLimit(h, mw):
    h.limit = max(h.lo, min(h.hi, mw / 1000.0))
    SET_CALLS.append((h.i, h.limit))


def nvmlDeviceGetPowerUsage(h): return h.draw * 1000


class _Util:
    def __init__(self, gpu):
        self.gpu = gpu


def nvmlDeviceGetUtilizationRates(h): return _Util(h.util)
