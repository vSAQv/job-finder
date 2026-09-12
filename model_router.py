"""
Static per-task model pools with runtime enforcement and passive demotion.

Design rationale
----------------
Free model availability on OpenRouter rotates frequently and individual
endpoints fail transiently (429, 404) or go away entirely. Two candidate
approaches were considered and rejected:

- Probing/battery ranking: re-ranking every model at refresh costs many
  requests and is fragile under rate limits; a single or repeated probe is a
  broad proxy, not a measurement of our exact tasks.
- LLM-with-web-search rating: the free OpenRouter tier exposes no web-search
  model, and third-party benchmark/opinion rankings are not representative of
  our two narrow tasks.

This module instead keeps a small, hand-curated pool per task, seeded from
verified live evidence (usage rankings, the live /models API, and behavioral
probes). Guarantees come from ENFORCEMENT at the point of use, not from
predicting which model is best:

- call_llm (main.py) validates every reply: judge replies must be an exact
  YES/NO token, cover letters must pass a Russian sanity check. A failed or
  malformed reply falls through to the next model automatically.
- Models that fail at runtime are demoted in-memory for a short window, so a
  broken endpoint does not keep being tried first, at zero extra request cost.

The user-configured OPENROUTER_MODEL override is always attempted first.
"""

import re
import threading
import time
from typing import Dict, List, Optional

# Curated pools, best-first, seeded from verified evidence (see decision
# 12a6b954). Judge favours strict, consistent rule-following; writer favours
# correct, natural Russian. Models absent from the current free set are kept
# as tail fallbacks and demoted on failure.
POOLS: Dict[str, List[str]] = {
    "judge": [
        "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free",
        "google/gemma-4-31b-it:free",
        "nvidia/nemotron-3-ultra-550b-a55b:free",
        "nvidia/nemotron-3-super-120b-a12b:free",
        "google/gemma-4-26b-a4b-it:free",
    ],
    "writer": [
        "google/gemma-4-31b-it:free",
        "nvidia/nemotron-3-ultra-550b-a55b:free",
        "google/gemma-4-26b-a4b-it:free",
        "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free",
        "nvidia/nemotron-3-super-120b-a12b:free",
    ],
}

DEMOTION_SECONDS = 1800  # 30 minutes


def _normalize_token(reply: Optional[str]) -> Optional[str]:
    """Return a canonical YES/NO/ДА/НЕТ token, or None if the reply rambles."""
    if not reply:
        return None
    token = re.sub(r"[^A-Za-zА-Яа-я]", "", reply).strip().upper()
    return token if token in {"YES", "NO", "ДА", "НЕТ"} else None


def _writer_sane(reply: Optional[str]) -> bool:
    """Heuristic sanity gate for generated Russian cover letters."""
    if not reply:
        return False
    stripped = reply.strip()
    cyr = sum(1 for ch in stripped if "\u0400" <= ch <= "\u04FF")
    if cyr == 0:
        return False
    if len(stripped.split()) < 6:
        return False
    if any(ch in stripped for ch in "[]"):
        return False
    return True


class ModelPool:
    """Thread-safe static pool with in-memory passive demotion of failures.

    get_ranked returns the pool order, optionally pinning the user override
    first and lifting demoted models back after a cooldown. The pool does not
    probe the network; failures are learned from real calls via mark_failure.
    """

    def __init__(self, demotion_seconds: int = DEMOTION_SECONDS) -> None:
        self._lock = threading.Lock()
        self._demotion_seconds = demotion_seconds
        self._demoted_until: Dict[str, float] = {}

    def get_ranked(self, task: str, override: Optional[str] = None) -> List[str]:
        """Ordered candidate list for a task, override pinned first."""
        with self._lock:
            now = time.time()
            pool = [m for m in POOLS.get(task, [])]
            # Lift expired demotions; keep currently-demoted at the tail.
            active = []
            cooldown = []
            for m in pool:
                until = self._demoted_until.get(m, 0.0)
                if until and now < until:
                    cooldown.append(m)
                else:
                    active.append(m)
            ranked = active + cooldown
        if override and override not in ranked:
            ranked = [override] + ranked
        elif override:
            ranked = [override] + [m for m in ranked if m != override]
        return ranked

    def mark_failure(self, task: str, model: Optional[str]) -> None:
        """Demote a model that failed at runtime for a cooldown period."""
        if not model:
            return
        with self._lock:
            if model in POOLS.get(task, []):
                self._demoted_until[model] = time.time() + self._demotion_seconds