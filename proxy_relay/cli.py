"""Command-line interface for proxy-relay."""
from __future__ import annotations

import argparse
import asyncio
import sys

from proxy_relay import __version__
from proxy_relay.config import RelayConfig
from proxy_relay.exceptions import ConfigError, ProxyRelayError, UpstreamError
from proxy_relay.logger import configure_logging, get_logger
from proxy_relay.server import ProxyServer
from proxy_relay.tz import check_timezone_mismatch
from proxy_relay.upstream import UpstreamManager

log = get_logger(__name__)


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser for proxy-relay.

    Returns:
        Configured ArgumentParser instance.
    """
    parser = argparse.ArgumentParser(
        prog="proxy-relay",
        description="Local HTTP CONNECT proxy forwarding via upstream SOCKS5.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"proxy-relay {__version__}",
    )

    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # start subcommand
    start_parser = subparsers.add_parser(
        "start",
        help="Start the local proxy relay server",
    )
    start_parser.add_argument(
        "--host",
        type=str,
        default=None,
        help="Bind address (default: from config or 127.0.0.1)",
    )
    start_parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="Bind port (default: from config or 8080)",
    )
    start_parser.add_argument(
        "--profile",
        type=str,
        default=None,
        help="proxy-st profile name (default: from config or 'browse')",
    )
    start_parser.add_argument(
        "--log-level",
        type=str,
        default=None,
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Log level (default: from config or INFO)",
    )

    return parser


def _cmd_start(args: argparse.Namespace) -> int:
    """Execute the 'start' subcommand.

    Args:
        args: Parsed command-line arguments.

    Returns:
        Exit code (0 for success, non-zero for error).
    """
    # Load configuration
    try:
        config = RelayConfig.load()
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 1

    # CLI overrides
    log_level = args.log_level or config.log_level
    configure_logging(log_level)

    host = args.host or config.server.host
    port = args.port or config.server.port
    profile_name = args.profile or config.proxy_st_profile

    log.info("proxy-relay %s starting", __version__)
    log.info("Config: bind=%s:%d, profile=%s", host, port, profile_name)

    # Timezone mismatch check
    if config.anti_leak.warn_timezone_mismatch:
        try:
            manager = UpstreamManager(profile_name)
            upstream = manager.get_upstream()
            if upstream.country:
                check_timezone_mismatch(upstream.country)
        except UpstreamError as exc:
            log.warning("Could not check timezone: %s", exc)

    # Run the server
    try:
        asyncio.run(_run(host, port, profile_name))
    except KeyboardInterrupt:
        log.info("Interrupted by user")
    except ProxyRelayError as exc:
        log.error("Fatal error: %s", exc)
        return 1

    return 0


async def _run(host: str, port: int, profile_name: str) -> None:
    """Create and run the proxy server.

    Args:
        host: Bind address.
        port: Bind port.
        profile_name: proxy-st profile name.
    """
    manager = UpstreamManager(profile_name)
    server = ProxyServer(host=host, port=port, upstream_manager=manager)

    await server.start()
    await server.serve_forever()


def main() -> None:
    """Entry point for the proxy-relay CLI."""
    parser = build_parser()
    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(0)

    if args.command == "start":
        sys.exit(_cmd_start(args))
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
