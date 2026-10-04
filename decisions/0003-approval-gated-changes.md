# 0003 — Approval gate for anything that changes a machine

**Status:** accepted

## Context
The tool must fix real problems (end processes, force-uninstall stubborn apps, run
SFC/DISM, install updates, clean junk, reset the network) on real laptops, driven partly
by an LLM. That is powerful and risky.

## Decision
Tools are an allowlisted catalog (`agent/tool_catalog.json`) tagged `read`, `change`,
`stress` or `shell`. The **server** reads its own copy of the catalog to decide what is
allowed and what needs approval — never a list supplied by an agent. `read` tools run
automatically (including when the LLM calls them); `change`/`stress`/`shell` tools enter
a `pending_approval` state and run only after the technician clicks Approve. The LLM can
only *propose* changes. Raw shell is refused unless `ALLOW_REMOTE_SHELL=true`. Risky
Windows changes back up first (registry export, folder quarantine, restore point) and
stress tests auto-stop on overheating.

## Consequences
Nothing changes a laptop without a human decision, and every change is auditable in the
command queue. Slightly more clicks, which is the right trade-off for remote repair.
