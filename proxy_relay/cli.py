"""Command-line interface for proxy-relay."""
from __future__ import annotations

import argparse
import asyncio
import json
import signal
import sys
from pathlib import Path

from proxy_relay import __version__
from proxy_relay import browse as _browse
from proxy_relay.config import MonitorConfig, RelayConfig
from proxy_relay.exceptions import BrowseError, ConfigError, ProxyRelayError, UpstreamError
from proxy_relay.logger import configure_logging, get_logger
from proxy_relay.pidfile import (
    PID_PATH,
    is_process_running,
    pid_path_for,
    read_pid,
    read_status,
    remove_pid,
    send_signal,
    status_path_for,
)
from proxy_relay.server import ProxyServer
from proxy_relay.tz import check_timezone_mismatch, get_timezone_for_country
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
    stop_parser = subparsers.add_parser(
        "stop",
        help="Stop the running proxy relay server",
    )
    stop_parser.add_argument(
        "--profile",
        type=str,
        default=None,
        help="proxy-st profile name (default: 'browse')",
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
    status_parser.add_argument(
        "--profile",
        type=str,
        default=None,
        help="proxy-st profile name (default: 'browse')",
    )

    # rotate subcommand
    rotate_parser = subparsers.add_parser(
        "rotate",
        help="Trigger upstream proxy rotation (sends SIGUSR1)",
    )
    rotate_parser.add_argument(
        "--profile",
        type=str,
        default=None,
        help="proxy-st profile name (default: 'browse')",
    )

    # profile-clean subcommand
    clean_parser = subparsers.add_parser(
        "profile-clean",
        help="List or delete browser profiles",
    )
    clean_parser.add_argument(
        "names",
        nargs="*",
        help="Profile name(s) to delete (omit to list all profiles)",
    )
    clean_parser.add_argument(
        "--all",
        action="store_true",
        dest="delete_all",
        help="Delete all browser profiles",
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
        "--profile",
        type=str,
        default=None,
        help="proxy-st profile name (overrides config, selects browser workspace)",
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
    browse_parser.add_argument(
        "--browser",
        type=str,
        default=None,
        help="Chromium-based browser binary name or path (default: auto-detect)",
    )
    browse_parser.add_argument(
        "--capture",
        action="store_true",
        default=False,
        help=(
            "Enable CDP traffic capture (requires proxy-relay[capture]: "
            "websockets + telemetry-monitor)"
        ),
    )
    browse_parser.add_argument(
        "--capture-domains",
        type=str,
        default=None,
        metavar="DOMAINS",
        help=(
            "Comma-separated list of domains to capture "
            "(default: tidal.com,qobuz.com). Only used with --capture."
        ),
    )

    # ── analyze ────────────────────────────────────────────────────────
    analyze_parser = subparsers.add_parser(
        "analyze",
        help="Analyze a CDP capture database",
    )
    analyze_parser.add_argument(
        "--db",
        type=str,
        default=None,
        help="Path to capture.db (default: ~/.config/proxy-relay/capture.db)",
    )
    analyze_parser.add_argument(
        "--report",
        action="store_true",
        default=False,
        help="Write a detailed markdown report file",
    )
    analyze_parser.add_argument(
        "--report-dir",
        type=str,
        default=None,
        help="Directory for report file (default: ~/.config/proxy-relay/)",
    )
    analyze_parser.add_argument(
        "--verbose",
        action="store_true",
        default=False,
        help="Include full JSON key inventories in API surface analysis",
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
    config_path = Path(args.config) if args.config else None
    try:
        config = RelayConfig.load(config_path)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 1

    # CLI overrides (use `is None` checks — 0 is a valid port)
    log_level = args.log_level or config.log_level
    configure_logging(log_level)

    # Validate CLI-supplied port range before using the value
    if args.port is not None and not (1 <= args.port <= 65535):
        print(
            f"Invalid --port {args.port!r}: must be an integer in 1-65535",
            file=sys.stderr,
        )
        return 1

    host = config.server.host if args.host is None else args.host
    port = config.server.port if args.port is None else args.port
    profile_name = args.profile or config.proxy_st_profile

    # Check for existing instance of this profile
    pid = read_pid(pid_path_for(profile_name))
    if pid is not None and is_process_running(pid):
        print(
            f"proxy-relay is already running for profile {profile_name!r} (PID {pid})",
            file=sys.stderr,
        )
        return 1

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
        profile_name=profile_name,
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
    profile = args.profile or "browse"
    pid_path = pid_path_for(profile)

    pid = read_pid(pid_path)
    if pid is None:
        # Legacy fallback: check old proxy-relay.pid
        legacy_pid = read_pid(PID_PATH)
        if legacy_pid is not None:
            print(
                f"WARNING: Legacy PID file found at {PID_PATH}. "
                f"Use 'kill {legacy_pid}' to stop the old instance, "
                f"then delete {PID_PATH}.",
                file=sys.stderr,
            )
        print(
            f"proxy-relay is not running for profile {profile!r} (no PID file found)",
            file=sys.stderr,
        )
        return 1

    if not is_process_running(pid):
        print(
            f"proxy-relay is not running for profile {profile!r} (stale PID {pid})",
            file=sys.stderr,
        )
        remove_pid(pid_path)
        return 1

    if send_signal(pid, signal.SIGTERM):
        print(f"Sent SIGTERM to proxy-relay (PID {pid}, profile {profile!r})")
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
    profile = args.profile or "browse"
    pid = read_pid(pid_path_for(profile))
    running = pid is not None and is_process_running(pid)
    status_data = read_status(status_path_for(profile))

    if args.json_output:
        output: dict = {
            "running": running,
            "pid": pid,
            "profile": profile,
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
        print(f"proxy-relay [{profile}]: {state}")
        return 1

    print(f"proxy-relay [{profile}]: running (PID {pid})")

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
            print("  Monitor:")
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
    profile = args.profile or "browse"
    pid = read_pid(pid_path_for(profile))
    if pid is None:
        print(
            f"proxy-relay is not running for profile {profile!r} (no PID file found)",
            file=sys.stderr,
        )
        return 1

    if not is_process_running(pid):
        print(
            f"proxy-relay is not running for profile {profile!r} (stale PID {pid})",
            file=sys.stderr,
        )
        return 1

    if send_signal(pid, signal.SIGUSR1):
        print(f"Sent SIGUSR1 to proxy-relay (PID {pid}, profile {profile!r}) — rotation triggered")
        return 0

    print(f"Failed to send SIGUSR1 to PID {pid}", file=sys.stderr)
    return 1


def _cmd_profile_clean(args: argparse.Namespace) -> int:
    """Execute the 'profile-clean' subcommand.

    Lists profiles when no names are given, or deletes the specified profiles.

    Args:
        args: Parsed command-line arguments.

    Returns:
        Exit code (0 for success, non-zero for error).
    """
    profiles = _browse.list_profiles()

    # List mode: no names and no --all
    if not args.names and not args.delete_all:
        if not profiles:
            print("No browser profiles found.")
        else:
            print(f"Browser profiles ({len(profiles)}):")
            for name in profiles:
                print(f"  - {name}")
        return 0

    # Delete mode
    to_delete = profiles if args.delete_all else args.names
    if not to_delete:
        print("No browser profiles to delete.")
        return 0

    errors = 0
    for name in to_delete:
        try:
            removed = _browse.delete_profile(name)
            for path in removed:
                print(f"  Removed: {path}")
        except BrowseError as exc:
            print(f"  {exc}", file=sys.stderr)
            errors += 1

    return 1 if errors else 0


def _cmd_browse(args: argparse.Namespace) -> int:
    """Execute the 'browse' subcommand with auto-start/stop lifecycle.

    If no server is running for the requested profile, one is auto-started
    on an OS-assigned port and stopped when the browser exits.

    Args:
        args: Parsed command-line arguments.

    Returns:
        Exit code (0 for success, non-zero for error).
    """
    # 1. Load config
    config_path = Path(args.config) if args.config else None
    try:
        config = RelayConfig.load(config_path)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 1

    profile_name = args.profile or config.proxy_st_profile
    log_level = config.log_level
    configure_logging(log_level)

    # Validate --rotate-min before doing any work (E-RL15).
    if args.rotate_min is not None and args.rotate_min < 0:
        print(
            f"Invalid --rotate-min {args.rotate_min!r}: must be a non-negative integer",
            file=sys.stderr,
        )
        return 1

    # 2. Check if a server is already running for this profile
    auto_started = False
    server_proc = None
    pid = read_pid(pid_path_for(profile_name))

    if pid is not None and is_process_running(pid):
        # Server already running — reuse it
        status_data = read_status(status_path_for(profile_name))
        if status_data is not None:
            host = status_data.get("host", config.server.host)
            port = status_data.get("port", config.server.port)
        else:
            host = config.server.host
            port = config.server.port
        relay_pid = pid
        print(f"Using existing server for profile {profile_name!r} (PID {pid})")
    else:
        # No server running — auto-start one
        auto_started = True
        print(f"Starting server for profile {profile_name!r}...")
        try:
            server_proc = _browse.auto_start_server(
                profile_name,
                host=config.server.host,
                config_path=Path(args.config) if args.config else None,
                log_level=log_level,
            )
            host, port = _browse.wait_for_server_ready(profile_name, server_proc)
            relay_pid = server_proc.pid
            print(f"Server started (PID {relay_pid}, port {port})")
        except BrowseError as exc:
            print(f"Failed to start server: {exc}", file=sys.stderr)
            return 1

        # Read status for country info (may still be None if the file
        # has not yet been flushed; downstream code uses the guard below)
        status_data = read_status(status_path_for(profile_name)) or {}

    try:
        # 3. Health check
        try:
            exit_ip = _browse.health_check(host, port)
            print(f"Proxy chain verified — exit IP: {exit_ip}")
        except BrowseError as exc:
            print(f"\nHealth check failed:\n  {exc}\n", file=sys.stderr)
            return 1

        # 4. Find browser (CLI --browser > config browser > auto-detect)
        browser_override = getattr(args, "browser", None) or config.browse.browser or None
        try:
            if browser_override:
                chromium_path = _browse.resolve_browser(browser_override)
            else:
                chromium_path = _browse.find_chromium()
            log.info("Found browser at %s", chromium_path)
        except BrowseError as exc:
            print(str(exc), file=sys.stderr)
            return 1

        # 5. Profile dir and timezone
        profile_dir = _browse.get_profile_dir(profile_name, chromium_path=chromium_path)

        country = (status_data or {}).get("country", "")
        proxy_tz: str | None = None
        if country:
            proxy_tz = get_timezone_for_country(country)
            if proxy_tz:
                print(f"Timezone spoofing: TZ={proxy_tz} (country: {country.upper()})")
            else:
                log.warning("No timezone mapping for country %r — using system timezone", country)

        # 6. Resolve rotation interval
        if args.no_rotate:
            rotate_min = 0
        elif args.rotate_min is not None:
            rotate_min = args.rotate_min
        else:
            rotate_min = config.browse.rotate_interval_min

        # 7. Optionally create capture session
        capture_session = None
        if getattr(args, "capture", False):
            from proxy_relay.capture import CaptureSession, is_capture_available
            from proxy_relay.capture.models import DEFAULT_CAPTURE_DOMAINS, CaptureConfig

            if not is_capture_available():
                print(
                    "Capture requires optional dependencies: "
                    "install proxy-relay[capture] (websockets + telemetry-monitor)",
                    file=sys.stderr,
                )
                return 1

            # Build CaptureConfig: CLI --capture-domains overrides config/defaults
            raw_domains_arg = getattr(args, "capture_domains", None)
            if raw_domains_arg:
                domains = frozenset(d.strip() for d in raw_domains_arg.split(",") if d.strip())
            elif config.capture is not None:
                domains = config.capture.capture_domains  # type: ignore[union-attr]
            else:
                domains = DEFAULT_CAPTURE_DOMAINS

            base_cfg = config.capture if config.capture is not None else CaptureConfig()
            capture_config = CaptureConfig(
                db_path=base_cfg.db_path,  # type: ignore[union-attr]
                domains=domains,
                redact_headers=base_cfg.redact_headers,  # type: ignore[union-attr]
                max_body_bytes=base_cfg.max_body_bytes,  # type: ignore[union-attr]
                cookie_poll_interval_s=base_cfg.cookie_poll_interval_s,  # type: ignore[union-attr]
                storage_poll_interval_s=base_cfg.storage_poll_interval_s,  # type: ignore[union-attr]
                report_dir=base_cfg.report_dir,  # type: ignore[union-attr]
                auto_analyze=base_cfg.auto_analyze,  # type: ignore[union-attr]
                auto_report=base_cfg.auto_report,  # type: ignore[union-attr]
            )
            capture_session = CaptureSession(config=capture_config, profile=profile_name)
            print(
                f"CDP capture enabled (domains: {', '.join(sorted(domains))}, "
                f"db: {capture_config.db_path})"
            )

        # 8. Create supervisor and run
        supervisor = _browse.BrowseSupervisor(
            chromium_path=chromium_path,
            proxy_host=host,
            proxy_port=port,
            profile_dir=profile_dir,
            relay_pid=relay_pid,
            rotate_interval_min=rotate_min,
            timezone=proxy_tz,
            capture_session=capture_session,
        )

        if rotate_min > 0:
            print(f"Auto-rotation enabled: every {rotate_min} minutes")
        else:
            print("Auto-rotation disabled")

        print(f"Launching Chromium (profile: {profile_name}, data: {profile_dir})...")
        return supervisor.run()

    finally:
        # 9. Auto-stop server if we started it
        if auto_started and server_proc is not None:
            _browse.auto_stop_server(server_proc, profile_name)


def _cmd_analyze(args: argparse.Namespace) -> int:
    """Execute the 'analyze' subcommand.

    Args:
        args: Parsed command-line arguments.

    Returns:
        Exit code (0 for success, non-zero for error).
    """
    from proxy_relay.capture.analyzer import analyze, write_report
    from proxy_relay.capture.analyzer import print_report as print_analysis
    from proxy_relay.capture.models import DEFAULT_CAPTURE_DB, DEFAULT_REPORT_DIR

    db_path = Path(args.db) if args.db else DEFAULT_CAPTURE_DB
    if not db_path.exists():
        print(f"Capture database not found: {db_path}", file=sys.stderr)
        return 1

    try:
        report = analyze(db_path, verbose=args.verbose)
    except Exception as exc:
        print(f"Analysis failed: {exc}", file=sys.stderr)
        return 1

    print_analysis(report)

    if args.report:
        if args.report_dir:
            report_dir = Path(args.report_dir)
        else:
            # Use config's report_dir if available, else default
            try:
                from proxy_relay.config import load_config

                cfg = load_config()
                capture_cfg = cfg.capture
                report_dir = (
                    capture_cfg.resolved_report_dir() if capture_cfg else DEFAULT_REPORT_DIR
                )
            except Exception:
                report_dir = DEFAULT_REPORT_DIR
        report_path = write_report(report, output_dir=report_dir)
        print(f"\nReport written to: {report_path}")

    return 0


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
        "profile-clean": _cmd_profile_clean,
        "browse": _cmd_browse,
        "analyze": _cmd_analyze,
    }

    handler = dispatch.get(args.command)
    if handler is not None:
        sys.exit(handler(args))
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
