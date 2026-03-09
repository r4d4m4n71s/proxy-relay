"""Command-line interface for proxy-relay."""
from __future__ import annotations

import argparse
import asyncio
import json
import signal
import sys
from pathlib import Path

from proxy_relay import __version__
from proxy_relay.config import MonitorConfig, RelayConfig
from proxy_relay import browse as _browse
from proxy_relay.exceptions import BrowseError, ConfigError, ProxyRelayError, UpstreamError
from proxy_relay.logger import configure_logging, get_logger
from proxy_relay.pidfile import is_process_running, read_pid, read_status, send_signal
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
        "--config",
        type=str,
        default=None,
        help="Path to config file (default: ~/.config/proxy-relay/config.toml)",
    )
    start_parser.add_argument(
        "--log-level",
        type=str,
        default=None,
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Log level (default: from config or INFO)",
    )

    # stop subcommand
    subparsers.add_parser(
        "stop",
        help="Stop the running proxy relay server",
    )

    # status subcommand
    status_parser = subparsers.add_parser(
        "status",
        help="Show status of the proxy relay server",
    )
    status_parser.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Output status as JSON",
    )

    # rotate subcommand
    subparsers.add_parser(
        "rotate",
        help="Trigger upstream proxy rotation (sends SIGUSR1)",
    )

    # browse subcommand
    browse_parser = subparsers.add_parser(
        "browse",
        help="Launch Chromium through the running proxy relay",
    )
    browse_parser.add_argument(
        "--rotate-min",
        type=int,
        default=None,
        help="Auto-rotate interval in minutes (default: from config or 30)",
    )
    browse_parser.add_argument(
        "--no-rotate",
        action="store_true",
        default=False,
        help="Disable auto-rotation",
    )
    browse_parser.add_argument(
        "--config",
        type=str,
        default=None,
        help="Path to config file",
    )

    return parser


def _cmd_start(args: argparse.Namespace) -> int:
    """Execute the 'start' subcommand.

    Args:
        args: Parsed command-line arguments.

    Returns:
        Exit code (0 for success, non-zero for error).
    """
    # Check for existing instance
    pid = read_pid()
    if pid is not None and is_process_running(pid):
        print(f"proxy-relay is already running (PID {pid})", file=sys.stderr)
        return 1

    # Load configuration
    config_path = Path(args.config) if args.config else None
    try:
        config = RelayConfig.load(config_path)
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
    monitor_config = config.monitor
    try:
        asyncio.run(_run(host, port, profile_name, monitor_config))
    except KeyboardInterrupt:
        log.info("Interrupted by user")
    except ProxyRelayError as exc:
        log.error("Fatal error: %s", exc)
        return 1

    return 0


async def _run(
    host: str,
    port: int,
    profile_name: str,
    monitor_config: MonitorConfig | None = None,
) -> None:
    """Create and run the proxy server.

    Args:
        host: Bind address.
        port: Bind port.
        profile_name: proxy-st profile name.
        monitor_config: Optional monitor configuration.
    """
    manager = UpstreamManager(profile_name)
    server = ProxyServer(
        host=host,
        port=port,
        upstream_manager=manager,
        monitor_config=monitor_config,
    )

    await server.start()
    await server.serve_forever()


def _cmd_stop(args: argparse.Namespace) -> int:
    """Execute the 'stop' subcommand.

    Reads the PID file, checks if the process is running, and sends SIGTERM.

    Args:
        args: Parsed command-line arguments.

    Returns:
        Exit code (0 for success, non-zero for error).
    """
    pid = read_pid()
    if pid is None:
        print("proxy-relay is not running (no PID file found)", file=sys.stderr)
        return 1

    if not is_process_running(pid):
        print(f"proxy-relay is not running (stale PID {pid})", file=sys.stderr)
        # Clean up stale PID file
        from proxy_relay.pidfile import remove_pid
        remove_pid()
        return 1

    if send_signal(pid, signal.SIGTERM):
        print(f"Sent SIGTERM to proxy-relay (PID {pid})")
        return 0

    print(f"Failed to send SIGTERM to PID {pid}", file=sys.stderr)
    return 1


def _cmd_status(args: argparse.Namespace) -> int:
    """Execute the 'status' subcommand.

    Reads the PID file and status file, displays server information.

    Args:
        args: Parsed command-line arguments.

    Returns:
        Exit code (0 for success, non-zero for error).
    """
    pid = read_pid()
    running = pid is not None and is_process_running(pid)
    status_data = read_status()

    if args.json_output:
        output: dict = {
            "running": running,
            "pid": pid,
        }
        if status_data is not None:
            output.update(status_data)
        print(json.dumps(output, indent=2))
        return 0

    # Human-readable output
    if not running:
        state = "not running"
        if pid is not None:
            state = f"not running (stale PID {pid})"
        print(f"proxy-relay: {state}")
        return 1

    print(f"proxy-relay: running (PID {pid})")

    if status_data is not None:
        host = status_data.get("host", "?")
        port = status_data.get("port", "?")
        upstream_url = status_data.get("upstream_url", "?")
        country = status_data.get("country", "?")
        active = status_data.get("active_connections", "?")
        total = status_data.get("total_connections", "?")

        print(f"  Listen:      {host}:{port}")
        print(f"  Upstream:    {upstream_url}")
        print(f"  Country:     {country}")
        print(f"  Connections: {active} active / {total} total")

        monitor = status_data.get("monitor")
        if monitor is not None:
            print(f"  Monitor:")
            print(f"    Window errors:   {monitor.get('window_error_count', '?')}")
            print(f"    Total errors:    {monitor.get('total_errors', '?')}")
            print(f"    Rotations:       {monitor.get('total_rotations', '?')}")
            avg = monitor.get("avg_latency_ms", 0)
            p95 = monitor.get("p95_latency_ms", 0)
            print(f"    Avg latency:     {avg:.0f}ms")
            print(f"    P95 latency:     {p95:.0f}ms")

    return 0


def _cmd_rotate(args: argparse.Namespace) -> int:
    """Execute the 'rotate' subcommand.

    Sends SIGUSR1 to the running proxy-relay process to trigger upstream rotation.

    Args:
        args: Parsed command-line arguments.

    Returns:
        Exit code (0 for success, non-zero for error).
    """
    pid = read_pid()
    if pid is None:
        print("proxy-relay is not running (no PID file found)", file=sys.stderr)
        return 1

    if not is_process_running(pid):
        print(f"proxy-relay is not running (stale PID {pid})", file=sys.stderr)
        return 1

    if send_signal(pid, signal.SIGUSR1):
        print(f"Sent SIGUSR1 to proxy-relay (PID {pid}) — rotation triggered")
        return 0

    print(f"Failed to send SIGUSR1 to PID {pid}", file=sys.stderr)
    return 1


def _cmd_browse(args: argparse.Namespace) -> int:
    """Execute the 'browse' subcommand.

    Launches Chromium through the running proxy relay, with optional
    auto-rotation of the upstream proxy.

    Args:
        args: Parsed command-line arguments.

    Returns:
        Exit code (0 for success, non-zero for error).
    """
    # 1. Check PID + liveness
    pid = read_pid()
    if pid is None or not is_process_running(pid):
        print("proxy-relay is not running — start it first with: proxy-relay start", file=sys.stderr)
        return 1

    # 2. Load config
    config_path = Path(args.config) if args.config else None
    try:
        config = RelayConfig.load(config_path)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 1

    # 3. Read status.json for actual host/port, falling back to config
    status_data = read_status()
    if status_data is not None:
        host = status_data.get("host", config.server.host)
        port = status_data.get("port", config.server.port)
    else:
        host = config.server.host
        port = config.server.port

    # 4. Health check
    try:
        exit_ip = _browse.health_check(host, port)
        print(f"Proxy chain verified — exit IP: {exit_ip}")
    except BrowseError as exc:
        print(f"Health check failed: {exc}", file=sys.stderr)
        return 1

    # 5. Find Chromium
    try:
        chromium_path = _browse.find_chromium()
        log.info("Found Chromium at %s", chromium_path)
    except BrowseError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    # 6. Determine profile dir
    profile_dir = _browse.get_profile_dir(config.proxy_st_profile)

    # 7. Resolve rotation interval
    if args.no_rotate:
        rotate_min = 0
    elif args.rotate_min is not None:
        rotate_min = args.rotate_min
    else:
        rotate_min = config.browse.rotate_interval_min

    # 8. Create supervisor and run
    supervisor = _browse.BrowseSupervisor(
        chromium_path=chromium_path,
        proxy_host=host,
        proxy_port=port,
        profile_dir=profile_dir,
        relay_pid=pid,
        rotate_interval_min=rotate_min,
    )

    if rotate_min > 0:
        print(f"Auto-rotation enabled: every {rotate_min} minutes")
    else:
        print("Auto-rotation disabled")

    print(f"Launching Chromium (profile: {config.proxy_st_profile})...")
    return supervisor.run()


def main() -> None:
    """Entry point for the proxy-relay CLI."""
    parser = build_parser()
    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(0)

    dispatch = {
        "start": _cmd_start,
        "stop": _cmd_stop,
        "status": _cmd_status,
        "rotate": _cmd_rotate,
        "browse": _cmd_browse,
    }

    handler = dispatch.get(args.command)
    if handler is not None:
        sys.exit(handler(args))
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
