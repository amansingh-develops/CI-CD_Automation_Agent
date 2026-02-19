"""
Command Resolver
================
Maps a detected project type to its standard build, install, and test commands.

Commands are minimal safe defaults.
Resolver never executes commands — it only returns string sequences.
Commands are passed to Build Executor for container execution.

Deterministic: same project_type → same commands, always.
"""
from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class ResolvedCommands:
    """
    Immutable container for resolved build/install/test commands.

    Fields
    ------
    install_command : str
        Dependency installation command (e.g. "pip install -r requirements.txt").
    test_command : str
        Test execution command (e.g. "pytest").
    build_command : str | None
        Optional compilation/build step (e.g. "mvn compile"). None if not needed.
    project_type : str
        The project type these commands were resolved for.
    """
    install_command: str
    test_command: str
    project_type: str
    build_command: Optional[str] = None


# ---------------------------------------------------------------------------
# Command mapping: project_type → ResolvedCommands
# ---------------------------------------------------------------------------
_COMMAND_MAP: dict[str, ResolvedCommands] = {
    "python": ResolvedCommands(
        install_command="pip install -r requirements.txt",
        test_command="pytest",
        project_type="python",
    ),
    "node": ResolvedCommands(
        install_command="npm install",
        test_command="npm test",
        build_command="npm run build",
        project_type="node",
    ),
    "java": ResolvedCommands(
        install_command="mvn dependency:resolve",
        test_command="mvn test",
        build_command="mvn compile",
        project_type="java",
    ),
    "go": ResolvedCommands(
        install_command="go mod download",
        test_command="go test ./...",
        build_command="go build ./...",
        project_type="go",
    ),
    "rust": ResolvedCommands(
        install_command="cargo fetch",
        test_command="cargo test",
        build_command="cargo build",
        project_type="rust",
    ),
    "docker_project": ResolvedCommands(
        install_command="echo 'no install step for docker_project'",
        test_command="docker compose up --build --abort-on-container-exit",
        build_command="docker build -t sandbox-build .",
        project_type="docker_project",
    ),
}

# Fallback when project type is unknown or None
_FALLBACK = ResolvedCommands(
    install_command="echo 'no install step — unknown project type'",
    test_command="echo 'no test command — unknown project type'",
    project_type="unknown",
)


def resolve_commands(project_type: Optional[str]) -> ResolvedCommands:
    """
    Look up build/install/test commands for the given project type.

    Parameters
    ----------
    project_type : str | None
        The detected project type from ProjectDetector.
        If None or unrecognised, returns fallback commands.

    Returns
    -------
    ResolvedCommands
        Frozen dataclass with install_command, test_command,
        optional build_command, and project_type.
    """
    if project_type is None:
        return _FALLBACK
    return _COMMAND_MAP.get(project_type, _FALLBACK)


def get_supported_project_types() -> list[str]:
    """Return all project types that have command mappings."""
    return sorted(_COMMAND_MAP.keys())
