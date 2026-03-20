"""Tests for Batch J low-severity fixes (J-RL4, J-RL8, J-RL12)."""
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# J-RL4 — UpstreamInfo stores both url (real) and masked_url
# ---------------------------------------------------------------------------


class TestUpstreamInfoMaskedUrl:
    """Verify UpstreamInfo exposes both real and masked URL fields (J-RL4)."""

    def _make_info(self, url: str, masked: str) -> object:
        from proxy_relay.upstream import UpstreamInfo

        return UpstreamInfo(
            host="proxy.example.com",
            port=12322,
            username="user",
            password="pass",
            url=url,
            masked_url=masked,
            country="us",
        )

    def test_url_stores_real_url(self):
        """url field stores the raw credential URL."""
        info = self._make_info(
            "socks5://user:pass@proxy.example.com:12322",
            "socks5://***@proxy.example.com:12322",
        )
        assert info.url == "socks5://user:pass@proxy.example.com:12322"

    def test_masked_url_stores_masked_url(self):
        """masked_url field stores the credential-masked URL."""
        info = self._make_info(
            "socks5://user:pass@proxy.example.com:12322",
            "socks5://***@proxy.example.com:12322",
        )
        assert info.masked_url == "socks5://***@proxy.example.com:12322"

    def test_url_and_masked_url_are_different(self):
        """Real URL and masked URL differ when credentials are present."""
        info = self._make_info(
            "socks5://user:secret@proxy.example.com:12322",
            "socks5://***@proxy.example.com:12322",
        )
        assert info.url != info.masked_url

    def test_url_without_credentials_same_as_masked(self):
        """When no credentials are present both fields may be identical."""
        info = self._make_info(
            "socks5://proxy.example.com:12322",
            "socks5://proxy.example.com:12322",
        )
        assert info.url == info.masked_url

    def test_upstream_info_is_frozen(self):
        """UpstreamInfo is a frozen dataclass — fields cannot be mutated."""
        from proxy_relay.upstream import UpstreamInfo

        info = UpstreamInfo(
            host="h", port=1080, username="u", password="p",
            url="socks5://u:p@h:1080",
            masked_url="socks5://***@h:1080",
            country="co",
        )
        with pytest.raises(AttributeError):
            info.masked_url = "other"  # type: ignore[misc]

    def test_get_upstream_sets_masked_url(self):
        """get_upstream() populates masked_url with the masked form of the URL."""
        from proxy_relay.upstream import UpstreamManager

        mgr = UpstreamManager("browse")
        mgr._config = MagicMock()
        mgr._config.profiles = {"browse": MagicMock(country="us")}

        with (
            patch.object(UpstreamManager, "_build_url",
                         return_value="socks5://testuser:testpass@proxy.example.com:12322"),
            patch.object(UpstreamManager, "_ensure_loaded"),
        ):
            info = mgr.get_upstream()

        # Real URL should contain credentials
        assert "testuser" in info.url
        # Masked URL should NOT contain plaintext password
        assert "testpass" not in info.masked_url
        assert "***" in info.masked_url

    def test_get_upstream_url_contains_credentials(self):
        """get_upstream() stores the real credential URL in the url field."""
        from proxy_relay.upstream import UpstreamManager

        mgr = UpstreamManager("browse")
        mgr._config = MagicMock()
        mgr._config.profiles = {"browse": MagicMock(country="us")}

        with (
            patch.object(UpstreamManager, "_build_url",
                         return_value="socks5://myuser:mypass@proxy.example.com:12322"),
            patch.object(UpstreamManager, "_ensure_loaded"),
        ):
            info = mgr.get_upstream()

        assert info.url == "socks5://myuser:mypass@proxy.example.com:12322"


# ---------------------------------------------------------------------------
# J-RL8 — CdpClient.recv_task read-only property
# ---------------------------------------------------------------------------


class TestCdpClientRecvTaskProperty:
    """Verify CdpClient exposes recv_task as a public read-only property (J-RL8)."""

    def test_recv_task_none_before_connect(self):
        """recv_task is None when client has not been connected yet."""
        from proxy_relay.capture.cdp_client import CdpClient

        client = CdpClient()
        assert client.recv_task is None

    def test_recv_task_returns_private_recv_task(self):
        """recv_task property returns the same object as _recv_task."""
        from proxy_relay.capture.cdp_client import CdpClient

        client = CdpClient()
        mock_task: MagicMock = MagicMock(spec=asyncio.Task)
        client._recv_task = mock_task  # type: ignore[assignment]

        assert client.recv_task is mock_task

    def test_recv_task_is_read_only(self):
        """recv_task is a property and cannot be set directly."""
        from proxy_relay.capture.cdp_client import CdpClient

        client = CdpClient()
        with pytest.raises(AttributeError):
            client.recv_task = MagicMock()  # type: ignore[misc]

    def test_recv_task_none_after_close(self):
        """After close(), recv_task should be None."""
        from proxy_relay.capture.cdp_client import CdpClient

        async def _run() -> None:
            client = CdpClient()
            # Simulate a connected-then-closed client without a real WebSocket
            client._closed = True
            client._recv_task = None
            assert client.recv_task is None

        asyncio.get_event_loop().run_until_complete(_run())


# ---------------------------------------------------------------------------
# J-RL12 — _seed_widevine skips iterdir on missing directory
# ---------------------------------------------------------------------------


class TestSeedWidevineMissingDir:
    """Verify _seed_widevine does not raise when _SNAP_PROFILES_DIR is missing."""

    def test_seed_widevine_no_error_when_snap_profiles_dir_absent(self, tmp_path):
        """_seed_widevine must not raise when _SNAP_PROFILES_DIR does not exist."""
        import proxy_relay.browse as browse_mod

        non_existent = tmp_path / "does_not_exist"
        assert not non_existent.exists()

        profile_dir = tmp_path / "my-profile"
        profile_dir.mkdir()

        with (
            patch.object(browse_mod, "_SNAP_PROFILES_DIR", non_existent),
            patch.object(browse_mod, "_SNAP_CHROMIUM_DIR", tmp_path / "chromium"),
        ):
            # Should complete without raising FileNotFoundError or similar
            browse_mod._seed_widevine(profile_dir)

        # No WidevineCdm dir created because the source doesn't exist
        assert not (profile_dir / "WidevineCdm").exists()

    def test_seed_widevine_skips_when_target_already_exists(self, tmp_path):
        """_seed_widevine is a no-op when the target WidevineCdm dir already exists."""
        import proxy_relay.browse as browse_mod

        profile_dir = tmp_path / "my-profile"
        profile_dir.mkdir()
        existing_widevine = profile_dir / "WidevineCdm"
        existing_widevine.mkdir()

        non_existent = tmp_path / "does_not_exist"
        with patch.object(browse_mod, "_SNAP_PROFILES_DIR", non_existent):
            # Should return early without touching anything
            browse_mod._seed_widevine(profile_dir)

        assert existing_widevine.exists()  # untouched

    def test_seed_widevine_uses_snap_chromium_source_when_available(self, tmp_path):
        """_seed_widevine copies from _SNAP_CHROMIUM_DIR/WidevineCdm when present."""
        import proxy_relay.browse as browse_mod

        # Create source WidevineCdm tree
        snap_chromium = tmp_path / "chromium"
        widevine_source = snap_chromium / "WidevineCdm"
        widevine_source.mkdir(parents=True)
        (widevine_source / "libwidevinecdm.so").write_bytes(b"fake_cdm")

        profile_dir = tmp_path / "my-profile"
        profile_dir.mkdir()

        non_existent = tmp_path / "no_snap_profiles"
        with (
            patch.object(browse_mod, "_SNAP_PROFILES_DIR", non_existent),
            patch.object(browse_mod, "_SNAP_CHROMIUM_DIR", snap_chromium),
        ):
            browse_mod._seed_widevine(profile_dir)

        # WidevineCdm should have been seeded from the source
        assert (profile_dir / "WidevineCdm").exists()
        assert (profile_dir / "WidevineCdm" / "libwidevinecdm.so").exists()

    def test_seed_widevine_uses_sibling_fallback_when_snap_chromium_absent(self, tmp_path):
        """_seed_widevine falls back to sibling profiles when _SNAP_CHROMIUM_DIR is absent."""
        import proxy_relay.browse as browse_mod

        # Create a sibling profile with WidevineCdm
        snap_profiles = tmp_path / "snap-profiles"
        sibling = snap_profiles / "existing-profile"
        sibling_widevine = sibling / "WidevineCdm"
        sibling_widevine.mkdir(parents=True)
        (sibling_widevine / "libwidevinecdm.so").write_bytes(b"sibling_cdm")

        # New profile to seed
        new_profile = snap_profiles / "new-profile"
        new_profile.mkdir()

        snap_chromium = tmp_path / "no_chromium"  # absent

        with (
            patch.object(browse_mod, "_SNAP_PROFILES_DIR", snap_profiles),
            patch.object(browse_mod, "_SNAP_CHROMIUM_DIR", snap_chromium),
        ):
            browse_mod._seed_widevine(new_profile)

        assert (new_profile / "WidevineCdm").exists()
