# Decision: Back-port the proxmox golden file into this fork, making the fork a superset

**Status:** Accepted
**Date:** 2026-08-24

## Context

`plans/decisions/005-lesson-golden-file-is-source-of-truth.md` established that
`~/src/proxmox/roles/proxmox_host/files/nvidia-fan-control.py` had drifted ~345 lines
ahead of this fork — a thermal governor (`--temp-target`, `observe_thermal`), a learned
common cap, idle-card detection and a reworked restore law, none of which were ever
back-ported. The power-ceiling feature was then built twice: once on this fork's stale
base, and once (correctly) on the golden file, which is what deployed to pve-ai and was
verified there.

Leaving the fork stale would mean the next person to read it gets a picture of
production that is wrong in the parts that matter most (the control law).

## Decision

**Copy the golden file wholesale into this fork**, byte-for-byte, and bring the docs up
to match. The fork's `nvidia-fan-control.py` is now identical to what runs on pve-ai.

This direction was safe to do mechanically: a symbol-level diff confirmed the golden file
is a strict **superset** — every constant, function and class in the fork already existed
there, so nothing fork-only was lost.

## Alternatives Considered

| Alternative | Pros | Cons | Why rejected |
|-------------|------|------|--------------|
| **Merge by hand, keeping the fork's own structure** | Preserves fork-local commit narrative | The two implementations of the ceiling would have to be reconciled line by line, and the fork's version was tested against a control law that no longer exists | Pure risk for zero benefit — the golden version is the one that ran on hardware |
| **Leave the fork stale; treat proxmox as canonical** | No work | The fork is the public artifact and is referenced by `Documentation=` in the unit; publishing a stale control law is worse than not publishing | Guarantees the same trap for the next reader |
| **Delete the fork** | No divergence possible | It is the open-source copy and the URL in the unit file | Not the operator's intent |

## Consequences

- The fork now carries `--temp-target`, `--power-ceiling`, `--power-ceiling-file`,
  `--no-power-ceiling-file` and the learned-cap/idle-reset control law.
- `tests/test_power_ceiling.py` (58 assertions, fake NVML, no GPU needed) lands here as
  the fork's first test suite. Run it before any change to the governor.
- **The two copies will drift again unless changes flow one way.** The convention going
  forward: make the change in the proxmox golden file (it is what deploys and what gets
  hardware-tested), then copy it here and re-run the tests. Verify with
  `diff <(cat nvidia-fan-control.py) ~/src/proxmox/roles/proxmox_host/files/nvidia-fan-control.py`.
- The fork's `nvidia-fan-control.service` is now the pve-ai reference invocation rather
  than a minimal example, so it and the Ansible template say the same thing.
