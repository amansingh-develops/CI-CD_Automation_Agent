"""
LLM Router
==========
Decides which LLM provider to use and manages provider switching.

Routing Strategy:
    1. Always attempt Gemini first (primary provider)
    2. On failure (HTTP error, timeout, rate limit) → switch to Groq
    3. On Groq failure → raise and let orchestrator handle retry

Provider Health Tracking:
    - Track consecutive failures per provider
    - If a provider fails 3+ times in a row, skip it for remaining iterations
    - Reset health counters on new agent run

Confidence Gating Integration:
    - Router receives confidence_score from LLM response
    - If confidence < threshold → reject fix, increment failure counter
    - Orchestrator may retry with escalated context
"""
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Any
from app.core.config import (
    GEMINI_API_KEY, GROQ_API_KEY, OPENROUTER_API_KEY,
    PROVIDER_COOLDOWN_THRESHOLD, PROVIDER_COOLDOWN_SKIP_COUNT,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Provider Configuration
# ---------------------------------------------------------------------------
@dataclass
class ProviderConfig:
    """Configuration for a single LLM provider."""
    name: str
    api_key: str
    base_url: str
    model: str
    max_retries: int = 2
    timeout_seconds: int = 30


# Default provider configs
GEMINI_CONFIG = ProviderConfig(
    name="gemini",
    api_key=GEMINI_API_KEY or "",
    base_url="https://generativelanguage.googleapis.com/v1beta",
    model="gemini-2.0-flash",
)

GROQ_CONFIG = ProviderConfig(
    name="groq",
    api_key=GROQ_API_KEY or "",
    base_url="https://api.groq.com/openai/v1",
    model="llama-3.3-70b-versatile",
)

OPENROUTER_CONFIG = ProviderConfig(
    name="openrouter",
    api_key=OPENROUTER_API_KEY or "",
    base_url="https://openrouter.ai/api/v1",
    model="stepfun/step-3.5-flash:free",
    timeout_seconds=30,
    max_retries=1,
)


# ---------------------------------------------------------------------------
# Provider Health Tracker
# ---------------------------------------------------------------------------
@dataclass
class ProviderHealth:
    """Tracks consecutive failures and cooldown for a provider."""
    consecutive_failures: int = 0
    is_healthy: bool = True
    max_failures: int = PROVIDER_COOLDOWN_THRESHOLD
    cooldown_remaining: int = 0

    def record_failure(self) -> None:
        """Record a failure. Enter cooldown after max consecutive failures."""
        self.consecutive_failures += 1
        if self.consecutive_failures >= self.max_failures:
            self.is_healthy = False
            self.cooldown_remaining = PROVIDER_COOLDOWN_SKIP_COUNT
            logger.warning(
                "Provider entering cooldown after %d failures (skip %d calls)",
                self.consecutive_failures, self.cooldown_remaining,
            )

    def record_success(self) -> None:
        """Record a success. Reset failure counter."""
        self.consecutive_failures = 0
        # Do NOT reset is_healthy — cooldown controls re-enable

    def tick_cooldown(self) -> None:
        """Decrement cooldown. Auto-re-enable when cooldown expires."""
        if self.cooldown_remaining > 0:
            self.cooldown_remaining -= 1
            if self.cooldown_remaining <= 0:
                self.is_healthy = True
                # Keep 1 so a single post-cooldown failure re-triggers immediately
                self.consecutive_failures = max(1, self.max_failures - 1)
                logger.info("Provider cooldown expired, re-enabled (cautious)")

    def reset(self) -> None:
        """Reset health for a new agent run."""
        self.consecutive_failures = 0
        self.is_healthy = True
        self.cooldown_remaining = 0


# ---------------------------------------------------------------------------
# Context Level Decision
# ---------------------------------------------------------------------------
CONTEXT_LEVELS = ("small", "medium", "large")


def decide_context_level(attempt_number: int) -> str:
    """
    Decide the context level based on the fix attempt number.

    Parameters
    ----------
    attempt_number : int
        1-based attempt number for the current bug.

    Returns
    -------
    str
        One of "small", "medium", "large".
    """
    if attempt_number <= 1:
        return "medium"  # Always send full file on first attempt
    elif attempt_number == 2:
        return "medium"
    else:
        return "large"


# ---------------------------------------------------------------------------
# LLM Router
# ---------------------------------------------------------------------------
class LLMRouter:
    """
    Routes LLM requests to the best available provider.

    Usage:
        router = LLMRouter()
        config = router.get_provider()
        # ... make request ...
        router.report_success("gemini")   # or report_failure("gemini")
    """

    def __init__(self) -> None:
        self._providers: list[ProviderConfig] = [GROQ_CONFIG, GEMINI_CONFIG, OPENROUTER_CONFIG]
        self._health: dict[str, ProviderHealth] = {
            "groq": ProviderHealth(),
            "gemini": ProviderHealth(),
            "openrouter": ProviderHealth(),
        }
        self._usage_log: List[Dict[str, Any]] = []

    def get_provider(self) -> ProviderConfig:
        """
        Get the best available provider.

        Returns the first healthy provider. Falls back to the
        second provider if the primary is unhealthy.

        Returns
        -------
        ProviderConfig
            The selected provider configuration.

        Raises
        ------
        RuntimeError
            If all providers are unhealthy.
        """
        # Tick cooldowns for all providers before selection
        for h in self._health.values():
            h.tick_cooldown()

        for provider in self._providers:
            health = self._health.get(provider.name)
            if health and health.is_healthy:
                logger.debug("Selected provider: %s", provider.name)
                return provider

        # All providers unhealthy — try primary anyway as last resort
        logger.warning("All providers unhealthy, falling back to primary")
        return self._providers[0]

    def get_fallback_provider(self, *exclude_names: str) -> ProviderConfig | None:
        """
        Get the next healthy provider, excluding all specified providers.

        Parameters
        ----------
        *exclude_names : str
            Names of providers to skip (already tried / failed).

        Returns
        -------
        ProviderConfig or None
            Next available provider, or None if no fallback available.
        """
        for provider in self._providers:
            if provider.name not in exclude_names:
                health = self._health.get(provider.name)
                if health and health.is_healthy:
                    logger.info("Falling back to %s (skipping %s)", provider.name, ", ".join(exclude_names))
                    return provider
        return None

    def report_success(self, provider_name: str) -> None:
        """Record a successful request for a provider."""
        health = self._health.get(provider_name)
        if health:
            health.record_success()

    def report_failure(self, provider_name: str) -> None:
        """Record a failed request for a provider."""
        health = self._health.get(provider_name)
        if health:
            health.record_failure()

    def reset(self) -> None:
        """Reset all provider health for a new agent run."""
        for health in self._health.values():
            health.reset()
        self._usage_log.clear()

    def get_health(self, provider_name: str) -> ProviderHealth | None:
        """Get health tracker for a provider (for testing)."""
        return self._health.get(provider_name)

    # -----------------------------------------------------------------------
    # Telemetry
    # -----------------------------------------------------------------------
    def log_provider_usage(
        self,
        provider_used: str,
        fallback_triggered: bool = False,
        cooldown_active: bool = False,
    ) -> None:
        """Record provider usage for a single fix attempt."""
        self._usage_log.append({
            "provider_used": provider_used,
            "fallback_triggered": fallback_triggered,
            "cooldown_active": cooldown_active,
        })

    def get_provider_usage_log(self) -> List[Dict[str, Any]]:
        """Return recorded provider usage events."""
        return list(self._usage_log)

    @property
    def provider_health_state(self) -> Dict[str, Any]:
        """Expose per-provider health and cooldown for telemetry/dashboard."""
        return {
            name: {
                "is_healthy": h.is_healthy,
                "consecutive_failures": h.consecutive_failures,
                "cooldown_remaining": h.cooldown_remaining,
            }
            for name, h in self._health.items()
        }
