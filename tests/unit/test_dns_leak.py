"""CRITICAL: Combined DNS leak static analysis for all data-path modules.

This test file statically verifies that tunnel.py, forwarder.py, and handler.py
never import or reference DNS resolution functions. This is the single most
important security property of the proxy relay.
"""
from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

import pytest

DANGEROUS_NAMES = {"getaddrinfo", "gethostbyname", "gethostbyname_ex", "getfqdn"}
DATA_PATH_MODULES = ("tunnel", "forwarder", "handler")


def _get_module_source(module_name: str) -> tuple[str, Path]:
    """Load source code for a proxy_relay module."""
    spec = importlib.util.find_spec(f"proxy_relay.{module_name}")
    assert spec is not None, f"proxy_relay.{module_name} module not found"
    assert spec.origin is not None, f"proxy_relay.{module_name} has no origin"
    path = Path(spec.origin)
    return path.read_text(), path


@pytest.mark.parametrize(
    "module_name",
    [
        pytest.param("tunnel", id="tunnel"),
        pytest.param("forwarder", id="forwarder"),
        pytest.param("handler", id="handler"),
    ],
)
def test_no_dns_resolution_in_data_path(module_name: str):
    """CRITICAL: Data-path modules must NEVER resolve DNS locally.

    Uses AST analysis to detect:
    - socket.getaddrinfo, socket.gethostbyname, etc. as attribute access
    - bare references to getaddrinfo, gethostbyname, etc. as name references
    - from socket import getaddrinfo, etc. as import statements
    """
    source, source_path = _get_module_source(module_name)
    tree = ast.parse(source)

    for node in ast.walk(tree):
        # Check attribute access: socket.getaddrinfo
        if isinstance(node, ast.Attribute) and node.attr in DANGEROUS_NAMES:
            raise AssertionError(
                f"proxy_relay/{module_name}.py uses {node.attr} "
                f"(line ~{getattr(node, 'lineno', '?')}) -- DNS leak risk!"
            )

        # Check bare name references: getaddrinfo()
        if isinstance(node, ast.Name) and node.id in DANGEROUS_NAMES:
            raise AssertionError(
                f"proxy_relay/{module_name}.py references {node.id} "
                f"(line ~{getattr(node, 'lineno', '?')}) -- DNS leak risk!"
            )

        # Check from-imports: from socket import getaddrinfo
        if isinstance(node, ast.ImportFrom) and node.module == "socket":
            imported_names = {alias.name for alias in (node.names or [])}
            dangerous_imports = imported_names & DANGEROUS_NAMES
            if dangerous_imports:
                raise AssertionError(
                    f"proxy_relay/{module_name}.py imports DNS functions "
                    f"from socket: {dangerous_imports} -- DNS leak risk!"
                )
