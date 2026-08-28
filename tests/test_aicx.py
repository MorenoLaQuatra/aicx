from __future__ import annotations

import argparse
import json
import os
import sqlite3
import stat
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from aicx.cli import (
    BALANCE_COLUMNS_PREFERENCE,
    build_parser,
    command_balance,
    command_forget,
    command_rename,
    command_tool,
    configure_color,
    format_cache_age,
    format_reset_remaining,
    format_table,
    format_window_minutes,
    main,
    parse_balance_columns,
    usage_bar,
)
from aicx.codex_rpc import CodexAppServer
from aicx.errors import AicxError
from aicx.history import (
    codex_history_is_shared,
    shared_history_count,
    sync_codex_history,
    sync_history,
)
from aicx.processes import ProcessRegistry
from aicx.providers import native_status, profile_env
from aicx.sessions import claude_sessions
from aicx.store import Store, ensure_codex_file_auth
from aicx.usage import (
    capture_claude_statusline,
    install_claude_usage_hook,
    read_claude_balance,
)


class TemporaryStoreTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = Store(self.root / "aicx")

    def tearDown(self) -> None:
        self.temporary.cleanup()


class PresentationTests(unittest.TestCase):
    def tearDown(self) -> None:
        configure_color("never")

    def test_provider_windows_use_human_units(self) -> None:
        self.assertEqual(format_window_minutes(300, "primary"), "5h")
        self.assertEqual(format_window_minutes(10_080, "secondary"), "7d")

    def test_usage_bar_shows_used_percentage_graphically(self) -> None:
        self.assertEqual(usage_bar("40%").text, "████░░░░░░ 40%")

    def test_cached_timestamp_becomes_relative_age(self) -> None:
        now = datetime.fromisoformat("2026-08-27T10:30:00+00:00")
        self.assertEqual(
            format_cache_age("2026-08-27T10:25:00+00:00", now=now),
            "cached 5m ago",
        )

    def test_reset_timestamp_becomes_a_human_countdown(self) -> None:
        now = datetime.fromisoformat("2026-08-27T10:00:00+00:00")
        reset = datetime.fromisoformat("2026-08-29T13:15:00+00:00").timestamp()
        self.assertEqual(format_reset_remaining(reset, now=now), "2d 3h")

    def test_color_can_be_forced_and_does_not_change_table_widths(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("NO_COLOR", None)
            configure_color("always")
            rendered = format_table(("USAGE",), ((usage_bar("50%"),),))
        self.assertIn("\033[", rendered)
        self.assertIn("█████░░░░░ 50%", rendered)

    def test_main_help_contains_the_workflow_cheatsheet(self) -> None:
        help_text = build_parser().format_help()
        self.assertIn("DAILY USE", help_text)
        self.assertIn("aicx codex @work", help_text)
        self.assertIn("aicx balance", help_text)


class BalancePresentationTests(TemporaryStoreTestCase):
    def test_balance_is_normalized_graphical_and_identifies_claude(self) -> None:
        self.store.create_profile("codex", "personal")
        claude_home = self.store.create_profile("claude", "personal")
        self.store.set_active("codex", "personal")
        self.store.set_active("claude", "personal")
        (claude_home / "aicx-usage.json").write_text(
            json.dumps(
                {
                    "limits": {
                        "five_hour": {
                            "used_percentage": 11,
                            "resets_at": 1_800_000_000,
                        }
                    },
                    "updated_at": datetime.now().astimezone().isoformat(),
                }
            )
        )
        codex_data = {
            "account": {"email": "personal@example.com"},
            "limits": [
                {
                    "primary": {
                        "windowDurationMins": 300,
                        "usedPercent": 40,
                        "resetsAt": 1_800_000_000,
                    },
                    "secondary": {
                        "windowDurationMins": 10_080,
                        "usedPercent": 52,
                        "resetsAt": 1_800_000_000,
                    },
                }
            ],
        }
        output = StringIO()
        args = argparse.Namespace(tool=None, profile=None, as_json=False, watch=False, interval=60)
        configure_color("never")
        with patch("aicx.cli.codex_balance", return_value=codex_data), patch(
            "aicx.cli.native_status",
            return_value={"logged_in": True, "email": "claude@example.com"},
        ), redirect_stdout(output):
            result = command_balance(self.store, args)

        rendered = output.getvalue()
        self.assertEqual(result, 0)
        self.assertIn("5h", rendered)
        self.assertIn("7d", rendered)
        self.assertNotIn("300m", rendered)
        self.assertIn("████░░░░░░ 40%", rendered)
        self.assertIn("60%", rendered)
        self.assertIn("claude@example.com", rendered)
        self.assertIn("cached just now", rendered)
        self.assertIn("RESET IN", rendered)

    def test_balance_watch_refreshes_until_interrupted(self) -> None:
        self.store.create_profile("codex", "personal")
        args = argparse.Namespace(
            tool="codex", profile=None, as_json=False, watch=True, interval=7
        )
        output = StringIO()
        data = {"account": None, "limits": [], "status": "not logged in"}
        with patch("aicx.cli.codex_balance", return_value=data) as balance, patch(
            "aicx.cli.time.sleep", side_effect=[None, KeyboardInterrupt]
        ), redirect_stdout(output):
            result = command_balance(self.store, args)

        self.assertEqual(result, 0)
        self.assertEqual(balance.call_count, 2)
        self.assertIn("refreshing every 7s", output.getvalue())

    def _run_balance(self, **overrides: object) -> str:
        args = argparse.Namespace(
            tool="codex",
            profile=None,
            as_json=False,
            watch=False,
            interval=60,
            columns=None,
            reset_columns=False,
        )
        for key, value in overrides.items():
            setattr(args, key, value)
        codex_data = {
            "account": {"email": "p@example.com"},
            "limits": [
                {"primary": {"windowDurationMins": 300, "usedPercent": 40, "resetsAt": 1_800_000_000}}
            ],
        }
        output = StringIO()
        configure_color("never")
        with patch("aicx.cli.codex_balance", return_value=codex_data), redirect_stdout(output):
            self.assertEqual(command_balance(self.store, args), 0)
        return output.getvalue()

    def test_columns_choice_is_applied_and_remembered(self) -> None:
        self.store.create_profile("codex", "personal")

        chosen = self._run_balance(columns="tool,profile,window,usage,reset-in")
        self.assertIn("RESET IN", chosen)
        self.assertNotIn("ACCOUNT", chosen)
        self.assertNotIn("SOURCE", chosen)
        self.assertEqual(
            self.store.get_preference(BALANCE_COLUMNS_PREFERENCE),
            ["tool", "profile", "window", "usage", "reset-in"],
        )

        remembered = self._run_balance()
        self.assertNotIn("ACCOUNT", remembered)
        self.assertIn("RESET IN", remembered)

        full = self._run_balance(reset_columns=True)
        self.assertIn("ACCOUNT", full)
        self.assertIn("SOURCE", full)
        self.assertIsNone(self.store.get_preference(BALANCE_COLUMNS_PREFERENCE))

    def test_parse_balance_columns_normalizes_and_rejects_unknown(self) -> None:
        self.assertEqual(
            parse_balance_columns("USAGE, Reset_In , tool"),
            ["tool", "usage", "reset-in"],
        )
        with self.assertRaisesRegex(AicxError, "Unknown balance column"):
            parse_balance_columns("tool,bogus")


class AccountPresentationTests(TemporaryStoreTestCase):
    def test_codex_account_detail_prefers_email_over_login_method(self) -> None:
        self.store.create_profile("codex", "personal")
        args = argparse.Namespace(tool="codex", as_json=True)
        output = StringIO()
        with patch(
            "aicx.cli.native_status",
            return_value={"logged_in": True, "detail": "Logged in using ChatGPT"},
        ), patch(
            "aicx.cli.codex_account",
            return_value={"email": "person@example.com"},
        ), redirect_stdout(output):
            from aicx.cli import command_accounts

            result = command_accounts(self.store, args)

        self.assertEqual(result, 0)
        records = json.loads(output.getvalue())
        self.assertEqual(records[0]["detail"], "person@example.com")


class StoreTests(TemporaryStoreTestCase):
    def test_codex_profile_is_private_and_uses_profile_local_auth(self) -> None:
        home = self.store.create_profile("codex", "work")

        self.assertEqual(stat.S_IMODE(home.stat().st_mode), 0o700)
        self.assertIn(
            'cli_auth_credentials_store = "file"',
            (home / "config.toml").read_text(encoding="utf-8"),
        )

    def test_codex_auth_setting_is_inserted_at_toml_root(self) -> None:
        config = self.root / "config.toml"
        config.write_text('model = "gpt-test"\n[features]\nfoo = true\n', encoding="utf-8")

        ensure_codex_file_auth(config)
        ensure_codex_file_auth(config)

        text = config.read_text(encoding="utf-8")
        self.assertEqual(text.count("cli_auth_credentials_store"), 1)
        self.assertLess(text.index("cli_auth_credentials_store"), text.index("[features]"))

    def test_adopt_copies_credentials_and_history_without_changing_source(self) -> None:
        source = self.root / "old-codex"
        (source / "sessions" / "2026").mkdir(parents=True)
        (source / "auth.json").write_text('{"token":"secret"}\n', encoding="utf-8")
        session = source / "sessions" / "2026" / "thread.jsonl"
        session.write_text('{"message":"hello"}\n', encoding="utf-8")
        os.mkfifo(source / "transient.pipe")

        target = self.store.adopt("codex", "personal", source)

        self.assertEqual((target / "auth.json").read_text(), '{"token":"secret"}\n')
        self.assertEqual((target / "sessions" / "2026" / "thread.jsonl").read_text(), session.read_text())
        self.assertTrue(source.exists())
        self.assertTrue(session.exists())
        self.assertFalse((target / "transient.pipe").exists())

    def test_adopt_rebases_codex_thread_paths_to_the_profile_home(self) -> None:
        source = self.root / ".codex"
        session_id = "01a03cc1-2ff1-72d2-902d-e9208da115e2"
        relative = Path(
            "sessions/2026/08/26/"
            f"rollout-2026-08-26T08-28-08-{session_id}.jsonl"
        )
        rollout = source / relative
        rollout.parent.mkdir(parents=True)
        rollout.write_text('{"type":"session_meta"}\n', encoding="utf-8")
        database = source / "state_5.sqlite"
        with sqlite3.connect(database) as connection:
            connection.execute(
                "CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT NOT NULL, archived INTEGER NOT NULL)"
            )
            connection.execute(
                "INSERT INTO threads (id, rollout_path, archived) VALUES (?, ?, 0)",
                (session_id, str(rollout)),
            )

        target = self.store.adopt("codex", "personal", source)

        with sqlite3.connect(target / "state_5.sqlite") as connection:
            stored_path = connection.execute(
                "SELECT rollout_path FROM threads WHERE id = ?", (session_id,)
            ).fetchone()[0]
        self.assertEqual(stored_path, str(target / relative))

    def test_use_only_changes_the_pointer(self) -> None:
        self.store.create_profile("codex", "work")
        self.store.create_profile("claude", "work")

        output = StringIO()
        with patch.dict(os.environ, {"AICX_HOME": str(self.store.root)}), patch(
            "aicx.cli.login", side_effect=AssertionError("login must not run")
        ), redirect_stdout(output):
            result = main(["use", "work"])

        self.assertEqual(result, 0)
        self.assertEqual(self.store.get_active("codex"), "work")
        self.assertEqual(self.store.get_active("claude"), "work")
        self.assertIn("No login command was run", output.getvalue())

    def test_adopt_claude_copies_the_default_companion_config(self) -> None:
        source = self.root / ".claude"
        source.mkdir()
        (source / ".credentials.json").write_text('{"oauth":"credential"}\n')
        companion = self.root / ".claude.json"
        companion.write_text('{"hasCompletedOnboarding":true}\n')

        target = self.store.adopt("claude", "personal", source)

        self.assertEqual((target / ".claude.json").read_text(), companion.read_text())

    def test_profile_environment_selects_provider_homes(self) -> None:
        codex_home = self.store.create_profile("codex", "hobby")
        claude_home = self.store.create_profile("claude", "hobby")

        self.assertEqual(profile_env(self.store, "codex", "hobby")["CODEX_HOME"], str(codex_home))
        self.assertEqual(
            profile_env(self.store, "claude", "hobby")["CLAUDE_CONFIG_DIR"],
            str(claude_home),
        )
        self.assertEqual(profile_env(self.store, "codex", "hobby")["AICX_PROFILE"], "hobby")

    def test_at_profile_selects_and_launches_in_one_command(self) -> None:
        self.store.create_profile("codex", "personal")
        work = self.store.create_profile("codex", "work")
        self.store.set_active("codex", "personal")
        output = StringIO()

        with patch("aicx.cli.sync_history"), patch(
            "aicx.providers.find_binary", return_value="/usr/bin/codex"
        ), patch("aicx.cli.run_tracked", return_value=0) as run, redirect_stdout(output):
            result = command_tool(self.store, "codex", ["@work", "resume", "--last"])

        self.assertEqual(result, 0)
        self.assertEqual(self.store.get_active("codex"), "work")
        self.assertEqual(run.call_args.args[2], "work")
        self.assertEqual(run.call_args.args[3], ["/usr/bin/codex", "resume", "--last"])
        self.assertEqual(run.call_args.args[4]["CODEX_HOME"], str(work))
        self.assertIn("Launching codex/work · shared history", output.getvalue())

    def test_unknown_at_profile_is_a_clear_error(self) -> None:
        self.store.create_profile("codex", "personal")

        with self.assertRaisesRegex(AicxError, "Available: personal"):
            command_tool(self.store, "codex", ["@missing"])


class ForgetAndRenameTests(TemporaryStoreTestCase):
    def test_forget_removes_one_tool_and_clears_the_active_pointer(self) -> None:
        self.store.create_profile("codex", "personal")
        self.store.create_profile("claude", "personal")
        self.store.set_active("claude", "personal")

        args = argparse.Namespace(tool="claude", profile="personal", yes=True)
        output = StringIO()
        with redirect_stdout(output):
            self.assertEqual(command_forget(self.store, args), 0)

        self.assertFalse(self.store.profile_exists("claude", "personal"))
        self.assertTrue(self.store.profile_exists("codex", "personal"))
        self.assertNotIn("claude", self.store.load_state()["active"])
        with self.assertRaises(AicxError):
            command_forget(self.store, args)

    def test_forget_refuses_while_a_process_is_running(self) -> None:
        self.store.create_profile("codex", "work")
        registry = ProcessRegistry(self.store)
        registry.add(os.getpid(), "codex", "work", ["codex"])
        try:
            args = argparse.Namespace(tool="codex", profile="work", yes=True)
            with self.assertRaisesRegex(AicxError, "running process"):
                command_forget(self.store, args)
        finally:
            registry.remove(os.getpid())

    def test_rename_moves_the_home_marker_and_active_pointer(self) -> None:
        home = self.store.create_profile("claude", "test1")
        (home / "projects").mkdir()
        (home / "projects" / "session.jsonl").write_text("{}\n", encoding="utf-8")
        self.store.set_active("claude", "test1")

        args = argparse.Namespace(tool="claude", old="test1", new="personal")
        output = StringIO()
        with redirect_stdout(output):
            self.assertEqual(command_rename(self.store, args), 0)

        self.assertFalse(self.store.profile_exists("claude", "test1"))
        renamed = self.store.tool_home("claude", "personal")
        self.assertEqual(
            (renamed / "projects" / "session.jsonl").read_text(), "{}\n"
        )
        marker = json.loads((renamed / ".aicx-profile.json").read_text())
        self.assertEqual(marker["profile"], "personal")
        self.assertEqual(self.store.get_active("claude"), "personal")

    def test_forget_needs_confirmation_when_not_a_tty(self) -> None:
        self.store.create_profile("codex", "work")
        args = argparse.Namespace(tool="codex", profile="work", yes=False)
        with patch("aicx.cli.sys.stdin") as stdin:
            stdin.isatty.return_value = False
            with self.assertRaisesRegex(AicxError, "confirmation"):
                command_forget(self.store, args)
        self.assertTrue(self.store.profile_exists("codex", "work"))

    def test_rename_refuses_to_overwrite_an_existing_profile(self) -> None:
        self.store.create_profile("claude", "test1")
        self.store.create_profile("claude", "personal")

        args = argparse.Namespace(tool="claude", old="test1", new="personal")
        with self.assertRaisesRegex(AicxError, "already contains"):
            command_rename(self.store, args)


class SharedHistoryTests(TemporaryStoreTestCase):
    def test_codex_rollouts_are_shared_without_copying_credentials(self) -> None:
        personal = self.store.create_profile("codex", "personal")
        work = self.store.create_profile("codex", "work")
        (personal / "auth.json").write_text("personal-secret")
        (work / "auth.json").write_text("work-secret")
        rollout = personal / "sessions" / "2026" / "08" / "thread.jsonl"
        rollout.parent.mkdir(parents=True)
        rollout.write_text('{"message":"personal conversation"}\n')

        result = sync_codex_history(self.store)

        copied = work / "sessions" / "2026" / "08" / "thread.jsonl"
        self.assertEqual(copied.read_text(), rollout.read_text())
        self.assertEqual((personal / "auth.json").read_text(), "personal-secret")
        self.assertEqual((work / "auth.json").read_text(), "work-secret")
        self.assertEqual(result.profiles, 2)
        self.assertEqual(shared_history_count(self.store, "codex"), 1)

    def test_newer_continuation_is_merged_back_to_every_profile(self) -> None:
        personal = self.store.create_profile("codex", "personal")
        work = self.store.create_profile("codex", "work")
        relative = Path("sessions/2026/08/thread.jsonl")
        personal_rollout = personal / relative
        personal_rollout.parent.mkdir(parents=True)
        personal_rollout.write_text("first turn\n")
        sync_codex_history(self.store)

        work_rollout = work / relative
        work_rollout.write_text("first turn\nwork continuation\n")
        newer = personal_rollout.stat().st_mtime_ns + 2_000_000_000
        os.utime(work_rollout, ns=(newer, newer))
        sync_codex_history(self.store)

        self.assertEqual(personal_rollout.read_text(), work_rollout.read_text())

    def test_codex_sync_repairs_a_stale_existing_thread_path(self) -> None:
        personal = self.store.create_profile("codex", "personal")
        work = self.store.create_profile("codex", "work")
        session_id = "01a03cc1-2ff1-72d2-902d-e9208da115e2"
        relative = Path(
            "sessions/2026/08/26/"
            f"rollout-2026-08-26T08-28-08-{session_id}.jsonl"
        )
        work_rollout = work / relative
        work_rollout.parent.mkdir(parents=True)
        work_rollout.write_text("latest continuation\n", encoding="utf-8")
        stale_rollout = self.root / ".codex" / relative
        stale_rollout.parent.mkdir(parents=True)
        stale_rollout.write_text("stale continuation\n", encoding="utf-8")
        database = personal / "state_5.sqlite"
        with sqlite3.connect(database) as connection:
            connection.execute(
                "CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT NOT NULL, archived INTEGER NOT NULL)"
            )
            connection.execute(
                "INSERT INTO threads (id, rollout_path, archived) VALUES (?, ?, 0)",
                (session_id, str(stale_rollout)),
            )

        sync_codex_history(self.store)

        personal_rollout = personal / relative
        self.assertEqual(personal_rollout.read_text(), work_rollout.read_text())
        with sqlite3.connect(database) as connection:
            stored_path = connection.execute(
                "SELECT rollout_path FROM threads WHERE id = ?", (session_id,)
            ).fetchone()[0]
        self.assertEqual(stored_path, str(personal_rollout))

    def test_codex_sync_updates_a_stale_paginated_history_projection(self) -> None:
        personal = self.store.create_profile("codex", "personal")
        work = self.store.create_profile("codex", "work")
        session_id = "01a03cc1-2ff1-72d2-902d-e9208da115e2"
        relative = Path(
            "sessions/2026/08/26/"
            f"rollout-2026-08-26T08-28-08-{session_id}.jsonl"
        )
        work_rollout = work / relative
        work_rollout.parent.mkdir(parents=True)
        work_rollout.write_text("old prefix\nnew continuation\n", encoding="utf-8")
        personal_rollout = personal / relative
        personal_rollout.parent.mkdir(parents=True)
        personal_rollout.write_text("old prefix\nold continuation\n", encoding="utf-8")
        personal_history = personal / "thread_history_1.sqlite"
        work_history = work / "thread_history_1.sqlite"
        for database, rollout, marker, ordinal in (
            (personal_history, personal_rollout, "stale", 1),
            (work_history, work_rollout, "latest", 2),
        ):
            with sqlite3.connect(database) as connection:
                connection.execute(
                    "CREATE TABLE thread_history_projection_state "
                    "(thread_id TEXT PRIMARY KEY, next_rollout_byte_offset INTEGER NOT NULL, "
                    "next_rollout_ordinal INTEGER NOT NULL)"
                )
                connection.execute(
                    "CREATE TABLE thread_turns (thread_id TEXT NOT NULL, marker TEXT)"
                )
                connection.execute(
                    "CREATE TABLE thread_items (thread_id TEXT NOT NULL, marker TEXT)"
                )
                connection.execute(
                    "CREATE TABLE thread_realtime_items "
                    "(thread_id TEXT NOT NULL, marker TEXT)"
                )
                connection.execute(
                    "INSERT INTO thread_history_projection_state VALUES (?, ?, ?)",
                    (session_id, rollout.stat().st_size, ordinal),
                )
                connection.execute(
                    "INSERT INTO thread_turns VALUES (?, ?)", (session_id, marker)
                )
                connection.execute(
                    "INSERT INTO thread_items VALUES (?, ?)", (session_id, marker)
                )
        newer = personal_rollout.stat().st_mtime_ns + 2_000_000_000
        os.utime(work_rollout, ns=(newer, newer))

        sync_codex_history(self.store)

        self.assertEqual(personal_rollout.read_text(), work_rollout.read_text())
        with sqlite3.connect(personal_history) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT next_rollout_byte_offset FROM thread_history_projection_state "
                    "WHERE thread_id = ?",
                    (session_id,),
                ).fetchone()[0],
                work_rollout.stat().st_size,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT marker FROM thread_turns WHERE thread_id = ?",
                    (session_id,),
                ).fetchone()[0],
                "latest",
            )
            self.assertEqual(
                connection.execute(
                    "SELECT marker FROM thread_items WHERE thread_id = ?",
                    (session_id,),
                ).fetchone()[0],
                "latest",
            )

    def test_shared_history_can_be_disabled(self) -> None:
        with patch.dict(os.environ, {"AICX_CODEX_HISTORY": "isolated"}):
            self.assertFalse(codex_history_is_shared())

    def test_claude_project_conversations_are_shared(self) -> None:
        personal = self.store.create_profile("claude", "personal")
        work = self.store.create_profile("claude", "work")
        session = personal / "projects" / "-repo" / "session.jsonl"
        session.parent.mkdir(parents=True)
        session.write_text('{"message":"shared Claude conversation"}\n')

        sync_history(self.store, "claude")

        self.assertEqual(
            (work / "projects" / "-repo" / "session.jsonl").read_text(),
            session.read_text(),
        )

    def test_claude_session_assets_and_checkpoints_are_shared(self) -> None:
        personal = self.store.create_profile("claude", "personal")
        work = self.store.create_profile("claude", "work")
        tool_result = personal / "projects" / "-repo" / "session" / "tool-results" / "1.txt"
        checkpoint = personal / "file-history" / "session" / "backup.py"
        tool_result.parent.mkdir(parents=True)
        checkpoint.parent.mkdir(parents=True)
        tool_result.write_text("large tool output\n")
        checkpoint.write_text("previous contents\n")

        sync_history(self.store, "claude")

        self.assertEqual(
            (work / "projects" / "-repo" / "session" / "tool-results" / "1.txt").read_text(),
            tool_result.read_text(),
        )
        self.assertEqual(
            (work / "file-history" / "session" / "backup.py").read_text(),
            checkpoint.read_text(),
        )

    def test_active_profile_rollouts_are_not_copied_mid_write(self) -> None:
        personal = self.store.create_profile("codex", "personal")
        work = self.store.create_profile("codex", "work")
        rollout = personal / "sessions" / "2026" / "active.jsonl"
        rollout.parent.mkdir(parents=True)
        rollout.write_text("in progress\n")
        registry = ProcessRegistry(self.store)
        registry.add(os.getpid(), "codex", "personal", ["codex"])

        try:
            result = sync_codex_history(self.store)
        finally:
            registry.remove(os.getpid())

        self.assertFalse((work / "sessions" / "2026" / "active.jsonl").exists())
        self.assertEqual(result.skipped_active_profiles, 1)


class ClaudeTests(TemporaryStoreTestCase):
    def test_native_status_uses_claude_json_fields(self) -> None:
        self.store.create_profile("claude", "work")
        provider_output = json.dumps(
            {
                "loggedIn": True,
                "authMethod": "oauth",
                "apiProvider": "firstParty",
                "subscriptionType": "max",
            }
        )
        with patch("aicx.providers.find_binary", return_value="/bin/claude"), patch(
            "aicx.providers.subprocess.run"
        ) as run:
            run.return_value.returncode = 0
            run.return_value.stdout = provider_output
            run.return_value.stderr = ""
            status = native_status(self.store, "claude", "work")

        self.assertTrue(status["logged_in"])
        self.assertEqual(status["detail"], "oauth · firstParty")
        self.assertEqual(status["plan"], "max")

    def test_status_line_captures_usage_without_credentials(self) -> None:
        home = self.store.create_profile("claude", "personal")
        payload = {
            "session_id": "session-1",
            "model": {"display_name": "Sonnet"},
            "context_window": {"used_percentage": 25},
            "rate_limits": {
                "five_hour": {"used_percentage": 10, "resets_at": 1_800_000_000},
                "seven_day": {"used_percentage": 20, "resets_at": 1_800_100_000},
            },
        }

        with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(home)}):
            line = capture_claude_statusline(payload)

        cached = read_claude_balance(self.store, "personal")
        self.assertEqual(cached["session_id"], "session-1")
        self.assertEqual(cached["limits"]["five_hour"]["used_percentage"], 10)
        self.assertEqual(line, "Sonnet · ctx 25% · 5h 10% · 7d 20%")
        self.assertNotIn("token", json.dumps(cached))

    def test_usage_hook_does_not_replace_an_existing_status_line(self) -> None:
        home = self.store.create_profile("claude", "work")
        settings = home / "settings.json"
        original = {"statusLine": {"type": "command", "command": "my-status"}}
        settings.write_text(json.dumps(original), encoding="utf-8")

        result = install_claude_usage_hook(home)

        self.assertIn("skipped", result)
        self.assertEqual(json.loads(settings.read_text()), original)

    def test_session_listing_uses_metadata_without_parsing_conversation(self) -> None:
        home = self.store.create_profile("claude", "work")
        project = home / "projects" / "-home-me-project"
        project.mkdir(parents=True)
        (project / "abc.jsonl").write_text("not-json-on-purpose\n", encoding="utf-8")

        sessions = claude_sessions(self.store, "work")

        self.assertEqual(sessions[0]["id"], "abc")
        self.assertEqual(sessions[0]["state"], "saved")


class ProcessTests(TemporaryStoreTestCase):
    def test_registry_recognizes_the_currently_running_process(self) -> None:
        registry = ProcessRegistry(self.store)
        registry.add(os.getpid(), "codex", "work", ["codex"])

        records = registry.list(tool="codex", profile="work")

        self.assertEqual([record["pid"] for record in records], [os.getpid()])
        registry.remove(os.getpid())

    def test_registry_drops_records_whose_pid_is_no_longer_alive(self) -> None:
        from aicx.processes import process_start_signature

        registry = ProcessRegistry(self.store)
        self.assertIsNotNone(process_start_signature(os.getpid()))

        registry.add(999_999_999, "codex", "work", ["codex"])
        self.assertEqual(registry.list(tool="codex"), [])


class RpcTests(unittest.TestCase):
    def test_json_rpc_client_handles_notifications_and_responses(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fake = Path(temporary) / "fake-codex"
            fake.write_text(
                "#!/usr/bin/env python3\n"
                "import json, sys\n"
                "for line in sys.stdin:\n"
                "    message = json.loads(line)\n"
                "    if 'id' not in message:\n"
                "        continue\n"
                "    print(json.dumps({'method': 'notice', 'params': {}}), flush=True)\n"
                "    print(json.dumps({'id': message['id'], 'result': {'method': message['method']}}), flush=True)\n",
                encoding="utf-8",
            )
            fake.chmod(0o700)

            with CodexAppServer(str(fake), os.environ.copy()) as server:
                result = server.request("account/read", {"refreshToken": False})

        self.assertEqual(result, {"method": "account/read"})


if __name__ == "__main__":
    unittest.main()
