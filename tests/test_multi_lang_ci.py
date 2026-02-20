"""
Unit Tests — Multi-Language CI Support
======================================
Tests for ci_config_reader, enhanced project_detector, and merge_conflict_detector.
"""
import pytest
import os
import tempfile
import yaml
from app.parser.ci_config_reader import (
    discover_ci_configs,
    parse_ci_config,
    read_ci_configs,
)
from app.executor.project_detector import detect_multi_project
from app.parser.merge_conflict_detector import detect_merge_conflicts, has_merge_conflicts

# ===========================================================================
# 1. CI Config Reader Tests
# ===========================================================================
class TestCIConfigReader:

    def test_discover_github_actions(self):
        with tempfile.TemporaryDirectory() as tmp:
            wf_dir = os.path.join(tmp, ".github", "workflows")
            os.makedirs(wf_dir)
            with open(os.path.join(wf_dir, "ci.yml"), "w") as f:
                f.write("name: CI")
            
            configs = discover_ci_configs(tmp)
            assert len(configs) == 1
            assert configs[0] == (".github/workflows/ci.yml", "github_actions")

    def test_parse_github_actions_steps(self):
        content = """
name: Build
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - name: Install
        run: npm install
      - name: Test
        run: npm test
        working-directory: ./client
"""
        with tempfile.TemporaryDirectory() as tmp:
            config_path = ".github/workflows/test.yml"
            config = parse_ci_config(tmp, config_path, "github_actions") # This will fail to read file but we can mock
            # Better to write the file first
            full_path = os.path.join(tmp, config_path)
            os.makedirs(os.path.dirname(full_path))
            with open(full_path, "w") as f:
                f.write(content)
            
            config = parse_ci_config(tmp, config_path, "github_actions")
            assert config.platform == "github_actions"
            assert len(config.jobs) == 1
            job = config.jobs[0]
            assert job.name == "test"
            assert len(job.steps) == 2
            assert job.steps[0].run_command == "npm install"
            assert job.steps[1].run_command == "npm test"
            assert job.steps[1].working_directory == "./client"

    def test_parse_makefile(self):
        content = """
build:
\tgo build -o app
\techo "built"

test:
\tgo test ./...
"""
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "Makefile"), "w") as f:
                f.write(content)
            
            config = parse_ci_config(tmp, "Makefile", "makefile")
            assert len(config.jobs) == 2
            test_job = next(j for j in config.jobs if j.name == "test")
            assert "go test ./..." in test_job.steps[0].run_command

# ===========================================================================
# 2. Multi-Project Detector Tests
# ===========================================================================
class TestMultiProjectDetector:

    def test_detect_full_stack_repo(self):
        with tempfile.TemporaryDirectory() as tmp:
            # Root: Dockerfile
            with open(os.path.join(tmp, "Dockerfile"), "w") as f:
                f.write("FROM alpine")
            
            # Client: Node
            client_dir = os.path.join(tmp, "client")
            os.makedirs(client_dir)
            with open(os.path.join(client_dir, "package.json"), "w") as f:
                f.write("{}")
            
            # Server: Python
            server_dir = os.path.join(tmp, "server")
            os.makedirs(server_dir)
            with open(os.path.join(server_dir, "requirements.txt"), "w") as f:
                f.write("pytest")
            
            contexts = detect_multi_project(tmp)
            assert len(contexts) == 3
            
            paths = [c.path for c in contexts]
            assert "." in paths
            assert "client" in paths
            assert "server" in paths
            
            # Verify types
            node_ctx = next(c for c in contexts if c.path == "client")
            assert node_ctx.project_type == "node"
            
            py_ctx = next(c for c in contexts if c.path == "server")
            assert py_ctx.project_type == "python"

# ===========================================================================
# 3. Merge Conflict Detector Tests
# ===========================================================================
class TestMergeConflictDetector:

    def test_detect_conflicts(self):
        content = """
def hello():
<<<<<<< HEAD
    print("hello world")
=======
    print("hi universe")
>>>>>>> feature-branch
"""
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "app.py"), "w") as f:
                f.write(content)
            
            conflicts = detect_merge_conflicts(tmp)
            assert len(conflicts) == 1
            c = conflicts[0]
            assert c.file_path == "app.py"
            assert c.line_number == 3
            assert c.ours_branch == "HEAD"
            assert c.theirs_branch == "feature-branch"
            assert has_merge_conflicts(tmp) is True

    def test_no_conflicts(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "clean.py"), "w") as f:
                f.write("print('ok')")
            
            assert has_merge_conflicts(tmp) is False


# ===========================================================================
# 4. Dynamic Docker Image Resolution Tests
# ===========================================================================
class TestDynamicDockerImages:

    def test_python_image(self):
        from app.executor.project_detector import resolve_docker_image
        img = resolve_docker_image("python")
        assert "python" in img

    def test_node_image(self):
        from app.executor.project_detector import resolve_docker_image
        img = resolve_docker_image("node")
        assert "node" in img

    def test_java_image(self):
        from app.executor.project_detector import resolve_docker_image
        img = resolve_docker_image("java")
        assert "maven" in img or "java" in img

    def test_go_image(self):
        from app.executor.project_detector import resolve_docker_image
        img = resolve_docker_image("go")
        assert "golang" in img

    def test_rust_image(self):
        from app.executor.project_detector import resolve_docker_image
        img = resolve_docker_image("rust")
        assert "rust" in img

    def test_unknown_falls_back(self):
        from app.executor.project_detector import resolve_docker_image
        from app.core.config import DOCKER_IMAGE
        assert resolve_docker_image("unknown_lang") == DOCKER_IMAGE

    def test_none_falls_back(self):
        from app.executor.project_detector import resolve_docker_image
        from app.core.config import DOCKER_IMAGE
        assert resolve_docker_image(None) == DOCKER_IMAGE

