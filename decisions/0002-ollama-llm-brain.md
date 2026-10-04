# 0002 — Local Ollama as the default LLM brain

**Status:** accepted

## Context
The brain must be free and run continuously. The user's host is CPU-only with 16 GB RAM.
Sentinel's existing technician used Groq (cloud).

## Decision
Default to a local Ollama provider with **qwen2.5:3b-instruct**: ~2 GB, runs on CPU with
16 GB RAM, and supports native tool/function calling (needed for the agentic loop).
Fallback model: `llama3.2:3b`; a stronger option if a GPU is added later:
`qwen2.5:7b-instruct`. Groq stays selectable via `LLM_PROVIDER=groq`. A single
`llm_provider` module abstracts both behind one `chat()` call, and the existing
technician endpoints route through it.

## Consequences
Free, private, always-on brain. On CPU the 3B model is slower than cloud, so the loop is
capped at 8 steps and a rule-based engine (`playbooks.py`) produces the same result shape
when no LLM is available — the tool never hard-depends on the LLM.
