# Decision: Persist the ceiling in a state file re-applied at boot, not in hardware

**Status:** Accepted
**Date:** 2026-08-24

## Context

The operator asked for the ceiling to "work like `nvidia-smi -pl`" and to "persist
during restarts of the servers". These are two different claims and only one of them
is something hardware can do.

`nvmlDeviceSetPowerManagementLimit` (and `nvidia-smi -pl`) sets a limit that persists
for the life of the driver state — it survives process exit, but **not** a reboot or
a driver reload. There is no NVML "persistent power limit" the way persistence mode
keeps the driver loaded.

## Decision

Split the requirement in two and satisfy each with the right mechanism:

1. **Survives daemon stop/restart** — hardware already does this, provided we stop
   undoing it. `restore_defaults()` now restores to the *ceiling* when one is active
   instead of the card default, so `systemctl stop` leaves the cards pinned. This is
   the true `nvidia-smi -pl` semantic.
2. **Survives a reboot** — the daemon re-applies the ceiling at startup from
   `/var/lib/nvidia-fan-control/power-ceiling` (systemd `StateDirectory=`). The unit
   is already `WantedBy=multi-user.target`, so the cap is restored early in boot.

## Alternatives Considered

| Alternative | Pros | Cons | Why rejected |
|-------------|------|------|--------------|
| **Rely on hardware persistence alone** | No state file | Silently wrong across reboots — the exact case the operator named | Does not meet the requirement |
| **Write `nvidia-smi -pl` into a boot-time oneshot unit** | No daemon changes | Two components can disagree about the cap; the governor would fight the oneshot's value on the first tick | Reintroduces the original bug (governor re-raising an externally set limit) |
| **`nvidia-persistenced` persistence mode** | Native | Persistence mode keeps the *driver* loaded; it does not preserve power limits across reboot | Solves a different problem |

## Consequences

- Between reboot and daemon start, the cards sit at the card default (600 W). This
  window is unavoidable without firmware support; it is bounded by service start time
  and there is no load running that early. Documented in `HANDOVER.md`.
- Stopping the service now leaves a pinned cap in place. Operators who want the cards
  back at stock must unpin explicitly (`--power-ceiling none` or write `none` to the
  file) — a deliberate trade for benchmark integrity.
- The state file needs `StateDirectory=nvidia-fan-control` in the unit so systemd
  creates `/var/lib/nvidia-fan-control` with the right ownership.
