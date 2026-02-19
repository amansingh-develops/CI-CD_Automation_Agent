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
from app.core.config import GEMINI_API_KEY, GROQ_API_KEY

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


# ---------------------------------------------------------------------------
# Provider Health Tracker
# ---------------------------------------------------------------------------
@dataclass
class ProviderHealth:
    """Tracks consecutive failures for a provider."""
    consecutive_failures: int = 0
    is_healthy: bool = True
    max_failures: int = 3

    def record_failure(self) -> None:
        """Record a failure. Mark unhealthy after max consecutive failures."""
        self.consecutive_failures += 1
        if self.consecutive_failures >= self.max_failures:
            self.is_healthy = False
            logger.warning(
                "Provider marked unhealthy after %d consecutive failures",
                self.consecutive_failures,
            )

    def record_success(self) -> None:
        """Record a success. Reset failure counter."""
        self.consecutive_failures = 0
        # Do NOT reset is_healthy — once unhealthy, stays unhealthy for this run

    def reset(self) -> None:
        """Reset health for a new agent run."""
        self.consecutive_failures = 0
        self.is_healthy = True


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
        return "small"
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
        self._providers: list[ProviderConfig] = [GEMINI_CONFIG, GROQ_CONFIG]
        self._health: dict[str, ProviderHealth] = {
            "gemini": ProviderHealth(),
            "groq": ProviderHealth(),
        }

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
        for provider in self._providers:
            health = self._health.get(provider.name)
            if health and health.is_healthy:
                logger.debug("Selected provider: %s", provider.name)
                return provider

        # All providers unhealthy — try primary anyway as last resort
        logger.warning("All providers unhealthy, falling back to primary")
        return self._providers[0]

    def get_fallback_provider(self, failed_provider: str) -> ProviderConfig | None:
        """
        Get a fallback provider after the given provider failed.

        Parameters
        ----------
        failed_provider : str
            Name of the provider that just failed.

        Returns
        -------
        ProviderConfig or None
            Next available provider, or None if no fallback available.
        """
        for provider in self._providers:
            if provider.name != failed_provider:
                health = self._health.get(provider.name)
                if health and health.is_healthy:
                    logger.info("Falling back from %s to %s", failed_provider, provider.name)
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

    def get_health(self, provider_name: str) -> ProviderHealth | None:
        """Get health tracker for a provider (for testing)."""
        return self._health.get(provider_name)
