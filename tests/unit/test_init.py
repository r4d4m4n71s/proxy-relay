"""Tests for proxy_relay.__init__ -- public API exports."""
from __future__ import annotations

import pytest


class TestPublicAPI:
    """Test that __init__.py exports the public API."""

    def test_version_is_string(self):
        """__version__ is a non-empty string."""
        from proxy_relay import __version__

        assert isinstance(__version__, str)
        assert len(__version__) > 0

    def test_version_follows_semver_pattern(self):
        """__version__ looks like a semver string (X.Y.Z)."""
        from proxy_relay import __version__

        parts = __version__.split(".")
        assert len(parts) >= 2, f"Expected at least X.Y, got {__version__!r}"
        # First two parts should be numeric
        assert parts[0].isdigit()
        assert parts[1].isdigit()

    def test_all_exports_defined(self):
        """__all__ contains the expected public names."""
        import proxy_relay

        assert hasattr(proxy_relay, "__all__")
        assert "__version__" in proxy_relay.__all__

    def test_all_entries_are_importable(self):
        """Every name in __all__ is actually importable from the package."""
        import proxy_relay

        for name in proxy_relay.__all__:
            assert hasattr(proxy_relay, name), (
                f"{name!r} is listed in __all__ but not accessible on proxy_relay"
            )

    def test_invalid_attribute_raises_error(self):
        """Accessing undefined attribute raises AttributeError."""
        import proxy_relay

        with pytest.raises(AttributeError):
            _ = proxy_relay.no_such_thing_at_all
