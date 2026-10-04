"""Decision memory: every finished investigation becomes a searchable case.

Each case (symptom, category, findings, root cause, actions taken, outcome) is
stored in SQLite and also written as a Markdown file under decisions/cases/,
so the technician can read or version them. When a new problem comes in, the
most similar past cases (TF-IDF cosine over symptom + root cause + findings)
are given to the LLM — fixed cases first — so the tool learns from experience.
"""

import json
import math
import re
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from .device_store import _connect as _device_connect

CASES_DIR = Path(__file__).resolve().parent.parent / "decisions" / "cases"
_TOKEN = re.compile(r"[a-z0-9]{3,}")
_STOP = {"the", "and", "for", "with", "this", "that", "from", "was", "are", "not", "but", "has", "have", "using", "laptop", "issue"}


def _connect():
    connection = _device_connect()
    connection.execute("""
        CREATE TABLE IF NOT EXISTS decision_cases (
            id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            device_id TEXT,
            hostname TEXT,
            os_type TEXT,
            session_id TEXT,
            symptom TEXT NOT NULL,
            category TEXT,
            root_cause TEXT,
            findings_json TEXT,
            actions_json TEXT,
            outcome TEXT,
            notes TEXT,
            markdown_path TEXT
        )
    """)
    connection.commit()
    return connection


def _tokens(text):
    return [token for token in _TOKEN.findall((text or "").lower()) if token not in _STOP]


def _slug(text):
    return re.sub(r"[^a-z0-9]+", "-", (text or "case").lower()).strip("-")[:40] or "case"


def _decode(row):
    item = dict(row)
    item["findings"] = json.loads(item.pop("findings_json") or "[]")
    item["actions"] = json.loads(item.pop("actions_json") or "[]")
    return item


def save_case(symptom, category, root_cause, findings, actions, outcome, notes="", device=None, session_id=None):
    case_id = f"case-{uuid.uuid4().hex[:10]}"
    now = datetime.now(timezone.utc)
    device = device or {}
    CASES_DIR.mkdir(parents=True, exist_ok=True)
    path = CASES_DIR / f"{now:%Y-%m-%d}-{_slug(device.get('hostname'))}-{_slug(symptom)}-{case_id[-4:]}.md"
    path.write_text(render_markdown(case_id, now, symptom, category, root_cause, findings, actions, outcome, notes, device), encoding="utf-8")
    with _connect() as connection:
        connection.execute(
            "INSERT INTO decision_cases (id, created_at, device_id, hostname, os_type, session_id, symptom, category, root_cause, findings_json, actions_json, outcome, notes, markdown_path) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (case_id, now.isoformat(), device.get("device_id"), device.get("hostname"), device.get("os_type"), session_id,
             symptom, category, root_cause, json.dumps(findings), json.dumps(actions), outcome, notes, str(path)),
        )
        connection.commit()
    return get_case(case_id)


def render_markdown(case_id, when, symptom, category, root_cause, findings, actions, outcome, notes, device):
    lines = [
        f"# Case {case_id}: {symptom[:80]}",
        "",
        f"- **Date:** {when:%Y-%m-%d %H:%M} UTC",
        f"- **Device:** {device.get('hostname', '?')} ({device.get('os_type', '?')})",
        f"- **Category:** {category or 'unknown'}",
        f"- **Outcome:** {outcome}",
        "",
        "## Symptom",
        symptom,
        "",
        "## Root cause (decision)",
        root_cause or "Not determined.",
        "",
        "## Evidence",
    ]
    lines += [f"- [{item.get('severity', '?')}] {item.get('title', '')} — {item.get('detail', '')}" for item in findings] or ["- None recorded."]
    lines += ["", "## Actions taken"]
    lines += [f"- `{item.get('tool')}` {json.dumps(item.get('args', {}))} → {item.get('status', '?')}" for item in actions] or ["- No changes made."]
    if notes:
        lines += ["", "## Technician notes", notes]
    return "\n".join(lines) + "\n"


def get_case(case_id):
    with _connect() as connection:
        row = connection.execute("SELECT * FROM decision_cases WHERE id = ?", (case_id,)).fetchone()
        return _decode(row) if row else None


def list_cases(limit=100):
    with _connect() as connection:
        return [_decode(row) for row in connection.execute("SELECT * FROM decision_cases ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()]


def search(query, limit=3, os_type=None):
    """Most similar past cases; fixed cases and same-OS cases rank higher."""
    cases = list_cases(limit=2000)
    if not cases or not query:
        return []
    documents = [_tokens(" ".join([case["symptom"], case.get("root_cause") or "", case.get("category") or "",
                                   " ".join(f.get("title", "") for f in case["findings"])])) for case in cases]
    frequency = Counter(token for doc in documents for token in set(doc))
    total = len(documents)

    def vector(tokens):
        counts = Counter(tokens)
        # +1 smoothing so terms still carry weight when few cases exist (idf would be 0).
        return {token: count * (math.log((1 + total) / (1 + frequency.get(token, 0))) + 1) for token, count in counts.items()}

    query_vector = vector(_tokens(query))
    query_norm = math.sqrt(sum(value * value for value in query_vector.values())) or 1
    scored = []
    for case, doc in zip(cases, documents):
        doc_vector = vector(doc)
        dot = sum(weight * doc_vector.get(token, 0) for token, weight in query_vector.items())
        norm = math.sqrt(sum(value * value for value in doc_vector.values())) or 1
        score = dot / (query_norm * norm)
        if score <= 0:
            continue
        if case.get("outcome") == "fixed":
            score *= 1.3
        if os_type and case.get("os_type") == os_type:
            score *= 1.1
        scored.append((score, case))
    scored.sort(key=lambda item: item[0], reverse=True)
    return [{**case, "similarity": round(score, 3)} for score, case in scored[:limit]]


def adr_documents():
    """The design-decision records (ADRs) in decisions/*.md, newest number last."""
    folder = CASES_DIR.parent
    docs = []
    for path in sorted(folder.glob("[0-9][0-9][0-9][0-9]-*.md")):
        text = path.read_text(encoding="utf-8")
        title = text.splitlines()[0].lstrip("# ").strip() if text else path.stem
        docs.append({"file": path.name, "title": title, "markdown": text})
    return docs
