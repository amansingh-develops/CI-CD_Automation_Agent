"""
CI Config Reader
================
Parses a repository's existing CI configuration files to extract
build/test jobs, steps, and commands.

Strategy:
    CI config is the source of truth — don't guess, read what the developers
    already defined. This is how professional DevOps tools work.

Supported CI Platforms:
    - GitHub Actions (.github/workflows/*.yml)
    - Makefile (Makefile, makefile)
    - Docker Compose (docker-compose.yml, docker-compose.yaml)
    - GitLab CI (.gitlab-ci.yml)

Fallback:
    If no CI config is found, returns None and the system falls back
    to project_detector + command_resolver heuristics.

Deterministic:
    Same repo → same parsed CI config, always.
    No LLM used.
"""
import os
import re
import logging
from dataclasses import dataclass, field
from typing import Optional

try:
    import yaml
    YAML_AVAILABLE = True
except ImportError:
    yaml = None  # type: ignore[assignment]
    YAML_AVAILABLE = False

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data Structures
# ---------------------------------------------------------------------------
@dataclass
class CIStep:
    """A single step in a CI job."""
    name: str
    run_command: str
    working_directory: str = ""


@dataclass
class CIJob:
    """A CI job containing ordered steps."""
    name: str
    steps: list[CIStep] = field(default_factory=list)
    working_directory: str = ""


@dataclass
class CIConfig:
    """
    Parsed CI configuration from a repository.

    Attributes
    ----------
    platform : str
        CI platform name ("github_actions", "makefile", "docker_compose", "gitlab_ci").
    config_path : str
        Path to the CI config file (relative to workspace).
    jobs : list[CIJob]
        Parsed jobs with their steps and commands.
    raw_content : str
        Raw text of the config file (for debugging).
    """
    platform: str
    config_path: str
    jobs: list[CIJob] = field(default_factory=list)
    raw_content: str = ""


# ---------------------------------------------------------------------------
# CI Config Discovery (priority order)
# ---------------------------------------------------------------------------
_CI_CONFIG_SIGNALS: list[tuple[str, str]] = [
    (".github/workflows", "github_actions"),
    (".gitlab-ci.yml",    "gitlab_ci"),
    ("Makefile",          "makefile"),
    ("makefile",          "makefile"),
    ("docker-compose.yml",  "docker_compose"),
    ("docker-compose.yaml", "docker_compose"),
]


def discover_ci_configs(workspace_path: str) -> list[tuple[str, str]]:
    """
    Discover all CI config files in the workspace.

    Returns
    -------
    list[tuple[str, str]]
        List of (relative_path, platform) tuples found.
    """
    found: list[tuple[str, str]] = []

    for signal, platform in _CI_CONFIG_SIGNALS:
        full_path = os.path.join(workspace_path, signal)

        if platform == "github_actions":
            # Scan for .yml files inside .github/workflows/
            if os.path.isdir(full_path):
                for fname in sorted(os.listdir(full_path)):
                    if fname.endswith((".yml", ".yaml")):
                        rel_path = f".github/workflows/{fname}"
                        found.append((rel_path, platform))
        else:
            if os.path.isfile(full_path):
                found.append((signal, platform))

    return found


# ---------------------------------------------------------------------------
# GitHub Actions Parser
# ---------------------------------------------------------------------------
def _parse_github_actions(content: str, config_path: str) -> CIConfig:
    """Parse a GitHub Actions workflow YAML file."""
    config = CIConfig(platform="github_actions", config_path=config_path, raw_content=content)

    if not YAML_AVAILABLE:
        logger.warning("PyYAML not installed — cannot parse GitHub Actions YAML")
        return config

    try:
        data = yaml.safe_load(content)
    except Exception as e:
        logger.warning("Failed to parse YAML %s: %s", config_path, e)
        return config

    if not isinstance(data, dict):
        return config

    jobs_data = data.get("jobs", {})
    if not isinstance(jobs_data, dict):
        return config

    for job_name, job_def in jobs_data.items():
        if not isinstance(job_def, dict):
            continue

        job = CIJob(name=job_name)

        # Job-level working directory
        defaults = job_def.get("defaults", {})
        if isinstance(defaults, dict):
            run_defaults = defaults.get("run", {})
            if isinstance(run_defaults, dict):
                job.working_directory = run_defaults.get("working-directory", "")

        # Parse steps
        steps = job_def.get("steps", [])
        if not isinstance(steps, list):
            continue

        for i, step_def in enumerate(steps):
            if not isinstance(step_def, dict):
                continue

            run_cmd = step_def.get("run")
            if not run_cmd:
                continue  # Skip action-only steps (uses:)

            step_name = step_def.get("name", f"step-{i}")
            working_dir = step_def.get("working-directory", "")

            job.steps.append(CIStep(
                name=step_name,
                run_command=str(run_cmd).strip(),
                working_directory=working_dir,
            ))

        if job.steps:
            config.jobs.append(job)

    return config


# ---------------------------------------------------------------------------
# Makefile Parser
# ---------------------------------------------------------------------------
_MAKEFILE_TARGET = re.compile(r'^([a-zA-Z_][\w-]*):\s*', re.MULTILINE)


def _parse_makefile(content: str, config_path: str) -> CIConfig:
    """Parse a Makefile to extract targets and their commands."""
    config = CIConfig(platform="makefile", config_path=config_path, raw_content=content)

    lines = content.split("\n")
    current_target: Optional[str] = None
    current_commands: list[str] = []

    for line in lines:
        target_match = _MAKEFILE_TARGET.match(line)
        if target_match:
            # Save previous target
            if current_target and current_commands:
                config.jobs.append(CIJob(
                    name=current_target,
                    steps=[CIStep(
                        name=current_target,
                        run_command=" && ".join(current_commands),
                    )],
                ))
            current_target = target_match.group(1)
            current_commands = []
        elif line.startswith("\t") and current_target:
            cmd = line.strip()
            if cmd and not cmd.startswith("#"):
                current_commands.append(cmd)

    # Save last target
    if current_target and current_commands:
        config.jobs.append(CIJob(
            name=current_target,
            steps=[CIStep(
                name=current_target,
                run_command=" && ".join(current_commands),
            )],
        ))

    return config


# ---------------------------------------------------------------------------
# Docker Compose Parser
# ---------------------------------------------------------------------------
def _parse_docker_compose(content: str, config_path: str) -> CIConfig:
    """Parse docker-compose.yml to extract service definitions as jobs."""
    config = CIConfig(platform="docker_compose", config_path=config_path, raw_content=content)

    if not YAML_AVAILABLE:
        logger.warning("PyYAML not installed — cannot parse docker-compose")
        return config

    try:
        data = yaml.safe_load(content)
    except Exception as e:
        logger.warning("Failed to parse YAML %s: %s", config_path, e)
        return config

    if not isinstance(data, dict):
        return config

    services = data.get("services", {})
    if not isinstance(services, dict):
        return config

    for svc_name, svc_def in services.items():
        if not isinstance(svc_def, dict):
            continue

        job = CIJob(name=svc_name)

        # Extract build context as working directory
        build = svc_def.get("build")
        if isinstance(build, str):
            job.working_directory = build
        elif isinstance(build, dict):
            job.working_directory = build.get("context", "")

        # Extract command
        command = svc_def.get("command")
        if command:
            cmd_str = command if isinstance(command, str) else " ".join(command)
            job.steps.append(CIStep(name=f"{svc_name}-run", run_command=cmd_str))

        if job.steps or job.working_directory:
            config.jobs.append(job)

    return config


# ---------------------------------------------------------------------------
# GitLab CI Parser
# ---------------------------------------------------------------------------
_GITLAB_RESERVED = {"stages", "variables", "default", "include", "image", "services", "before_script", "after_script", "cache"}


def _parse_gitlab_ci(content: str, config_path: str) -> CIConfig:
    """Parse .gitlab-ci.yml to extract jobs."""
    config = CIConfig(platform="gitlab_ci", config_path=config_path, raw_content=content)

    if not YAML_AVAILABLE:
        return config

    try:
        data = yaml.safe_load(content)
    except Exception:
        return config

    if not isinstance(data, dict):
        return config

    for key, value in data.items():
        if key.startswith(".") or key in _GITLAB_RESERVED:
            continue
        if not isinstance(value, dict):
            continue

        script = value.get("script", [])
        if not isinstance(script, list):
            continue

        job = CIJob(name=key)
        for i, cmd in enumerate(script):
            if isinstance(cmd, str):
                job.steps.append(CIStep(name=f"{key}-step-{i}", run_command=cmd.strip()))

        if job.steps:
            config.jobs.append(job)

    return config


# ---------------------------------------------------------------------------
# Parser Dispatcher
# ---------------------------------------------------------------------------
_PARSERS = {
    "github_actions": _parse_github_actions,
    "makefile":        _parse_makefile,
    "docker_compose":  _parse_docker_compose,
    "gitlab_ci":       _parse_gitlab_ci,
}


def parse_ci_config(workspace_path: str, config_path: str, platform: str) -> CIConfig:
    """
    Parse a single CI config file.

    Parameters
    ----------
    workspace_path : str
        Repository root directory.
    config_path : str
        Relative path to the CI config file.
    platform : str
        Platform identifier (github_actions, makefile, etc.).

    Returns
    -------
    CIConfig
        Parsed CI config. May have empty jobs if parsing fails.
    """
    full_path = os.path.join(workspace_path, config_path)

    try:
        with open(full_path, "r", encoding="utf-8") as f:
            content = f.read()
    except Exception as e:
        logger.warning("Could not read %s: %s", full_path, e)
        return CIConfig(platform=platform, config_path=config_path)

    parser = _PARSERS.get(platform)
    if parser is None:
        logger.warning("No parser for platform: %s", platform)
        return CIConfig(platform=platform, config_path=config_path, raw_content=content)

    return parser(content, config_path)


# ---------------------------------------------------------------------------
# Public API: Read All CI Configs
# ---------------------------------------------------------------------------
def read_ci_configs(workspace_path: str) -> list[CIConfig]:
    """
    Discover and parse all CI configs in a workspace.

    Returns
    -------
    list[CIConfig]
        All parsed CI configurations found. Empty list if none found.
    """
    discovered = discover_ci_configs(workspace_path)

    configs: list[CIConfig] = []
    for config_path, platform in discovered:
        config = parse_ci_config(workspace_path, config_path, platform)
        if config.jobs:
            configs.append(config)

    logger.info(
        "Found %d CI config(s) with %d total jobs",
        len(configs),
        sum(len(c.jobs) for c in configs),
    )
    return configs


def get_all_commands(configs: list[CIConfig]) -> list[tuple[str, str, str]]:
    """
    Flatten all CI configs into a list of (job_name, working_dir, command) tuples.

    Useful for the executor to replay CI steps.
    """
    commands: list[tuple[str, str, str]] = []
    for config in configs:
        for job in config.jobs:
            for step in job.steps:
                wd = step.working_directory or job.working_directory
                commands.append((job.name, wd, step.run_command))
    return commands
