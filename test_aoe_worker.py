#!/usr/bin/env python3
"""Behavioral tests for the Agent of Empires pane worker."""
import importlib.util
import json
import os
from pathlib import Path
import select
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("aoe_worker", ROOT / "aoe_worker.py")
worker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(worker)


def _first_item_card(payload):
    return next(
        block for block in worker._walk_blocks(payload["blocks"])
        if block.get("kind") == "section" and block.get("collapsible") is not True
    )

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


class PanePayloadTests(unittest.TestCase):
    def test_empty_items_have_explicit_empty_state_and_refresh(self):
        payload = worker.pane_payload([])
        self.assertEqual(payload["default_location"], "right")
        toolbar = payload["blocks"][0]
        self.assertEqual(toolbar["kind"], "columns")
        self.assertEqual([action["label"] for action in toolbar["children"]], ["Refresh"])
        self.assertEqual(toolbar["children"][0]["method"], "attention.refresh")
        self.assertEqual(payload["blocks"][1], {"kind": "note", "text": "No attention items."})

    def test_status_notice_precedes_cards_without_repeating_the_pane_heading(self):
        payload = worker.status_payload("Source refresh failed; showing cached items.")
        self.assertEqual(payload["blocks"][0]["kind"], "columns")
        self.assertEqual(payload["blocks"][1], {
            "kind": "note", "text": "Source refresh failed; showing cached items."
        })

    def test_cards_are_compact_and_the_primary_row_opens_the_source(self):
        item = {
            "_plugin": "github", "status": "NEEDS ATTENTION", "context": "private-repo",
            "title": "Ship release", "details": "Review required", "weight": 99,
            "repo_path": "/repositories/private-repo",
            "repo_project_path": "/registered/private-repo",
            "actions": [
                {"_plugin": "github", "key": "o", "label": "open", "primary": True,
                 "payload": {"kind": "open", "url": "https://example.com/issue", "token": "secret-token"}},
                {"_plugin": "github", "key": "m", "label": "Merge", "payload": {"kind": "merge", "command": "secret-merge"}},
                {"_plugin": "github", "key": "c", "label": "Comment", "payload": {"kind": "comment"}},
                {"_plugin": "generic", "key": "2", "label": "Prompt", "payload": {"command": ["echo"], "inputs": [{"prompt": "secret-prompt"}]}},
                {"_plugin": "generic", "key": "3", "label": "Run", "payload": {"command": ["echo", "secret-command"]}},
                {"_plugin": "github", "key": "l", "label": "Lumen",
                 "payload": {"command": ["/opt/homebrew/bin/lumen", "diff", "https://private.example/issue"]}},
            ],
        }
        tokens = iter(["opaque-run", "opaque-session", "opaque-filter"])
        payload, actions = worker.build_pane([item], token_factory=lambda: next(tokens))
        card = _first_item_card(payload)
        self.assertEqual(set(card), {"kind", "boxed", "children"})
        self.assertTrue(card["boxed"])
        row = card["children"][0]
        self.assertEqual(row["icon"], "git-branch")
        self.assertEqual(row["href"], "https://example.com/issue")
        button_group = card["children"][1]
        buttons = button_group["children"]
        self.assertEqual([button["label"] for button in buttons], ["Run", "New session"])
        self.assertEqual([button["params"] for button in buttons], [
            {"token": "opaque-run"}, {"token": "opaque-session"},
        ])
        self.assertEqual(buttons[-1]["method"], "attention.new_session")
        self.assertEqual(set(actions), {"opaque-filter", "opaque-run", "opaque-session"})
        self.assertEqual(actions["opaque-run"], item["actions"][4])
        self.assertEqual(actions["opaque-session"], {
            "_aoe_operation": "new_session", "title": "Attention: Ship release",
            "repo_path": "/repositories/private-repo",
            "project_path": "/registered/private-repo",
        })
        self.assertEqual(actions["opaque-filter"]["filter_kind"], "source")
        rendered = json.dumps(payload)
        for secret in ("secret-token", "secret-merge", "secret-command", "secret-prompt", "private.example"):
            self.assertNotIn(secret, rendered)

    def test_new_session_selects_registered_project_for_github_items(self):
        items = [
            {
                "_plugin": "github", "kind": "pull_request", "title": "PR",
                "repo_path": "/worktrees/repo", "repo_project_path": "/registered/repo",
                "head_branch": "feature/pr-42", "base_branch": "main",
            },
            {
                "_plugin": "github", "kind": "issue", "title": "Issue",
                "repo_path": "/worktrees/repo", "repo_project_path": "/registered/repo",
            },
        ]
        tokens = iter(str(index) for index in range(20))
        _, actions = worker.build_pane(items, token_factory=lambda: next(tokens))
        sessions = {
            action["title"]: action for action in actions.values()
            if action.get("_aoe_operation") == "new_session"
        }
        pr_action = sessions["Attention: PR"]
        self.assertEqual(pr_action["project_path"], "/registered/repo")
        self.assertEqual(pr_action["head_branch"], "feature/pr-42")
        self.assertEqual(pr_action["base_branch"], "main")
        self.assertTrue(pr_action["is_pull_request"])
        issue_action = sessions["Attention: Issue"]
        self.assertEqual(issue_action["project_path"], "/registered/repo")
        self.assertNotIn("head_branch", issue_action)
        self.assertNotIn("base_branch", issue_action)

    def test_source_and_provider_filters_hide_only_matching_items(self):
        items = [
            {"_plugin": "github", "title": "PR"},
            {"_plugin": "linear", "title": "Issue"},
            {"_plugin": "generic", "_provider": "alpha", "title": "Alpha item"},
            {"_plugin": "generic", "_provider": "beta", "title": "Beta item"},
        ]
        token = iter(range(20))
        payload, actions = worker.build_pane(
            items, token_factory=lambda: str(next(token)),
            hidden_sources={"github"}, hidden_providers={"alpha"},
        )
        cards = [
            block for block in payload["blocks"]
            if block.get("kind") == "section" and block.get("collapsible") is not True
        ]
        labels = [card["children"][0]["label"] for card in cards]
        self.assertEqual(labels, ["Issue", "Beta item"])
        controls = next(
            block for block in payload["blocks"]
            if block.get("kind") == "section" and block.get("title") == "Filters"
        )
        filter_labels = {block["label"] for block in controls["children"]}
        self.assertIn("Show source: github", filter_labels)
        self.assertIn("Show provider: alpha", filter_labels)
        self.assertIn("Hide source: linear", filter_labels)
        self.assertIn("Hide provider: beta", filter_labels)
        self.assertEqual(set(actions), worker._payload_action_tokens(payload))

    def test_filter_action_toggles_source_and_rejects_stale_session(self):
        names = ("live_session_ids", "action_tokens", "hidden_sources", "filter_refresh_requested")
        original = {name: getattr(worker, name, None) for name in names}
        try:
            worker.live_session_ids = {"s1"}
            worker.action_tokens = {
                "filter": {
                    "_aoe_operation": "filter",
                    "filter_kind": "source",
                    "value": "github",
                },
            }
            worker.hidden_sources = set()
            worker.filter_refresh_requested = False
            worker.dispatch({
                "method": "attention.filter",
                "params": {"token": "filter", "session_id": "s1"},
            })
            self.assertEqual(worker.hidden_sources, {"github"})
            self.assertTrue(worker.filter_refresh_requested)
            self.assertNotIn("filter", worker.action_tokens)
        finally:
            for name, value in original.items():
                if value is None:
                    delattr(worker, name)
                else:
                    setattr(worker, name, value)

    def test_pull_request_stacks_are_collapsible_with_ordered_independent_cards(self):
        items = [
            {"_plugin": "github", "title": "Grandchild", "identity_key": "pr:3",
             "parent_identity_key": "pr:2", "url": "https://example.com/3"},
            {"_plugin": "github", "title": "Child", "identity_key": "pr:2",
             "parent_identity_key": "pr:1", "url": "https://example.com/2",
             "repo_path": "/repo", "head_branch": "feature",
             "actions": [{"label": "Run child", "payload": {"command": ["echo", "child"]}}]},
            {"_plugin": "github", "title": "Base", "identity_key": "pr:1",
             "url": "https://example.com/1"},
            {"_plugin": "github", "title": "Unrelated", "identity_key": "pr:4",
             "url": "https://example.com/4"},
        ]
        token_number = iter(range(9))
        payload, actions = worker.build_pane(
            items, token_factory=lambda: f"token-{next(token_number)}",
        )
        top_level = [
            block for block in payload["blocks"]
            if block.get("kind") == "section" and block.get("title") != "Filters"
        ]
        self.assertEqual(len(top_level), 3)
        base, stack, unrelated = top_level
        self.assertEqual(base["children"][0]["label"], "Base")
        self.assertEqual(stack["title"], "Stacked pull requests")
        self.assertEqual(stack["value"], "2 PRs")
        self.assertTrue(stack["collapsible"])
        self.assertTrue(stack["collapsed"])
        cards = stack["children"]
        self.assertEqual(
            [card["children"][0]["label"] for card in cards],
            ["└─ Child", "   └─ Grandchild"],
        )
        self.assertEqual(unrelated["children"][0]["label"], "Unrelated")
        child_buttons = cards[0]["children"][1]["children"]
        self.assertEqual([button["label"] for button in child_buttons], ["Run child", "New session"])
        linked = worker.payload_with_sessions(payload, items, [{
            "id": "stack-child", "title": "Work", "status": "Running",
            "workspace_repos": [{"source_path": "/repo", "branch": "feature"}],
        }])
        linked_stack = next(
            block for block in linked["blocks"]
            if block.get("kind") == "section" and block.get("title") == "Stacked pull requests"
        )
        self.assertEqual(linked_stack["children"][0]["children"][1]["href"], "/session/stack-child")
        self.assertEqual(set(actions), worker._payload_action_tokens(payload))
        self.assertEqual(len(cards), 2)
        self.assertEqual(items[0]["title"], "Grandchild")
        orphan_payload = worker.pane_payload([{
            "_plugin": "github", "title": "Orphan", "identity_key": "pr:5",
            "parent_identity_key": "pr:missing",
        }])
        orphan = next(block for block in worker._walk_blocks(orphan_payload["blocks"])
                      if block.get("kind") == "row")
        self.assertEqual(orphan["label"], "Orphan")



    def test_known_source_icons_are_rendered_on_rows(self):
        expected_icons = {
            "github": "git-branch",
            "linear": "circle-dot",
            "calendar": "calendar-days",
            "reminders": "list-todo",
            "generic": "inbox",
        }
        for source, icon in expected_icons.items():
            with self.subTest(source=source):
                payload = worker.pane_payload([{
                    "_plugin": source, "title": "Readable title",
                    "url": "https://example.com/item",
                }])
                row = _first_item_card(payload)["children"][0]
                self.assertEqual(row["icon"], icon)
                self.assertEqual(row["label"], "Readable title")
                self.assertEqual(row["href"], "https://example.com/item")


    def test_source_icon_uses_item_collector_not_cross_linked_actions(self):
        payload = worker.pane_payload([{
            "_plugin": "linear", "title": "Linked issue",
            "actions": [{"_plugin": "github", "label": "Open",
                         "payload": {"kind": "open", "url": "https://github.com/example/repo/pull/1"}}],
        }])
        row = _first_item_card(payload)["children"][0]
        self.assertEqual(row["icon"], "circle-dot")
        self.assertEqual(row["href"], "https://github.com/example/repo/pull/1")

    def test_pull_request_rows_render_state_iconography(self):
        cases = [
            ("CI failing", "circle-x", "danger", "ci failing"),
            ("Changes requested", "circle-x", "danger", "changes requested"),
            ("Approved", "badge-check", "success", "approved"),
        ]
        for state, icon, tone, tooltip in cases:
            with self.subTest(state=state):
                payload = worker.pane_payload([{
                    "_plugin": "github", "kind": "pull_request",
                    "title": "Review PR", "indicators": {"state": state},
                }])
                row = _first_item_card(payload)["children"][0]
                self.assertEqual(row["badges"], [{"icon": icon, "tone": tone, "tooltip": tooltip}])

    def test_active_matching_session_gets_an_encoded_in_app_link(self):
        item = {"title": "Review PR", "repo_path": "/repo", "head_branch": "feature"}
        payload = worker.pane_payload([item])
        linked = worker.payload_with_sessions(payload, [item], [{
            "id": "session/id", "title": "Work", "status": "Running",
            "workspace_repos": [{"source_path": "/repo", "branch": "feature"}],
        }])
        card = _first_item_card(linked)
        self.assertEqual(card["children"][1]["href"], "/session/session%2Fid")
        self.assertEqual(card["children"][1]["sublabel"], "Running")
        self.assertNotIn("href", _first_item_card(payload)["children"][0])

    def test_missing_or_inactive_session_does_not_add_session_link(self):
        item = {"title": "Review PR", "repo_path": "/repo", "head_branch": "feature"}
        payload = worker.pane_payload([item])
        for sessions in (
            [],
            [{"id": "idle", "status": "Idle", "workspace_repos": [
                {"source_path": "/repo", "branch": "feature"},
            ]}],
            [{"id": "archived", "status": "Running", "archived": True, "workspace_repos": [
                {"source_path": "/repo", "branch": "feature"},
            ]}],
        ):
            with self.subTest(sessions=sessions):
                linked = worker.payload_with_sessions(payload, [item], sessions)
                self.assertEqual(len(_first_item_card(linked)["children"]), 2)

    def test_attention_created_session_title_links_only_when_unique(self):
        item = {"title": "Fix build"}
        session = {"id": "session-1", "title": "Attention: Fix build", "status": "Waiting"}
        payload = worker.pane_payload([item])
        linked = worker.payload_with_sessions(payload, [item], [session])
        self.assertEqual(_first_item_card(linked)["children"][1]["href"], "/session/session-1")
        duplicate = worker.pane_payload([item, item])
        linked = worker.payload_with_sessions(duplicate, [item, item], [session])
        self.assertFalse(any(
            child.get("icon") == "square-terminal"
            for block in linked["blocks"] if block["kind"] == "section"
            for child in block["children"]
        ))

    def test_unknown_or_missing_collectors_use_fallback_not_url_or_context(self):
        for collector in ("custom", None):
            with self.subTest(collector=collector):
                item = {
                    "title": "Custom item", "context": "github",
                    "url": "https://github.com/example/repo/issues/1",
                }
                if collector is not None:
                    item["_plugin"] = collector
                row = _first_item_card(worker.pane_payload([item]))["children"][0]
                self.assertEqual(row["icon"], "inbox")
                self.assertEqual(row["href"], item["url"])

    def test_own_pull_request_hides_approve_and_keeps_another_supported_action(self):
        item = {
            "_plugin": "github", "title": "Fix release",
            "actions": [
                {"_plugin": "github", "label": "approve", "_is_own_pr": True, "payload": {"kind": "approve"}},
                {"_plugin": "generic", "label": "Run check", "payload": {"command": ["echo", "check"]}},
            ],
        }
        tokens = iter(["run-token", "session-token", "filter-token"])
        payload, actions = worker.build_pane([item], token_factory=lambda: next(tokens))
        card = _first_item_card(payload)
        buttons = card["children"][1]["children"]
        self.assertEqual([button["label"] for button in buttons], ["Run check", "New session"])
        self.assertNotIn("approve", json.dumps(payload).casefold())
        self.assertEqual(set(actions), {"run-token", "session-token", "filter-token"})

    def test_each_item_shows_at_most_one_source_action_plus_new_session(self):
        item = {
            "title": "Build",
            "actions": [
                {"label": label, "payload": {"command": ["echo", label]}}
                for label in ("First", "Second", "Third")
            ],
        }
        tokens = iter(["first-token", "session-token"])
        payload, actions = worker.build_pane([item], token_factory=lambda: next(tokens))
        card = next(block for block in payload["blocks"] if block["kind"] == "section")
        buttons = card["children"][1]["children"]
        self.assertEqual([button["label"] for button in buttons], ["First", "New session"])
        self.assertLessEqual(len(buttons), 2)
        self.assertEqual(set(actions), {"first-token", "session-token"})

    def test_command_based_open_uses_the_linked_row_without_an_open_button(self):
        open_action = {
            "_plugin": "generic", "key": "open", "label": "Open",
            "payload": {"command": ["open", "https://example.com/build"]},
        }
        tokens = iter(["open-token", "session-token"])
        payload, actions = worker.build_pane(
            [{"title": "Build failed", "actions": [open_action]}],
            token_factory=lambda: next(tokens),
        )
        card = next(block for block in payload["blocks"] if block["kind"] == "section")
        row = card["children"][0]
        self.assertEqual(row["method"], "attention.action")
        self.assertEqual(row["params"], {"token": "open-token"})
        buttons = card["children"][1]["children"]
        self.assertEqual([button["label"] for button in buttons], ["New session"])
        self.assertEqual(actions["open-token"], open_action)

    def test_unsafe_source_urls_are_not_rendered_as_links(self):
        payload = worker.pane_payload([{"title": "Unsafe", "url": "javascript:alert(1)"}])
        row = next(block for block in payload["blocks"] if block["kind"] == "section")["children"][0]
        self.assertNotIn("href", row)

    def test_summary_deduplicates_status_context_and_reason(self):
        payload = worker.pane_payload([{
            "status": "OVERDUE", "context": "Accounting", "title": "Pay invoice",
            "attention_reason": " overdue ", "details": "OVERDUE",
        }])
        card = next(block for block in payload["blocks"] if block["kind"] == "section")
        self.assertEqual(card["children"][0]["sublabel"], "OVERDUE · Accounting")
        self.assertNotIn("Details", json.dumps(card))

    def test_trimming_keeps_priority_prefix_prunes_tokens_and_obeys_host_limit(self):
        items = [{
            "_plugin": "generic", "status": "NOW", "context": "repo",
            "title": f"Item {index}", "details": "x" * 500, "weight": 100 - index,
            "actions": [{"_plugin": "generic", "key": "o", "label": "Open",
                         "payload": {"url": f"https://example.com/{index}"}}],
        } for index in range(12)]
        token_number = iter(range(40))
        token_factory = lambda: f"token-{next(token_number)}"
        with patch.object(worker, "MAX_PANE_BYTES", 2400):
            payload, actions = worker.build_pane(items, token_factory=token_factory)
        cards = [
            block for block in payload["blocks"]
            if block.get("kind") == "section" and block.get("title") != "Filters"
        ]
        titles = [card["children"][0]["label"] for card in cards]
        self.assertLess(len(titles), len(items))
        self.assertEqual(titles, [f"Item {index}" for index in range(len(titles))])
        self.assertIn("Additional items omitted", json.dumps(payload))
        visible_tokens = worker._payload_action_tokens(payload)
        self.assertEqual(set(actions), visible_tokens)
        self.assertLess(len(actions), 2 * len(items))
        encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.assertLessEqual(len(encoded), 2400)
        huge = [{"status": "NOW", "context": "repo", "title": str(i), "details": "界" * 600}
                for i in range(500)]
        with patch.object(worker, "MAX_PANE_BYTES", 64 * 1024):
            encoded = json.dumps(worker.pane_payload(huge), separators=(",", ":")).encode("utf-8")
        self.assertLessEqual(len(encoded), worker.MAX_PANE_BYTES)



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
    def test_manifest_requests_unattended_session_creation(self):
        import tomllib

        manifest = tomllib.loads((ROOT / "aoe-plugin.toml").read_text())
        self.assertIn("session.create", manifest["capabilities"])
        self.assertIn("session.prompt", manifest["capabilities"])
        self.assertIn("session.unattended", manifest["capabilities"])

    def test_missing_agent_setting_does_not_request_session_creation(self):
        request = {
            "token": "single-use-token",
            "session_id": "s1",
            "action": {
                "title": "Attention: Review PR",
                "project_path": "/repos/app",
            },
        }
        cli_sessions = json.dumps([{"id": "s1", "profile": "test-profile", "group": ""}])
        with patch.dict(worker.live_sessions_by_id, {"s1": {"id": "s1"}}):
            with patch.object(worker.subprocess, "run", return_value=subprocess.CompletedProcess(
                args=["aoe"], returncode=0, stdout=cli_sessions, stderr="",
            )) as run:
                with patch.object(worker, "rpc", return_value={"value": None}) as rpc:
                    with self.assertRaisesRegex(RuntimeError, "Choose an ACP agent"):
                        worker.create_session(request)
        rpc.assert_called_once_with("config.get", {"key": "agent_id"})
        run.assert_called_once()


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

    def test_cli_and_pane_show_sanitized_partial_and_total_collector_failures(self):
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
                pane_text = json.dumps(worker.pane_payload(worker.attention_items()))
            self.assertIn("Healthy item", pane_text)
            self.assertIn("failing collector failed: RuntimeError", pane_text)
            self.assertNotIn("collector-secret", pane_text)

            set_plugins([failing])
            output = run_cli()
            self.assertIn("failing collector failed", output)
            self.assertNotIn("Nothing needs attention", output)
            with patch.dict(os.environ, env), patch.object(worker, "ATTENTION_PATH", ROOT / "attention"):
                pane_text = json.dumps(worker.pane_payload(worker.attention_items()))
            self.assertIn("failing collector failed: RuntimeError", pane_text)
            self.assertNotIn("No attention items.", pane_text)

            healthy.write_text("def fetch(config):\n    return []\n")
            set_plugins([healthy])
            self.assertEqual(run_cli(), "")
            with patch.dict(os.environ, env), patch.object(worker, "ATTENTION_PATH", ROOT / "attention"):
                pane_text = json.dumps(worker.pane_payload(worker.attention_items()))
            self.assertIn("No attention items.", pane_text)
            self.assertNotIn("collector failed", pane_text)

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
    def test_pr_session_refuses_missing_repository_or_head_branch(self):
        cases = (
            (
                "missing repository",
                {"is_pull_request": True, "head_branch": "feature/pr-42"},
                "no AoE project path",
            ),
            (
                "missing head branch",
                {"is_pull_request": True, "project_path": "/repos/app"},
                "no valid head branch",
            ),
            (
                "malformed head branch",
                {
                    "is_pull_request": True,
                    "project_path": "/repos/app",
                    "head_branch": "feature/pr-42 ",
                },
                "no valid head branch",
            ),
        )
        with patch.object(worker, "rpc") as rpc:
            for label, action, message in cases:
                with self.subTest(label=label):
                    with self.assertRaisesRegex(RuntimeError, message):
                        worker.create_session({
                            "action": action, "session_id": "s1", "token": "token",
                        })
            rpc.assert_not_called()

    def test_new_session_action_creates_an_isolated_worktree_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            attention = root / "attention"
            repo = root / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.com"], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
            (repo / "README").write_text("fixture\n")
            subprocess.run(["git", "-C", str(repo), "add", "README"], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-m", "fixture"], check=True, capture_output=True)
            state_path = root / "aoe-state.json"
            worktrees = root / "worktrees"
            state_path.write_text(json.dumps({
                "sessions": [{"id": "s1", "profile": "test-profile", "group": "test-group"}],
                "repo": str(repo), "worktrees": str(worktrees),
            }))
            bin_dir = root / "bin"
            bin_dir.mkdir()
            aoe = bin_dir / "aoe"
            aoe.write_text("""#!/usr/bin/env python3
import json, os, subprocess, sys
state_path = os.environ["ATTENTION_AOE_STATE"]
state = json.load(open(state_path))
args = sys.argv[1:]
profile = None
if args[:1] == ["--profile"]:
    profile, args = args[1], args[2:]
if args[:3] == ["list", "--all", "--json"]:
    print(json.dumps(state["sessions"]))
elif args[:3] == ["project", "list", "--json"]:
    print(json.dumps([{"path": state["repo"]}]) if profile == "test-profile" else "[]")
elif args[:1] == ["add"]:
    repo = args[1]
    branch = args[args.index("--worktree") + 1]
    title = args[args.index("--title") + 1]
    group = args[args.index("--group") + 1]
    agent = args[args.index("--agent") + 1]
    path = os.path.join(state["worktrees"], branch.replace("/", "_"))
    os.makedirs(state["worktrees"], exist_ok=True)
    subprocess.run(["git", "-C", repo, "worktree", "add", "-b", branch, path], check=True, capture_output=True)
    state["created"] = {"path": path, "branch": branch, "title": title, "group": group,
                        "agent": agent, "profile": profile, "repo": repo}
    state["sessions"].append({"id": "created-session", "profile": profile,
                              "worktree": {"branch": branch, "main_repo_path": os.path.realpath(repo)}})
    with open(state_path, "w") as f: json.dump(state, f)
elif args[:2] == ["acp", "status"]:
    pass
elif args[:2] == ["acp", "prompt"]:
    state["prompt_attempts"] = state.get("prompt_attempts", 0) + 1
    with open(state_path, "w") as f: json.dump(state, f)
    if state["prompt_attempts"] == 1:
        print("structured view session not found on the daemon", file=sys.stderr)
        raise SystemExit(1)
    state["prompt"] = args[-1]
    with open(state_path, "w") as f: json.dump(state, f)
else:
    raise SystemExit("unexpected aoe command: " + repr(args))
""")
            aoe.chmod(0o755)
            attention.write_text(
                "import os\n"
                "def load_config(): return {}\n"
                "def build_prioritized_items(config):\n"
                "    assert os.environ.get('AGENT_OF_EMPIRES_PROFILE') == 'test-profile'\n"
                "    return [{'status':'now','context':'repo','title':'Fix regression','details':'ready',"
                "'_plugin':'github','kind':'pull_request','head_branch':'feature/pr-42',"
                "'base_branch':'main','repo_path':" + repr(str(repo)) + ","
                "'repo_project_path':" + repr(str(repo)) + ","
                "'session_prompt':'Review this PR; check out feature/pr-42.'}]\n"
            )
            source = (ROOT / "aoe_worker.py").read_text().replace(
                'ATTENTION_PATH = Path(__file__).resolve().with_name("attention")',
                f'ATTENTION_PATH = Path({str(attention)!r})',
            )
            worker_path = root / "aoe_worker.py"
            worker_path.write_text(source)
            env = os.environ.copy()
            env["PATH"] = str(bin_dir) + os.pathsep + env.get("PATH", "")
            env["ATTENTION_AOE_STATE"] = str(state_path)
            proc = subprocess.Popen(
                [sys.executable, str(worker_path)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
            )
            stdout_buffer = bytearray()

            def receive(timeout=3):
                return _receive_json_line(proc.stdout, stdout_buffer, timeout, "worker did not complete session creation")

            def reply(request, result=None):
                proc.stdin.write((json.dumps({
                    "jsonrpc": "2.0", "id": request["id"], "result": result or {},
                }) + "\n").encode())
                proc.stdin.flush()

            try:
                listing = receive()
                self.assertEqual(listing["method"], "sessions.list")
                reply(listing, {"sessions": [{"id": "s1", "project_path": str(repo)}]})
                loading = receive()
                self.assertEqual(loading["method"], "ui.state.set")
                reply(loading)
                proc.stdin.write((json.dumps({
                    "jsonrpc": "2.0", "id": "initial-refresh",
                    "method": "attention.refresh", "params": {"session_id": "s1"},
                }) + "\n").encode())
                proc.stdin.flush()
                self.assertTrue(receive()["result"]["ok"])
                snapshot = receive()
                button = next(
                    block for block in worker._walk_blocks(snapshot["params"]["payload"]["blocks"])
                    if block.get("label") == "New session"
                )
                reply(snapshot)
                proc.stdin.write((json.dumps({
                    "jsonrpc": "2.0", "id": "create-session-action", "method": button["method"],
                    "params": {**button["params"], "session_id": "s1"},
                }) + "\n").encode())
                proc.stdin.flush()
                self.assertTrue(receive()["result"]["ok"])
                creating = receive()
                self.assertIn("Creating session: Attention: Fix regression",
                              json.dumps(creating["params"]["payload"]))
                reply(creating)
                settings = receive()
                self.assertEqual((settings["method"], settings["params"]),
                                 ("config.get", {"key": "agent_id"}))
                reply(settings, {"value": "omp"})
                completed = receive()
                self.assertIn("Created session: created-session",
                              json.dumps(completed["params"]["payload"]))
                reply(completed)
                state = json.loads(state_path.read_text())
                created = state["created"]
                self.assertEqual(created["repo"], str(repo))
                self.assertEqual(created["profile"], "test-profile")
                self.assertEqual(created["group"], "test-group")
                self.assertEqual(created["agent"], "omp")
                self.assertEqual(created["branch"], "attention/" + __import__("hashlib").sha256(
                    button["params"]["token"].encode()).hexdigest()[:20])
                self.assertEqual(state["prompt_attempts"], 2)
                self.assertEqual(state["prompt"], "Review this PR; check out feature/pr-42.")
                self.assertNotEqual(os.path.realpath(created["path"]), os.path.realpath(repo))
                self.assertEqual(subprocess.run(
                    ["git", "-C", created["path"], "branch", "--show-current"],
                    check=True, capture_output=True, text=True,
                ).stdout.strip(), created["branch"])
            finally:
                proc.kill()
                proc.wait(timeout=5)
                proc.stdin.close()
                proc.stdout.close()
                proc.stderr.close()

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
                self.assertIn("Select Refresh", json.dumps(loading_payload))
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
                    self.assertIn("Select Refresh", json.dumps(loading["params"]["payload"]))
                    reply(loading, {})
                    proc.stdin.write(json.dumps({
                        "jsonrpc": "2.0", "id": "refresh",
                        "method": "attention.refresh", "params": {"session_id": "s1"},
                    }) + "\n")
                    proc.stdin.flush()
                    self.assertEqual(receive()["id"], "refresh")
                    break

                deadline = time.monotonic() + 3
                while True:
                    published = receive(timeout=max(0, deadline - time.monotonic()))
                    if published["method"] == "sessions.list":
                        reply(published, {"sessions": [{"id": "s1"}]})
                        continue
                    self.assertEqual(published["method"], "ui.state.set")
                    self.assertEqual(next(block for block in published["params"]["payload"]["blocks"] if block["kind"] == "section")["children"][0]["label"], "Recovered")
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
                self.assertIn("Select Refresh", json.dumps(loading["params"]["payload"]))
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
                    self.assertEqual(next(block for block in params["payload"]["blocks"] if block["kind"] == "section")["children"][0]["label"], "Ship")
                    toolbar = params["payload"]["blocks"][0]
                    self.assertEqual(toolbar["kind"], "columns")
                    self.assertEqual(toolbar["children"][-1]["method"], "attention.refresh")
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


class WorkerActionProtocolTests(unittest.TestCase):
    def test_action_is_session_scoped_single_use_without_implicit_refresh(self):
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
                "    return [{'status':'NOW','context':'QA','title':f'Fetch {count}','details':'ready',"
                "'weight':10,'_plugin':'generic','actions':[{'key':'o','label':'Run QA','primary':True,"
                "'_plugin':'generic','_original_key':'o','payload':{'command':['echo','private-command']}}]}]\n"
                "if len(sys.argv) > 1 and sys.argv[1] == '__worker_action__':\n"
                "    DISPATCHED.write_text(json.dumps(json.load(sys.stdin)))\n"
                "    print(json.dumps({'ok': True}))\n"
            )
            source = (ROOT / "aoe_worker.py").read_text().replace(
                'ATTENTION_PATH = Path(__file__).resolve().with_name("attention")',
                f'ATTENTION_PATH = Path({str(attention)!r})',
            )
            worker_path = root / "aoe_worker.py"
            worker_path.write_text(source)
            proc = subprocess.Popen([sys.executable, str(worker_path)], stdin=subprocess.PIPE,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

            stdout_buffer = bytearray()

            def receive(timeout=3):
                return _receive_json_line(proc.stdout, stdout_buffer, timeout, "worker did not send a protocol message")

            def reply(request, result=None):
                proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": request["id"],
                                             "result": result or {}}) + "\n")
                proc.stdin.flush()

            def send_action(request_id, token, session_id, method="attention.action"):
                proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": request_id,
                    "method": method, "params": {"token": token, "session_id": session_id}}) + "\n")
                proc.stdin.flush()

            def wait_response(request_id, timeout=4):
                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline:
                    message = receive(max(0.01, deadline - time.monotonic()))
                    if message.get("method") == "ui.state.set":
                        reply(message)
                    elif message.get("method") == "sessions.list":
                        reply(message, {"sessions": [{"id": "s1", "project_path": "/repo"}]})
                    elif message.get("method") == "config.get":
                        reply(message, {"value": "omp"})
                    elif message.get("method") == "sessions.create":
                        created_params.append(message["params"])
                        reply(message, {"session_id": "created-session", "created": True})
                    elif message.get("id") == request_id:
                        return message
                self.fail(f"worker did not reply to {request_id}")

            try:
                listing = receive()
                self.assertEqual(listing["method"], "sessions.list")
                reply(listing, {"sessions": [{"id": "s1", "project_path": "/repositories/attention-open"}]})
                loading = receive()
                self.assertEqual(loading["method"], "ui.state.set")
                reply(loading)
                proc.stdin.write(json.dumps({
                    "jsonrpc": "2.0", "id": "initial-refresh",
                    "method": "attention.refresh", "params": {"session_id": "s1"},
                }) + "\n")
                proc.stdin.flush()
                self.assertEqual(receive()["id"], "initial-refresh")
                first = receive()
                while first.get("method") != "ui.state.set":
                    self.assertEqual(first.get("method"), "sessions.list")
                    reply(first, {"sessions": [{"id": "s1", "project_path": "/repo"}]})
                    first = receive()
                payload = first["params"]["payload"]
                card = _first_item_card(payload)
                button_group = next(child for child in card["children"] if child.get("kind") == "columns")
                button = next(child for child in button_group["children"] if child.get("method") == "attention.action")
                token = button["params"]["token"]
                new_session = next(block for block in worker._walk_blocks(payload["blocks"])
                                   if block.get("method") == "attention.new_session")
                new_session_token = new_session["params"]["token"]
                self.assertNotIn("private-command", json.dumps(payload))
                reply(first)

                send_action("wrong-session", token, "other")
                wrong = wait_response("wrong-session")
                self.assertFalse(wrong["result"]["ok"])
                self.assertIn("Unknown session", wrong["result"]["error"])

                send_action("run", token, "s1")
                accepted = wait_response("run")
                self.assertTrue(accepted["result"]["ok"])
                send_action("duplicate", token, "s1")
                duplicate = wait_response("duplicate")
                self.assertFalse(duplicate["result"]["ok"])

                deadline = time.monotonic() + 5
                completed_seen = False
                while time.monotonic() < deadline and not completed_seen:
                    message = receive(max(0.01, deadline - time.monotonic()))
                    if message.get("method") == "sessions.list":
                        reply(message, {"sessions": [{"id": "s1", "project_path": "/repo"}]})
                        continue
                    self.assertEqual(message.get("method"), "ui.state.set")
                    state = json.dumps(message["params"]["payload"])
                    completed_seen = "Completed: Run QA" in state
                    reply(message)
                self.assertTrue(dispatched.exists(), "source action child was not invoked")
                self.assertEqual(json.loads(dispatched.read_text())["payload"]["command"],
                                 ["echo", "private-command"])
                self.assertTrue(completed_seen, "pane omitted action completion feedback")
                self.assertEqual(fetches.read_text(), "1", "action completion fetched implicitly")

                send_action("stale", token, "s1")
                stale = wait_response("stale")
                self.assertFalse(stale["result"]["ok"])
                self.assertIn("expired", stale["result"]["error"])
                self.assertIsNotNone(new_session_token)
                send_action("create-session", new_session_token, "s1", method="attention.new_session")
                accepted = wait_response("create-session")
                self.assertTrue(accepted["result"]["ok"])
                created_notice_seen = False
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline and not created_notice_seen:
                    message = receive(max(0.01, deadline - time.monotonic()))
                    if message.get("method") == "ui.state.set":
                        state = json.dumps(message["params"]["payload"])
                        created_notice_seen = (
                            "Session creation failed: Attention item has no AoE project path."
                            in state
                        )
                        reply(message)
                    elif message.get("method") == "sessions.list":
                        reply(message, {"sessions": [{"id": "s1", "project_path": "/repo"}]})
                    else:
                        self.fail(f"unexpected host RPC during create: {message}")
                self.assertTrue(created_notice_seen, "pane did not fail closed for an unmapped repository")
                self.assertEqual(fetches.read_text(), "1", "session creation fetched implicitly")
                send_action("duplicate-session", new_session_token, "s1", method="attention.new_session")
                duplicate_session = wait_response("duplicate-session")
                self.assertFalse(duplicate_session["result"]["ok"])
                self.assertIn("expired", duplicate_session["result"]["error"])
            finally:
                proc.kill()
                proc.wait(timeout=5)
                proc.stdin.close()
                proc.stdout.close()
                proc.stderr.close()


if __name__ == "__main__":
    unittest.main()
