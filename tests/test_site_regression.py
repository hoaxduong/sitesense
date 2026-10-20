"""Import hygiene of the site recommendation package (TC-129 of the test plan)."""

import ast
import os
import subprocess
import sys
from pathlib import Path

import pytest

PACKAGE_DIR = Path(__file__).resolve().parents[1] / "src" / "sitesense" / "site_recommendation"
PURE_MODULES = ("models", "metrics", "estimator", "scoring", "explain")
ALL_MODULES = (*PURE_MODULES, "data", "views")
FORBIDDEN_IN_PURE = {"streamlit", "psycopg"}

# Runs in a fresh interpreter: sockets are blocked, no thread may be started by the import.
IMPORT_PROBE = """
import importlib, socket, sys, threading

def blocked(*args, **kwargs):
    raise AssertionError("network access at import time")

socket.socket.connect = blocked
socket.create_connection = blocked
name = sys.argv[1]
before = threading.active_count()
importlib.import_module(name)
assert threading.active_count() == before, "import started a thread"
if sys.argv[2] == "pure":
    leaked = sorted({"streamlit", "psycopg"} & set(sys.modules))
    assert not leaked, f"pure module pulled in {leaked}"
print("ok")
"""


def run_probe(
    module: str, kind: str, extra_env: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if k not in {"DATABASE_URL"}}
    env.update(extra_env)
    return subprocess.run(
        [sys.executable, "-c", IMPORT_PROBE, f"sitesense.site_recommendation.{module}", kind],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


@pytest.mark.parametrize("module", ALL_MODULES)
def test_tc129_modules_import_without_database_or_network(module: str) -> None:
    """TC-129: each module imports in a fresh interpreter with no DB URL and sockets blocked."""
    kind = "pure" if module in PURE_MODULES else "impure"
    result = run_probe(module, kind, {"SITESENSE_AI_API_KEY": "", "SITESENSE_AI_BASE_URL": ""})
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"


def test_tc129_importing_every_module_has_no_cycle() -> None:
    """TC-129: importing all modules in one interpreter, in reverse order, succeeds."""
    code = "import importlib\n" + "".join(
        f"importlib.import_module('sitesense.site_recommendation.{name}')\n"
        for name in reversed(ALL_MODULES)
    )
    env = {k: v for k, v in os.environ.items() if k != "DATABASE_URL"}
    result = subprocess.run(
        [sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, result.stderr


def imported_roots(path: Path) -> set[str]:
    roots: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            roots.add(node.module.split(".")[0])
    return roots


@pytest.mark.parametrize("module", PURE_MODULES)
def test_tc129_pure_modules_do_not_import_streamlit_or_psycopg(module: str) -> None:
    """TC-129: pure modules never import streamlit or psycopg (checked with ast)."""
    assert not imported_roots(PACKAGE_DIR / f"{module}.py") & FORBIDDEN_IN_PURE


@pytest.mark.parametrize("module", PURE_MODULES)
def test_tc129_pure_modules_do_not_import_the_ui_or_data_layers(module: str) -> None:
    """TC-129: pure modules never import data or views, which keeps the graph acyclic."""
    text = (PACKAGE_DIR / f"{module}.py").read_text(encoding="utf-8")
    tree = ast.parse(text)
    targets = {
        node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module
    }
    assert "sitesense.site_recommendation.data" not in targets
    assert "sitesense.site_recommendation.views" not in targets
    assert "sitesense.database" not in targets
