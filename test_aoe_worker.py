#!/usr/bin/env python3
"""Behavioral tests for the Agent of Empires pane worker."""
import importlib.util
import json
import os
import queue
import select
import subprocess
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("aoe_worker", ROOT / "aoe_worker.py")
worker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(worker)





def _receive_json_line(stream, buffer, timeout, timeout_message):
    deadline = time.monotonic() + timeout
    while True:
        newline = buffer.find(b"\n")
        if newline >= 0:
            line = bytes(buffer[:newline])
            del buffer[:newline + 1]
            return json.loads(line)
        remaining = deadline - time.monotonic()
        ready, _, _ = select.select([stream.fileno()], [], [], max(0, remaining))
        if not ready:
            raise AssertionError(timeout_message)
        chunk = os.read(stream.fileno(), 4096)
        if not chunk:
            raise AssertionError("worker exited before replying")
        buffer.extend(chunk)



class SidebarPayloadTests(unittest.TestCase):
    def test_session_ui_state_parses_primary_cli_url_without_starting_daemon(self):
        output = SimpleNamespace(
            returncode=0,
            stdout="primary\thttp://127.0.0.1:4097/?token=secret\n"
                   "lan\thttp://192.0.2.1:4097/?token=secret\n",
        )
        with patch.object(worker.subprocess, "run", return_value=output) as run, \
                patch.object(worker, "urlopen") as urlopen:
            urlopen.return_value.__enter__.return_value.read.return_value = b'{"entries":[]}'
            self.assertEqual(worker.session_ui_state(), [])
        run.assert_called_once_with(
            ["aoe", "url", "--no-autostart"],
            capture_output=True, text=True, timeout=5,
        )
        urlopen.assert_called_once_with(
            "http://127.0.0.1:4097/api/plugins/ui-state?token=secret", timeout=5,
        )

    def test_native_sidebar_uses_github_style_selector_and_details(self):
        action = {
            "key": "o", "label": "Open", "primary": True,
            "payload": {"kind": "open"},
        }
        token_actions = {}
        snapshot = worker.attention_payload([{
            "_plugin": "github", "kind": "pull_request", "title": "Review PR",
            "status": "Review needed", "attention_reason": "Requested changes",
            "context": "acme/repo", "details": "Inspect the failing test.",
            "url": "https://github.com/acme/repo/pull/42", "actions": [action],
        }, {
            "_plugin": "linear", "kind": "issue", "title": "Linear issue",
            "status": "Assigned", "url": "https://linear.app/acme/issue/ENG-4",
        }], [], token_actions)
        pane = worker._pane_payload(snapshot)
        self.assertEqual(pane["default_location"], "right")
        queue = pane["blocks"][0]
        self.assertEqual(queue["title"], "Work queue")
        self.assertTrue(queue["boxed"])
        self.assertTrue(queue["scroll"])
        self.assertEqual(queue["badges"][0]["text"], "2")
        first, second = queue["children"]
        self.assertEqual(first["label"], "Review PR")
        self.assertTrue(first["selected"])
        self.assertEqual(first["sublabel"], "acme/repo")
        self.assertEqual(first["badges"], [{"text": "Review needed", "tone": "warn"}])
        self.assertEqual(first["href"], "https://github.com/acme/repo/pull/42")
        self.assertFalse(second["selected"])

        callout = pane["blocks"][1]
        self.assertEqual(callout["kind"], "callout")
        self.assertEqual(callout["title"], "Review PR")
        self.assertEqual(callout["detail"],
                         "Requested changes · Inspect the failing test.")
        self.assertEqual(callout["actions"][0]["label"], "Open source")
        self.assertEqual(callout["actions"][0]["href"],
                         "https://github.com/acme/repo/pull/42")

        details = pane["blocks"][2]
        self.assertEqual(details["title"], "Item details")
        detail_rows = {row["label"]: row["value"] for row in details["children"]}
        self.assertEqual(detail_rows["Status"], "Review needed")
        self.assertEqual(detail_rows["Source"], "GitHub")
        self.assertEqual(detail_rows["Context"], "acme/repo")

        actions = pane["blocks"][3]
        self.assertEqual(actions["title"], "Actions")
        self.assertTrue(actions["collapsible"])
        self.assertTrue(actions["collapsed"])
        self.assertEqual(actions["children"][0]["variant"], "primary")
        self.assertEqual(actions["children"][0]["method"], "attention.action")
        token = actions["children"][0]["params"]["token"]
        self.assertIs(token_actions[token], action)
        self.assertEqual(pane["blocks"][-1]["label"], "Refresh")
        self.assertEqual(pane["blocks"][-1]["method"], "attention.refresh")
        self.assertEqual(pane["blocks"][-2]["kind"], "divider")

    def test_empty_native_sidebar_distinguishes_source_failures(self):
        empty = worker._pane_payload({"items": [], "failures": []})
        failure = worker._pane_payload({
            "items": [], "failures": [{"source": "GitHub", "error": "unavailable"}],
        })
        self.assertEqual(empty["blocks"][0]["title"], "Nothing needs attention")
        self.assertEqual(empty["blocks"][0]["tone"], "success")
        self.assertEqual(failure["blocks"][0]["title"], "Sources need attention")
        failures = next(block for block in failure["blocks"]
                        if block.get("title") == "Collector failures")
        self.assertIn("GitHub: unavailable", failures["children"][0]["text"])

    def test_open_session_identity_suppresses_only_matching_github_and_linear_work(self):
        items = [
            {"kind": "pull_request", "title": "Shared branch without identity facet",
             "url": "https://github.com/acme/repo/pull/99", "repo_path": "/repo",
             "head_branch": "shared"},
            {"kind": "pull_request", "title": "Owned", "url": "https://github.com/Acme/Repo/pull/42"},
            {"kind": "issue", "title": "Linked", "url": "https://linear.app/acme/issue/ENG-4/slug"},
            {"kind": "issue", "title": "Unrelated", "url": "https://linear.app/acme/issue/ENG-5"},
            {"kind": "pull_request", "title": "Stopped", "url": "https://github.com/acme/repo/pull/43"},
        ]
        sessions = [
            {"id": "idle", "status": "Idle",
             "workspace_repos": [{"source_path": "/repo", "branch": "shared"}],
             "_attention_items": [
                 ("github", "acme", "repo", "pull", 42),
                 ("linear", "acme", "ENG-4"),
             ]},
            {"id": "stopped", "status": "Stopped", "_attention_items": [
                ("github", "acme", "repo", "pull", 43),
            ]},
        ]
        snapshot = worker.attention_payload(items, sessions)
        self.assertEqual(
            {card["title"] for card in snapshot["items"]},
            {"Unrelated", "Stopped", "Shared branch without identity facet"},
        )

    def test_absorbed_linear_issue_identity_suppresses_its_github_host(self):
        loader = importlib.machinery.SourceFileLoader(
            "attention_linked_identity_test", str(ROOT / "attention"),
        )
        spec = importlib.util.spec_from_loader(loader.name, loader)
        attention = importlib.util.module_from_spec(spec)
        loader.exec_module(attention)
        host = {
            "kind": "pull_request", "title": "PR", "details": "review",
            "url": "https://github.com/acme/repo/pull/42",
        }
        guest = {
            "kind": "issue", "title": "Issue", "details": "assigned",
            "url": "https://linear.app/acme/issue/ENG-4",
        }
        attention._absorb(host, guest)
        snapshot = worker.attention_payload([host], [{
            "id": "linear-session", "status": "Idle",
            "_attention_items": [("linear", "acme", "ENG-4")],
        }])
        self.assertEqual(snapshot["items"], [])

    def test_session_facets_use_only_canonical_plugin_item_links(self):
        entries = [{
            "plugin_id": "agent-of-empires.github",
            "slot": "row-badge",
            "id": "github_pr_badge",
            "session_id": "s1",
            "payload": {"items": [
                {"href": "https://github.com/acme/repo/pull/42"},
                {"href": "https://github.com/acme/repo/pull/42#issuecomment-123"},
                {"href": "https://example.com/acme/repo/pull/42"},
            ]},
        }, {
            "plugin_id": "community.linear",
            "slot": "pane",
            "id": "linear_issue_pane",
            "session_id": "s1",
            "payload": {"blocks": [{"children": [
                {"method": "linear.select_issue", "href": "https://linear.app/acme/issue/ENG-4"},
                {"href": "https://linear.app/acme/issue/ENG-9"},
            ]}]},
        }]
        entries.extend([{
            "plugin_id": "agent-of-empires.github", "slot": "row-badge",
            "id": "github_pr_badge", "session_id": "s1",
            "payload": {"items": 42},
        }, {
            "plugin_id": "community.linear", "slot": "pane",
            "id": "linear_issue_pane", "session_id": "s1",
            "payload": {"blocks": {"method": "linear.select_issue"}},
        }, {
            "plugin_id": "other.plugin", "slot": "row-badge",
            "id": "github_pr_badge", "session_id": "s1",
            "payload": {"href": "https://github.com/acme/repo/pull/99"},
        }, {
            "plugin_id": "athal7.attention", "slot": "pane",
            "id": "attention", "session_id": "s2", "payload": {},
        }, {
            "plugin_id": "unknown.plugin", "slot": "row-badge",
            "id": "unknown", "session_id": "s3", "payload": {},
        }])
        self.assertEqual(worker._session_item_identities(entries), {
            "s1": [
                ("github", "acme", "repo", "pull", 42),
                ("linear", "acme", "ENG-4"),
            ],
        })

    def test_native_sidebar_keeps_selection_per_session(self):
        items = [
            {"title": "First", "status": "Review needed", "actions": []},
            {"title": "Second", "status": "Assigned", "actions": []},
        ]
        keys = [worker._item_key(item) for item in items]
        calls = []
        with patch.object(worker, "selected_item_keys", {"s1": keys[0], "s2": keys[1]}), \
                patch.object(worker, "published_ids", set(), create=True), \
                patch.object(worker, "action_tokens", {}, create=True), \
                patch.object(worker, "rpc", side_effect=lambda method, params: calls.append(params)):
            worker.publish_payload(
                [{"id": "s1"}, {"id": "s2"}], worker.status_payload(), items=items,
            )
        panes = [call["payload"] for call in calls]
        queues = [next(block for block in pane["blocks"]
                       if block.get("title") == "Work queue")
                  for pane in panes]
        selected = [
            next(row["label"] for row in queue["children"] if row.get("selected"))
            for queue in queues
        ]
        self.assertEqual(selected, ["First", "Second"])
        self.assertEqual(
            [next(block["title"] for block in pane["blocks"]
                  if block.get("kind") == "callout") for pane in panes],
            ["First", "Second"],
        )
        self.assertEqual(
            [next(row["value"] for row in next(
                block for block in pane["blocks"]
                if block.get("title") == "Item details"
            )["children"] if row["label"] == "Status") for pane in panes],
            ["Review needed", "Assigned"],
        )

    def test_sidebar_item_selection_dispatch_requests_a_pane_update(self):
        with patch.object(worker, "live_session_ids", {"s1", "s2"}, create=True), \
                patch.object(worker, "pane_refresh_requested", False), \
                patch.object(worker, "send") as send:
            worker.dispatch({
                "jsonrpc": "2.0", "id": "select",
                "method": "attention.select_item",
                "params": {"session_id": "s1", "item_key": "item-b"},
            })
            self.assertEqual(worker.selected_item_keys, {"s1": "item-b"})
            self.assertTrue(worker.pane_refresh_requested)
            self.assertTrue(send.call_args.args[0]["result"]["ok"])
            worker.dispatch({
                "jsonrpc": "2.0", "id": "unknown-session",
                "method": "attention.select_item",
                "params": {"session_id": "s3", "item_key": "item-c"},
            })
            self.assertEqual(worker.selected_item_keys, {"s1": "item-b"})
            self.assertFalse(send.call_args.args[0]["result"]["ok"])

    def test_native_pane_trims_lower_priority_queue_rows_to_payload_limit(self):
        items = [
            {"title": "Highest priority", "details": "h" * 1800, "weight": 100},
            {"title": "Middle priority", "details": "m" * 1800, "weight": 50},
            {"title": "Lowest priority", "details": "l" * 1800, "weight": 1},
        ]
        snapshot = worker.attention_payload(items, [])
        pane = worker._pane_payload(snapshot)
        with patch.object(worker, "MAX_PANE_BYTES", 2000):
            fitted = worker._fit_pane(pane)
        self.assertLessEqual(
            len(json.dumps(fitted, separators=(",", ":")).encode("utf-8")),
            worker.MAX_PANE_BYTES,
        )
        queue = next(block for block in fitted["blocks"]
                     if block.get("title") == "Work queue")
        self.assertEqual(
            [row["label"] for row in queue["children"]],
            ["Highest priority"],
        )

    def test_native_pane_trims_actions_without_dropping_selected_item(self):
        item = {
            "title": "Too many actions",
            "actions": [
                {"key": str(index), "label": f"Action {index} " + "x" * 80,
                 "payload": {"command": ["echo", str(index)]}}
                for index in range(100)
            ],
        }
        snapshot = worker.attention_payload([item], [])
        pane = worker._pane_payload(snapshot)
        with patch.object(worker, "MAX_PANE_BYTES", 6000):
            fitted = worker._fit_pane(pane)
        encoded = json.dumps(fitted, separators=(",", ":")).encode("utf-8")
        self.assertLessEqual(len(encoded), worker.MAX_PANE_BYTES)
        self.assertIn("Too many actions", json.dumps(fitted))
        actions = next(block for block in fitted["blocks"]
                       if block.get("title") == "Actions")
        self.assertLess(len(actions["children"]), 100)
        self.assertIn("omitted", json.dumps(fitted))


    def test_sidebar_actions_are_opaque_tokens(self):
        lumen_action = {
            "_plugin": "github", "label": "Lumen",
            "payload": {"command": [
                "/opt/homebrew/bin/lumen", "diff",
                "https://github.com/example/repo/pull/42",
            ], "terminal": True},
        }
        prompt_action = {
            "label": "Comment",
            "payload": {"command": ["echo"], "inputs": [{"prompt": "Review note"}]},
        }
        token_actions = {}
        snapshot = worker.attention_payload([{
            "_plugin": "github", "kind": "pull_request", "title": "Review PR",
            "actions": [lumen_action, prompt_action],
        }], [], token_actions)
        actions = snapshot["items"][0]["actions"]
        self.assertEqual([action["label"] for action in actions],
                         ["Lumen", "Comment", "New session"])
        pane = worker._pane_payload(snapshot)
        encoded = json.dumps(pane)
        self.assertNotIn("/opt/homebrew/bin/lumen", encoded)
        action_tokens = worker._pane_action_tokens(pane)
        self.assertEqual(action_tokens, {action["token"] for action in actions})
        self.assertIs(token_actions[actions[0]["token"]], lumen_action)

    def test_action_feedback_is_published_only_in_session_pane(self):
        names = ("published_ids", "action_tokens", "selected_item_keys")
        previous = {name: getattr(worker, name) for name in names if hasattr(worker, name)}
        worker.published_ids = set()
        worker.action_tokens = {}
        worker.selected_item_keys = {}
        try:
            with patch.object(worker, "rpc") as send:
                worker.publish_payload(
                    [{"id": "s1"}],
                    worker.status_payload("Failed: Lumen: Action did not complete."),
                    items=[{"title": "Review PR", "actions": []}],
                )
            params = send.call_args.args[1]
            self.assertEqual(params["slot"], "pane")
            self.assertEqual(params["session_id"], "s1")
            notice = next(block for block in params["payload"]["blocks"]
                          if block.get("kind") == "note")
            self.assertEqual(notice["text"],
                             "Failed: Lumen: Action did not complete.")
            self.assertNotIn("page", json.dumps(params))
        finally:
            for name in names:
                if name in previous:
                    setattr(worker, name, previous[name])
                elif hasattr(worker, name):
                    delattr(worker, name)
    def test_republished_pane_rejects_old_action_tokens_immediately(self):
        names = (
            "published_ids", "action_tokens", "action_token_sessions",
            "selected_item_keys", "live_session_ids", "active_action",
            "action_requests", "consumed_action_ids",
        )
        previous = {name: getattr(worker, name) for name in names if hasattr(worker, name)}
        action = {"label": "Run", "payload": {"command": ["echo"]}}
        worker.published_ids = set()
        worker.action_tokens = {"old-token": action}
        worker.action_token_sessions = {"old-token": "s1"}
        worker.selected_item_keys = {}
        worker.live_session_ids = {"s1", "s2"}
        worker.active_action = None
        worker.action_requests = queue.Queue()
        worker.consumed_action_ids = set()
        responses = []

        def publish(method, params):
            if params["session_id"] == "s1":
                with patch.object(worker, "send", side_effect=responses.append):
                    worker.dispatch({
                        "jsonrpc": "2.0", "id": "stale",
                        "method": "attention.action",
                        "params": {"token": "old-token", "session_id": "s1"},
                    })

        try:
            with patch.object(worker, "rpc", side_effect=publish):
                worker.publish_payload(
                    [{"id": "s1"}, {"id": "s2"}], {},
                    items=[{"title": "Review PR", "actions": [action]}],
                )
            self.assertEqual(responses[0]["result"]["ok"], False)
            self.assertIn("expired", responses[0]["result"]["error"])
            with self.assertRaises(queue.Empty):
                worker.action_requests.get_nowait()
        finally:
            for name in names:
                if name in previous:
                    setattr(worker, name, previous[name])
                elif hasattr(worker, name):
                    delattr(worker, name)

    def test_skipped_pane_publish_keeps_existing_session_action_token(self):
        names = (
            "published_ids", "action_tokens", "action_token_sessions",
            "selected_item_keys", "live_session_ids", "active_action",
            "action_requests", "consumed_action_ids", "consumed_session_item_ids",
        )
        previous = {name: getattr(worker, name) for name in names if hasattr(worker, name)}
        action = {"label": "Run", "payload": {"command": ["echo"]}}
        worker.published_ids = {"s1"}
        worker.action_tokens = {"existing-token": action}
        worker.action_token_sessions = {"existing-token": "s1"}
        worker.selected_item_keys = {}
        worker.live_session_ids = {"s1", "s2"}
        worker.active_action = None
        worker.action_requests = queue.Queue()
        worker.consumed_action_ids = set()
        worker.consumed_session_item_ids = set()
        published = []
        responses = []

        try:
            with patch.object(
                    worker, "rpc", side_effect=lambda method, params: published.append(params)):
                worker.publish_payload(
                    [{"id": "s1"}, {"id": "s2"}], worker.status_payload(),
                    only_new=True,
                    items=[{"title": "Review PR", "actions": [action]}],
                )

            self.assertEqual([params["session_id"] for params in published], ["s2"])
            self.assertIs(worker.action_tokens["existing-token"], action)
            self.assertEqual(worker.action_token_sessions["existing-token"], "s1")

            with patch.object(worker, "send", side_effect=responses.append):
                worker.dispatch({
                    "jsonrpc": "2.0", "id": "foreign",
                    "method": "attention.action",
                    "params": {"token": "existing-token", "session_id": "s2"},
                })
                self.assertFalse(responses[-1]["result"]["ok"])
                self.assertIn("expired", responses[-1]["result"]["error"])

                worker.dispatch({
                    "jsonrpc": "2.0", "id": "existing",
                    "method": "attention.action",
                    "params": {"token": "existing-token", "session_id": "s1"},
                })
                self.assertTrue(responses[-1]["result"]["ok"])

            request = worker.action_requests.get_nowait()
            self.assertEqual(request["session_id"], "s1")
            self.assertIs(request["action"], action)
            self.assertEqual(request["token"], "existing-token")
            with self.assertRaises(queue.Empty):
                worker.action_requests.get_nowait()
        finally:
            for name in names:
                if name in previous:
                    setattr(worker, name, previous[name])
                elif hasattr(worker, name):
                    delattr(worker, name)

    def test_sidebar_preserves_live_session_matches(self):
        item = {"title": "Review PR", "repo_path": "/repo", "head_branch": "feature"}
        sessions = [{
            "id": "session/id", "title": "Work", "status": "Running",
            "workspace_repos": [{"source_path": "/repo", "branch": "feature"}],
        }]
        snapshot = worker.attention_payload([item], sessions)
        self.assertEqual(
            snapshot["items"][0]["session"], {"id": "session/id", "title": "Work"},
        )

    def test_generated_session_action_preserves_registered_repo_and_pr_branches(self):
        item = {
            "_plugin": "github", "kind": "pull_request", "title": "PR",
            "repo_path": "/worktrees/repo", "repo_project_path": "/registered/repo",
            "head_branch": "feature/pr-42", "base_branch": "main",
        }
        token_actions = {}
        snapshot = worker.attention_payload([item], [], token_actions)
        new_session = snapshot["items"][0]["actions"][-1]
        self.assertEqual(new_session["label"], "New session")
        action = token_actions[new_session["token"]]
        self.assertEqual(action["params"]["path"], "/registered/repo")
        self.assertTrue(action["params"]["is_pull_request"])
        self.assertEqual(action["params"]["worktree_branch"], "feature/pr-42")
        self.assertEqual(action["params"]["base_branch"], "main")

    def test_configured_session_action_replaces_generated_session_action(self):
        session_action = {
            "_plugin": "generic", "key": "s", "label": "session",
            "payload": {"command": ["aoe", "add"]},
        }
        item = {
            "_plugin": "generic", "title": "Existing session flow",
            "repo_path": "/repo", "actions": [session_action],
        }
        token_actions = {}
        snapshot = worker.attention_payload([item], [], token_actions)
        actions = snapshot["items"][0]["actions"]
        self.assertEqual([action["label"] for action in actions], ["session"])
        self.assertEqual(actions[0]["method"], "attention.action")
        self.assertIs(token_actions[actions[0]["token"]], session_action)


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
class SessionCreationTests(unittest.TestCase):
    def test_plugin_manifest_uses_cli_spawning_without_structured_session_grants(self):
        import tomllib

        manifest = tomllib.loads((ROOT / "aoe-plugin.toml").read_text())
        self.assertIn("process.spawn", manifest["capabilities"])
        for capability in ("session.create", "session.prompt", "session.unattended"):
            self.assertNotIn(capability, manifest["capabilities"])

class AttentionSourceTests(unittest.TestCase):
    def test_real_attention_module_reads_config_without_stale_module_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "attention"
            config.mkdir()
            (config / "config.json").write_text('{"plugins": []}')
            state = Path(tmp) / "state"
            with patch.dict(os.environ, {"XDG_CONFIG_HOME": tmp, "XDG_STATE_HOME": str(state)}):
                with patch.object(worker, "ATTENTION_PATH", ROOT / "attention"):
                    self.assertEqual(worker.attention_items(), [])

    def test_cli_and_sidebar_show_sanitized_partial_and_total_collector_failures(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_dir = root / "config" / "attention"
            config_dir.mkdir(parents=True)
            healthy = root / "healthy.py"
            failing = root / "failing.py"
            healthy.write_text(
                "def fetch(config):\n"
                "    return [{'status':'OPEN','context':'repo','title':'Healthy item',"
                "'details':'','weight':1}]\n"
            )
            failing.write_text(
                "def fetch(config):\n"
                "    raise RuntimeError('token=collector-secret')\n"
            )
            config_path = config_dir / "config.json"
            state = root / "state"
            env = {
                "XDG_CONFIG_HOME": str(root / "config"),
                "XDG_STATE_HOME": str(state),
            }

            def set_plugins(plugins):
                config_path.write_text(json.dumps({"plugins": [str(p) for p in plugins]}))

            def run_cli():
                return subprocess.run(
                    [sys.executable, str(ROOT / "attention"), "list"],
                    env={**os.environ, **env}, capture_output=True, text=True, check=True,
                ).stdout

            set_plugins([healthy, failing])
            output = run_cli()
            self.assertIn("Healthy item", output)
            self.assertIn("failing collector failed", output)
            self.assertIn("RuntimeError", output)
            self.assertNotIn("collector-secret", output)
            with patch.dict(os.environ, env), patch.object(worker, "ATTENTION_PATH", ROOT / "attention"):
                snapshot = worker.attention_payload(worker.attention_items(), [])
            snapshot_text = json.dumps(snapshot)
            self.assertEqual([item["title"] for item in snapshot["items"]], ["Healthy item"])
            self.assertEqual(len(snapshot["failures"]), 1)
            self.assertIn("failing", snapshot_text.casefold())
            self.assertNotIn("collector-secret", snapshot_text)

            set_plugins([failing])
            output = run_cli()
            self.assertIn("failing collector failed", output)
            self.assertNotIn("Nothing needs attention", output)
            with patch.dict(os.environ, env), patch.object(worker, "ATTENTION_PATH", ROOT / "attention"):
                snapshot = worker.attention_payload(worker.attention_items(), [])
            self.assertEqual(snapshot["items"], [])
            self.assertEqual(len(snapshot["failures"]), 1)

            healthy.write_text("def fetch(config):\n    return []\n")
            set_plugins([healthy])
            self.assertEqual(run_cli(), "")
            with patch.dict(os.environ, env), patch.object(worker, "ATTENTION_PATH", ROOT / "attention"):
                snapshot = worker.attention_payload(worker.attention_items(), [])
            self.assertEqual(snapshot["items"], [])
            self.assertEqual(snapshot["failures"], [])

    def test_interactive_dashboard_renders_failed_and_healthy_collectors(self):
        dashboard_spec = importlib.util.spec_from_file_location(
            "dashboard_failure_test", ROOT / "dashboard.py",
        )
        dashboard = importlib.util.module_from_spec(dashboard_spec)
        dashboard_spec.loader.exec_module(dashboard)

        class Presenter:
            def __init__(self):
                self.snapshots = []

            def push_snapshot(self, rows, pending):
                self.snapshots.append((rows, pending))

            def launch(self):
                pass

            def stop(self):
                pass

        presenter = Presenter()

        def fetch(name):
            if name == "broken":
                raise RuntimeError("token=collector-secret")
            return [{"title": "Healthy item"}]

        controller = dashboard.DashboardController(
            ["broken", "healthy"], presenter,
            fetch_plugin=fetch,
            build_snapshot=lambda grouped, recent: [
                item for values in grouped.values() for item in values
            ],
            render_rows=lambda items: [item["title"] for item in items],
            act=lambda key, row: None,
        )
        round_ = controller._create_round()
        controller._run_plugin_fetch(round_, "broken")
        controller._run_plugin_fetch(round_, "healthy")
        rows = presenter.snapshots[-1][0]
        self.assertIn("broken collector failed", rows)
        self.assertIn("Healthy item", rows)
        self.assertNotIn("collector-secret", json.dumps(rows))


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


class WorkerProtocolTests(unittest.TestCase):
    def test_pr_session_requires_repository_path_and_valid_head_branch(self):
        cases = (
            (
                {"title": "PR", "path": "", "scratch": False,
                 "is_pull_request": True, "worktree_branch": "feature/pr-42"},
                "no repository path",
            ),
            (
                {"title": "PR", "path": "/repos/app", "scratch": False,
                 "is_pull_request": True},
                "no valid head branch",
            ),
            (
                {"title": "PR", "path": "/repos/app", "scratch": False,
                 "is_pull_request": True, "worktree_branch": "feature/pr-42 "},
                "no valid head branch",
            ),
            (
                {"title": "PR", "path": "/repos/app", "scratch": False,
                 "is_pull_request": True, "worktree_branch": "feature/pr-42",
                 "base_branch": "main "},
                "no valid target branch",
            ),
        )
        for options, message in cases:
            with self.subTest(options=options):
                self.assertIn(message, worker._session_options_error(options))

    def test_github_repository_resolution_failure_is_fail_closed(self):
        action = worker._new_session_action({
            "_plugin": "github",
            "kind": "issue",
            "title": "Issue",
            "repo_path": "",
            "repo_project_path": "",
            "repo_resolution_error": "No unique local GitHub checkout for repo.",
        })
        self.assertFalse(action["params"]["scratch"])
        self.assertEqual(action["params"]["repo_resolution_error"],
                         "No unique local GitHub checkout for repo.")
        self.assertIn("No unique local GitHub checkout",
                      worker._session_options_error(action["params"]))
        results = queue.Queue()
        with patch.object(worker.subprocess, "run") as run:
            worker.start_new_session("token", action["params"], results)
            event = results.get(timeout=3)
        run.assert_not_called()
        self.assertFalse(event["completed"])
        self.assertIn("No unique local GitHub checkout", event["error"])

    def test_session_options_allow_project_and_scratch_creation(self):
        self.assertIsNone(worker._session_options_error({
            "title": "Project session", "path": "/repo", "scratch": False,
        }))
        self.assertIsNone(worker._session_options_error({
            "title": "Scratch session", "path": "", "scratch": True,
        }))
        self.assertIn("project path", worker._session_options_error({
            "title": "", "path": "", "scratch": False,
        }))


    def test_session_options_accept_cli_profile_group_and_worktree_flags(self):
        options = {
            "title": "Worktree session", "path": "/repo", "profile": "qa", "tool": "omp",
            "group": "reviews", "scratch": False, "worktree_branch": "feature/task",
            "new_branch": True, "base_branch": "main",
        }
        self.assertIsNone(worker._session_options_error(options))
        self.assertIn("new worktree", worker._session_options_error({
            **options, "new_branch": False,
        }))
        self.assertIn("worktree options", worker._session_options_error({
            **options, "scratch": True,
        }))

    def test_configured_agent_per_directory_drives_the_new_session_tool(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            work = root / "work"
            work_repo = work / "repo"
            personal = root / "personal"
            work_repo.mkdir(parents=True)
            personal.mkdir()
            config_dir = root / "config" / "attention"
            config_dir.mkdir(parents=True)
            (config_dir / "config.json").write_text(json.dumps({
                "plugins": [],
                "session": {"agents": [
                    {"path": str(work), "tool": "omp"},
                    {"path": str(work_repo), "tool": "pi"},
                    {"path": str(personal), "tool": "freebuf"},
                ]},
            }))
            env = {
                "XDG_CONFIG_HOME": str(root / "config"),
                "XDG_STATE_HOME": str(root / "state"),
            }
            with patch.dict(os.environ, env), \
                 patch.object(worker, "ATTENTION_PATH", ROOT / "attention"):
                worker.attention_items()
                self.assertEqual(worker._session_tool_for_path(str(work)), "omp")
                self.assertEqual(worker._session_tool_for_path(str(work_repo)), "pi")
                self.assertEqual(worker._session_tool_for_path(str(work_repo / "app")), "pi")
                self.assertEqual(
                    worker._session_tool_for_path(str(personal / "notes")), "freebuf",
                )
                self.assertEqual(worker._session_tool_for_path(str(root / "elsewhere")), "")
                self.assertEqual(worker._session_tool_for_path(""), "")

                action = worker._new_session_action({
                    "title": "Fix regression", "repo_path": str(work_repo / "app"),
                })
                self.assertEqual(action["params"]["tool"], "pi")
                self.assertIsNone(worker._session_options_error(action["params"]))

                github_action = worker._new_session_action({
                    "_plugin": "github", "kind": "issue", "title": "Issue",
                    "repo_path": str(personal / "clone"),
                    "repo_project_path": str(work_repo),
                })
                self.assertEqual(github_action["params"]["tool"], "pi")

                unconfigured = worker._new_session_action({
                    "title": "Scratch follow-up", "repo_path": str(root / "elsewhere"),
                })
                self.assertNotIn("tool", unconfigured["params"])

    def test_invalid_session_agent_configuration_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_dir = root / "config" / "attention"
            config_dir.mkdir(parents=True)
            (config_dir / "config.json").write_text(json.dumps({
                "plugins": [],
                "session": {"agents": [{"path": "/repos", "tool": "--title"}]},
            }))
            env = {
                "XDG_CONFIG_HOME": str(root / "config"),
                "XDG_STATE_HOME": str(root / "state"),
            }
            with patch.dict(os.environ, env), \
                 patch.object(worker, "ATTENTION_PATH", ROOT / "attention"):
                with self.assertRaises(RuntimeError):
                    worker.attention_items()

    def test_new_session_passes_cli_profile_tool_group_and_branch_options(self):
        commands = []
        outputs = iter([
            SimpleNamespace(returncode=0, stdout="ID: created-session\n", stderr=""),
            SimpleNamespace(returncode=0, stdout="", stderr=""),
            SimpleNamespace(returncode=0, stdout="", stderr=""),
        ])

        def run(command, **kwargs):
            commands.append(command)
            return next(outputs)

        results = queue.Queue()
        with patch.object(worker.subprocess, "run", side_effect=run), \
             patch.object(worker.time, "sleep") as sleep:
            worker.start_new_session("token", {
                "profile": "qa", "tool": "omp", "group": "reviews", "title": "Worktree session",
                "path": "/repo", "scratch": False, "worktree_branch": "feature/task",
                "new_branch": True, "base_branch": "main", "initial_turn": "Review this task.",
            }, results)
            event = results.get(timeout=3)
        self.assertEqual(commands, [
            ["aoe", "add", "--profile", "qa", "--tool", "omp",
             "--title", "Worktree session",
             "--group", "reviews", "--worktree", "feature/task", "--new-branch",
             "--base-branch", "main", "/repo"],
            ["aoe", "session", "start", "--profile", "qa", "created-session"],
            ["aoe", "send", "--no-revive", "--profile", "qa", "created-session",
             "Review this task."],
        ])
        sleep.assert_called_once_with(worker.INITIAL_MESSAGE_DELAY_SECONDS)
        self.assertTrue(event["completed"])
        self.assertEqual(event["session_id"], "created-session")
    def test_per_item_session_action_is_single_use_and_session_scoped(self):
        item = {
            "_plugin": "github", "kind": "pull_request", "title": "Fix regression",
            "repo_path": "/repositories/target", "head_branch": "feature/pr-42",
            "session_prompt": "Review this pull request.",
        }
        action = worker._new_session_action(item)
        token = "single-use-session-token"
        action_tokens = {token: action}
        consumed = set()
        requests = queue.Queue()
        with patch.object(worker, "live_session_ids", {"s1", "s2"}, create=True), \
             patch.object(worker, "action_tokens", action_tokens, create=True), \
             patch.object(worker, "action_token_sessions", {token: "s1"}, create=True), \
             patch.object(worker, "consumed_session_item_ids", consumed, create=True), \
             patch.object(worker, "action_requests", requests, create=True), \
             patch.object(worker, "active_action", None, create=True), \
             patch.object(worker, "send") as send:
            request = {
                "jsonrpc": "2.0", "id": "create-session",
                "method": "attention.new_session",
                "params": {"token": token, "session_id": "s1"},
            }
            foreign_request = {
                **request, "id": "foreign-session",
                "params": {"token": token, "session_id": "s2"},
            }
            worker.dispatch(foreign_request)
            foreign = send.call_args.args[0]["result"]
            self.assertFalse(foreign["ok"])
            self.assertIn("expired", foreign["error"])
            with self.assertRaises(queue.Empty):
                requests.get_nowait()
            send.reset_mock()
            worker.dispatch(request)
            send.assert_called_once_with({
                "jsonrpc": "2.0", "id": "create-session", "result": {"ok": True},
            })
            queued = requests.get_nowait()
            self.assertEqual(queued["action"]["params"]["worktree_branch"], "feature/pr-42")
            self.assertEqual(queued["action"]["params"]["initial_turn"], "Review this pull request.")
            self.assertEqual(consumed, {id(item)})

            send.reset_mock()
            worker.dispatch(request)
            self.assertFalse(send.call_args.args[0]["result"]["ok"])
            self.assertIn("expired", send.call_args.args[0]["result"]["error"])

    def test_new_session_without_sidebar_action_token_is_rejected(self):
        requests = queue.Queue()
        with patch.object(worker, "action_tokens", {}, create=True), \
             patch.object(worker, "action_requests", requests, create=True), \
             patch.object(worker, "active_action", None, create=True), \
             patch.object(worker, "send") as send:
            worker.dispatch({
                "jsonrpc": "2.0", "id": "form",
                "method": "attention.new_session",
                "params": {"title": "Worktree session", "path": "/repo"},
            })
            response = send.call_args.args[0]
            self.assertFalse(response["result"]["ok"])
            self.assertIn("expired", response["result"]["error"])
        with self.assertRaises(queue.Empty):
            requests.get_nowait()
    def test_pr_worktree_reuses_an_existing_local_head_branch(self):
        commands = []
        outputs = iter([
            SimpleNamespace(returncode=0),
            SimpleNamespace(returncode=0),
        ])

        def run(command, **kwargs):
            commands.append(command)
            return next(outputs)

        with patch.object(worker.subprocess, "run", side_effect=run):
            args = worker._pr_worktree_args("/repo", "feature/pr-42", "main")
        self.assertEqual(args, ["--worktree", "feature/pr-42"])
        self.assertEqual(commands, [
            ["git", "-C", "/repo", "check-ref-format", "--branch", "feature/pr-42"],
            ["git", "-C", "/repo", "show-ref", "--verify", "--quiet",
             "refs/heads/feature/pr-42"],
        ])

    def test_pr_worktree_creates_a_missing_head_from_the_target_branch(self):
        commands = []
        outputs = iter([
            SimpleNamespace(returncode=0),
            SimpleNamespace(returncode=1),
            SimpleNamespace(returncode=0),
        ])

        def run(command, **kwargs):
            commands.append(command)
            return next(outputs)

        with patch.object(worker.subprocess, "run", side_effect=run):
            args = worker._pr_worktree_args("/repo", "feature/pr-42", "main")
        self.assertEqual(args, [
            "--worktree", "feature/pr-42", "--new-branch",
            "--base-branch", "main",
        ])
        self.assertEqual(commands[-1], [
            "git", "-C", "/repo", "check-ref-format", "--branch", "main",
        ])

    def test_pr_worktree_rejects_invalid_head_and_missing_target(self):
        with patch.object(worker.subprocess, "run", return_value=SimpleNamespace(returncode=1)):
            with self.assertRaisesRegex(ValueError, "head branch"):
                worker._pr_worktree_args("/repo", "bad branch", "main")

        outputs = iter([
            SimpleNamespace(returncode=0),
            SimpleNamespace(returncode=1),
        ])
        with patch.object(worker.subprocess, "run", side_effect=lambda *a, **k: next(outputs)):
            with self.assertRaisesRegex(ValueError, "target branch"):
                worker._pr_worktree_args("/repo", "feature/pr-42", "")

    def test_new_session_runs_add_start_and_initial_send_in_cli_order(self):
        commands = []
        outputs = iter([
            SimpleNamespace(returncode=0, stdout="✓ Added session\n  ID: created-session\n", stderr=""),
            SimpleNamespace(returncode=0, stdout="", stderr=""),
            SimpleNamespace(returncode=0, stdout="", stderr=""),
        ])

        def run(command, **kwargs):
            commands.append(command)
            return next(outputs)

        options = {
            "title": "Attention: Fix regression",
            "path": "/repositories/target",
            "scratch": False,
            "is_pull_request": True,
            "worktree_branch": "feature/pr-42",
            "base_branch": "main",
            "initial_turn": "Review this pull request.",
        }
        results = queue.Queue()
        with patch.object(worker, "_pr_worktree_args", return_value=[
            "--worktree", "feature/pr-42", "--new-branch", "--base-branch", "main",
        ]) as worktree_args, \
             patch.object(worker.subprocess, "run", side_effect=run), \
             patch.object(worker.time, "sleep") as sleep:
            worker.start_new_session("token", options, results)
            event = results.get(timeout=3)
        worktree_args.assert_called_once_with(
            "/repositories/target", "feature/pr-42", "main",
        )
        self.assertEqual(commands, [
            ["aoe", "add", "--title", "Attention: Fix regression",
             "--worktree", "feature/pr-42", "--new-branch",
             "--base-branch", "main", "/repositories/target"],
            ["aoe", "session", "start", "created-session"],
            ["aoe", "send", "--no-revive", "created-session",
             "Review this pull request."],
        ])
        sleep.assert_called_once_with(worker.INITIAL_MESSAGE_DELAY_SECONDS)
        self.assertTrue(event["completed"])
        self.assertEqual(event["session_id"], "created-session")

    def test_start_failure_reports_created_session_without_sending_initial_turn(self):
        commands = []
        outputs = iter([
            SimpleNamespace(returncode=0, stdout="  ID: created-session\n", stderr=""),
            SimpleNamespace(returncode=1, stdout="", stderr="tmux unavailable"),
        ])

        def run(command, **kwargs):
            commands.append(command)
            return next(outputs)

        results = queue.Queue()
        with patch.object(worker.subprocess, "run", side_effect=run):
            worker.start_new_session("token", {
                "title": "Attention: Test", "path": "/repo", "scratch": False,
                "initial_turn": "Do work",
            }, results)
            event = results.get(timeout=3)
        self.assertEqual(commands, [
            ["aoe", "add", "--title", "Attention: Test", "/repo"],
            ["aoe", "session", "start", "created-session"],
        ])
        self.assertFalse(event["completed"])
        self.assertIn("Created created-session, but start failed", event["error"])

 

    def test_blocked_source_fetch_does_not_block_loading_or_refresh(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            attention = root / "attention"
            calls = root / "calls"
            gates = root / "gates"
            gates.mkdir()
            attention.write_text(
                "from pathlib import Path\n"
                "import time\n"
                f"CALLS = Path({str(calls)!r})\n"
                f"GATES = Path({str(gates)!r})\n"
                "def load_config(): return {}\n"
                "def build_prioritized_items(config):\n"
                "    count = int(CALLS.read_text()) + 1 if CALLS.exists() else 1\n"
                "    CALLS.write_text(str(count))\n"
                "    while not (GATES / f'release-{count}').exists(): time.sleep(0.01)\n"
                "    return [{'status':'now','context':'repo','title':f'Fetch {count}','details':'done'}]\n"
            )
            source = (ROOT / "aoe_worker.py").read_text().replace(
                'ATTENTION_PATH = Path(__file__).resolve().with_name("attention")',
                f'ATTENTION_PATH = Path({str(attention)!r})',
            ).replace("REFRESH_SECONDS = 60", "REFRESH_SECONDS = 0.05")
            worker_path = root / "aoe_worker.py"
            worker_path.write_text(source)
            proc = subprocess.Popen(
                [sys.executable, str(worker_path)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
            )

            stdout_buffer = bytearray()

            def receive(timeout=3):
                return _receive_json_line(proc.stdout, stdout_buffer, timeout, "worker did not respond before timeout")

            def reply(request, result=None):
                proc.stdin.write(json.dumps({
                    "jsonrpc": "2.0", "id": request["id"], "result": result or {},
                }) + "\n")
                proc.stdin.flush()

            try:
                listing = receive()
                self.assertEqual(listing["method"], "sessions.list")
                reply(listing, {"sessions": [{"id": "s1"}]})
                loading = receive()
                self.assertEqual(loading["method"], "ui.state.set")
                loading_payload = loading["params"]["payload"]
                self.assertEqual(loading_payload["blocks"][-1]["method"],
                                 "attention.refresh")
                self.assertEqual(loading_payload["default_location"], "right")
                reply(loading)
                time.sleep(0.15)
                self.assertFalse(calls.exists(), "worker fetched during startup or elapsed timer")
                proc.stdin.write(json.dumps({
                    "jsonrpc": "2.0", "id": "refresh",
                    "method": "attention.refresh", "params": {"session_id": "s1"},
                }) + "\n")
                proc.stdin.flush()
                self.assertEqual(receive()["id"], "refresh")
                deadline = time.monotonic() + 3
                while not calls.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(calls.exists(), "explicit refresh did not start source fetch")
                self.assertEqual(calls.read_text(), "1")

                for request_id in ("refresh-1", "refresh-2", "refresh-3"):
                    proc.stdin.write(json.dumps({
                        "jsonrpc": "2.0", "id": request_id,
                        "method": "attention.refresh", "params": {"session_id": "s1"},
                    }) + "\n")
                proc.stdin.flush()
                responses = []
                while len(responses) < 3:
                    response = receive()
                    if response.get("method") == "sessions.list":
                        reply(response, {"sessions": [{"id": "s1"}]})
                        continue
                    responses.append(response)
                self.assertTrue(all(response["result"]["ok"] for response in responses))

            finally:
                proc.kill()
                proc.wait(timeout=5)
                proc.stdin.close()
                proc.stdout.close()

    def test_transient_host_rpc_error_retries_after_delay(self):
        with tempfile.TemporaryDirectory() as tmp:
            attention = Path(tmp) / "attention"
            attention.write_text("def load_config(): return {}\ndef build_prioritized_items(config): return [{'status':'now','context':'QA','title':'Recovered','details':'ready'}]\n")
            source = (ROOT / "aoe_worker.py").read_text().replace(
                'ATTENTION_PATH = Path(__file__).resolve().with_name("attention")',
                f'ATTENTION_PATH = Path({str(attention)!r})',
            ).replace("SESSION_POLL_SECONDS = 60", "SESSION_POLL_SECONDS = 0.2")
            worker_path = Path(tmp) / "aoe_worker.py"
            worker_path.write_text(source)
            proc = subprocess.Popen(
                [sys.executable, str(worker_path)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )

            stdout_buffer = bytearray()

            def receive(timeout=3):
                return _receive_json_line(proc.stdout, stdout_buffer, timeout, "worker did not retry or publish before timeout")

            def reply(request, result=None, error=None):
                response = {"jsonrpc": "2.0", "id": request["id"]}
                if error:
                    response["error"] = {"message": error}
                else:
                    response["result"] = result or {}
                proc.stdin.write(json.dumps(response) + "\n")
                proc.stdin.flush()

            try:
                first = receive()
                self.assertEqual(first["method"], "sessions.list")
                reply(first, error="temporary store error")

                probe = {"jsonrpc": "2.0", "id": "probe", "method": "attention.unknown"}
                proc.stdin.write(json.dumps(probe) + "\n")
                proc.stdin.flush()
                response = receive()
                self.assertEqual(response["id"], "probe")
                self.assertFalse(response["result"]["ok"])

                retry = receive()
                self.assertEqual(retry["method"], "sessions.list")
                reply(retry, {"sessions": [{"id": "s1"}]})
                deadline = time.monotonic() + 3
                while True:
                    loading = receive(timeout=max(0, deadline - time.monotonic()))
                    if loading["method"] == "sessions.list":
                        reply(loading, {"sessions": [{"id": "s1"}]})
                        continue
                    self.assertEqual(loading["method"], "ui.state.set")
                    self.assertEqual(loading["params"]["payload"]["blocks"][-1]["method"],
                                     "attention.refresh")
                    reply(loading, {})
                    proc.stdin.write(json.dumps({
                        "jsonrpc": "2.0", "id": "refresh",
                        "method": "attention.refresh", "params": {"session_id": "s1"},
                    }) + "\n")
                    proc.stdin.flush()
                    response = receive(timeout=max(0, deadline - time.monotonic()))
                    while response.get("id") != "refresh":
                        if response.get("method") == "sessions.list":
                            reply(response, {"sessions": [{"id": "s1"}]})
                        elif response.get("method") == "ui.state.set":
                            reply(response, {})
                        else:
                            self.fail(f"unexpected host RPC before refresh response: {response}")
                        response = receive(timeout=max(0, deadline - time.monotonic()))
                    self.assertTrue(response["result"]["ok"])
                    break

                deadline = time.monotonic() + 3
                while True:
                    published = receive(timeout=max(0, deadline - time.monotonic()))
                    if published["method"] == "sessions.list":
                        reply(published, {"sessions": [{"id": "s1"}]})
                        continue
                    self.assertEqual(published["method"], "ui.state.set")
                    self.assertEqual(published["params"]["payload"]["blocks"][-1]["method"],
                                     "attention.refresh")
                    reply(published, {})
                    break
            finally:
                proc.kill()
                proc.wait(timeout=5)
                proc.stdin.close()
                proc.stdout.close()
                proc.stderr.close()

    def test_rpc_ignores_non_object_messages_before_host_response(self):
        request_id = worker.rpc.next_id
        with patch.object(worker, "send") as send, patch.object(
            worker, "read_message",
            side_effect=[None, [], "unexpected", {"id": request_id, "result": {"sessions": []}}],
        ):
            result = worker.rpc("sessions.list")
        self.assertEqual(result, {"sessions": []})
        send.assert_called_once()

    def test_refresh_publishes_live_session_and_removes_stale_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            attention = Path(tmp) / "attention"
            attention.write_text("def load_config(): return {}\ndef build_prioritized_items(config): return [{'status':'now','context':'repo','title':'Ship','details':'review'}]\n")
            source = (ROOT / "aoe_worker.py").read_text().replace(
                'ATTENTION_PATH = Path(__file__).resolve().with_name("attention")',
                f'ATTENTION_PATH = Path({str(attention)!r})',
            ).replace("SESSION_POLL_SECONDS = 60", "SESSION_POLL_SECONDS = 0.05")
            worker_path = Path(tmp) / "aoe_worker.py"
            worker_path.write_text(source)
            proc = subprocess.Popen([sys.executable, str(worker_path)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)

            stdout_buffer = bytearray()

            def receive(timeout=3):
                return _receive_json_line(proc.stdout, stdout_buffer, timeout, "worker did not publish before timeout")

            def reply(request, result=None):
                proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result or {}}) + "\n")
                proc.stdin.flush()

            try:
                listing = receive()
                self.assertEqual(listing["method"], "sessions.list")
                reply(listing, {"sessions": [{"id": "s1"}]})
                loading = receive()
                self.assertEqual(loading["method"], "ui.state.set")
                self.assertEqual(loading["params"]["payload"]["blocks"][-1]["method"],
                                 "attention.refresh")
                reply(loading)
                proc.stdin.write(json.dumps({
                    "jsonrpc": "2.0", "id": "refresh",
                    "method": "attention.refresh", "params": {"session_id": "s1"},
                }) + "\n")
                proc.stdin.flush()
                self.assertEqual(receive()["id"], "refresh")

                while True:
                    pushed = receive()
                    if pushed["method"] == "sessions.list":
                        reply(pushed, {"sessions": [{"id": "s1"}]})
                        continue
                    self.assertEqual(pushed["method"], "ui.state.set")
                    params = pushed["params"]
                    self.assertEqual((params["session_id"], params["slot"], params["id"]), ("s1", "pane", "attention"))
                    self.assertEqual(params["payload"]["blocks"][-1]["method"],
                                     "attention.refresh")
                    reply(pushed)
                    break

                stale_check = receive()
                self.assertEqual(stale_check["method"], "sessions.list")
                reply(stale_check, {"sessions": []})
                removed = receive()
                self.assertEqual(removed["method"], "ui.state.remove")
                self.assertEqual(removed["params"]["session_id"], "s1")
            finally:
                proc.kill()
                proc.wait(timeout=5)
                proc.stdin.close()
                proc.stdout.close()


class AttentionActionDispatchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        loader = importlib.machinery.SourceFileLoader("attention_action_test", str(ROOT / "attention"))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        cls.core = importlib.util.module_from_spec(spec)
        loader.exec_module(cls.core)

    def test_dispatch_preserves_plugin_payload_and_wip_transitions(self):
        class Plugin:
            def __init__(self):
                self.calls = []
            def act(self, key, payload):
                self.calls.append((key, payload))
                return True

        plugin = Plugin()
        action = {"_plugin": "generic", "_original_key": "o", "key": "1",
                  "payload": {"url": "private", "terminal": True}, "wip": True, "_wip_id": "generic:item"}
        with patch.object(self.core, "load_plugin", return_value=plugin), \
             patch.object(self.core, "mark_wip_item") as mark, \
             patch.object(self.core, "unmark_wip_item") as unmark:
            self.assertTrue(self.core.dispatch_item_action(action))
            self.assertEqual(plugin.calls, [("o", {"url": "private", "terminal": True})])
            mark.assert_called_once_with("generic:item")
            unmark.assert_not_called()
            action["wip"] = "clear"
            self.assertTrue(self.core.dispatch_item_action(action))
            unmark.assert_called_once_with("generic:item")

    def test_terminal_act_keeps_its_row_key_contract(self):
        import base64
        action = {"key": "o", "_plugin": "generic", "payload": {}}
        line = "row\t" + base64.b64encode(json.dumps([action]).encode()).decode()
        with patch.object(self.core, "dispatch_item_action", return_value=True) as dispatch:
            self.assertTrue(self.core.act("o", line))
        dispatch.assert_called_once_with(action)

    def test_worker_action_cli_keeps_plugin_stdout_off_json_channel(self):
        import io
        import contextlib
        action = {"_plugin": "fake", "key": "o", "payload": {}}
        output = io.StringIO()
        diagnostics = io.StringIO()
        with patch.object(self.core, "dispatch_item_action", side_effect=lambda _: (print("provider output") or True)), \
             patch("sys.stdin", io.StringIO(json.dumps(action))), \
             patch("sys.stderr", diagnostics), contextlib.redirect_stdout(output):
            self.assertEqual(self.core.worker_action_cli(), 0)
        self.assertEqual(json.loads(output.getvalue()), {"ok": True})
        self.assertEqual(diagnostics.getvalue(), "provider output\n")

    def test_run_cmd_output_keeps_worker_json_channel_valid(self):
        import contextlib
        import io

        loader = importlib.machinery.SourceFileLoader(
            "attention_redirected_util_test", str(ROOT / "sources" / "_util.py"),
        )
        spec = importlib.util.spec_from_loader(loader.name, loader)
        util = importlib.util.module_from_spec(spec)
        loader.exec_module(util)

        action = {"_plugin": "fake", "key": "o", "payload": {}}
        output = io.StringIO()
        diagnostics = io.StringIO()
        with patch.object(
                self.core, "dispatch_item_action",
                side_effect=lambda _: util.run_cmd([
                    sys.executable, "-c", "print('command output')",
                ]),
        ), patch("sys.stdin", io.StringIO(json.dumps(action))), \
                patch("sys.stderr", diagnostics), contextlib.redirect_stdout(output):
            self.assertEqual(self.core.worker_action_cli(), 0)

        self.assertEqual(json.loads(output.getvalue()), {"ok": True})
        self.assertIn("command output", diagnostics.getvalue())





class WorkerActionProtocolTests(unittest.TestCase):
    def test_actions_are_session_scoped_and_prompt_actions_fail_in_sidebar(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            attention = root / "attention"
            fetches = root / "fetches"
            dispatched = root / "dispatched.json"
            attention.write_text(
                "import json, sys\nfrom pathlib import Path\n"
                f"FETCHES = Path({str(fetches)!r})\nDISPATCHED = Path({str(dispatched)!r})\n"
                "def load_config(): return {}\n"
                "def build_prioritized_items(config):\n"
                "    count = int(FETCHES.read_text()) + 1 if FETCHES.exists() else 1\n"
                "    FETCHES.write_text(str(count))\n"
                "    return [{'status':'NOW','context':'QA','title':'Fetch','details':'ready',"
                "'weight':10,'_plugin':'github','kind':'issue',"
                "'repo_resolution_error':'Attention item has no AoE project path.','actions':["
                "{'key':'o','label':'Run QA','primary':True,'_plugin':'generic',"
                "'_original_key':'o','payload':{'command':['echo','run']}},"
                "{'key':'p','label':'Prompt QA','_plugin':'generic',"
                "'_original_key':'p','payload':{'command':['echo','prompt']}}]},"
                "{'status':'WAITING','context':'QA','title':'Second item',"
                "'details':'selected detail','attention_reason':'Second item selected',"
                "'weight':5,'_plugin':'generic','kind':'task','id':'second','actions':[]}]\n"
                "if len(sys.argv) > 1 and sys.argv[1] == '__worker_action_interactive__':\n"
                "    action = json.loads(sys.stdin.readline())\n"
                "    DISPATCHED.write_text(json.dumps(action))\n"
                "    command = action['payload']['command'][-1]\n"
                "    if command == 'prompt':\n"
                "        print(json.dumps({'prompt': {'text':'Confirm action?'}}), flush=True)\n"
                "        sys.stdin.readline()\n"
                "    else:\n"
                "        sys.stderr.write('source action output\\n')\n"
                "        print(json.dumps({'ok': True}))\n"
            )
            source = (ROOT / "aoe_worker.py").read_text().replace(
                'ATTENTION_PATH = Path(__file__).resolve().with_name("attention")',
                f'ATTENTION_PATH = Path({str(attention)!r})',
            )
            worker_path = root / "aoe_worker.py"
            worker_path.write_text(source)
            proc = subprocess.Popen(
                [sys.executable, str(worker_path)], stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            stdout_buffer = bytearray()

            def receive(timeout=3):
                return _receive_json_line(
                    proc.stdout, stdout_buffer, timeout, "worker did not send a protocol message",
                )

            def reply(request, result=None):
                proc.stdin.write(json.dumps({
                    "jsonrpc": "2.0", "id": request["id"], "result": result or {},
                }) + "\n")
                proc.stdin.flush()

            def send_action(request_id, token, session_id, method="attention.action"):
                proc.stdin.write(json.dumps({
                    "jsonrpc": "2.0", "id": request_id, "method": method,
                    "params": {"token": token, "session_id": session_id},
                }) + "\n")
                proc.stdin.flush()

            def find_action(pane, label):
                pending = list(pane["blocks"])
                while pending:
                    block = pending.pop()
                    if not isinstance(block, dict):
                        continue
                    if block.get("label") == label and block.get("method"):
                        return block
                    children = block.get("children")
                    if isinstance(children, list):
                        pending.extend(children)
                raise AssertionError(f"no sidebar action named {label!r}")

            def service(message):
                if message.get("method") == "sessions.list":
                    reply(message, {"sessions": [
                        {"id": "s1", "project_path": "/repo"},
                        {"id": "s2", "project_path": "/other"},
                    ]})
                    return None
                if message.get("method") == "ui.state.set":
                    params = message["params"]
                    self.assertEqual(params["slot"], "pane")
                    self.assertIn(params["session_id"], {"s1", "s2"})
                    reply(message)
                    return params["session_id"], params["payload"]
                return None

            def wait_response(request_id, timeout=4):
                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline:
                    message = receive(max(0.01, deadline - time.monotonic()))
                    if message.get("method") in {"sessions.list", "ui.state.set"}:
                        service(message)
                    elif message.get("id") == request_id:
                        return message
                self.fail(f"worker did not reply to {request_id}")

            def wait_pane(predicate, session_id="s1", timeout=5):
                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline:
                    message = receive(max(0.01, deadline - time.monotonic()))
                    pane = service(message)
                    if (pane is not None and pane[0] == session_id
                            and predicate(pane[1])):
                        return pane[1]
                self.fail(f"worker did not publish the expected sidebar state for {session_id}")

            try:
                listing = receive()
                self.assertEqual(listing["method"], "sessions.list")
                reply(listing, {"sessions": [
                    {"id": "s1", "project_path": "/repo"},
                    {"id": "s2", "project_path": "/other"},
                ]})
                loading = receive()
                self.assertEqual(loading["method"], "ui.state.set")
                self.assertEqual(loading["params"]["slot"], "pane")
                reply(loading)

                proc.stdin.write(json.dumps({
                    "jsonrpc": "2.0", "id": "refresh", "method": "attention.refresh",
                    "params": {"session_id": "s1"},
                }) + "\n")
                proc.stdin.flush()
                self.assertTrue(wait_response("refresh")["result"]["ok"])
                pane = wait_pane(lambda value: find_action(value, "Run QA") is not None)
                run_action = find_action(pane, "Run QA")
                token = run_action["params"]["token"]
                self.assertNotIn('"echo", "run"', json.dumps(pane))
                other_pane = wait_pane(
                    lambda value: find_action(value, "Run QA") is not None,
                    session_id="s2",
                )
                other_token = find_action(other_pane, "Run QA")["params"]["token"]
                self.assertNotEqual(token, other_token)

                send_action("wrong-session", token, "other")
                wrong = wait_response("wrong-session")
                self.assertFalse(wrong["result"]["ok"])
                self.assertIn("Unknown session", wrong["result"]["error"])
                send_action("foreign-live-session", token, "s2")
                foreign = wait_response("foreign-live-session")
                self.assertFalse(foreign["result"]["ok"])
                self.assertIn("expired", foreign["result"]["error"])

                send_action("run", token, "s1")
                self.assertTrue(wait_response("run")["result"]["ok"])
                send_action("duplicate", token, "s1")
                duplicate = wait_response("duplicate")
                self.assertFalse(duplicate["result"]["ok"])
                self.assertIn("already running", duplicate["result"]["error"])

                completed = wait_pane(
                    lambda value: any(
                        block.get("text", "").startswith("Completed: Run QA")
                        for block in value["blocks"] if isinstance(block, dict)
                    )
                )
                send_action("stale-after-completion", token, "s1")
                stale = wait_response("stale-after-completion")
                self.assertFalse(stale["result"]["ok"])
                self.assertIn("expired", stale["result"]["error"])
                self.assertTrue(dispatched.exists(), "source action child was not invoked")
                self.assertEqual(json.loads(dispatched.read_text())["payload"]["command"],
                                 ["echo", "run"])
                self.assertIn("source action output", json.dumps(completed))
                self.assertEqual(fetches.read_text(), "1", "action completion fetched implicitly")

                new_session = find_action(completed, "New session")
                send_action(
                    "create-session", new_session["params"]["token"], "s1",
                    method="attention.new_session",
                )
                rejected = wait_response("create-session")
                self.assertFalse(rejected["result"]["ok"])
                self.assertIn(
                    "Attention item has no AoE project path.", rejected["result"]["error"],
                )
                self.assertEqual(fetches.read_text(), "1", "session creation fetched implicitly")

                prompt = find_action(completed, "Prompt QA")
                send_action("prompt", prompt["params"]["token"], "s1")
                self.assertTrue(wait_response("prompt")["result"]["ok"])
                failed = wait_pane(
                    lambda value: any(
                        "requires interactive input" in block.get("text", "")
                        for block in value["blocks"] if isinstance(block, dict)
                    )
                )
                self.assertNotEqual(
                    find_action(failed, "Prompt QA")["params"]["token"],
                    prompt["params"]["token"],
                )
                self.assertEqual(fetches.read_text(), "1", "actions fetched implicitly")
                prompt_token = find_action(failed, "Prompt QA")["params"]["token"]
                queue = next(block for block in failed["blocks"]
                             if block.get("title") == "Work queue")
                second = next(row for row in queue["children"]
                              if row["label"] == "Second item")
                proc.stdin.write(json.dumps({
                    "jsonrpc": "2.0", "id": "select-second",
                    "method": "attention.select_item",
                    "params": {
                        "session_id": "s1",
                        "item_key": second["params"]["item_key"],
                    },
                }) + "\n")
                proc.stdin.flush()
                self.assertTrue(wait_response("select-second")["result"]["ok"])
                selected = wait_pane(lambda value: any(
                    row.get("selected") and row.get("label") == "Second item"
                    for block in value["blocks"] if block.get("title") == "Work queue"
                    for row in block.get("children", [])
                ))
                queue = next(block for block in selected["blocks"]
                             if block.get("title") == "Work queue")
                self.assertEqual(
                    [row["label"] for row in queue["children"] if row.get("selected")],
                    ["Second item"],
                )
                detail = next(
                    block for block in selected["blocks"]
                    if block.get("kind") == "callout"
                )
                self.assertEqual(detail["title"], "Second item")
                self.assertEqual(detail["detail"],
                                 "Second item selected · selected detail")
                send_action("expired-hidden-action", prompt_token, "s1")
                expired = wait_response("expired-hidden-action")
                self.assertFalse(expired["result"]["ok"])
                self.assertIn("expired", expired["result"]["error"])
                self.assertEqual(fetches.read_text(), "1", "selection fetched implicitly")
            finally:
                proc.kill()
                proc.wait(timeout=5)
                proc.stdin.close()
                proc.stdout.close()
                proc.stderr.close()
if __name__ == "__main__":
    unittest.main()
