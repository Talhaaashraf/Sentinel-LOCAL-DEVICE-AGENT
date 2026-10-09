"""Shared provider errors and helpers."""

import re

THINK_BLOCK = re.compile(r"<think>.*?</think>", re.DOTALL)


class LLMUnavailableError(RuntimeError):
    """The provider cannot be reached or is not configured."""


class LLMModelError(LLMUnavailableError):
    """The provider is reachable but rejected the configured model or request."""


def clean_content(text):
    """Drop reasoning blocks some local models emit before the answer."""
    return THINK_BLOCK.sub("", text or "").strip()
