# Lesson: NVML does not revert manual fan control when the process dies

**Date:** 2026-09-29
**Area:** fan control

## What we were trying to do

Confirm what happens to the fans if the daemon crashes. The unit's comment claimed *"NVML
also auto-reverts manual fan control if the process stops refreshing, so a crash can't leave
the fans stuck"*, and `--once` used to log *"fans will return to auto control after a few
minutes"*. Neither had ever been tested.

## What we tried

| Approach | Result | Why |
|---|---|---|
| Trusted the comment | Wrong | Nobody had measured it |
| `kill -9` on pve-ai with the fans manual at 100%, then read the policy and speed every 15 s | **Still manual at 100% after 150 s** | On driver 580.126, manual fan policy persists after the process dies |

## Root cause

A manual fan policy is driver state, like a power limit. It lasts until something changes it,
and nothing watches for a dead controller.

## Solution

- `nvidia-fan-control.py --reset-fans` hands every fan back to the factory curve and exits.
- The unit runs it as `ExecStopPost=`, which systemd runs after **any** exit, including a
  crash. `Restart=always` then brings the controller back.
- A **hand-run** has no such net: `kill -9` of a hand-run leaves the fans where they were.
  Run `--reset-fans` afterwards.

## How to avoid in future

- **Test safety claims on hardware before writing them down.** A comment saying something is
  "auto-reverted" is a hypothesis until it's been measured.
- **Two traps from the same test session:**
  - **`pgrep -f` inside an `ssh … '<script>'` matches the remote shell itself**, because the
    shell's command line contains the pattern. That killed our own session instead of the
    daemon. Use the PID the daemon writes (`/run/nvidia-fan-control/daemon.lock`).
  - **The fake NVML had the fan-policy numbers reversed** (real pynvml: MANUAL = 1, factory =
    0). A correct hardware readback then looked like a bug. The fake now matches the real
    library. Always read back with the library's named constants.
