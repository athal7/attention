#!/usr/bin/env python3
"""Behavioral tests for CLI actions and repository resolution."""
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent


class GitHubRepositoryResolutionTests(unittest.TestCase):
    def test_verified_identity_prefers_registered_projects_and_resolves_bare_heads(self):
        source_path = str(ROOT / "sources")
        sys.path.insert(0, source_path)
        try:
            spec = importlib.util.spec_from_file_location(
                "attention_github_source", ROOT / "sources" / "github.py",
            )
            github = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(github)
        finally:
            sys.path.remove(source_path)

        def git(*args):
            return subprocess.run(
                ["git", *(str(arg) for arg in args)], check=True,
                capture_output=True, text=True,
            ).stdout.strip()

        def init_repo(path, origin):
            path.mkdir()
            git("init", "-b", "main", path)
            git("-C", path, "config", "user.email", "test@example.com")
            git("-C", path, "config", "user.name", "Test")
            (path / "README").write_text("fixture\n")
            git("-C", path, "add", "README")
            git("-C", path, "commit", "-m", "fixture")
            git("-C", path, "remote", "add", "origin", origin)

        with tempfile.TemporaryDirectory() as tmp:
            code_dir = Path(tmp) / "code"
            code_dir.mkdir()
            registered = code_dir / "registered-copy"
            duplicate = code_dir / "different-name"
            init_repo(registered, "git@github.com:Org/Widget.git")
            init_repo(duplicate, "https://github.com/org/widget.git")

            renamed = code_dir / "renamed-checkout"
            init_repo(renamed, "git@github.com:fork/project.git")
            git("-C", renamed, "remote", "add", "upstream", "https://github.com/org/project.git")

            ambiguous_a = code_dir / "ambiguous-a"
            ambiguous_b = code_dir / "ambiguous-b"
            init_repo(ambiguous_a, "git@github.com:org/ambiguous.git")
            init_repo(ambiguous_b, "git@github.com:org/ambiguous.git")

            foreign = code_dir / "foreign-host-checkout"
            init_repo(foreign, "git@gitlab.com:org/private.git")

            seed = Path(tmp) / "seed"
            init_repo(seed, "git@github.com:org/bare.git")
            bare = code_dir / "registered-bare.git"
            git("clone", "--bare", seed, bare)
            git("-C", bare, "symbolic-ref", "HEAD", "refs/heads/main")
            git("-C", bare, "remote", "set-url", "origin", "git@github.com:org/bare.git")
            bare_head = code_dir / "bare-default-checkout"
            git("-C", bare, "worktree", "add", bare_head, "main")

            registered_projects = {str(registered.resolve()), str(bare.resolve())}
            with patch.object(github, "_registered_repo_paths", return_value=registered_projects):
                index = github._build_repo_dir_index(str(code_dir))

            self.assertEqual(index["org/widget"], {
                "repo_path": str(registered.resolve()),
                "repo_project_path": str(registered.resolve()),
            })
            self.assertEqual(index["fork/project"]["repo_path"], os.path.abspath(renamed))
            self.assertEqual(index["org/project"]["repo_path"], os.path.abspath(renamed))
            self.assertNotIn("org/ambiguous", index)
            self.assertNotIn("org/private", index)
            self.assertEqual(index["org/bare"], {
                "repo_path": str(bare_head.resolve()),
                "repo_project_path": str(bare.resolve()),
            })


class InteractiveActionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        loader = importlib.machinery.SourceFileLoader("attention_util_test", str(ROOT / "sources" / "_util.py"))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        cls.util = importlib.util.module_from_spec(spec)
        loader.exec_module(cls.util)

    def test_configured_terminal_action_uses_the_shared_action_runner(self):
        command = ["lumen", "diff", "https://example.com/compare/main; touch /tmp/marker"]
        with patch.object(self.util, "run_terminal", return_value=True) as run_terminal, \
             patch.object(self.util, "run_cmd") as run_cmd:
            self.assertTrue(self.util.run_configured_action({"command": command, "terminal": True}))
        run_terminal.assert_called_once_with(command)
        run_cmd.assert_not_called()

    def test_configured_terminal_setting_runs_through_the_terminal_action(self):
        config = [{
            "key": "l",
            "label": "Lumen",
            "command": ["lumen", "diff", "{url}"],
            "terminal": True,
        }]
        action = self.util.resolve_configured_actions(
            config, {"url": "https://example.com/compare/main"},
        )[0]
        command = ["lumen", "diff", "https://example.com/compare/main"]
        with patch.object(self.util, "run_terminal", return_value=True) as run_terminal, \
             patch.object(self.util, "run_cmd") as run_cmd:
            self.assertTrue(self.util.run_configured_action(action["payload"]))
        run_terminal.assert_called_once_with(command)
        run_cmd.assert_not_called()

    def test_session_action_exports_the_resolved_agent_as_aoe_tool(self):
        with patch.object(self.util, "run_cmd", return_value=True) as run_cmd:
            self.assertTrue(self.util.run_configured_action({
                "command": ["my-session"], "session_tool": "omp",
            }))
        run_cmd.assert_called_once_with(["my-session"], env={"AOE_TOOL": "omp"})

    def test_action_without_a_resolved_agent_leaves_aoe_tool_absent(self):
        with patch.object(self.util, "run_cmd", return_value=True) as run_cmd:
            self.assertTrue(self.util.run_configured_action({"command": ["my-session"]}))
        run_cmd.assert_called_once_with(["my-session"])

    def test_session_tool_placeholder_is_left_for_dispatch_time(self):
        action = self.util.resolve_configured_actions(
            [{"key": "s", "label": "session", "command": ["my-session", "--tool", "{session_tool}"]}],
            {"url": "https://example.com/42"},
        )[0]
        self.assertEqual(action["payload"]["command"],
                         ["my-session", "--tool", "{session_tool}"])
        with patch.object(self.util, "run_cmd", return_value=True) as run_cmd:
            self.util.run_configured_action(dict(action["payload"], session_tool="pi"))
        run_cmd.assert_called_once_with(["my-session", "--tool", "pi"], env={"AOE_TOOL": "pi"})

    def test_background_session_action_exports_aoe_tool(self):
        with patch.object(self.util, "dispatch_background", return_value=True) as dispatch:
            self.assertTrue(self.util.run_configured_action({
                "command": ["my-session"], "background": True, "session_tool": "omp",
            }))
        dispatch.assert_called_once_with(["my-session"], env={"AOE_TOOL": "omp"})

    def test_terminal_session_action_exports_aoe_tool(self):
        with patch.object(self.util, "run_terminal", return_value=True) as run_terminal:
            self.assertTrue(self.util.run_configured_action({
                "command": ["my-session"], "terminal": True, "session_tool": "omp",
            }))
        run_terminal.assert_called_once_with(["my-session"], env={"AOE_TOOL": "omp"})

    def test_run_cmd_adds_the_session_agent_to_the_environment(self):
        with patch.object(self.util.subprocess, "run") as run:
            run.return_value = SimpleNamespace(returncode=0, stdout="", stderr="")
            self.assertTrue(self.util.run_cmd([sys.executable, "-c", ""], env={"AOE_TOOL": "pi"}))
        self.assertEqual(run.call_args.kwargs["env"]["AOE_TOOL"], "pi")
        self.assertIn("PATH", run.call_args.kwargs["env"])

    def test_macos_terminal_prefixes_the_session_agent(self):
        with patch.object(self.util.sys, "platform", "darwin"), \
             patch.object(self.util.subprocess, "run") as run:
            self.assertTrue(self.util.run_terminal(
                ["my-session", "--tool", "omp"], env={"AOE_TOOL": "omp"},
            ))
        script = run.call_args.args[0][2]
        self.assertIn("AOE_TOOL=omp my-session --tool omp", script)

    def test_linux_terminal_passes_the_session_agent_environment(self):
        with patch.object(self.util.sys, "platform", "linux"), \
             patch.dict(os.environ, {"TERMINAL": "custom-term"}), \
             patch.object(self.util.subprocess, "Popen") as popen:
            self.assertTrue(self.util.run_terminal(
                ["my-session"], env={"AOE_TOOL": "omp"},
            ))
        self.assertEqual(popen.call_args.kwargs["env"]["AOE_TOOL"], "omp")

    def test_run_cmd_handles_redirected_output_and_failures(self):
        import contextlib
        import io

        output = io.StringIO()
        diagnostics = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(diagnostics):
            self.assertTrue(self.util.run_cmd([
                sys.executable, "-c", "print('command output')",
            ]))
            self.assertFalse(self.util.run_cmd([
                sys.executable, "-c",
                "import sys; print('partial output'); print('failure detail', file=sys.stderr); sys.exit(7)",
            ]))
        self.assertIn("command output", output.getvalue())
        self.assertIn("partial output", output.getvalue())
        self.assertIn("Command failed:", output.getvalue())
        self.assertIn("failure detail", diagnostics.getvalue())

    def test_macos_terminal_shell_quotes_configured_arguments(self):
        url = "https://example.com/compare/main; touch /tmp/marker"
        with patch.object(self.util.sys, "platform", "darwin"), \
             patch.object(self.util.subprocess, "run") as run:
            self.assertTrue(self.util.run_terminal(["lumen", "diff", url]))
        args = run.call_args.args[0]
        self.assertEqual(args[:2], ["osascript", "-e"])
        self.assertIn("lumen diff 'https://example.com/compare/main; touch /tmp/marker'", args[2])
        run.assert_called_once()


    def test_linux_terminal_passes_argv_without_shell_reparsing(self):
        command = ["lumen", "diff", "https://example.com/change; touch /tmp/marker"]
        with patch.object(self.util.sys, "platform", "linux"), \
             patch.dict(os.environ, {"TERMINAL": "custom-term --window"}), \
             patch.object(self.util.subprocess, "Popen") as popen:
            self.assertTrue(self.util.run_terminal(command))
        popen.assert_called_once_with(
            ["custom-term", "--window", "-e", *command], start_new_session=True,
        )

    def test_windows_terminal_receives_argv_without_a_shell(self):
        command = ["lumen", "diff", "https://example.com/change; touch marker"]
        with patch.object(self.util.sys, "platform", "win32"), \
             patch.object(self.util.shutil, "which", return_value="C:/Windows/wt.exe"), \
             patch.object(self.util.subprocess, "Popen") as popen:
            self.assertTrue(self.util.run_terminal(command))
        popen.assert_called_once_with(["C:/Windows/wt.exe", "new-tab", "--", *command])


class TerminalPresentationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        loader = importlib.machinery.SourceFileLoader("attention_presentation", str(ROOT / "attention"))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        cls.core = importlib.util.module_from_spec(spec)
        loader.exec_module(cls.core)

    def test_nested_rows_keep_individual_statuses_and_actions(self):
        import base64

        def item(identity, parent, title, state):
            return {
                "identity_key": identity, "parent_identity_key": parent,
                "title": title, "status": state, "context": "org/repo",
                "details": "", "kind": "pull_request", "indicators": {"state": state},
                "actions": [{"key": "o", "label": "open", "payload": {"id": identity}}],
            }

        items = [item("3", "2", "Grandchild", "Blocked"),
                 item("2", "1", "Child", "Review requested"),
                 item("1", None, "Base", "Ready")]
        groups, _ = self.core.dashboard_groups({})
        rows = self.core.render_grouped_dashboard_rows(items, groups)
        self.assertEqual([json.loads(base64.b64decode(row.split("\t")[1]))[0]["payload"]["id"]
                          for row in rows], ["1", "2", "3"])
        self.assertTrue(rows[0].startswith("Base"))
        self.assertTrue(rows[1].startswith("└─ Child"))
        self.assertTrue(rows[2].startswith("   └─ Grandchild"))
        self.assertIn("✓ Ready", rows[0])
        self.assertIn("◉ Review requested", rows[1])
        self.assertIn("✗ Blocked", rows[2])

    def test_flat_rows_preserve_tree_order_and_header_alignment(self):
        items = [
            {"title": "Child", "identity_key": "c", "parent_identity_key": "p",
             "context": "repo", "status": "Blocked", "indicators": {"state": "Blocked"}},
            {"title": "Parent", "identity_key": "p", "context": "repo",
             "status": "Ready", "indicators": {"state": "Ready"}},
        ]
        rows = self.core.render_dashboard_rows(self.core._tree_order(items))
        self.assertTrue(rows[0].startswith("Parent"))
        self.assertTrue(rows[1].startswith("└─ Child"))
        metadata = json.loads(rows[0].split("\t")[4])
        self.assertEqual(rows[0].index("✓"), metadata["offset"])
        self.assertEqual(rows[1].index("✗"), rows[0].index("✓"))

    def test_indicator_columns_share_aligned_headers_and_rows(self):
        loader = importlib.machinery.SourceFileLoader("attention_presenter", str(ROOT / "dashboard.py"))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        dashboard = importlib.util.module_from_spec(spec)
        loader.exec_module(dashboard)
        items = [
            {"title": "One", "context": "repo", "status": "Ready",
             "indicators": {"ci": "Passed", "review": "Changes"}},
            {"title": "Two", "context": "repo", "status": "Blocked",
             "indicators": {"ci": "Failed", "review": "Approved"}},
        ]
        rows = self.core.render_dashboard_rows(items, ["ci", "review"])
        header = dashboard.CursesPresenter()._indicator_header(rows)
        self.assertEqual(header.index("CI"), rows[0].index("✓ Passed"))
        self.assertEqual(header.index("Review"), rows[0].index("✗ Changes"))
        self.assertEqual(header.index("CI"), rows[1].index("✗ Failed"))
        self.assertEqual(header.index("Review"), rows[1].index("✓ Approved"))

    def test_displayed_row_dispatches_its_action_and_wip_transition(self):
        action = {"_plugin": "generic", "_original_key": "o", "key": "1", "label": "open",
                  "payload": {"url": "private"}, "wip": True, "_wip_id": "generic:item"}
        item = {"title": "Item", "context": "repo", "status": "Ready",
                "indicators": {"state": "Ready"}, "actions": [action]}
        class Plugin:
            def act(self, key, payload):
                self.called = (key, payload)
                return True
        plugin = Plugin()
        row = self.core.render_dashboard_rows([item])[0]
        with patch.object(self.core, "load_plugin", return_value=plugin), \
             patch.object(self.core, "mark_wip_item") as mark:
            self.assertTrue(self.core.act("1", row))
        self.assertEqual(plugin.called, ("o", {"url": "private"}))
        mark.assert_called_once_with("generic:item")
