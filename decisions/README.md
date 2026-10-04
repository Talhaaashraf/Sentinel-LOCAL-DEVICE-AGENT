# Decisions

This folder is the project's memory. It has two parts:

1. **Design decisions (ADRs)** — `0001-*.md`, `0002-*.md`, … One short file per
   significant decision made while building the diagnostic/agentic features, so the
   reasoning is never lost. These are also shown in the dashboard under
   **Decisions → Design decisions**.

2. **Solved cases** — `cases/*.md`, written automatically every time a diagnosis is
   closed with an outcome (fixed / partly / not fixed). Each records the symptom, the
   evidence gathered, the root cause, the actions taken and the result. The AI recalls
   the most similar past cases when diagnosing a new problem, so the tool gets better
   with use. These appear under **Decisions → Solved cases**.

## Spelling glossary

The problem box accepts typos; they are corrected before matching (and the AI reads
intent directly). Common corrections — including the ones from the original request —
live in `backend/typos.py`. Examples:

| Typed | Understood as |
|-------|---------------|
| dignose / dignosing | diagnose / diagnosing |
| lpatop, labtop | laptop |
| isssue | issue |
| storgae, stroage | storage |
| dissiocon, decison | decision |
| meny | many |
| predit | predict |
| alot | a lot |
| useing | using |
| uninstal, unistall | uninstall |
| corupt, currupt | corrupt |
| contol pannel | control panel |
| perfomance | performance |
