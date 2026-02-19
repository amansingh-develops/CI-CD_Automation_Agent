"""
Unit Tests — Build Executor (Basic)
====================================
Tests for project detection, command resolution, log excerpting,
and executor structure — all with mocked Docker.

No real Docker daemon is required to run these tests.
"""
import os
import tempfile
import pytest
from unittest.mock import patch, MagicMock

from app.executor.project_detector import (
    detect_project_type,
    detect_all_signals,
    SIGNAL_MAP,
)
from app.executor.command_resolver import (
    resolve_commands,
    get_supported_project_types,
    ResolvedCommands,
)
from app.executor.build_executor import (
    create_log_excerpt,
    run_in_container,
    ExecutionResult,
    _build_shell_command,
)


# ---------------------------------------------------------------------------
# 1. Project Detection
# ---------------------------------------------------------------------------
class TestProjectDetector:

    def test_detect_python_from_requirements(self, tmp_path):
        (tmp_path / "requirements.txt").write_text("pytest\n")
        assert detect_project_type(str(tmp_path)) == "python"

    def test_detect_python_from_pyproject(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("[project]\n")
        assert detect_project_type(str(tmp_path)) == "python"

    def test_detect_node_from_package_json(self, tmp_path):
        (tmp_path / "package.json").write_text("{}\n")
        assert detect_project_type(str(tmp_path)) == "node"

    def test_detect_java_from_pom(self, tmp_path):
        (tmp_path / "pom.xml").write_text("<project/>\n")
        assert detect_project_type(str(tmp_path)) == "java"

    def test_detect_go_from_go_mod(self, tmp_path):
        (tmp_path / "go.mod").write_text("module example\n")
        assert detect_project_type(str(tmp_path)) == "go"

    def test_detect_rust_from_cargo(self, tmp_path):
        (tmp_path / "Cargo.toml").write_text("[package]\n")
        assert detect_project_type(str(tmp_path)) == "rust"

    def test_detect_docker_from_dockerfile(self, tmp_path):
        (tmp_path / "Dockerfile").write_text("FROM ubuntu\n")
        assert detect_project_type(str(tmp_path)) == "docker_project"

    def test_returns_none_for_empty_dir(self, tmp_path):
        assert detect_project_type(str(tmp_path)) is None

    def test_returns_none_for_nonexistent_dir(self):
        assert detect_project_type("/nonexistent/path/xyz") is None

    def test_priority_node_over_docker(self, tmp_path):
        """package.json has higher priority than Dockerfile."""
        (tmp_path / "package.json").write_text("{}\n")
        (tmp_path / "Dockerfile").write_text("FROM node\n")
        assert detect_project_type(str(tmp_path)) == "node"

    def test_detect_all_signals(self, tmp_path):
        (tmp_path / "package.json").write_text("{}\n")
        (tmp_path / "Dockerfile").write_text("FROM node\n")
        signals = detect_all_signals(str(tmp_path))
        assert "package.json" in signals
        assert "Dockerfile" in signals

    def test_detect_all_signals_empty(self, tmp_path):
        assert detect_all_signals(str(tmp_path)) == []

    def test_deterministic_same_dir(self, tmp_path):
        """Same directory must always return the same result."""
        (tmp_path / "requirements.txt").write_text("flask\n")
        r1 = detect_project_type(str(tmp_path))
        r2 = detect_project_type(str(tmp_path))
        assert r1 == r2 == "python"


# ---------------------------------------------------------------------------
# 2. Command Resolver
# ---------------------------------------------------------------------------
class TestCommandResolver:

    def test_python_commands(self):
        cmds = resolve_commands("python")
        assert cmds.project_type == "python"
        assert "pip install" in cmds.install_command
        assert "pytest" in cmds.test_command

    def test_node_commands(self):
        cmds = resolve_commands("node")
        assert cmds.project_type == "node"
        assert "npm install" in cmds.install_command
        assert "npm test" in cmds.test_command
        assert cmds.build_command is not None

    def test_java_commands(self):
        cmds = resolve_commands("java")
        assert cmds.project_type == "java"
        assert "mvn" in cmds.test_command

    def test_go_commands(self):
        cmds = resolve_commands("go")
        assert cmds.project_type == "go"
        assert "go test" in cmds.test_command

    def test_rust_commands(self):
        cmds = resolve_commands("rust")
        assert cmds.project_type == "rust"
        assert "cargo test" in cmds.test_command

    def test_docker_project_commands(self):
        cmds = resolve_commands("docker_project")
        assert cmds.project_type == "docker_project"

    def test_unknown_type_returns_fallback(self):
        cmds = resolve_commands("brainfuck")
        assert cmds.project_type == "unknown"

    def test_none_type_returns_fallback(self):
        cmds = resolve_commands(None)
        assert cmds.project_type == "unknown"

    def test_resolved_commands_is_frozen(self):
        cmds = resolve_commands("python")
        with pytest.raises(AttributeError):
            cmds.test_command = "something else"

    def test_get_supported_types(self):
        types = get_supported_project_types()
        assert "python" in types
        assert "node" in types
        assert isinstance(types, list)

    def test_deterministic_resolution(self):
        """Same project_type must always resolve the same commands."""
        c1 = resolve_commands("python")
        c2 = resolve_commands("python")
        assert c1 == c2


# ---------------------------------------------------------------------------
# 3. Log Excerpt
# ---------------------------------------------------------------------------
class TestLogExcerpt:

    def test_short_log_returned_as_is(self):
        log = "line1\nline2\nline3"
        assert create_log_excerpt(log) == log

    def test_long_log_truncated(self):
        lines = [f"line {i}" for i in range(200)]
        full = "\n".join(lines)
        excerpt = create_log_excerpt(full, head=5, tail=5)
        assert "line 0" in excerpt
        assert "line 199" in excerpt
        assert "omitted" in excerpt

    def test_empty_log(self):
        assert create_log_excerpt("") == ""

    def test_exact_boundary(self):
        """Log with exactly head+tail lines should not be truncated."""
        lines = [f"line {i}" for i in range(10)]
        full = "\n".join(lines)
        excerpt = create_log_excerpt(full, head=5, tail=5)
        assert "omitted" not in excerpt


# ---------------------------------------------------------------------------
# 4. Shell Command Builder
# ---------------------------------------------------------------------------
class TestShellCommandBuilder:

    def test_python_command_has_set_e(self):
        cmds = resolve_commands("python")
        shell = _build_shell_command(cmds)
        assert "set -e" in shell

    def test_python_command_has_install_and_test(self):
        cmds = resolve_commands("python")
        shell = _build_shell_command(cmds)
        assert "pip install" in shell
        assert "pytest" in shell

    def test_node_command_includes_build_step(self):
        cmds = resolve_commands("node")
        shell = _build_shell_command(cmds)
        assert "npm run build" in shell

    def test_command_without_build_step(self):
        cmds = resolve_commands("python")
        shell = _build_shell_command(cmds)
        assert ">>> BUILD" not in shell


# ---------------------------------------------------------------------------
# 5. ExecutionResult Structure
# ---------------------------------------------------------------------------
class TestExecutionResult:

    def test_default_values(self):
        r = ExecutionResult()
        assert r.exit_code == -1
        assert r.full_log == ""
        assert r.log_excerpt == ""
        assert r.execution_time_seconds == 0.0
        assert r.detected_project_type is None
        assert r.resolved_commands is None
        assert r.environment_metadata == {}
        assert r.error is None

    def test_custom_values(self):
        r = ExecutionResult(
            exit_code=0,
            full_log="all tests passed",
            execution_time_seconds=12.5,
            detected_project_type="python",
        )
        assert r.exit_code == 0
        assert r.detected_project_type == "python"
        assert r.execution_time_seconds == 12.5


# ---------------------------------------------------------------------------
# 6. run_in_container (Mocked Docker)
# ---------------------------------------------------------------------------
class TestRunInContainerMocked:

    @patch("app.executor.build_executor.docker")
    def test_successful_execution(self, mock_docker):
        """Mock a full successful container run."""
        mock_container = MagicMock()
        mock_container.wait.return_value = {"StatusCode": 0}
        mock_container.logs.return_value = b"OK: all tests passed\n"
        mock_container.short_id = "abc123"

        mock_client = MagicMock()
        mock_client.containers.run.return_value = mock_container
        mock_docker.from_env.return_value = mock_client

        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "requirements.txt"), "w") as f:
                f.write("pytest\n")

            result = run_in_container(tmp)

        assert result.exit_code == 0
        assert "all tests passed" in result.full_log
        assert result.detected_project_type == "python"
        assert result.resolved_commands is not None
        assert result.execution_time_seconds >= 0
        assert result.error is None
        mock_container.remove.assert_called_once_with(force=True)

    @patch("app.executor.build_executor.docker")
    def test_failed_execution_returns_result(self, mock_docker):
        """Non-zero exit must still return a valid ExecutionResult."""
        mock_container = MagicMock()
        mock_container.wait.return_value = {"StatusCode": 1}
        mock_container.logs.return_value = b"FAILED: test_foo\n"
        mock_container.short_id = "def456"

        mock_client = MagicMock()
        mock_client.containers.run.return_value = mock_container
        mock_docker.from_env.return_value = mock_client

        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "requirements.txt"), "w") as f:
                f.write("pytest\n")
            result = run_in_container(tmp)

        assert result.exit_code == 1
        assert "FAILED" in result.full_log
        assert result.error is None  # build failure, not infra error

    @patch("app.executor.build_executor.docker")
    def test_image_not_found_returns_error(self, mock_docker):
        """Missing Docker image must return error, not raise."""
        mock_client = MagicMock()
        mock_client.containers.run.side_effect = Exception("Image not found")
        mock_docker.from_env.return_value = mock_client

        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "requirements.txt"), "w") as f:
                f.write("pytest\n")
            result = run_in_container(tmp)

        assert result.exit_code == -1
        assert result.error is not None

    @patch("app.executor.build_executor.docker")
    def test_project_type_override(self, mock_docker):
        """Manual project_type override must skip auto-detection."""
        mock_container = MagicMock()
        mock_container.wait.return_value = {"StatusCode": 0}
        mock_container.logs.return_value = b"OK\n"
        mock_container.short_id = "ghi789"

        mock_client = MagicMock()
        mock_client.containers.run.return_value = mock_container
        mock_docker.from_env.return_value = mock_client

        with tempfile.TemporaryDirectory() as tmp:
            result = run_in_container(tmp, project_type="node")

        assert result.detected_project_type == "node"
        assert result.resolved_commands.project_type == "node"

    @patch("app.executor.build_executor.docker")
    def test_container_always_cleaned_up(self, mock_docker):
        """Container must be removed even if log capture fails."""
        mock_container = MagicMock()
        mock_container.wait.return_value = {"StatusCode": 0}
        mock_container.logs.side_effect = Exception("log error")
        mock_container.short_id = "jkl012"

        mock_client = MagicMock()
        mock_client.containers.run.return_value = mock_container
        mock_docker.from_env.return_value = mock_client

        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "requirements.txt"), "w") as f:
                f.write("pytest\n")
            result = run_in_container(tmp)

        mock_container.remove.assert_called_once_with(force=True)
        assert result.error is not None

