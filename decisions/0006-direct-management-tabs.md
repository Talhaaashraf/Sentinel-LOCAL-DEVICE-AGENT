# 0006 — Direct Software & Automation tabs (beyond the AI flow)

**Status:** accepted

## Context
The agent already had tools to list apps (flagging hidden-from-Control-Panel and orphaned/broken
entries), force-uninstall, remove orphaned entries and find leftovers, but they were only
reachable through the AI Diagnose flow. The technician wanted direct, one-click control —
especially to remove broken/incomplete installs that still show in Control Panel but are gone,
or are hidden from it — plus undo, startup management, scheduling and fleet actions.

## Decision
- Add a **Software** tab (Installed apps / Startup / Backups) and an **Automation** tab
  (Scheduled checks / Fleet actions), reusing the existing command queue and tools.
- Removal is **one-click + a confirm dialog**; the command is created pre-approved
  (`approve:true`). The technician's deliberate click on a specific app is the approval, so this
  stays consistent with ADR 0003 (no change runs without a human decision) while being fast.
- Every reversible change now writes a **manifest** (`sentinel_backups/manifest.json`) on the
  agent, and new `list_backups` / `restore_backup` tools power a Backups/Undo view, so a bad
  removal can be reversed (quarantined folder moved back, exported `.reg` re-imported, startup
  item re-enabled).
- Scheduled checks run a preset's read-only tools on a timer and raise alerts (they never
  auto-apply fixes). Fleet actions are limited to a safe allowlist.

## Consequences
Common cleanup is now a couple of clicks without invoking the LLM, and it is undoable. Scheduled
checks add a lightweight background loop. Auto-remediation is deliberately excluded — changes
still need a human, in keeping with the safety model.
