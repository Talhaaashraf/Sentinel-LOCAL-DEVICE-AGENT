# 0001 — Build the diagnostic/agentic tool by extending Sentinel

**Status:** accepted

## Context
The request was for a web-based agentic tool to diagnose laptops: paste an install
script on a target machine on the same network, have it connect back to a dashboard,
and diagnose issues (browser RAM, storage spikes, CPU spikes, hangs, OS corruption,
stuck uninstalls, updates) with an LLM brain, plus stress tests and a memory of every
decision. Sentinel already had agent registration, cross-platform telemetry collection,
an alerts/rules engine, event-log and security collectors, and an AI technician.

## Decision
Extend Sentinel rather than start a new app. Reuse its agent transport, device store,
auth and dashboard shell; add the command channel, tool catalog, agentic loop, stress
tests, decision memory and new dashboard tabs on top.

## Consequences
Faster to a working tool and one consistent codebase. The main shift is that Sentinel
was strictly read-only; this work adds the ability to change the target machine, which
is why every change is gated behind explicit technician approval (see 0003).
