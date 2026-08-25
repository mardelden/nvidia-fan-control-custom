# Lesson: The deployed golden file, not this fork, is the source of truth

**Date:** 2026-08-24
**Area:** deployment

## What We Were Trying to Do

Implement `--power-ceiling` in this fork and deploy it to pve-ai.

## What We Tried

| Approach | Result | Why it failed/worked |
|----------|--------|---------------------|
| Implement against this fork's `main` (f95b043), assuming it is upstream of the deployment | **Wrong base** | The deployed file was ~345 lines *ahead*: a whole thermal governor (`--temp-target`, `observe_thermal`), `learned_cap_w`, idle-card detection and a different restore law |
| Hand-copy the built file to `/opt/nvidia-fan-control/` | **Not attempted** | The unit is `# Ansible managed`; a hand-copy is reverted by the next `just hosts` run |
| Port the feature onto `~/src/proxmox/roles/proxmox_host/files/nvidia-fan-control.py` and deploy with `just hosts --limit pve-ai --tags gpu-fan` | **Worked** | That file is byte-identical to what runs on the host, and Ansible owns the whole lifecycle (pip dep, unit template, restart) |

## Root Cause

The vendoring is one-directional in practice and currently inconsistent. The Ansible
task comments say *"Vendored from mardelden/nvidia-fan-control-custom (fork)"*, implying
fork → proxmox. But two rounds of work (`d0a7171 fix(pve-ai): harden UPS power governor`,
`b1c626a feat(pve-ai): coordinate GPU thermal and UPS control`) landed **directly in the
proxmox repo** and were never back-ported. So the comment describes an intent, not the
actual flow, and reading the fork gives a stale picture of production.

## Solution

Treat `~/src/proxmox/roles/proxmox_host/files/nvidia-fan-control.py` as the source of
truth for anything that must run on a host. Deploy with:

```bash
just hosts --limit pve-ai --tags gpu-fan --check --diff   # always check first
just hosts --limit pve-ai --tags gpu-fan
```

The fork remains useful as the public/open-source copy, but it needs an explicit
back-port before it can be trusted as a base again.

## How to Avoid in Future

- **Before implementing anything for a deployed host, diff the deployed artifact against
  the repo you are about to edit.** `scp` the live file and `diff` it; do not assume the
  git repo named in a comment is ahead.
- Check for `# Ansible managed` (or any config-management header) in the deployed unit
  before planning a deployment path.
- When a repo says "vendored from X", verify the direction — `git log` on both sides for
  the same file tells you which one actually receives changes.
