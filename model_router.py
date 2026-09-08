"""
Dynamic selection of free OpenRouter models per task profile.

Free model availability on OpenRouter changes and individual endpoints go
temporarily unavailable or rate-limited. Hard-coded model lists therefore go
stale and break the bot. This module:

1. Always tries the user-configured OPENROUTER_MODEL first when set.
2. Maintains two curated task pools (judge / writer) that differ in fitness:
   strict rule-following for judge tasks, original Russian prose for writer.
3. Runs a cheap deterministic probe against each candidate at refresh time to
   re-rank pools and demote unavailable or badly-behaving models.
4. Caches ranked results with a TTL so short-lived rate limits do not force
   re-probing on every cycle.

The system degrades gracefully to the hard-coded pool order whenever the
OpenRouter models endpoint or probes are unavailable (e.g. cold start with no
network).
"""

import json
import os
import threading
import time
from typing import Callable, Dict, List, Optional

OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
CACHE_PATH = "/app/.model_pool_cache.json"
CACHE_TTL_SECONDS = 6 * 3600  # 6h
PROBE_TIMEOUT_SECONDS = 10.0
MAX_PROBES_PER_POOL = 4

JUDGE_PROMPT = "Reply with exactly 'YES' or 'NO'. Is 2 + 2 equal to 4?"
JUDGE_EXPECTED = {"YES", "NO"}
WRITER_PROMPT = (
    "Write one short and completely original Russian sentence about working on a project."
)

# Curated initial pools ordered by fitness for each task. The router re-ranks
# and prunes at runtime; these only define the default preference order.
POOLS: Dict[str, List[str]] = {
    "judge": [
        "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free",
        "google/gemma-4-31b-it:free",
        "nvidia/nemotron-3-ultra-550b-a55b:free",
        "nvidia/nemotron-3-super-120b-a12b:free",
        "google/gemma-4-26b-a4b-it:free",
    ],
    "writer": [
        "nvidia/nemotron-3-ultra-550b-a55b:free",
        "google/gemma-4-31b-it:free",
        "google/gemma-4-26b-a4b-it:free",
        "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free",
        "nvidia/nemotron-3-super-120b-a12b:free",
    ],
}


def _score_reply(task: str, reply: Optional[str], latency: float) -> float:
    """Heuristic fitness of a probe reply for the given task profile.

    Judge: strict rule-following; the reply must be exactly the expected token.
    Writer: must produce non-trivial text that actually contains Cyrillic.
    """
    if not reply:
        return 0.0
    stripped = reply.strip()
    if task == "judge":
        if stripped in JUDGE_EXPECTED:
            return 100.0 - min(latency, 20.0)
        if stripped.upper() in JUDGE_EXPECTED:
            return 60.0
        return 10.0
    cyr = sum(1 for ch in stripped if "\u0400" <= ch <= "\u04FF")
    if cyr == 0:
        return 0.0
    if len(stripped.split()) >= 5:
        return 100.0 - min(latency, 20.0)
    return 30.0


class ModelPool:
    """Thread-safe, TTL-cached ranked per-task model pool.

    Probing and the models API fetch are injected as callables so the class is
    testable offline; the OpenAI HTTP client lives in main.py.
    """

    def __init__(
        self,
        probe: Optional[Callable[[str, str], tuple[Optional[str], float]]] = None,
        cache_path: str = CACHE_PATH,
        ttl_seconds: int = CACHE_TTL_SECONDS,
    ) -> None:
        self._probe = probe
        self._cache_path = cache_path
        self._ttl_seconds = ttl_seconds
        self._lock = threading.Lock()
        self._ranked: Dict[str, List[str]] = {}
        self._refreshed_at = 0.0
        self._load_cache()

    def _load_cache(self) -> None:
        try:
            with open(self._cache_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            self._ranked = data.get("ranked", {})
            self._refreshed_at = float(data.get("refreshed_at", 0.0))
        except (OSError, ValueError, json.JSONDecodeError):
            self._ranked = {}
            self._refreshed_at = 0.0

    def _save_cache(self) -> None:
        try:
            with open(self._cache_path, "w", encoding="utf-8") as fh:
                json.dump(
                    {
                        "ranked": self._ranked,
                        "refreshed_at": self._refreshed_at,
                    },
                    fh,
                    ensure_ascii=False,
                )
        except OSError:
            # Best-effort; failing to write the cache must not break the bot.
            pass

    def _stale(self) -> bool:
        return time.time() - self._refreshed_at > self._ttl_seconds

    def refresh(
        self,
        http_get: Callable[[str], dict],
        force: bool = False,
    ) -> None:
        """Fetch the current free-model list and re-rank each pool.

        http_get must return the OpenRouter /models JSON payload.
        """
        if not force and not self._stale():
            return
        try:
            payload = http_get(OPENROUTER_MODELS_URL)
        except Exception:
            # Keep the current cache on transient API or network errors.
            return

        available: set[str] = {
            m.get("id")
            for m in payload.get("data", [])
            if m.get("id", "").endswith(":free")
        }
        available.discard(None)

        ranked: Dict[str, List[str]] = {}
        for task, pool in POOLS.items():
            ranked[task] = self._rank_pool(task, pool, available)
        with self._lock:
            self._ranked = ranked
            self._refreshed_at = time.time()
        self._save_cache()

    def _rank_pool(self, task: str, pool: List[str], available: set) -> List[str]:
        """Rank one pool: working probes first, unprobed next, failing last.

        A probe failure (rate limit, timeout) demotes a model but never removes
        it: free endpoints are unreliable, and a transient 429 should not
        exile a model for the rest of the cache TTL. Models missing from the
        live API list are demoted to the tail but still kept for offline runs.
        """
        if self._probe is None:
            return list(pool)
        working: List[tuple[float, int, str]] = []
        failing: List[tuple[float, int, str]] = []
        unavailable: List[tuple[float, int, str]] = []
        fallback_order = {m: i for i, m in enumerate(pool)}

        def _slot(model: str) -> tuple[float, int, str]:
            return (0.0, fallback_order.get(model, 999), model)

        for model in pool:
            if model not in available:
                unavailable.append(_slot(model))
                continue
            score = self._probe_and_score(task, model)
            target = working if score > 0.0 else failing
            target.append((score, fallback_order[model], model))
        # Anything in the pool beyond the probe budget keeps curated order.
        probed = {m for _, _, m in working} | {
            m for _, _, m in failing
        }
        unprobed = [
            _slot(m) for m in pool[:MAX_PROBES_PER_POOL] if m not in probed
        ]
        working.sort(key=lambda x: (-x[0], x[1]))
        failing.sort(key=lambda x: (-x[0], x[1]))
        unavailable.sort(key=lambda x: x[1])
        return [m for _, _, m in working + unprobed + failing + unavailable]

    def _probe_and_score(self, task: str, model: str) -> float:
        if self._probe is None:
            return 100.0
        prompt = JUDGE_PROMPT if task == "judge" else WRITER_PROMPT
        try:
            reply, latency = self._probe(model, prompt)
        except Exception:
            return 0.0
        return _score_reply(task, reply, latency)

    def get_ranked(self, task: str, override: Optional[str] = None) -> List[str]:
        """Ordered candidate list for a task, with optional override pinned first."""
        with self._lock:
            ranked = list(self._ranked.get(task, []))
        if not ranked:
            ranked = list(POOLS.get(task, []))
        if override:
            ranked = [override] + [m for m in ranked if m != override]
        return ranked


def default_override() -> Optional[str]:
    """OPENROUTER_MODEL env value if set, else None."""
    return os.environ.get("OPENROUTER_MODEL")