"""Shared fixtures for proxy-relay tests."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.fixture
def tmp_config_dir(tmp_path):
    """Provide a temporary directory for config files."""
    return tmp_path


@pytest.fixture
def minimal_toml(tmp_path):
    """Create a minimal valid TOML config file (per-profile schema).

    Contains [profiles.default] as required by the per-profile refactor.
    Used by test_config.py to test RelayConfig.load() with minimal input.
    """
    path = tmp_path / "config.toml"
    path.write_text(
        '[server]\n'
        'host = "127.0.0.1"\n'
        '\n'
        '[profiles.default]\n'
        'port = 8080\n'
    )
    return path


@pytest.fixture
def malformed_toml(tmp_path):
    """Create a malformed TOML config file."""
    path = tmp_path / "config.toml"
    path.write_text("this is [not valid toml ===\n")
    return path


@pytest.fixture
def mock_client_reader():
    """Create a mock asyncio StreamReader for the client side."""
    reader = AsyncMock(spec=asyncio.StreamReader)
    reader.read = AsyncMock(return_value=b"")
    reader.readline = AsyncMock(return_value=b"")
    reader.readuntil = AsyncMock(return_value=b"")
    reader.at_eof = MagicMock(return_value=False)
    return reader


@pytest.fixture
def mock_client_writer():
    """Create a mock asyncio StreamWriter for the client side."""
    writer = AsyncMock(spec=asyncio.StreamWriter)
    writer.write = MagicMock()
    writer.close = MagicMock()
    writer.wait_closed = AsyncMock()
    writer.drain = AsyncMock()
    writer.is_closing = MagicMock(return_value=False)
    return writer


@pytest.fixture
def mock_upstream_reader():
    """Create a mock asyncio StreamReader for the upstream side."""
    reader = AsyncMock(spec=asyncio.StreamReader)
    reader.read = AsyncMock(return_value=b"")
    reader.at_eof = MagicMock(return_value=False)
    return reader


@pytest.fixture
def mock_upstream_writer():
    """Create a mock asyncio StreamWriter for the upstream side."""
    writer = AsyncMock(spec=asyncio.StreamWriter)
    writer.write = MagicMock()
    writer.close = MagicMock()
    writer.wait_closed = AsyncMock()
    writer.drain = AsyncMock()
    writer.is_closing = MagicMock(return_value=False)
    return writer
