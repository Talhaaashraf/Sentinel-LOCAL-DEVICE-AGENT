# 0004 — Outbound polling command channel

**Status:** accepted

## Context
The dashboard must send tool commands to a laptop that may be behind NAT or a firewall,
with no inbound ports open. Sentinel's agent already reports outbound over HTTP with a
bearer token.

## Decision
Keep everything agent-initiated. The agent polls `GET /api/agents/commands/next`,
runs each approved command in its own thread, streams progress to
`/api/agents/commands/{id}/progress`, and posts the result to `/result`. The server
queues commands in SQLite (`command_store`) and, for the agentic loop, waits
synchronously for results via `dispatch.py`. No inbound connection to the laptop is ever
needed.

## Consequences
Works through NAT/firewalls with no network changes on the target. Latency is bounded by
the poll interval (1–3 s), which is fine for interactive diagnosis. Long operations
stream progress so the UI stays live, and cancellation is delivered on the next poll.
