"""Spelling correction for technician problem descriptions.

Typed problems like "dignose browser useing alot of ram" are normalised before
matching playbooks, so rule matching works even when the LLM is offline. The
LLM still receives the original text plus the corrected version. Corrections
are reported back so the dashboard can show "Interpreted as: ...".
"""

import difflib
import re

# Known misspellings -> correction. Includes the ones from the original request.
KNOWN = {
    "dignose": "diagnose", "dignosing": "diagnosing", "diagnoes": "diagnose", "daignose": "diagnose", "diagnos": "diagnose",
    "lpatop": "laptop", "labtop": "laptop", "laptp": "laptop", "loptop": "laptop",
    "isssue": "issue", "isue": "issue", "issu": "issue", "isses": "issues", "isssues": "issues",
    "storgae": "storage", "stroage": "storage", "storge": "storage", "sotrage": "storage",
    "dissiocon": "decision", "decison": "decision", "desicion": "decision", "dicision": "decision",
    "speeling": "spelling", "meny": "many", "predit": "predict",
    "alot": "a lot", "useing": "using", "usng": "using",
    "brwoser": "browser", "browzer": "browser", "broswer": "browser", "chorme": "chrome", "crome": "chrome",
    "memmory": "memory", "memroy": "memory", "memery": "memory",
    "cpu's": "cpu", "proccess": "process", "procces": "process", "prosess": "process",
    "hangging": "hanging", "haning": "hanging", "freezs": "freezes", "freez": "freeze",
    "corupt": "corrupt", "currupt": "corrupt", "corrput": "corrupt", "curropt": "corrupt",
    "uninstal": "uninstall", "unistall": "uninstall", "uninstalling": "uninstalling", "unintall": "uninstall",
    "updat": "update", "upadte": "update", "updaet": "update", "updte": "update",
    "intenet": "internet", "internt": "internet", "wfi": "wifi", "wify": "wifi",
    "batery": "battery", "battry": "battery", "batterry": "battery",
    "contol": "control", "controll": "control", "pannel": "panel", "panal": "panel",
    "perfomance": "performance", "preformance": "performance", "performence": "performance",
    "stres": "stress", "sress": "stress", "spkie": "spike", "spik": "spike",
    "unusal": "unusual", "unusuall": "unusual", "slwo": "slow", "sloww": "slow",
    "virsu": "virus", "viurs": "virus", "drvier": "driver", "dirver": "driver",
    "bluescreen": "blue screen", "bsod's": "bsod",
}

# Vocabulary for fuzzy matching unknown words (only words longer than 4 letters are fuzzed).
VOCABULARY = sorted(set(KNOWN.values()) | {
    "browser", "chrome", "firefox", "memory", "storage", "space", "drive", "disk", "spike", "processor",
    "hanging", "freeze", "freezing", "crash", "crashing", "corrupt", "corrupted", "uninstall", "remove",
    "update", "updates", "network", "internet", "battery", "startup", "service", "driver", "performance",
    "stress", "temperature", "overheating", "screen", "responding", "control", "panel", "program", "programs",
    "application", "windows", "restart", "rebooting", "virus", "malware", "printer", "keyboard", "bluetooth",
})


def normalize(text):
    """Return (corrected_text, [(wrong, right), ...])."""
    corrections = []
    output = []
    for token in re.findall(r"[A-Za-z']+|[^A-Za-z']+", text or ""):
        lowered = token.lower()
        if not token.isalpha() and "'" not in token:
            output.append(token)
            continue
        if lowered in KNOWN:
            fixed = KNOWN[lowered]
        elif len(lowered) > 4 and lowered not in VOCABULARY:
            match = difflib.get_close_matches(lowered, VOCABULARY, n=1, cutoff=0.84)
            fixed = match[0] if match else lowered
        else:
            fixed = lowered
        if fixed != lowered:
            corrections.append((token, fixed))
            output.append(fixed)
        else:
            output.append(token)
    return "".join(output), corrections
