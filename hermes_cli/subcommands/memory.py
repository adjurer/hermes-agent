"""``hermes memory`` subcommand parser.

Extracted from ``hermes_cli/main.py:main()`` (god-file Phase 2 follow-up).
Handler injected to avoid importing ``main``.
"""

from __future__ import annotations

from typing import Callable


def build_memory_parser(subparsers, *, cmd_memory: Callable) -> None:
    """Attach the ``memory`` subcommand to ``subparsers``."""
    memory_parser = subparsers.add_parser(
        "memory",
        help="Configure external memory provider",
        description=(
            "Set up and manage external memory provider plugins.\n\n"
            "Available providers: honcho, openviking, mem0, hindsight,\n"
            "holographic, retaindb, byterover.\n\n"
            "Only one external provider can be active at a time.\n"
            "Built-in memory (MEMORY.md/USER.md) is always active."
        ),
    )
    memory_sub = memory_parser.add_subparsers(dest="memory_command")
    _setup_parser = memory_sub.add_parser(
        "setup", help="Interactive provider selection and configuration"
    )
    _setup_parser.add_argument(
        "provider",
        nargs="?",
        default=None,
        help="Provider to configure directly (e.g. honcho), skipping the picker",
    )
    memory_sub.add_parser("status", help="Show current memory provider config")
    memory_sub.add_parser("off", help="Disable external provider (built-in only)")
    _audit_parser = memory_sub.add_parser(
        "audit",
        help="Audit Yuri knowledge-spine recall and related Kanban evidence",
    )
    _audit_parser.add_argument(
        "query",
        nargs="*",
        help="Task id or free-text query to audit",
    )
    _audit_parser.add_argument(
        "--limit",
        type=int,
        default=5,
        help="Maximum spine/task matches to include",
    )
    _graph_parser = memory_sub.add_parser(
        "graph-export",
        help="Export Yuri knowledge-spine graph edges for Graphiti/Zep experiments",
    )
    _graph_parser.add_argument(
        "query",
        nargs="*",
        help="Optional free-text query; omitted means recent graph edges",
    )
    _graph_parser.add_argument(
        "--limit",
        type=int,
        default=50,
        help="Maximum graph edges to export",
    )
    _graph_parser.add_argument(
        "--format",
        choices=["jsonl", "summary"],
        default="jsonl",
        help="Output JSONL for ingestion or a compact human summary",
    )
    _okf_parser = memory_sub.add_parser(
        "okf-export",
        help="Export Yuri knowledge-spine memory as an OKF v0.1 markdown bundle",
    )
    _okf_parser.add_argument(
        "query",
        nargs="*",
        help="Optional free-text query; omitted means recent/all spine events",
    )
    _okf_parser.add_argument(
        "--limit",
        type=int,
        default=200,
        help="Maximum spine events to export",
    )
    _okf_parser.add_argument(
        "--output",
        default=None,
        help="Output directory; defaults to the Yuri spine okf_bundle directory",
    )
    _learn_parser = memory_sub.add_parser(
        "learn-report",
        help="Show Yuri review-pass lessons used for future context injection",
    )
    _learn_parser.add_argument(
        "query",
        nargs="*",
        help="Optional free-text query; omitted means recent lessons",
    )
    _learn_parser.add_argument(
        "--limit",
        type=int,
        default=8,
        help="Maximum lessons to include",
    )
    _learn_rebuild_parser = memory_sub.add_parser(
        "learn-rebuild",
        help="Backfill Yuri review-pass lessons from existing spine events",
    )
    _learn_rebuild_parser.add_argument(
        "--limit",
        type=int,
        default=1000,
        help="Maximum spine events to scan",
    )
    _reset_parser = memory_sub.add_parser(
        "reset",
        help="Erase all built-in memory (MEMORY.md and USER.md)",
    )
    _reset_parser.add_argument(
        "--yes",
        "-y",
        action="store_true",
        help="Skip confirmation prompt",
    )
    _reset_parser.add_argument(
        "--target",
        choices=["all", "memory", "user"],
        default="all",
        help="Which store to reset: 'all' (default), 'memory', or 'user'",
    )
    memory_parser.set_defaults(func=cmd_memory)
