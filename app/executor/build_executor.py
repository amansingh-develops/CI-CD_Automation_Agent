"""
Build Executor
==============
Runs build and test commands inside an ephemeral Docker sandbox container.
Returns structured execution results (logs, exit code, timing).

BOUNDARY RULES (CRITICAL):
    - Executor ONLY observes execution.
    - Executor NEVER fixes code.
    - Executor NEVER classifies bugs — that is the Parser's job.
    - Executor NEVER commits changes — that is the Git Agent's job.
    - Executor NEVER calls LLM.
    - Executor is a pure execution microscope.

DOCKER STRATEGY:
    - One container per iteration (ephemeral).
    - Workspace mounted as volume at /workspace.
    - No repo cloning inside container.
    - Container destroyed after execution.

PERFORMANCE NOTES:
    - Executor will run multiple times per agent session.
    - Keep startup lightweight: reuse workspace, avoid heavy preprocessing.
    - Dependency re-install caching is a future optimisation (not implemented yet).

DETERMINISM:
    Same workspace + same commands → same executor output.
"""
import time
import logging
from dataclasses import dataclass, field
from typing import Optional

import docker
from docker.errors import (
    ContainerError,
    ImageNotFound,
    APIError,
)

from app.core.config import DOCKER_IMAGE, DEFAULT_EXECUTION_TIMEOUT
from app.executor.project_detector import detect_project_type
from app.executor.command_resolver import resolve_commands, ResolvedCommands

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Execution Result (returned to Orchestrator / Parser)
# ---------------------------------------------------------------------------
@dataclass
class ExecutionResult:
    """
    Structured output from a single build/test execution.

    This becomes the primary input for the Failure Parser.

    Fields
    ------
    exit_code : int
        Process exit code (0 = success, non-zero = failure).
    full_log : str
        Full combined stdout + stderr from the container.
    log_excerpt : str
        Abbreviated log (first + last N lines) for dashboard preview.
    execution_time_seconds : float
        Wall clock duration of the execution.
    detected_project_type : str | None
        Project type detected from workspace signals.
    resolved_commands : ResolvedCommands | None
        The commands that were executed.
    environment_metadata : dict
        Runtime info: image used, container ID, timeout applied.
    error : str | None
        Error message if execution infrastructure failed (not build errors).
    """
    exit_code: int = -1
    full_log: str = ""
    log_excerpt: str = ""
    execution_time_seconds: float = 0.0
    detected_project_type: Optional[str] = None
    resolved_commands: Optional[ResolvedCommands] = None
    environment_metadata: dict = field(default_factory=dict)
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# Log Excerpt Helper
# ---------------------------------------------------------------------------
_EXCERPT_HEAD_LINES = 30
_EXCERPT_TAIL_LINES = 30


def create_log_excerpt(full_log: str,
                       head: int = _EXCERPT_HEAD_LINES,
                       tail: int = _EXCERPT_TAIL_LINES) -> str:
    """
    Create an abbreviated log showing the first and last N lines.

    Parameters
    ----------
    full_log : str
        The complete build/test output.
    head : int
        Number of lines to keep from the start.
    tail : int
        Number of lines to keep from the end.

    Returns
    -------
    str
        Abbreviated log string. If the log is short enough, returns it as-is.
    """
    lines = full_log.splitlines()
    total = len(lines)

    if total <= head + tail:
        return full_log

    head_lines = lines[:head]
    tail_lines = lines[-tail:]
    omitted = total - head - tail

    return "\n".join(
        head_lines
        + [f"\n... ({omitted} lines omitted) ...\n"]
        + tail_lines
    )


# ---------------------------------------------------------------------------
# Container Execution
# ---------------------------------------------------------------------------
# Docker resource limits (placeholders — tune per project type in future)
_MEMORY_LIMIT = "2g"
_CPU_COUNT = 2
_NETWORK_MODE = None  # None = default bridge; set "none" for isolation


def _build_shell_command(commands: ResolvedCommands) -> str:
    """
    Combine install + build + test into a single shell command string.

    Uses `set -e` so the shell exits on first failure and we capture
    exactly which stage broke.
    """
    parts = ["set -e"]
    parts.append(f"echo '>>> INSTALL' && {commands.install_command}")
    if commands.build_command:
        parts.append(f"echo '>>> BUILD' && {commands.build_command}")
    # Disable exit-on-error for the test command so we capture all failures
    parts.append(f"set +e && echo '>>> TEST' && {commands.test_command}")
    return " && ".join(parts)


def run_in_container(
    workspace_path: str,
    project_type: Optional[str] = None,
    timeout_seconds: int = DEFAULT_EXECUTION_TIMEOUT,
    docker_image: str = DOCKER_IMAGE,
    working_dir: str = "",
    custom_command: Optional[str] = None,
) -> ExecutionResult:
    """
    Execute build/test commands inside an ephemeral Docker container.

    Lifecycle:
        1. Detect project type (if not provided)
        2. Resolve commands for the detected type
        3. Create ephemeral container with workspace mounted
        4. Execute combined install → build → test command
        5. Capture logs, exit code, timing
        6. Destroy container
        7. Return ExecutionResult

    Parameters
    ----------
    workspace_path : str
        Absolute path to the cloned repository on host.
    project_type : str | None
        Override for project detection. If None, auto-detect.
    timeout_seconds : int
        Max execution time before container is killed.
    docker_image : str
        Docker image to use for the sandbox container.
    working_dir : str
        Sub-directory within the workspace to set as working directory.
        For monorepo/full-stack projects (e.g. "client", "server").
        Empty string means workspace root.
    custom_command : str | None
        If provided, use this command instead of auto-resolved commands.
        Used when replaying CI config steps directly.

    Returns
    -------
    ExecutionResult
        Always returned — never raises unhandled exceptions.
        On infrastructure failure, exit_code is -1 and error is set.
    """
    result = ExecutionResult()
    start_time = time.monotonic()



    # ------------------------------------------------------------------
    # 1. Detect project type
    # ------------------------------------------------------------------
    if project_type is None:
        project_type = detect_project_type(workspace_path)
    result.detected_project_type = project_type

    # ------------------------------------------------------------------
    # 2. Resolve commands
    # ------------------------------------------------------------------
    if custom_command:
        commands = ResolvedCommands(
            project_type=project_type or "custom",
            install_command="echo 'skipping install (custom command)'",
            test_command=custom_command,
        )
    else:
        commands = resolve_commands(project_type)
    result.resolved_commands = commands

    # ------------------------------------------------------------------
    # 3–6. Run in Docker container
    # ------------------------------------------------------------------
    container = None
    # Determine container working directory
    container_workdir = "/workspace"
    if working_dir:
        container_workdir = f"/workspace/{working_dir.strip('/')}"
    try:
        client = docker.from_env()

        shell_cmd = _build_shell_command(commands)

        logger.info(
            "Starting container | image=%s | project=%s | timeout=%ds | workdir=%s",
            docker_image, project_type, timeout_seconds, container_workdir,
        )

        container = client.containers.run(
            image=docker_image,
            command=["bash", "-c", shell_cmd],
            volumes={
                workspace_path: {"bind": "/workspace", "mode": "rw"},
            },
            environment={"CI": "true"},
            working_dir=container_workdir,
            mem_limit=_MEMORY_LIMIT,
            nano_cpus=_CPU_COUNT * 1_000_000_000,
            network_mode=_NETWORK_MODE,
            name=f"rift-sandbox-{int(time.time())}",
            labels={"project": "rift-healer", "role": "sandbox"},
            detach=True,
            stdout=True,
            stderr=True,
        )

        # Wait for container to finish (with timeout)
        wait_result = container.wait(timeout=timeout_seconds)
        result.exit_code = wait_result.get("StatusCode", -1)

        # Capture logs
        log_bytes = container.logs(stdout=True, stderr=True)
        result.full_log = log_bytes.decode("utf-8", errors="replace")

        result.environment_metadata = {
            "image": docker_image,
            "container_id": container.short_id,
            "timeout_applied": timeout_seconds,
            "memory_limit": _MEMORY_LIMIT,
            "cpu_count": _CPU_COUNT,
        }

    except ImageNotFound:
        result.error = f"Docker image '{docker_image}' not found. Run build_sandbox.sh first."
        result.exit_code = -1
        logger.error(result.error)

    except ContainerError as e:
        result.error = f"Container execution error: {e}"
        result.exit_code = e.exit_status if hasattr(e, "exit_status") else -1
        result.full_log = str(e)
        logger.error(result.error)

    except APIError as e:
        result.error = f"Docker API error: {e}"
        result.exit_code = -1
        logger.error(result.error)

    except Exception as e:
        # Catch-all: orchestrator must always receive a result
        result.error = f"Unexpected executor error: {type(e).__name__}: {e}"
        result.exit_code = -1
        logger.exception(result.error)

    finally:
        # Always destroy the container
        if container is not None:
            try:
                container.remove(force=True)
                logger.info("Container %s destroyed", container.short_id)
            except Exception:
                logger.warning("Failed to remove container", exc_info=True)

    # ------------------------------------------------------------------
    # 7. Finalize result
    # ------------------------------------------------------------------
    result.execution_time_seconds = round(time.monotonic() - start_time, 3)
    result.log_excerpt = create_log_excerpt(result.full_log)

    logger.info(
        "Execution complete | exit=%d | time=%.2fs | project=%s",
        result.exit_code, result.execution_time_seconds, project_type,
    )

    return result


def run_ci_stages(
    workspace_path: str,
    stages: list[tuple[str, str]],
    project_type: Optional[str] = None,
    timeout_seconds: int = DEFAULT_EXECUTION_TIMEOUT,
    docker_image: str = DOCKER_IMAGE,
) -> ExecutionResult:
    """
    Execute multiple CI stages sequentially and combine their results.

    Each stage runs as a separate ``run_in_container`` call with a
    custom command. All logs are concatenated with stage headers.
    The combined exit code is non-zero if ANY stage failed.

    Parameters
    ----------
    workspace_path : str
        Absolute path to the cloned repository on host.
    stages : list[tuple[str, str]]
        List of (label, shell_command) pairs from ``resolve_from_ci_config``.
    project_type : str | None
        Detected project type.
    timeout_seconds : int
        Max execution time per stage.
    docker_image : str
        Docker image for sandbox containers.

    Returns
    -------
    ExecutionResult
        Combined result from all stages.
    """
    combined_result = ExecutionResult()
    combined_result.detected_project_type = project_type
    all_logs: list[str] = []
    total_time = 0.0
    worst_exit = 0
    start_time = time.monotonic()

    logger.info(
        "[CI_STAGES] Running %d stages for %s project",
        len(stages), project_type or "unknown",
    )

    for i, (label, command) in enumerate(stages, 1):
        logger.info("[CI_STAGES] Stage %d/%d: %s", i, len(stages), label)

        # Per-stage timeout: divide remaining time, minimum 30s
        elapsed_total = time.monotonic() - start_time
        remaining = max(30, timeout_seconds - elapsed_total)

        stage_result = run_in_container(
            workspace_path=workspace_path,
            project_type=project_type,
            timeout_seconds=int(remaining),
            docker_image=docker_image,
            custom_command=command,
        )

        # Collect logs with stage header
        stage_header = f"\n{'='*60}\n>>> STAGE: {label}\n>>> COMMAND: {command}\n{'='*60}\n"
        all_logs.append(stage_header)
        all_logs.append(stage_result.full_log or "")

        # Track total time
        total_time += stage_result.execution_time_seconds

        # Track worst exit code
        if stage_result.exit_code != 0:
            worst_exit = stage_result.exit_code
            all_logs.append(f"\n>>> STAGE {label}: FAILED (exit {stage_result.exit_code})\n")
        else:
            all_logs.append(f"\n>>> STAGE {label}: PASSED\n")

        # If there was an infrastructure error, note it but continue
        if stage_result.error:
            all_logs.append(f">>> STAGE ERROR: {stage_result.error}\n")
            if not combined_result.error:
                combined_result.error = stage_result.error

        # Preserve the last resolved commands
        if stage_result.resolved_commands:
            combined_result.resolved_commands = stage_result.resolved_commands

    # Combine all logs
    combined_result.full_log = "\n".join(all_logs)
    combined_result.log_excerpt = create_log_excerpt(combined_result.full_log)
    combined_result.exit_code = worst_exit
    combined_result.execution_time_seconds = round(total_time, 3)

    logger.info(
        "[CI_STAGES] All stages complete | exit=%d | time=%.2fs | stages=%d",
        combined_result.exit_code, combined_result.execution_time_seconds, len(stages),
    )

    return combined_result
