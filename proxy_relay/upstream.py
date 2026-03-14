"""Upstream proxy manager wrapping proxy-st for SOCKS5 URL resolution."""
from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse

from proxy_relay.exceptions import UpstreamError
from proxy_relay.logger import get_logger

log = get_logger(__name__)


@dataclass(frozen=True)
class UpstreamInfo:
    """Parsed upstream SOCKS5 connection parameters.

    Attributes:
        host: SOCKS5 proxy hostname.
        port: SOCKS5 proxy port.
        username: Authentication username (empty if whitelist mode).
        password: Authentication password with IProyal parameters.
        url: Full SOCKS5 URL for logging (masked).
        country: Target country code from the profile.
    """

    host: str
    port: int
    username: str
    password: str
    url: str
    country: str


class UpstreamManager:
    """Manages the upstream SOCKS5 proxy connection via proxy-st.

    Loads proxy-st configuration, resolves the SOCKS5 URL for a named profile,
    and provides parsed connection parameters for python-socks.

    Args:
        profile_name: proxy-st profile name to use (e.g., "browse").
    """

    def __init__(self, profile_name: str) -> None:
        self._profile_name = profile_name
        self._config = None  # Lazy-loaded proxy-st AppConfig
        self._session_store = None  # Lazy-loaded SessionStore
        self._current: UpstreamInfo | None = None

    def _ensure_loaded(self) -> None:
        """Lazy-load proxy-st config and session store on first use.

        Raises:
            UpstreamError: If proxy-st is not installed or config is invalid.
        """
        if self._config is not None:
            return

        try:
            from proxy_st.config import AppConfig
            from proxy_st.session_store import SessionStore
        except ImportError as exc:
            raise UpstreamError(
                "proxy-st is not installed. Install it with: pip install proxy-st"
            ) from exc

        try:
            self._config = AppConfig.load()
            self._session_store = SessionStore()
        except Exception as exc:
            raise UpstreamError(f"Failed to load proxy-st configuration: {exc}") from exc

        if self._profile_name not in self._config.profiles:
            available = ", ".join(sorted(self._config.profiles))
            raise UpstreamError(
                f"proxy-st profile {self._profile_name!r} not found. "
                f"Available profiles: {available}"
            )

        log.info("proxy-st loaded, using profile %r", self._profile_name)

    def _build_url(self) -> str:
        """Build the upstream SOCKS5 URL from proxy-st config.

        Returns:
            Full SOCKS5 URL string.

        Raises:
            UpstreamError: If URL building fails.
        """
        self._ensure_loaded()
        assert self._config is not None
        assert self._session_store is not None

        try:
            from proxy_st.url import build_url, mask_url
        except ImportError as exc:
            raise UpstreamError("proxy-st URL module not available") from exc

        profile = self._config.profiles[self._profile_name]
        defaults = self._config.defaults
        auth = self._config.auth

        proxy_url = build_url(
            self._profile_name,
            profile,
            auth,
            defaults,
            session_store=self._session_store,
        )

        log.debug("Built upstream URL: %s", mask_url(proxy_url.url))
        return proxy_url.url

    def get_upstream(self) -> UpstreamInfo:
        """Resolve and return the current upstream SOCKS5 connection info.

        Parses the SOCKS5 URL into individual connection parameters suitable
        for python-socks ``Proxy.create_connection()``.

        Returns:
            UpstreamInfo with parsed host, port, username, password.

        Raises:
            UpstreamError: If the URL cannot be built or parsed.
        """
        url = self._build_url()

        try:
            from proxy_st.url import mask_url
        except ImportError:
            mask_url_fn = lambda u: u.split("@")[-1] if "@" in u else u  # noqa: E731
        else:
            mask_url_fn = mask_url

        parsed = urlparse(url)

        if not parsed.hostname or not parsed.port:
            raise UpstreamError(f"Invalid upstream URL structure: {mask_url_fn(url)}")

        assert self._config is not None
        profile = self._config.profiles[self._profile_name]

        self._current = UpstreamInfo(
            host=parsed.hostname,
            port=parsed.port,
            username=parsed.username or "",
            password=parsed.password or "",
            url=mask_url_fn(url),
            country=profile.country,
        )

        log.info(
            "Upstream resolved: %s:%d (country=%s)",
            self._current.host,
            self._current.port,
            self._current.country or "any",
        )
        return self._current

    def rotate(self) -> UpstreamInfo:
        """Force session rotation and return new upstream info.

        Clears the current sticky session in proxy-st's SessionStore,
        forcing a new session ID (and therefore a new exit IP) on the
        next URL build.

        Returns:
            Fresh UpstreamInfo after rotation.

        Raises:
            UpstreamError: If rotation or URL building fails.
        """
        self._ensure_loaded()
        assert self._session_store is not None

        self._session_store.rotate(self._profile_name)
        log.info("Session rotated for profile %r", self._profile_name)

        self._current = None
        return self.get_upstream()

    @property
    def current(self) -> UpstreamInfo | None:
        """Return the most recently resolved upstream info, or None."""
        return self._current

    @property
    def profile_name(self) -> str:
        """Return the proxy-st profile name."""
        return self._profile_name
