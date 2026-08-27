from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable, Sequence

from . import __version__
from .errors import AicxError
from .history import history_is_shared, shared_history_count, sync_history
from .processes import ProcessRegistry, run_tracked
from .providers import (
    build_tool_command,
    default_home,
    find_binary,
    login,
    native_status,
)
from .sessions import (
    claude_sessions,
    codex_sessions,
    format_timestamp,
    interrupt_all_codex_sessions,
    interrupt_codex_session,
)
from .store import Store, TOOLS
from .usage import (
    capture_claude_statusline,
    codex_account,
    codex_balance,
    install_claude_usage_hook,
    read_claude_balance,
)
from .vscode import launch_vscode, vscode_launch_context


_COLOR_ENABLED = False


@dataclass(frozen=True)
class StyledCell:
    text: str
    ansi: str


def styled(value: Any, ansi: str) -> StyledCell:
    return StyledCell(str(value), ansi)


def configure_color(mode: str) -> None:
    global _COLOR_ENABLED
    environment_mode = os.environ.get("AICX_COLOR")
    if mode == "auto" and environment_mode in {"always", "never"}:
        mode = environment_mode
    if "NO_COLOR" in os.environ:
        mode = "never"
    _COLOR_ENABLED = mode == "always" or (
        mode == "auto"
        and sys.stdout.isatty()
        and os.environ.get("TERM", "") != "dumb"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="aicx",
        description="Named account contexts with private credentials and shared conversations.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
DAILY USE (history synchronization is automatic)
    aicx codex @work             select work and launch Codex
    aicx codex @personal         select personal and launch Codex
    aicx codex @work resume --last
                                 resume shared history using work
    aicx claude @work            select work and launch Claude Code
    aicx balance                 show usage for every account

SETUP ONCE
    aicx login codex work --device-auth
    aicx login codex personal --device-auth
    aicx login claude work --sso

  To preserve a login made before installing aicx:
    aicx adopt codex personal

The @profile form selects and launches in one command. Without it, `aicx codex`
or `aicx claude` uses the last selected profile. Login is never repeated.

ADVANCED
  Existing commands remain available for explicit selection, synchronization,
  session/process management, VS Code, and shell integration:
    aicx use | sync | sessions | close | vscode | shell-init

Run `aicx COMMAND --help` for command-specific options.
""",
    )
    parser.add_argument("--version", action="version", version=f"aicx {__version__}")
    parser.add_argument(
        "--color",
        choices=("auto", "always", "never"),
        default="auto",
        help="colorize output (default: auto; also respects NO_COLOR)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    login_parser = subparsers.add_parser("login", help="Log in once inside a named profile")
    login_parser.add_argument("tool", choices=TOOLS)
    login_parser.add_argument("profile")
    login_parser.add_argument("--force", action="store_true", help="Run login even if already authenticated")
    login_parser.add_argument("--device-auth", action="store_true", help="Use Codex device-code login")
    login_parser.add_argument("--console", action="store_true", help="Use Claude Console authentication")
    login_parser.add_argument("--sso", action="store_true", help="Force Claude SSO authentication")
    login_parser.add_argument("--email", help="Pre-fill the Claude login email")

    adopt_parser = subparsers.add_parser(
        "adopt", help="Copy an existing tool home, including auth and history, into a profile"
    )
    adopt_parser.add_argument("tool", choices=TOOLS)
    adopt_parser.add_argument("profile")
    adopt_parser.add_argument("--from", dest="source", type=Path)

    use_parser = subparsers.add_parser("use")
    use_parser.add_argument("selection", nargs="+", metavar="TOOL_OR_PROFILE")

    accounts_parser = subparsers.add_parser("accounts", help="List profiles and authentication status")
    accounts_parser.add_argument("tool", nargs="?", choices=TOOLS)
    accounts_parser.add_argument("--json", action="store_true", dest="as_json")

    sync_parser = subparsers.add_parser("sync")
    sync_parser.add_argument("tool", choices=TOOLS)

    balance_parser = subparsers.add_parser("balance", help="Show available usage information")
    balance_parser.add_argument("tool", nargs="?", choices=TOOLS)
    balance_parser.add_argument("--profile")
    balance_parser.add_argument("--json", action="store_true", dest="as_json")
    balance_parser.add_argument(
        "-w",
        "--watch",
        "--continuous",
        action="store_true",
        help="refresh continuously until interrupted",
    )
    balance_parser.add_argument(
        "--interval",
        type=positive_int,
        default=60,
        metavar="SECONDS",
        help="refresh interval for --watch (default: 60)",
    )

    sessions_parser = subparsers.add_parser("sessions")
    sessions_parser.add_argument("tool", choices=TOOLS)
    sessions_parser.add_argument("--profile")
    sessions_parser.add_argument("--all-profiles", action="store_true")
    sessions_parser.add_argument("--limit", type=positive_int, default=100)
    sessions_parser.add_argument("--json", action="store_true", dest="as_json")

    close_parser = subparsers.add_parser("close")
    close_parser.add_argument("tool", choices=TOOLS)
    close_parser.add_argument("target", help="Tracked PID, Codex session ID, or 'all'")
    close_parser.add_argument("--profile")

    for tool in TOOLS:
        tool_parser = subparsers.add_parser(
            tool,
            help=f"Run {tool}; optionally start with @PROFILE",
            add_help=False,
        )
        tool_parser.add_argument("args", nargs=argparse.REMAINDER)

    vscode_parser = subparsers.add_parser("vscode")
    vscode_parser.add_argument("profile")
    vscode_parser.add_argument("target", nargs="?", default=".")
    vscode_parser.add_argument("extra_args", nargs=argparse.REMAINDER)

    hook_parser = subparsers.add_parser("usage-hook")
    hook_parser.add_argument("tool", choices=("claude",))
    hook_parser.add_argument("profile")

    shell_parser = subparsers.add_parser("shell-init")
    shell_parser.add_argument("shell", choices=("bash", "zsh"))

    subparsers.add_parser("doctor", help="Check local platform and tool availability")

    return parser


def format_table(headers: Sequence[str], rows: Iterable[Sequence[Any]]) -> str:
    materialized = [list(row) for row in rows]

    def plain(cell: Any) -> str:
        return cell.text if isinstance(cell, StyledCell) else str(cell)

    widths = [len(header) for header in headers]
    for row in materialized:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(plain(cell)))
    header_line = "  ".join(header.ljust(widths[index]) for index, header in enumerate(headers))
    divider = "  ".join("-" * width for width in widths)
    if _COLOR_ENABLED:
        header_line = f"\033[1m{header_line}\033[0m"
        divider = f"\033[2m{divider}\033[0m"
    lines = [header_line, divider]
    for row in materialized:
        rendered: list[str] = []
        for index, cell in enumerate(row):
            padded = plain(cell).ljust(widths[index])
            if _COLOR_ENABLED and isinstance(cell, StyledCell):
                padded = f"\033[{cell.ansi}m{padded}\033[0m"
            rendered.append(padded)
        lines.append("  ".join(rendered))
    return "\n".join(lines)


def tool_cell(tool: str) -> StyledCell:
    return styled(tool, "36" if tool == "codex" else "35")


def percent_cell(value: str) -> StyledCell:
    try:
        percentage = float(value.rstrip("%"))
    except ValueError:
        return styled(value, "2")
    color = "32" if percentage < 50 else "33" if percentage < 80 else "31"
    return styled(value, color)


def usage_bar(value: str, width: int = 10) -> StyledCell:
    try:
        percentage = max(0.0, min(100.0, float(value.rstrip("%"))))
    except ValueError:
        return styled(value, "2")
    filled = round(percentage * width / 100)
    meter = f"{'█' * filled}{'░' * (width - filled)} {percentage:.0f}%"
    color = "32" if percentage < 50 else "33" if percentage < 80 else "31"
    return styled(meter, color)


def format_window_minutes(value: Any, fallback: str) -> str:
    try:
        minutes = int(value)
    except (TypeError, ValueError):
        return fallback
    if minutes % 1_440 == 0:
        return f"{minutes // 1_440}d"
    if minutes % 60 == 0:
        return f"{minutes // 60}h"
    return f"{minutes}m"


def format_cache_age(value: Any, *, now: datetime | None = None) -> str:
    if not value:
        return "cached"
    try:
        updated = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=UTC)
        seconds = max(0, int(((now or datetime.now(UTC)) - updated).total_seconds()))
    except (TypeError, ValueError):
        return "cached"
    if seconds < 60:
        return "cached just now"
    if seconds < 3_600:
        return f"cached {seconds // 60}m ago"
    if seconds < 86_400:
        return f"cached {seconds // 3_600}h ago"
    return f"cached {seconds // 86_400}d ago"


def format_reset_remaining(value: Any, *, now: datetime | None = None) -> str:
    if value in (None, ""):
        return "-"
    try:
        try:
            timestamp = float(value)
        except (TypeError, ValueError):
            target = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if target.tzinfo is None:
                target = target.replace(tzinfo=UTC)
        else:
            if timestamp > 100_000_000_000:
                timestamp /= 1_000
            target = datetime.fromtimestamp(timestamp, tz=UTC)
        seconds = max(0, int((target - (now or datetime.now(UTC))).total_seconds()))
    except (TypeError, ValueError, OSError):
        return "-"
    if seconds == 0:
        return "now"
    days, seconds = divmod(seconds, 86_400)
    hours, seconds = divmod(seconds, 3_600)
    minutes, seconds = divmod(seconds, 60)
    if days:
        return f"{days}d {hours}h" if hours else f"{days}d"
    if hours:
        return f"{hours}h {minutes}m" if minutes else f"{hours}h"
    if minutes:
        return f"{minutes}m"
    return f"{seconds}s"


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


def active_map(store: Store) -> dict[str, str]:
    return {
        tool: str(profile)
        for tool, profile in store.load_state()["active"].items()
        if tool in TOOLS
    }


def command_login(store: Store, args: argparse.Namespace) -> int:
    if args.device_auth and args.tool != "codex":
        raise AicxError("--device-auth is only valid for Codex")
    if (args.console or args.sso or args.email) and args.tool != "claude":
        raise AicxError("--console, --sso, and --email are only valid for Claude")
    extra: list[str] = []
    if args.device_auth:
        extra.append("--device-auth")
    if args.console:
        extra.append("--console")
    if args.sso:
        extra.append("--sso")
    if args.email:
        extra.extend(("--email", args.email))
    # Fail before creating a profile if the provider itself is not installed.
    find_binary(args.tool)
    status_before: dict[str, Any] | None = None
    store.create_profile(args.tool, args.profile)
    try:
        status_before = native_status(store, args.tool, args.profile)
    except AicxError:
        status_before = None
    if status_before and status_before["logged_in"] and not args.force:
        store.set_active(args.tool, args.profile)
        sync_history(store, args.tool)
        print(f"{args.tool}/{args.profile} is already logged in; selected it without re-authenticating.")
        return 0
    # Status was checked above so the provider wrapper must not check it a second time.
    code = login(store, args.tool, args.profile, force=True, extra_args=extra)
    if code == 0:
        if args.tool == "claude":
            hook_status = install_claude_usage_hook(store.tool_home("claude", args.profile))
            print(f"Claude usage collector: {hook_status}")
        store.set_active(args.tool, args.profile)
        sync_history(store, args.tool)
        print(f"Logged in and selected: {args.tool}/{args.profile}")
    return code


def command_adopt(store: Store, args: argparse.Namespace) -> int:
    source = args.source or default_home(args.tool)
    target = store.adopt(args.tool, args.profile, source)
    if args.tool == "claude":
        hook_status = install_claude_usage_hook(target)
        print(f"Claude usage collector: {hook_status}")
    store.set_active(args.tool, args.profile)
    sync_history(store, args.tool)
    print(f"Adopted {source} as {args.tool}/{args.profile}")
    print("Authentication and conversation history were copied together; the source was not changed.")
    return 0


def command_use(store: Store, args: argparse.Namespace) -> int:
    selection = args.selection
    if len(selection) == 1:
        profile = selection[0]
        matched = [tool for tool in TOOLS if store.profile_exists(tool, profile)]
        if not matched:
            raise AicxError(f"Profile '{profile}' does not contain Codex or Claude.")
        for tool in matched:
            store.set_active(tool, profile)
        print(f"Selected '{profile}' for: {', '.join(matched)}")
    elif len(selection) == 2:
        tool, profile = selection
        if tool not in TOOLS:
            raise AicxError("Two-argument form is: aicx use TOOL PROFILE")
        store.set_active(tool, profile)
        print(f"Selected {tool}/{profile}")
    else:
        raise AicxError("Usage: aicx use PROFILE  or  aicx use TOOL PROFILE")
    print("No login command was run. Existing sessions keep their original account.")
    return 0


def command_accounts(store: Store, args: argparse.Namespace) -> int:
    active = active_map(store)
    history_labels: dict[str, str] = {}
    for tool in TOOLS:
        count = shared_history_count(store, tool)
        history_labels[tool] = "isolated" if count is None else f"shared · {count}"
    records: list[dict[str, Any]] = []
    for tool, profile in store.list_profiles(args.tool):
        try:
            status = native_status(store, tool, profile)
            auth = "logged in" if status["logged_in"] else "not logged in"
            detail = status.get("email") or status.get("plan") or status.get("detail") or ""
            if tool == "codex" and status["logged_in"]:
                try:
                    account = codex_account(store, profile)
                except AicxError:
                    account = None
                if isinstance(account, dict) and account.get("email"):
                    detail = str(account["email"])
        except AicxError as exc:
            auth = "unavailable"
            detail = str(exc)
        records.append(
            {
                "tool": tool,
                "profile": profile,
                "active": active.get(tool) == profile,
                "auth": auth,
                "detail": detail,
                "history": history_labels[tool],
            }
        )
    if args.as_json:
        print(json.dumps(records, indent=2))
    else:
        print(
            format_table(
                ("TOOL", "PROFILE", "ACTIVE", "AUTH", "HISTORY", "DETAIL"),
                (
                    (
                        tool_cell(record["tool"]),
                        record["profile"],
                        styled("●", "32") if record["active"] else "",
                        styled(
                            record["auth"],
                            "32" if record["auth"] == "logged in" else "31",
                        ),
                        styled(record["history"], "36"),
                        record["detail"],
                    )
                    for record in records
                ),
            )
        )
    return 0


def profiles_for_command(store: Store, tool: str, profile: str | None) -> list[str]:
    if profile:
        if not store.profile_exists(tool, profile):
            raise AicxError(f"Profile '{profile}' does not contain {tool}.")
        return [profile]
    return [name for candidate, name in store.list_profiles(tool) if candidate == tool]


def balance_records(store: Store, args: argparse.Namespace) -> list[dict[str, Any]]:
    tools = (args.tool,) if args.tool else TOOLS
    active = active_map(store)
    records: list[dict[str, Any]] = []
    for tool in tools:
        if args.profile and not store.profile_exists(tool, args.profile):
            if args.tool:
                raise AicxError(f"Profile '{args.profile}' does not contain {tool}.")
            continue
        for profile in profiles_for_command(store, tool, args.profile):
            if tool == "codex":
                try:
                    data = codex_balance(store, profile)
                    account = data.get("account")
                    identity = ""
                    if isinstance(account, dict):
                        identity = str(account.get("email") or account.get("type") or "")
                    limits = data.get("limits") or []
                    if not limits:
                        records.append(
                            {
                                "tool": tool,
                                "profile": profile,
                                "active": active.get(tool) == profile,
                                "account": identity,
                                "window": "-",
                                "used": "-",
                                "left": "-",
                                "resets": "-",
                                "resets_in": "-",
                                "freshness": data.get("status", "unavailable"),
                            }
                        )
                    for bucket in limits:
                        if not isinstance(bucket, dict):
                            continue
                        for label in ("primary", "secondary"):
                            window = bucket.get(label)
                            if not isinstance(window, dict):
                                continue
                            duration = window.get("windowDurationMins")
                            used_percentage = float(window.get("usedPercent", 0))
                            records.append(
                                {
                                    "tool": tool,
                                    "profile": profile,
                                    "active": active.get(tool) == profile,
                                    "account": identity,
                                    "window": format_window_minutes(duration, label),
                                    "used": f"{used_percentage:.0f}%",
                                    "left": f"{max(0, 100 - used_percentage):.0f}%",
                                    "resets": format_timestamp(window.get("resetsAt")),
                                    "resets_in": format_reset_remaining(window.get("resetsAt")),
                                    "freshness": "live",
                                }
                            )
                except AicxError as exc:
                    records.append(
                        {
                            "tool": tool,
                            "profile": profile,
                            "active": active.get(tool) == profile,
                            "account": "",
                            "window": "-",
                            "used": "-",
                            "left": "-",
                            "resets": "-",
                            "resets_in": "-",
                            "freshness": str(exc),
                        }
                    )
            else:
                data = read_claude_balance(store, profile)
                try:
                    claude_status = native_status(store, "claude", profile)
                    identity = str(
                        claude_status.get("email")
                        or claude_status.get("plan")
                        or ""
                    )
                except AicxError:
                    identity = ""
                limits = data.get("limits") or {}
                if not limits:
                    records.append(
                        {
                            "tool": tool,
                            "profile": profile,
                            "active": active.get(tool) == profile,
                            "account": identity,
                            "window": "-",
                            "used": "-",
                            "left": "-",
                            "resets": "-",
                            "resets_in": "-",
                            "freshness": data.get("status", "unavailable"),
                        }
                    )
                windows = (
                    ("five_hour", "5h"),
                    ("seven_day", "7d"),
                    ("seven_day_sonnet", "7d Sonnet"),
                    ("seven_day_opus", "7d Opus"),
                    ("overage", "overage"),
                )
                for key, label in windows:
                    window = limits.get(key)
                    if not isinstance(window, dict):
                        continue
                    used_percentage = float(window.get("used_percentage", 0))
                    records.append(
                        {
                            "tool": tool,
                            "profile": profile,
                            "active": active.get(tool) == profile,
                            "account": identity,
                            "window": label,
                            "used": f"{used_percentage:.0f}%",
                            "left": f"{max(0, 100 - used_percentage):.0f}%",
                            "resets": format_timestamp(window.get("resets_at")),
                            "resets_in": format_reset_remaining(window.get("resets_at")),
                            "freshness": format_cache_age(data.get("updated_at")),
                        }
                    )
    return records


def render_balance(records: list[dict[str, Any]], *, as_json: bool) -> str:
    if as_json:
        return json.dumps(records, indent=2)
    return format_table(
        (
            "TOOL", "PROFILE", "ACTIVE", "ACCOUNT", "WINDOW", "USAGE",
            "LEFT", "RESET IN", "RESET (LOCAL)", "SOURCE",
        ),
        (
            (
                tool_cell(row["tool"]),
                row["profile"],
                styled("●", "32") if row["active"] else "",
                row["account"],
                row["window"],
                usage_bar(row["used"]),
                percent_cell(row["left"]),
                row["resets_in"],
                row["resets"],
                styled(
                    row["freshness"],
                    "32" if row["freshness"] == "live" else "36",
                ),
            )
            for row in records
        ),
    )


def command_balance(store: Store, args: argparse.Namespace) -> int:
    watch = bool(getattr(args, "watch", False))
    interval = int(getattr(args, "interval", 60))
    if watch and args.as_json:
        raise AicxError("--json cannot be combined with --watch")
    first = True
    while True:
        if watch and sys.stdout.isatty():
            print("\033[2J\033[H", end="")
        elif watch and not first:
            print()
        if watch:
            updated = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")
            print(f"Updated {updated} - refreshing every {interval}s - Ctrl-C to stop\n")
        print(render_balance(balance_records(store, args), as_json=args.as_json))
        if not watch:
            return 0
        first = False
        try:
            time.sleep(interval)
        except KeyboardInterrupt:
            return 0


def command_sessions(store: Store, args: argparse.Namespace) -> int:
    if args.profile and args.all_profiles:
        raise AicxError("Use either --profile or --all-profiles, not both")
    sync_history(store, args.tool)
    if args.profile:
        profiles = profiles_for_command(store, args.tool, args.profile)
    elif args.all_profiles:
        profiles = profiles_for_command(store, args.tool, None)
    else:
        profiles = [store.get_active(args.tool)]
    records: list[dict[str, Any]] = []
    registry = ProcessRegistry(store)
    history_profiles = profiles[:1] if history_is_shared(args.tool) else profiles
    for profile in history_profiles:
        provider_sessions = (
            codex_sessions(store, profile, args.limit)
            if args.tool == "codex"
            else claude_sessions(store, profile, args.limit)
        )
        for session in provider_sessions:
            history_label = args.profile or (
                "shared" if history_is_shared(args.tool) else profile
            )
            records.append({"kind": "session", "profile": history_label, **session})
    for profile in profiles:
        for process in registry.list(tool=args.tool, profile=profile):
            records.append(
                {
                    "kind": "process",
                    "profile": profile,
                    "id": str(process["pid"]),
                    "name": " ".join(process.get("command", [])[1:3]),
                    "state": "running",
                    "updated_at": process.get("started_at"),
                    "cwd": "",
                }
            )
    if args.as_json:
        print(json.dumps(records, indent=2))
    else:
        print(
            format_table(
                ("KIND", "PROFILE", "ID/PID", "STATE", "UPDATED", "NAME/PROJECT"),
                (
                    (
                        row["kind"], row["profile"], row["id"], row["state"],
                        format_timestamp(row["updated_at"]), row["name"] or row["cwd"],
                    )
                    for row in records
                ),
            )
        )
    return 0


def command_close(store: Store, args: argparse.Namespace) -> int:
    profile = args.profile or store.get_active(args.tool)
    actions: list[str] = []
    if args.target == "all":
        registry = ProcessRegistry(store)
        if registry.list(tool=args.tool, profile=profile):
            pids = registry.terminate(args.tool, "all", profile)
            actions.extend(f"sent SIGTERM to PID {pid}" for pid in pids)
        if args.tool == "codex":
            interrupted = interrupt_all_codex_sessions(store, profile)
            actions.extend(f"interrupted Codex session {session_id}" for session_id in interrupted)
    elif args.target.isdigit():
        pids = ProcessRegistry(store).terminate(args.tool, args.target, profile)
        actions.extend(f"sent SIGTERM to PID {pid}" for pid in pids)
    elif args.tool == "codex":
        try:
            interrupt_codex_session(store, profile, args.target)
        except ValueError as exc:
            raise AicxError(str(exc)) from exc
        actions.append(f"interrupted Codex session {args.target}")
    else:
        raise AicxError("Claude session IDs are saved histories; close an active tracked PID instead.")
    if not actions:
        raise AicxError(f"No active {args.tool}/{profile} sessions or processes matched.")
    print("\n".join(actions))
    return 0


def command_tool(store: Store, tool: str, raw_args: Sequence[str]) -> int:
    passthrough = list(raw_args)
    if passthrough[:1] == ["--"]:
        passthrough = passthrough[1:]
    requested_profile: str | None = None
    if passthrough and passthrough[0].startswith("@"):
        requested_profile = passthrough.pop(0)[1:]
        if not requested_profile:
            raise AicxError(f"Usage: aicx {tool} @PROFILE [arguments]")
        if not store.profile_exists(tool, requested_profile):
            available = [
                profile
                for candidate, profile in store.list_profiles(tool)
                if candidate == tool
            ]
            suffix = f" Available: {', '.join(available)}" if available else ""
            raise AicxError(
                f"Profile '{requested_profile}' does not contain {tool}.{suffix}"
            )
        store.set_active(tool, requested_profile)
    sync_history(store, tool)
    command, env, profile = build_tool_command(
        store,
        tool,
        passthrough,
        profile=requested_profile,
    )
    history_label = "shared history" if history_is_shared(tool) else "isolated history"
    print(f"Launching {tool}/{profile} · {history_label}", flush=True)
    try:
        return run_tracked(store, tool, profile, command, env)
    finally:
        sync_history(store, tool)


def command_sync(store: Store, args: argparse.Namespace) -> int:
    result = sync_history(store, args.tool)
    print(
        f"{args.tool.capitalize()} history synchronized across {result.profiles} profiles "
        f"({result.imported} imported, {result.distributed} distributed)."
    )
    if result.skipped_active_profiles:
        print(
            f"Skipped {result.skipped_active_profiles} active profile(s); "
            "they will synchronize after their processes exit."
        )
    return 0


def command_vscode(store: Store, args: argparse.Namespace) -> int:
    extra = list(args.extra_args)
    if extra[:1] == ["--"]:
        extra = extra[1:]
    pid = launch_vscode(store, args.profile, args.target, extra)
    print(f"Opened isolated VS Code profile '{args.profile}' (launcher PID {pid}).")
    print("This does not change existing VS Code windows and does not run a login command.")
    return 0


def command_shell_init(args: argparse.Namespace) -> int:
    print("# aicx shell integration")
    print("codex() { command aicx codex \"$@\"; }")
    print("claude() { command aicx claude \"$@\"; }")
    return 0


def command_doctor(store: Store) -> int:
    rows: list[tuple[str, str, str]] = []
    linux = platform.system() == "Linux"
    rows.append(("platform", platform.platform(), "ok" if linux else "unsupported in 0.3"))
    for tool in TOOLS:
        binary = shutil.which(tool)
        version = "not found"
        status = "missing"
        if binary:
            result = subprocess.run(
                [binary, "--version"], text=True, capture_output=True, timeout=10, check=False
            )
            version_output = (result.stdout or result.stderr).strip().splitlines()
            version = version_output[0] if version_output else binary
            status = "ok" if result.returncode == 0 else "error"
        rows.append((tool, version, status))
    credential_overrides = [
        name
        for name in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY")
        if os.environ.get(name)
    ]
    rows.append(
        (
            "credential env",
            ", ".join(credential_overrides) if credential_overrides else "none detected",
            "warning" if credential_overrides else "ok",
        )
    )
    vscode_binary = shutil.which("code")
    rows.append(
        (
            "vscode",
            vscode_binary or "not found (optional dependency)",
            "ok" if vscode_binary else "missing (optional)",
        )
    )
    if vscode_binary:
        launch_context, launchable = vscode_launch_context()
        rows.append(
            (
                "vscode launch",
                launch_context,
                "ok" if launchable else "unavailable here",
            )
        )
    rows.append(("aicx home", str(store.root), "ok"))
    print(format_table(("COMPONENT", "DETAIL", "STATUS"), rows))
    return 0 if linux else 1


def dispatch(store: Store, args: argparse.Namespace) -> int:
    if args.command == "login":
        return command_login(store, args)
    if args.command == "adopt":
        return command_adopt(store, args)
    if args.command == "use":
        return command_use(store, args)
    if args.command == "accounts":
        return command_accounts(store, args)
    if args.command == "sync":
        return command_sync(store, args)
    if args.command == "balance":
        return command_balance(store, args)
    if args.command == "sessions":
        return command_sessions(store, args)
    if args.command == "close":
        return command_close(store, args)
    if args.command in TOOLS:
        return command_tool(store, args.command, args.args)
    if args.command == "vscode":
        return command_vscode(store, args)
    if args.command == "usage-hook":
        home = store.tool_home("claude", args.profile)
        if not home.is_dir():
            raise AicxError(f"Profile '{args.profile}' does not contain Claude.")
        print(install_claude_usage_hook(home))
        return 0
    if args.command == "shell-init":
        return command_shell_init(args)
    if args.command == "doctor":
        return command_doctor(store)
    raise AicxError(f"Unknown command: {args.command}")


def main(argv: Sequence[str] | None = None) -> int:
    raw_arguments = list(sys.argv[1:] if argv is None else argv)
    if raw_arguments == ["_claude-statusline"]:
        try:
            payload = json.load(sys.stdin)
            print(capture_claude_statusline(payload))
            return 0
        except (AicxError, OSError, json.JSONDecodeError) as exc:
            print(f"aicx: {exc}", file=sys.stderr)
            return 2
    parser = build_parser()
    args = parser.parse_args(raw_arguments)
    configure_color(args.color)
    try:
        if platform.system() != "Linux" and args.command not in {"doctor", "--version"}:
            raise AicxError(
                f"aicx {__version__} supports Linux only; macOS support is planned next."
            )
        return dispatch(Store(), args)
    except (AicxError, OSError) as exc:
        print(f"aicx: {exc}", file=sys.stderr)
        return 2
