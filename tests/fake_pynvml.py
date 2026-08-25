"""Minimal in-memory stand-in for pynvml, enough to drive PowerGovernor."""
NVML_FAN_POLICY_MANUAL = 0
NVML_FAN_POLICY_TEMPERATURE_CONTINOUS_SW = 1
NVML_TEMPERATURE_GPU = 0

class NVMLError(Exception):
    pass

class _Dev:
    def __init__(self, i, lo=100.0, hi=600.0, default=600.0, now=600.0):
        self.i = i; self.lo = lo; self.hi = hi
        self.default = default; self.limit = now
        self.draw = 100.0; self.temp = 50; self.fan = 30

DEVS = []
SET_CALLS = []

def reset(n=2, **kw):
    global DEVS, SET_CALLS
    DEVS = [_Dev(i, **kw) for i in range(n)]
    SET_CALLS = []

def nvmlInit(): pass
def nvmlShutdown(): pass
def nvmlDeviceGetCount(): return len(DEVS)
def nvmlDeviceGetHandleByIndex(i): return DEVS[i]
def nvmlDeviceGetName(h): return "FAKE RTX PRO 6000"
def nvmlDeviceGetNumFans(h): return 2
def nvmlDeviceSetFanControlPolicy(h, f, p): pass
def nvmlDeviceGetTemperature(h, s): return h.temp
def nvmlDeviceGetFanSpeed_v2(h, f): return h.fan
def nvmlDeviceSetFanSpeed_v2(h, f, v): h.fan = v

def nvmlDeviceGetPowerManagementLimitConstraints(h): return (h.lo * 1000, h.hi * 1000)
def nvmlDeviceGetPowerManagementDefaultLimit(h): return h.default * 1000
def nvmlDeviceGetPowerManagementLimit(h): return h.limit * 1000
def nvmlDeviceSetPowerManagementLimit(h, mw):
    h.limit = mw / 1000.0
    SET_CALLS.append((h.i, h.limit))
def nvmlDeviceGetPowerUsage(h): return h.draw * 1000

class _Util:
    def __init__(self, gpu): self.gpu = gpu

def nvmlDeviceGetUtilizationRates(h): return _Util(getattr(h, "util", 0))
