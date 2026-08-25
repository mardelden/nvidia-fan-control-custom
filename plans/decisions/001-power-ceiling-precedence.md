# Decision: CLI `--power-ceiling` beats the persisted state file at startup

**Status:** Accepted
**Date:** 2026-08-24

## Context

The ceiling has two sources: the `--power-ceiling` flag (typically baked into the
systemd unit) and `/var/lib/nvidia-fan-control/power-ceiling` (written by the daemon,
and by operators live-pinning a benchmark). When both are present at daemon start
they can disagree. The operator asked for two things that pull in opposite directions:

- "it should persist during restarts of the servers, when the ceiling was set"
- the flag is a normal, declarative CLI option like every other governor flag

## Decision

**Config beats runtime at startup.** If `--power-ceiling` is passed, it becomes the
active ceiling and is written to the state file. Only when the flag is absent does
the file supply the value. After startup, live edits to the file always win — that
is the whole point of the control file.

The recommended deployment, documented in `HANDOVER.md`, is therefore to **leave
`--power-ceiling` out of the unit file** and let the state file be the single source
of truth. The flag exists for one-off manual runs and for hosts that want a
permanently declared cap.

## Alternatives Considered

| Alternative | Pros | Cons | Why rejected |
|-------------|------|------|--------------|
| **File always wins; CLI is only a seed** | Live-set survives every restart, so a `systemctl restart` mid-benchmark can never change wattage | Editing the unit file and restarting silently does nothing — the classic "why is my config ignored" trap. Requires a separate reset command to ever escape a stale file | The confusion cost is permanent and affects everyone; the footgun it avoids is narrow and is fully avoided by just not putting the flag in the unit file |
| **CLI always wins; never write the file from CLI** | Simplest mental model | The persisted value drifts from reality, so the file becomes untrustworthy as a status source | Silent divergence between "what is pinned" and "what the file says" is worse than either policy |
| **Separate `--power-ceiling-default` and `--power-ceiling-force`** | Expresses both intents precisely | Three-way precedence for a benchmarking knob; more surface than the problem warrants | Over-engineered for a single-operator fleet |

## Consequences

- If the unit file declares a ceiling, a `systemctl restart` resets any live override
  back to the declared value. This is documented in `HANDOVER.md` as the one restart
  behaviour to know about.
- The state file is always an accurate record of the active ceiling, so
  `cat /var/lib/nvidia-fan-control/power-ceiling` is a valid status check.
- Unpinning is durable: writing `none` clears the file, and with no CLI flag the next
  start comes up unpinned.
