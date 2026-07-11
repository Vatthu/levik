"""Unit tests for compute_dependency_graph() semantic dependency detection.

Tests validate:
- Python import detection (import X, from X import Y)
- Go import path detection (import "pkg/path")
- TypeScript/JavaScript import detection (import/require)
- Cross-task dependency identification when one task modifies interfaces another imports
- Edge cases: no overlaps, self-dependencies excluded, unknown file types

Validates: Requirements 36.1, 36.2, 36.3, 36.4, 37.1, 37.2, 37.3, 37.4
"""

from __future__ import annotations

import pytest

from vikram_orchestrator.conflict_detector import (
    ConflictDetector,
    SemanticDependency,
    extract_exported_interfaces,
    extract_imports,
    _module_matches_import,
)


# ---------------------------------------------------------------------------
# Helper to make assertions easier
# ---------------------------------------------------------------------------


def dep_tuple(dep: SemanticDependency) -> tuple[str, str, str, str]:
    """Extract key fields for easier assertion."""
    return (dep.source_task_id, dep.source_file, dep.consumer_task_id, dep.consumer_file)


# ---------------------------------------------------------------------------
# Tests: extract_imports
# ---------------------------------------------------------------------------


class TestExtractImports:
    """Test import extraction for various languages."""

    def test_python_import_statement(self) -> None:
        """Detects 'import module' statements."""
        content = "import os\nimport sys\n"
        result = extract_imports("main.py", content)
        assert "os" in result
        assert "sys" in result

    def test_python_from_import_statement(self) -> None:
        """Detects 'from module import name' statements."""
        content = "from pathlib import Path\nfrom os.path import join\n"
        result = extract_imports("main.py", content)
        assert "pathlib" in result
        assert "os.path" in result

    def test_python_dotted_import(self) -> None:
        """Detects dotted module imports."""
        content = "from vikram_orchestrator.conflict_detector import ConflictDetector\n"
        result = extract_imports("app.py", content)
        assert "vikram_orchestrator.conflict_detector" in result

    def test_go_single_import(self) -> None:
        """Detects Go single-line imports."""
        content = 'import "fmt"\n'
        result = extract_imports("main.go", content)
        assert "fmt" in result

    def test_go_grouped_imports(self) -> None:
        """Detects Go grouped imports."""
        content = '''import (
    "fmt"
    "net/http"
    "github.com/user/pkg/auth"
)
'''
        result = extract_imports("server.go", content)
        assert "fmt" in result
        assert "net/http" in result
        assert "github.com/user/pkg/auth" in result

    def test_typescript_import_from(self) -> None:
        """Detects TypeScript 'import ... from' statements."""
        content = '''import { Component } from "./component"
import React from "react"
'''
        result = extract_imports("app.ts", content)
        assert "./component" in result
        assert "react" in result

    def test_typescript_require(self) -> None:
        """Detects CommonJS require() statements."""
        content = '''const fs = require("fs")
const path = require("path")
'''
        result = extract_imports("util.js", content)
        assert "fs" in result
        assert "path" in result

    def test_typescript_import_side_effect(self) -> None:
        """Detects side-effect imports."""
        content = '''import "./polyfill"
'''
        result = extract_imports("index.ts", content)
        assert "./polyfill" in result

    def test_unknown_extension_returns_empty(self) -> None:
        """Unknown file types return no imports."""
        content = "some random content"
        result = extract_imports("data.csv", content)
        assert result == []

    def test_empty_content_returns_empty(self) -> None:
        """Empty file returns no imports."""
        result = extract_imports("main.py", "")
        assert result == []


# ---------------------------------------------------------------------------
# Tests: extract_exported_interfaces
# ---------------------------------------------------------------------------


class TestExtractExportedInterfaces:
    """Test exported interface extraction for various languages."""

    def test_python_function_defs(self) -> None:
        """Detects Python function definitions."""
        content = """
def compute_something():
    pass

def _private_helper():
    pass

async def fetch_data():
    pass
"""
        from vikram_orchestrator.conflict_detector import extract_exported_interfaces
        result = extract_exported_interfaces("utils.py", content)
        assert "compute_something" in result
        assert "fetch_data" in result
        # Private names excluded
        assert "_private_helper" not in result

    def test_python_class_defs(self) -> None:
        """Detects Python class definitions."""
        content = """
class MyService:
    pass

class _InternalHelper:
    pass
"""
        from vikram_orchestrator.conflict_detector import extract_exported_interfaces
        result = extract_exported_interfaces("service.py", content)
        assert "MyService" in result
        assert "_InternalHelper" not in result

    def test_go_exported_functions(self) -> None:
        """Detects Go exported functions (capitalized)."""
        content = """
func HandleRequest(w http.ResponseWriter, r *http.Request) {}
func (s *Server) Start() {}
func helper() {}
"""
        from vikram_orchestrator.conflict_detector import extract_exported_interfaces
        result = extract_exported_interfaces("handler.go", content)
        assert "HandleRequest" in result
        assert "Start" in result
        # Unexported function not included
        assert "helper" not in result

    def test_go_exported_types(self) -> None:
        """Detects Go exported types."""
        content = """
type Server struct {}
type Config struct {}
type internal struct {}
"""
        from vikram_orchestrator.conflict_detector import extract_exported_interfaces
        result = extract_exported_interfaces("types.go", content)
        assert "Server" in result
        assert "Config" in result
        assert "internal" not in result

    def test_typescript_exports(self) -> None:
        """Detects TypeScript exported declarations."""
        content = """
export function createApp() {}
export class UserService {}
export const API_URL = "http://..."
export type Config = {}
export interface IDatabase {}
"""
        from vikram_orchestrator.conflict_detector import extract_exported_interfaces
        result = extract_exported_interfaces("app.ts", content)
        assert "createApp" in result
        assert "UserService" in result
        assert "API_URL" in result
        assert "Config" in result
        assert "IDatabase" in result

    def test_unknown_extension_returns_empty(self) -> None:
        """Unknown file types return no exports."""
        from vikram_orchestrator.conflict_detector import extract_exported_interfaces
        result = extract_exported_interfaces("data.json", '{"key": "value"}')
        assert result == []


# ---------------------------------------------------------------------------
# Tests: compute_dependency_graph - Python
# ---------------------------------------------------------------------------


class TestComputeDependencyGraphPython:
    """Test dependency graph construction with Python files."""

    def test_basic_python_dependency(self) -> None:
        """Task A modifies a module that Task B imports."""
        detector = ConflictDetector()

        file_contents = {
            "src/auth/service.py": (
                "class AuthService:\n"
                "    def authenticate(self, token: str) -> bool:\n"
                "        return True\n"
            ),
            "src/api/handler.py": (
                "from auth.service import AuthService\n\n"
                "def handle_request():\n"
                "    svc = AuthService()\n"
            ),
        }

        task_targets = {
            "task-auth": ["src/auth/service.py"],
            "task-api": ["src/api/handler.py"],
        }

        deps = detector.compute_dependency_graph(task_targets, file_contents)

        # task-api depends on task-auth (imports from auth.service)
        assert len(deps) > 0
        assert any(
            d.source_task_id == "task-auth"
            and d.consumer_task_id == "task-api"
            and d.modified_interface == "AuthService"
            for d in deps
        )

    def test_no_dependency_when_no_imports(self) -> None:
        """Tasks with no cross-imports have no dependencies."""
        detector = ConflictDetector()

        file_contents = {
            "src/module_a.py": "def func_a():\n    pass\n",
            "src/module_b.py": "def func_b():\n    pass\n",
        }

        task_targets = {
            "task-a": ["src/module_a.py"],
            "task-b": ["src/module_b.py"],
        }

        deps = detector.compute_dependency_graph(task_targets, file_contents)
        assert deps == []

    def test_self_dependencies_excluded(self) -> None:
        """Dependencies within the same task are not reported."""
        detector = ConflictDetector()

        file_contents = {
            "src/utils.py": "def helper():\n    pass\n",
            "src/main.py": "from utils import helper\ndef run():\n    helper()\n",
        }

        # Both files belong to the same task
        task_targets = {
            "task-single": ["src/utils.py", "src/main.py"],
        }

        deps = detector.compute_dependency_graph(task_targets, file_contents)
        assert deps == []

    def test_multiple_interfaces_in_source(self) -> None:
        """Multiple exported interfaces from a source file produce multiple dependencies."""
        detector = ConflictDetector()

        file_contents = {
            "src/models.py": (
                "class User:\n    pass\n\n"
                "class Session:\n    pass\n\n"
                "def create_user():\n    pass\n"
            ),
            "src/views.py": (
                "from models import User, Session\n\n"
                "def render():\n    pass\n"
            ),
        }

        task_targets = {
            "task-models": ["src/models.py"],
            "task-views": ["src/views.py"],
        }

        deps = detector.compute_dependency_graph(task_targets, file_contents)

        # Should find dependencies for User, Session, and create_user
        source_interfaces = {d.modified_interface for d in deps}
        assert "User" in source_interfaces
        assert "Session" in source_interfaces
        assert "create_user" in source_interfaces


# ---------------------------------------------------------------------------
# Tests: compute_dependency_graph - Go
# ---------------------------------------------------------------------------


class TestComputeDependencyGraphGo:
    """Test dependency graph construction with Go files."""

    def test_basic_go_dependency(self) -> None:
        """Task A modifies a Go package that Task B imports."""
        detector = ConflictDetector()

        file_contents = {
            "pkg/auth/handler.go": (
                'package auth\n\n'
                'func Authenticate(token string) bool {\n'
                '    return true\n'
                '}\n'
            ),
            "cmd/server/main.go": (
                'package main\n\n'
                'import (\n'
                '    "github.com/user/project/pkg/auth"\n'
                ')\n\n'
                'func main() {\n'
                '    auth.Authenticate("token")\n'
                '}\n'
            ),
        }

        task_targets = {
            "task-auth": ["pkg/auth/handler.go"],
            "task-server": ["cmd/server/main.go"],
        }

        deps = detector.compute_dependency_graph(task_targets, file_contents)

        assert len(deps) > 0
        assert any(
            d.source_task_id == "task-auth"
            and d.consumer_task_id == "task-server"
            and d.modified_interface == "Authenticate"
            for d in deps
        )


# ---------------------------------------------------------------------------
# Tests: compute_dependency_graph - TypeScript
# ---------------------------------------------------------------------------


class TestComputeDependencyGraphTypeScript:
    """Test dependency graph construction with TypeScript files."""

    def test_basic_typescript_dependency(self) -> None:
        """Task A modifies a TS module that Task B imports."""
        detector = ConflictDetector()

        file_contents = {
            "src/services/auth.ts": (
                'export class AuthService {\n'
                '    verify(token: string): boolean { return true; }\n'
                '}\n'
                'export function createAuthService() { return new AuthService(); }\n'
            ),
            "src/routes/login.ts": (
                'import { AuthService } from "../services/auth"\n\n'
                'export function loginHandler() {\n'
                '    const svc = new AuthService();\n'
                '}\n'
            ),
        }

        task_targets = {
            "task-auth-svc": ["src/services/auth.ts"],
            "task-login": ["src/routes/login.ts"],
        }

        deps = detector.compute_dependency_graph(task_targets, file_contents)

        assert len(deps) > 0
        assert any(
            d.source_task_id == "task-auth-svc"
            and d.consumer_task_id == "task-login"
            and d.modified_interface == "AuthService"
            for d in deps
        )


# ---------------------------------------------------------------------------
# Tests: compute_dependency_graph - Edge Cases
# ---------------------------------------------------------------------------


class TestComputeDependencyGraphEdgeCases:
    """Test edge cases and robustness of dependency graph computation."""

    def test_empty_task_targets(self) -> None:
        """No tasks means no dependencies."""
        detector = ConflictDetector()
        deps = detector.compute_dependency_graph({}, {})
        assert deps == []

    def test_single_task_no_dependencies(self) -> None:
        """A single task cannot have cross-task dependencies."""
        detector = ConflictDetector()
        file_contents = {
            "src/main.py": "import os\ndef main():\n    pass\n",
        }
        task_targets = {"task-only": ["src/main.py"]}
        deps = detector.compute_dependency_graph(task_targets, file_contents)
        assert deps == []

    def test_missing_file_content_gracefully_handled(self) -> None:
        """Files that cannot be read are skipped without error."""
        detector = ConflictDetector()

        # No file_contents provided and files don't exist on disk
        task_targets = {
            "task-a": ["/nonexistent/path/a.py"],
            "task-b": ["/nonexistent/path/b.py"],
        }

        # Should not raise, just return empty
        deps = detector.compute_dependency_graph(task_targets, {})
        assert deps == []

    def test_bidirectional_dependencies(self) -> None:
        """Two tasks that import from each other produce bidirectional dependencies."""
        detector = ConflictDetector()

        file_contents = {
            "src/module_a.py": (
                "from module_b import FuncB\n\n"
                "class ClassA:\n    pass\n"
            ),
            "src/module_b.py": (
                "from module_a import ClassA\n\n"
                "def FuncB():\n    pass\n"
            ),
        }

        task_targets = {
            "task-a": ["src/module_a.py"],
            "task-b": ["src/module_b.py"],
        }

        deps = detector.compute_dependency_graph(task_targets, file_contents)

        # task-b depends on task-a (imports ClassA)
        assert any(
            d.source_task_id == "task-a" and d.consumer_task_id == "task-b"
            for d in deps
        )
        # task-a depends on task-b (imports FuncB)
        assert any(
            d.source_task_id == "task-b" and d.consumer_task_id == "task-a"
            for d in deps
        )

    def test_multiple_tasks_complex_graph(self) -> None:
        """Three tasks forming a dependency chain: A -> B -> C."""
        detector = ConflictDetector()

        file_contents = {
            "src/base.py": "class BaseModel:\n    pass\n",
            "src/service.py": (
                "from base import BaseModel\n\n"
                "class Service:\n    pass\n"
            ),
            "src/handler.py": (
                "from service import Service\n\n"
                "def handle():\n    pass\n"
            ),
        }

        task_targets = {
            "task-base": ["src/base.py"],
            "task-service": ["src/service.py"],
            "task-handler": ["src/handler.py"],
        }

        deps = detector.compute_dependency_graph(task_targets, file_contents)

        # task-service depends on task-base
        assert any(
            d.source_task_id == "task-base" and d.consumer_task_id == "task-service"
            for d in deps
        )
        # task-handler depends on task-service
        assert any(
            d.source_task_id == "task-service" and d.consumer_task_id == "task-handler"
            for d in deps
        )

    def test_dependency_includes_import_path(self) -> None:
        """SemanticDependency captures the import path correctly."""
        detector = ConflictDetector()

        file_contents = {
            "src/utils.py": "def helper():\n    pass\n",
            "src/main.py": "from utils import helper\n\ndef run():\n    pass\n",
        }

        task_targets = {
            "task-utils": ["src/utils.py"],
            "task-main": ["src/main.py"],
        }

        deps = detector.compute_dependency_graph(task_targets, file_contents)

        assert len(deps) > 0
        dep = next(d for d in deps if d.consumer_task_id == "task-main")
        assert dep.import_path == "utils"
        assert dep.source_file == "src/utils.py"
        assert dep.consumer_file == "src/main.py"
        assert dep.modified_interface == "helper"
