"""
CLI entrypoint for ARIP.

Usage:
    python -m arip --help
    python -m arip check-config
    python -m arip run

Subcommands:
    check-config    Validate configuration and print a summary. No pipeline runs.
    run             Execute one pipeline run immediately (manual trigger).
                    Phase 0: prints "Pipeline not yet implemented."
                    Phase 1+: runs the full pipeline.

The `arip` script alias also invokes this module (defined in pyproject.toml
under [project.scripts]).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def cmd_check_config(args: argparse.Namespace) -> int:
    """Validate configuration and print a summary.

    Returns:
        Exit code: 0 on success, 1 on config error.
    """
    from arip.config import load_settings, print_settings_summary
    from arip.exceptions import ConfigError

    yaml_path = Path(args.config)
    print(f"Loading configuration from: {yaml_path}")

    try:
        settings = load_settings(yaml_path)
    except ConfigError as exc:
        print(f"\n✗ Configuration error:\n{exc}", file=sys.stderr)
        return 1

    print_settings_summary(settings)
    print("\n✓ Configuration is valid.")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    """Execute one pipeline run.

    Phase 0: Validates config and DB, then exits cleanly.
    Phase 1+: Runs the full pipeline orchestrator.

    Returns:
        Exit code: 0 on success, 1 on error.
    """
    import structlog

    from arip.container import build_app_components
    from arip.exceptions import ConfigError

    yaml_path = Path(args.config)

    try:
        components = build_app_components(yaml_path)
    except ConfigError as exc:
        print(f"✗ Configuration error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"✗ Startup failed: {exc}", file=sys.stderr)
        return 1

    log = structlog.get_logger("arip.main")
    log.info("arip_startup", version="0.1.0", database=components.settings.database.url)

    # ── Phase 0 stub ────────────────────────────────────────────────────
    # Phase 1 replaces this with: orchestrator.run_once()
    print("Pipeline not yet implemented. (Phase 0 bootstrap complete.)")
    log.info("phase0_complete", message="Infrastructure verified successfully.")
    # ────────────────────────────────────────────────────────────────────

    return 0


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser."""
    parser = argparse.ArgumentParser(
        prog="arip",
        description="AI Research Intelligence Platform — collect, rank, generate, publish.",
    )
    parser.add_argument(
        "--config",
        default="config/settings.yaml",
        metavar="PATH",
        help="Path to settings YAML file (default: config/settings.yaml)",
    )

    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")
    subparsers.required = True

    # check-config
    check_parser = subparsers.add_parser(
        "check-config",
        help="Validate configuration and print a summary.",
    )
    check_parser.set_defaults(func=cmd_check_config)

    # run
    run_parser = subparsers.add_parser(
        "run",
        help="Execute one pipeline run immediately.",
    )
    run_parser.set_defaults(func=cmd_run)

    return parser


def main() -> None:
    """Main entry point. Parses arguments and dispatches to the subcommand."""
    parser = build_parser()
    args = parser.parse_args()
    exit_code = args.func(args)
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
