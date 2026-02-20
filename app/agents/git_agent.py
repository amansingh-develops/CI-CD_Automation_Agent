"""
Git Agent
=========
Handles repository operations: writing fixes, committing, and pushing.
Enforces strict naming conventions and safety limits.
"""
import os
import re
import subprocess
import logging
import time
from typing import Optional, Dict, Any, List
from app.models.fix_result import FixResult
from app.core.config import MAX_COMMITS_PER_RUN, TEAM_NAME, LEADER_NAME, PATCH_TRUNCATION_RATIO

logger = logging.getLogger(__name__)

# Priority tiers — lower number = higher priority
BUG_PRIORITY_TIERS = {
    "SYNTAX": 1,
    "IMPORT": 2,
    "TYPE": 3,
    "LOGIC": 4,
    "LINTING": 5,
}


class GitAgent:
    """
    Agent responsible for applying code changes to the local workspace
    and pushing them to the remote repository.
    """

    def __init__(self, commit_prefix: str = "[AI-AGENT] Fix:") -> None:
        self.commit_prefix = commit_prefix
        self.commit_count = 0
        self.branch_name = ""
        self.push_status = "pending"
        self.efficiency_penalty_risk = False
        self.commit_priority_delta = 0
        self._previous_bug_tiers: List[int] = []

    def generate_branch_name(self, team_name: str = TEAM_NAME, leader_name: str = LEADER_NAME) -> str:
        """
        Generate a deterministic branch name: TEAM_NAME_LEADER_NAME_AI_Fix
        """
        # Clean team and leader names: uppercase, spaces to underscores, remove special chars
        def clean(s: str) -> str:
            s = s.upper()
            s = re.sub(r"\s+", "_", s) # spaces to underscores
            s = re.sub(r"[^A-Z0-9_]", "", s) # remove special chars (keep underscores)
            s = re.sub(r"_+", "_", s) # deduplicate underscores
            return s.strip("_")

        t = clean(team_name)
        l = clean(leader_name)
        # Final join with single underscores
        parts = [p for p in [t, l, "AI", "Fix"] if p]
        self.branch_name = "_".join(parts)
        return self.branch_name

    def checkout_branch(self, workspace_path: str, branch_name: str) -> bool:
        """
        Create and checkout a new branch in the workspace.
        """
        try:
            subprocess.run(
                ["git", "checkout", "-b", branch_name],
                cwd=workspace_path,
                check=True,
                capture_output=True,
                text=True,
            )
            logger.info("Successfully checked out branch: %s", branch_name)
            return True
        except subprocess.CalledProcessError as e:
            logger.warning("Branch checkout failed (may already exist): %s", e.stderr)
            # Try switching to it if it exists
            try:
                subprocess.run(
                    ["git", "checkout", branch_name],
                    cwd=workspace_path,
                    check=True,
                    capture_output=True,
                    text=True,
                )
                return True
            except subprocess.CalledProcessError:
                return False
        except Exception as e:
            logger.error("Unexpected error during checkout: %s", e)
            return False

    def validate_branch_name(self, name: str) -> bool:
        """
        Validate branch name follows exact format: [A-Z0-9_]+_AI_Fix
        """
        pattern = r"^[A-Z0-9_]+_AI_Fix$"
        return bool(re.match(pattern, name))

    def apply_fix(self, fix_result: FixResult, workspace_path: str) -> bool:
        """
        Write the patched content to disk and commit it.
        """
        if not fix_result.success or not fix_result.patched_content:
            logger.warning("Refusing to apply unsuccessful or empty fix")
            return False

        if self.commit_count >= MAX_COMMITS_PER_RUN:
            logger.error("Max commit limit reached (%d), blocking further fixes", MAX_COMMITS_PER_RUN)
            self.efficiency_penalty_risk = True
            return False

        repo_relative_path = fix_result.bug_report.file_path
        abs_path = os.path.normpath(os.path.join(workspace_path, repo_relative_path))

        if not abs_path.startswith(os.path.abspath(workspace_path)):
            logger.error("Security violation: attempt to write outside workspace: %s", abs_path)
            return False

        try:
            # 1. Write the file
            with open(abs_path, "w", encoding="utf-8") as f:
                f.write(fix_result.patched_content)
            
            # 2. Stage and commit
            bug = fix_result.bug_report
            # Ensure prefix is present
            msg_content = f"{bug.bug_type}/{bug.sub_type} in {bug.file_path}"
            commit_msg = f"{self.commit_prefix} {msg_content}"

            # git add <file>
            subprocess.run(
                ["git", "add", repo_relative_path],
                cwd=workspace_path,
                check=True,
                capture_output=True,
                text=True
            )

            # Verify there are actual staged changes before committing
            diff_check = subprocess.run(
                ["git", "diff", "--cached", "--quiet"],
                cwd=workspace_path,
                capture_output=True,
                text=True,
            )
            if diff_check.returncode == 0:
                # returncode 0 = NO differences → nothing to commit
                logger.warning(
                    "Patch for %s produced no diff (identical content), skipping commit",
                    repo_relative_path,
                )
                return False

            # git commit -m <msg>
            subprocess.run(
                ["git", "commit", "-m", commit_msg],
                cwd=workspace_path,
                check=True,
                capture_output=True,
                text=True
            )

            self.commit_count += 1
            logger.info("Successfully committed fix: %s", commit_msg)
            return True

        except Exception as e:
            logger.error("Failed to apply/commit fix for %s: %s", repo_relative_path, e)
            return False

    def push(self, workspace_path: str, branch: str = "") -> str:
        """
        Push local commits to the remote repository with safety checks and retry.
        Returns push_status.
        """
        # If no branch provided, use generated branch or default to a safe naming scheme
        if not branch:
            branch = self.branch_name or self.generate_branch_name()

        # VALIDATION GATE
        if branch.lower() == "main" or branch.lower() == "master":
            logger.error("SAFETY VIOLATION: Refusing to push to %s", branch)
            self.push_status = "rejected_main"
            return self.push_status

        if not self.validate_branch_name(branch):
            logger.error("VALIDATION FAILED: Invalid branch naming convention: %s", branch)
            self.push_status = "invalid_branch_name"
            return self.push_status

        # PUSH WITH CONFLICT-SAFE RETRY
        attempts = 0
        max_attempts = 2
        
        while attempts < max_attempts:
            attempts += 1
            try:
                subprocess.run(
                    ["git", "push", "origin", branch],
                    cwd=workspace_path,
                    check=True,
                    capture_output=True,
                    text=True
                )
                logger.info("Successfully pushed changes to branch: %s", branch)
                self.push_status = "success"
                return self.push_status
            except subprocess.CalledProcessError as e:
                logger.error("Push attempt %d failed: %s", attempts, e.stderr)
                if attempts < max_attempts:
                    # Fetch + rebase before retrying
                    logger.info("Attempting fetch + rebase before retry...")
                    try:
                        subprocess.run(
                            ["git", "fetch", "origin", branch],
                            cwd=workspace_path, check=True,
                            capture_output=True, text=True
                        )
                        subprocess.run(
                            ["git", "rebase", f"origin/{branch}"],
                            cwd=workspace_path, check=True,
                            capture_output=True, text=True
                        )
                    except subprocess.CalledProcessError as rebase_err:
                        logger.error("Fetch/rebase failed: %s", rebase_err.stderr)
                        self.push_status = "conflict_unresolved"
                        return self.push_status
                    time.sleep(2)
                else:
                    self.push_status = "conflict_unresolved"
        
        return self.push_status

    def get_last_commit_sha(self, workspace_path: str) -> str:
        """Get the SHA of the HEAD commit."""
        try:
            res = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=workspace_path,
                check=True,
                capture_output=True,
                text=True
            )
            return res.stdout.strip()
        except Exception:
            return ""

    @property
    def state(self) -> Dict[str, Any]:
        """Expose internal state for the orchestrator/dashboard."""
        remaining = max(0, MAX_COMMITS_PER_RUN - self.commit_count)
        return {
            "branch_name": self.branch_name,
            "commit_count": self.commit_count,
            "push_status": self.push_status,
            "efficiency_penalty_risk": self.efficiency_penalty_risk,
            "remaining_commit_budget": remaining,
            "commit_budget_risk": remaining <= 3,
            "commit_priority_delta": self.commit_priority_delta,
        }

    # -------------------------------------------------------------------
    # Patch Truncation Guard
    # -------------------------------------------------------------------
    @staticmethod
    def validate_patch_size(
        original: str,
        patched: str,
        ratio: float = PATCH_TRUNCATION_RATIO,
    ) -> bool:
        """
        Reject patches significantly smaller than the original.
        Returns True if patch is acceptable, False if truncated.
        O(1) comparison.
        """
        if not original:
            return True  # Nothing to compare against
        return len(patched) / len(original) >= ratio

    # -------------------------------------------------------------------
    # Commit Priority Gating
    # -------------------------------------------------------------------
    def compute_priority_delta(
        self,
        current_bug_types: List[str],
        previous_bug_types: List[str],
    ) -> int:
        """
        Compute improvement in highest-priority bug tier.
        Positive delta = improvement (higher-priority bugs resolved).
        """
        def best_tier(types: List[str]) -> int:
            tiers = [BUG_PRIORITY_TIERS.get(t, 99) for t in types]
            return min(tiers) if tiers else 99

        prev_best = best_tier(previous_bug_types)
        curr_best = best_tier(current_bug_types)

        # Delta > 0 means the worst remaining tier is now lower-priority
        self.commit_priority_delta = curr_best - prev_best
        return self.commit_priority_delta

    def should_commit_by_priority(
        self,
        current_bug_types: List[str],
        previous_bug_types: List[str],
    ) -> bool:
        """
        Allow commit when highest-priority tier improved,
        even if lower-priority failures remain.
        """
        delta = self.compute_priority_delta(current_bug_types, previous_bug_types)
        return delta > 0
