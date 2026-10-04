# 0005 — Decision memory as SQLite + Markdown, recalled by TF-IDF

**Status:** accepted

## Context
The tool should "remember all decisions" and get better over time, while staying local
and dependency-light (no vector database, no embedding service).

## Decision
Each closed diagnosis is saved as a case in SQLite and as a Markdown file under
`decisions/cases/`. When a new problem arrives, the most similar past cases are found
with a small in-process TF-IDF cosine similarity over symptom + root cause + findings
(`decision_memory.search`), ranking fixed cases and same-OS cases higher, and fed into
the LLM prompt (and shown in the UI). Design decisions live as ADRs in this folder.

## Consequences
Zero extra infrastructure; cases are human-readable and git-friendly. TF-IDF is lexical,
not semantic, which is adequate at small scale; it can be swapped for embeddings later
without changing the stored format.
