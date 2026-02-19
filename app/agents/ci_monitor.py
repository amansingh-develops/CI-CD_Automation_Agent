"""
CI Monitor Agent
================
Polls GitHub Actions API to check the status of a specific commit/branch.
"""
import httpx
import time
import asyncio
import logging
import re
from typing import Optional, Literal

logger = logging.getLogger(__name__)

CIStatus = Literal["success", "failure", "pending", "timeout", "error"]

class CIMonitor:
    """
    Agent that monitors the health of the CI/CD pipeline on GitHub.
    """

    def __init__(self, github_token: str = "") -> None:
        self.github_token = github_token
        self.headers = {
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": "CI-Healing-Agent"
        }
        if github_token:
            self.headers["Authorization"] = f"token {github_token}"

    def _extract_repo_path(self, repo_url: str) -> str:
        """Extract 'owner/repo' from GitHub URL."""
        # Handle https://github.com/owner/repo.git or git@github.com:owner/repo.git
        match = re.search(r"github\.com[:/](.+?)(?:\.git)?$", repo_url)
        if match:
            return match.group(1).rstrip("/")
        return ""

    async def poll_status(
        self, 
        repo_url: str, 
        commit_sha: str, 
        timeout_seconds: int = 300,
        max_poll_attempts: int = 30
    ) -> CIStatus:
        """
        Poll GitHub Actions until a final status is reached or timeout.
        
        Parameters
        ----------
        repo_url : str
            The repository URL.
        commit_sha : str
            The SHA of the commit to check.
        timeout_seconds : int
            Max time to wait in seconds (default: 5 min).
        max_poll_attempts : int
            Max number of polling attempts before giving up (default: 30).

        Returns
        -------
        CIStatus
            The final status: success, failure, timeout, unknown_timeout, or error.
        """
        repo_path = self._extract_repo_path(repo_url)
        if not repo_path:
            logger.error("Could not extract repo path from %s", repo_url)
            return "error"

        url = f"https://api.github.com/repos/{repo_path}/commits/{commit_sha}/check-runs"
        
        start_time = time.time()
        backoff = 5  # start with 5 seconds
        attempts = 0

        logger.info("Starting CI poll for %s (commit: %s)", repo_path, commit_sha[:7])

        async with httpx.AsyncClient(headers=self.headers) as client:
            while (time.time() - start_time) < timeout_seconds and attempts < max_poll_attempts:
                attempts += 1
                try:
                    response = await client.get(url)
                    response.raise_for_status()
                    data = response.json()

                    runs = data.get("check_runs", [])
                    if not runs:
                        # No check runs yet, wait and retry
                        logger.info("No check runs found yet, waiting... (attempt %d/%d)", attempts, max_poll_attempts)
                    else:
                        # Check if all runs are completed
                        all_completed = all(r.get("status") == "completed" for r in runs)
                        if all_completed:
                            # Check if any failed
                            any_failed = any(
                                r.get("conclusion") in ("failure", "timed_out", "action_required") 
                                for r in runs
                            )
                            if any_failed:
                                logger.info("CI Finished: FAILURE")
                                return "failure"
                            
                            # Success if all conclusions are success or neutral/skipped
                            all_success = all(
                                r.get("conclusion") in ("success", "neutral", "skipped")
                                for r in runs
                            )
                            if all_success:
                                logger.info("CI Finished: SUCCESS")
                                return "success"

                    logger.info("CI still pending, sleeping %ds... (attempt %d/%d)", backoff, attempts, max_poll_attempts)
                    await asyncio.sleep(backoff)
                    # Simple linear backoff cap
                    backoff = min(backoff + 5, 30)

                except Exception as e:
                    logger.error("Error polling CI status: %s", e)
                    # We don't exit on transient errors, just wait
                    await asyncio.sleep(10)

        if attempts >= max_poll_attempts:
            logger.warning("CI poll exhausted max attempts (%d)", max_poll_attempts)
            return "unknown_timeout"

        logger.warning("CI poll timed out after %ds", timeout_seconds)
        return "timeout"
