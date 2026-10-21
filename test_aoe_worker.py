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


@unittest.skipUnless(shutil.which("node"), "Node.js is required for dashboard page tests")
class DashboardPageTests(unittest.TestCase):
    def test_page_accepts_delivery_ack_and_shows_worker_or_bridge_errors(self):
        harness = r"""
const fs = require("node:fs");
const vm = require("node:vm");
const html = fs.readFileSync(process.argv[1], "utf8");
const source = html.match(/<script>([\s\S]*?)<\/script>/)[1];

async function run(actionReply) {
  const elements = new Map();
  const makeElement = tag => ({
    tagName: String(tag).toUpperCase(),
    children: [],
    style: { setProperty() {} },
    append(...children) { this.children.push(...children); },
    replaceChildren(...children) { this.children = children; },
    prepend(child) { this.children.unshift(child); },
    setAttribute() {},
    focus() {},
    querySelectorAll() { return []; },
    hidden: false,
    disabled: false,
    value: "",
  });
  const document = {
    getElementById(id) {
      if (!elements.has(id)) elements.set(id, makeElement("div"));
      return elements.get(id);
    },
    createElement: makeElement,
    documentElement: makeElement("html"),
  };
  document.getElementById("items");
  const timers = [];
  const port1 = {
    onmessage: null,
    postMessage(message) {
      const reply = message.method === "action" ? actionReply : { result: [] };
      queueMicrotask(() => this.onmessage({ data: { id: message.id, ...reply } }));
    },
  };
  vm.runInNewContext(source, {
    document,
    MessageChannel: function MessageChannel() { return { port1, port2: {} }; },
    parent: { postMessage() {} },
    location: { hash: "#nonce" },
    setTimeout(callback) { timers.push(callback); },
    setInterval() {},
  });
  await timers[0]();
  return elements.get("items").children
    .filter(child => child.className === "notice error")
    .map(child => child.textContent);
}

(async () => {
  const errors = await run({ result: { accepted: true } });
  if (errors.length) throw new Error(`delivery acknowledgement rejected: ${errors}`);
  const workerRejected = await run({ result: { ok: false, error: "busy" } });
  if (workerRejected[0] !== "busy") {
    throw new Error(`worker rejection was not shown: ${workerRejected}`);
  }
  const rejected = await run({ error: { message: "bridge unavailable" } });
  if (rejected[0] !== "bridge unavailable") {
    throw new Error(`bridge rejection was not shown: ${rejected}`);
  }
})().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
"""
        result = subprocess.run(
            [shutil.which("node"), "-e", harness, str(ROOT / "attention.html")],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


    def test_lumen_page_action_dispatches_its_configured_token(self):
        harness = r"""
const fs = require("node:fs");
const vm = require("node:vm");
const html = fs.readFileSync(process.argv[1], "utf8");
const source = html.match(/<script>([\s\S]*?)<\/script>/)[1];
const sent = [];
const timers = [];
const payload = {
  items: [{
    title: "Review pull request",
    source: "github",
    provider: "github",
    group: "Pull requests",
    actions: [{ label: "Lumen", token: "lumen-action-token", method: "attention.action" }],
  }],
  failures: [],
  fetched_at: "2026-10-21 12:00 UTC",
  busy: false,
  status: "",
  error: "",
  prompt: null,
};
const elements = new Map();
function descendants(node) {
  return [node, ...node.children.flatMap(descendants)];
}
function makeElement(tag) {
  const node = {
    tagName: String(tag).toUpperCase(),
    children: [],
    dataset: {},
    style: { setProperty() {} },
    append(...children) { this.children.push(...children); },
    replaceChildren(...children) { this.children = children; },
    prepend(child) { this.children.unshift(child); },
    setAttribute() {},
    focus() {},
    querySelectorAll(selector) {
      return selector === "button"
        ? descendants(this).filter(child => child.tagName === "BUTTON")
        : [];
    },
    hidden: false,
    disabled: false,
    value: "",
  };
  Object.defineProperty(node, "childNodes", { get() { return this.children; } });
  return node;
}
const document = {
  getElementById(id) {
    if (!elements.has(id)) elements.set(id, makeElement("div"));
    return elements.get(id);
  },
  createElement: makeElement,
  documentElement: makeElement("html"),
};
document.getElementById("new-session").hidden = true;
const port1 = {
  onmessage: null,
  postMessage(message) {
    sent.push(message);
    const result = message.method === "plugins.uiState"
      ? [{ plugin_id: "athal7.attention", slot: "page", id: "dashboard", payload }]
      : { accepted: true };
    queueMicrotask(() => this.onmessage({ data: { id: message.id, result } }));
  },
};
vm.runInNewContext(source, {
  document,
  MessageChannel: function MessageChannel() { return { port1, port2: {} }; },
  parent: { postMessage() {} },
  location: { hash: "#nonce" },
  setTimeout(callback) { timers.push(callback); },
  setInterval() {},
});
function find(node, predicate) {
  return descendants(node).find(predicate);
}
(async () => {
  await timers[0]();
  const action = find(document.getElementById("items"),
    node => node.tagName === "BUTTON" && node.textContent === "Lumen");
  if (!action) throw new Error("Lumen action was not rendered");
  const actionMenu = find(document.getElementById("items"),
    node => node.tagName === "DETAILS");
  if (!actionMenu || actionMenu.open !== false) {
    throw new Error("Configured actions should stay inside a collapsed menu");
  }
  action.onclick();
  const dispatch = sent.find(message => message.method === "action"
    && message.params.method === "attention.action");
  if (!dispatch || dispatch.params.params.token !== "lumen-action-token") {
    throw new Error(`Lumen action did not reach the configured worker route: ${JSON.stringify(sent)}`);
  }
  document.getElementById("show-new-session").onclick();
  if (document.getElementById("new-session").hidden) {
    throw new Error("New session form is not accessible from the unified page");
  }
  const filter = find(document.getElementById("filters"),
    node => node.tagName === "BUTTON" && node.textContent === "source: github");
  if (!filter) throw new Error("Source filter was not rendered");
  filter.onclick();
  const remainingAction = find(document.getElementById("items"),
    node => node.tagName === "BUTTON" && node.textContent === "Lumen");
  if (remainingAction) throw new Error("Source filter did not hide the item's actions");
  const empty = find(document.getElementById("items"),
    node => node.textContent === "No attention items match the current filters.");
  if (!empty) throw new Error("Filtered empty state was not shown");
})().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
"""
        result = subprocess.run(
            [shutil.which("node"), "-e", harness, str(ROOT / "attention.html")],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

class DashboardPayloadTests(unittest.TestCase):
    def test_native_pane_opens_dashboard_and_renders_status(self):
        payload = worker.status_payload()
        self.assertEqual(payload["default_location"], "right")
        self.assertEqual(payload["blocks"], [{
            "kind": "row",
            "label": "Open Attention dashboard",
            "icon": "inbox",
            "href": worker.DASHBOARD_PATH,
        }])

        notice = worker.status_payload("Lumen: Could not open Terminal.")
        self.assertEqual(notice["blocks"][0], payload["blocks"][0])
        self.assertEqual(notice["blocks"][1], {
            "kind": "note", "text": "Lumen: Could not open Terminal.",
        })

    def test_dashboard_page_trims_low_priority_entries_to_payload_limit(self):
        items = [
            {"title": "Highest priority", "details": "h" * 4000, "weight": 100},
            {"title": "Middle priority", "details": "m" * 4000, "weight": 50},
            {"title": "Lowest priority", "details": "l" * 4000, "weight": 1},
        ]
        page = worker.dashboard_payload(items, "2026-10-19 12:00:00 UTC", [])
        with patch.object(worker, "MAX_DASHBOARD_BYTES", 6000):
            fitted = worker._fit_dashboard(page)
        self.assertLessEqual(
            len(json.dumps(fitted, separators=(",", ":")).encode("utf-8")),
            worker.MAX_DASHBOARD_BYTES,
        )
        self.assertEqual([item["title"] for item in fitted["items"]],
                         ["Highest priority"])
        self.assertEqual(fitted["omitted_items"], 2)

    def test_dashboard_page_omits_a_single_card_that_exceeds_the_payload_limit(self):
        item = {
            "title": "Too many actions",
            "actions": [
                {"key": str(index), "label": f"Action {index} " + "x" * 80,
                 "payload": {"command": ["echo", str(index)]}}
                for index in range(100)
            ],
        }
        page = worker.dashboard_payload([item], "2026-10-19 12:00:00 UTC", [])
        with patch.object(worker, "MAX_DASHBOARD_BYTES", 6000):
            fitted = worker._fit_dashboard(page)
        encoded = json.dumps(fitted, separators=(",", ":")).encode("utf-8")
        self.assertLessEqual(len(encoded), worker.MAX_DASHBOARD_BYTES)
        self.assertEqual(fitted["items"], [])
        self.assertEqual(fitted["omitted_items"], 1)

    def test_dashboard_page_exposes_lumen_and_prompt_actions_as_opaque_tokens(self):
        lumen_action = {
            "_plugin": "github", "label": "Lumen",
            "payload": {
                "command": [
                    "/opt/homebrew/bin/lumen", "diff",
                    "https://github.com/example/repo/pull/42",
                ],
                "terminal": True,
            },
        }
        prompt_action = {
            "label": "Comment",
            "payload": {"command": ["echo"], "inputs": [{"prompt": "Review note"}]},
        }
        token_actions = {}
        page = worker.dashboard_payload([{
            "_plugin": "github", "kind": "pull_request", "title": "Review PR",
            "actions": [lumen_action, prompt_action],
        }], "2026-10-19 12:00:00 UTC", [], token_actions)

        actions = page["items"][0]["actions"]
        self.assertEqual([action["label"] for action in actions],
                         ["Lumen", "Comment", "New session"])
        lumen_button = actions[0]
        self.assertEqual(lumen_button["method"], "attention.action")
        dispatched_action = token_actions[lumen_button["token"]]
        self.assertIs(dispatched_action, lumen_action)
        self.assertEqual(dispatched_action["payload"]["command"], [
            "/opt/homebrew/bin/lumen", "diff",
            "https://github.com/example/repo/pull/42",
        ])
        self.assertNotIn("/opt/homebrew/bin/lumen", json.dumps(page))

    def test_action_failure_and_terminal_diagnostic_are_published_to_dashboard(self):
        previous = {
            name: getattr(worker, name)
            for name in ("dashboard_active", "snapshot_fetched_at",
                         "active_action", "active_prompt")
        }
        had_action_tokens = hasattr(worker, "action_tokens")
        previous_action_tokens = getattr(worker, "action_tokens", None)
        try:
            worker.dashboard_active = True
            worker.snapshot_fetched_at = "2026-10-19 12:00:00 UTC"
            worker.action_tokens = {}
            worker.active_action = None
            worker.active_prompt = None
            with patch.object(worker, "rpc") as send:
                worker.publish_payload(
                    [], worker.status_payload("Failed: Lumen: Action did not complete."),
                    items=[{"title": "Review PR", "actions": []}],
                    action_output="Could not open Terminal: permission denied.",
                )
            page = send.call_args.args[1]["payload"]
            self.assertEqual(page["status"], "Failed: Lumen: Action did not complete.")
            self.assertEqual(page["action_output"],
                             "Could not open Terminal: permission denied.")
            self.assertEqual(page["error"], page["status"])
        finally:
            for name, value in previous.items():
                setattr(worker, name, value)
            if had_action_tokens:
                worker.action_tokens = previous_action_tokens
            else:
                del worker.action_tokens

    def test_dashboard_page_preserves_live_session_matches(self):
        item = {
            "title": "Review PR", "repo_path": "/repo", "head_branch": "feature",
        }
        sessions = [{
            "id": "session/id", "title": "Work", "status": "Running",
            "workspace_repos": [{"source_path": "/repo", "branch": "feature"}],
        }]
        page = worker.dashboard_payload(
            [item], "2026-10-19 12:00:00 UTC", sessions,
        )
        self.assertEqual(page["items"][0]["session"], {"id": "session/id", "title": "Work"})

    def test_generated_session_action_preserves_registered_repo_and_pr_branches(self):
        item = {
            "_plugin": "github", "kind": "pull_request", "title": "PR",
            "repo_path": "/worktrees/repo", "repo_project_path": "/registered/repo",
            "head_branch": "feature/pr-42", "base_branch": "main",
        }
        token_actions = {}
        page = worker.dashboard_payload(
            [item], "2026-10-19 12:00:00 UTC", [], token_actions,
        )
        new_session = page["items"][0]["actions"][-1]
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
        page = worker.dashboard_payload(
            [item], "2026-10-19 12:00:00 UTC", [], token_actions,
        )
        actions = page["items"][0]["actions"]
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

    def test_cli_and_dashboard_show_sanitized_partial_and_total_collector_failures(self):
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
                dashboard = worker.dashboard_payload(worker.attention_items(), None, [])
            dashboard_text = json.dumps(dashboard)
            self.assertEqual([item["title"] for item in dashboard["items"]], ["Healthy item"])
            self.assertEqual(len(dashboard["failures"]), 1)
            self.assertIn("failing", dashboard_text.casefold())
            self.assertNotIn("collector-secret", dashboard_text)

            set_plugins([failing])
            output = run_cli()
            self.assertIn("failing collector failed", output)
            self.assertNotIn("Nothing needs attention", output)
            with patch.dict(os.environ, env), patch.object(worker, "ATTENTION_PATH", ROOT / "attention"):
                dashboard = worker.dashboard_payload(worker.attention_items(), None, [])
            self.assertEqual(dashboard["items"], [])
            self.assertEqual(len(dashboard["failures"]), 1)

            healthy.write_text("def fetch(config):\n    return []\n")
            set_plugins([healthy])
            self.assertEqual(run_cli(), "")
            with patch.dict(os.environ, env), patch.object(worker, "ATTENTION_PATH", ROOT / "attention"):
                dashboard = worker.dashboard_payload(worker.attention_items(), None, [])
            self.assertEqual(dashboard["items"], [])
            self.assertEqual(dashboard["failures"], [])

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
        with patch.object(worker, "live_session_ids", {"s1"}, create=True), \
             patch.object(worker, "action_tokens", action_tokens, create=True), \
             patch.object(worker, "consumed_session_item_ids", consumed, create=True), \
             patch.object(worker, "action_requests", requests, create=True), \
             patch.object(worker, "active_action", None, create=True), \
             patch.object(worker, "dashboard_active", False, create=True), \
             patch.object(worker, "send") as send:
            request = {
                "jsonrpc": "2.0", "id": "create-session",
                "method": "attention.new_session",
                "params": {"token": token, "session_id": "s1"},
            }
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

    def test_new_session_form_routes_cli_options_to_the_worker(self):
        options = {
            "title": "Worktree session", "path": "/repo", "profile": "qa", "tool": "omp",
            "group": "reviews", "worktree_branch": "feature/task",
            "new_branch": True, "base_branch": "main", "scratch": False,
        }
        requests = queue.Queue()
        with patch.object(worker, "action_tokens", {}, create=True), \
             patch.object(worker, "action_requests", requests, create=True), \
             patch.object(worker, "active_action", None, create=True), \
             patch.object(worker, "dashboard_active", False, create=True), \
             patch.object(worker, "send") as send:
            worker.dispatch({
                "jsonrpc": "2.0", "id": "form",
                "method": "attention.new_session", "params": options,
            })
            send.assert_called_once_with({
                "jsonrpc": "2.0", "id": "form", "result": {"ok": True},
            })
        queued = requests.get_nowait()
        self.assertEqual(queued["action"]["params"], options)

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
                self.assertEqual(loading_payload["blocks"][0], {
                    "kind": "row", "label": "Open Attention dashboard",
                    "icon": "inbox", "href": worker.DASHBOARD_PATH,
                })
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
                    self.assertEqual(loading["params"]["payload"]["blocks"][0], {
                        "kind": "row", "label": "Open Attention dashboard",
                        "icon": "inbox", "href": worker.DASHBOARD_PATH,
                    })
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
                    self.assertEqual(published["params"]["payload"]["blocks"][0], {
                        "kind": "row", "label": "Open Attention dashboard",
                        "icon": "inbox", "href": worker.DASHBOARD_PATH,
                    })
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
                self.assertEqual(loading["params"]["payload"]["blocks"][0], {
                    "kind": "row", "label": "Open Attention dashboard",
                    "icon": "inbox", "href": worker.DASHBOARD_PATH,
                })
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
                    self.assertEqual(params["payload"]["blocks"][0], {
                        "kind": "row", "label": "Open Attention dashboard",
                        "icon": "inbox", "href": worker.DASHBOARD_PATH,
                    })
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
                "    return [{'status':'NOW','context':'QA','title':'Fetch','details':'ready',"
                "'weight':10,'_plugin':'github','kind':'issue',"
                "'repo_resolution_error':'Attention item has no AoE project path.','actions':["
                "{'key':'o','label':'Run QA','primary':True,'_plugin':'generic',"
                "'_original_key':'o','payload':{'command':['echo','run'] }},"
                "{'key':'p','label':'Prompt QA','_plugin':'generic',"
                "'_original_key':'p','payload':{'command':['echo','prompt']}},"
                "{'key':'x','label':'Oversized QA','_plugin':'generic',"
                "'_original_key':'x','payload':{'command':['echo','oversized']}}]}]\n"
                "if len(sys.argv) > 1 and sys.argv[1] in ('__worker_action__', '__worker_action_interactive__'):\n"
                "    action = json.loads(sys.stdin.readline())\n"
                "    DISPATCHED.write_text(json.dumps(action))\n"
                "    command = action['payload']['command'][-1]\n"
                "    if command == 'prompt':\n"
                "        print(json.dumps({'prompt': {'text':'Confirm action?', 'choices':['yes','no'], 'default':'yes'}}), flush=True)\n"
                "        answer = json.loads(sys.stdin.readline())\n"
                "        if answer.get('value') is None:\n"
                "            print(json.dumps({'ok':False,'error':'Action canceled.'}))\n"
                "        else:\n"
                "            print(json.dumps({'ok':True}))\n"
                "    elif command == 'oversized':\n"
                "        choices = ['choice-' + str(i) + '-' + ('界' * 200) for i in range(100)]\n"
                "        print(json.dumps({'prompt': {'text':'Large choices', 'choices':choices}}), flush=True)\n"
                "        print(json.dumps({'ok':True}))\n"
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
            proc = subprocess.Popen([sys.executable, str(worker_path)], stdin=subprocess.PIPE,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

            stdout_buffer = bytearray()
            published_pages = []

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
                        params = message["params"]
                        if params.get("slot") == "page":
                            published_pages.append(params["payload"])
                        reply(message)
                    elif message.get("method") == "sessions.list":
                        reply(message, {"sessions": [{"id": "s1", "project_path": "/repo"}]})
                    elif message.get("id") == request_id:
                        return message
                self.fail(f"worker did not reply to {request_id}")

            def wait_page(predicate, timeout=5):
                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline:
                    for index, page in enumerate(published_pages):
                        if predicate(page):
                            del published_pages[index]
                            return page
                    message = receive(max(0.01, deadline - time.monotonic()))
                    if message.get("method") == "sessions.list":
                        reply(message, {"sessions": [{"id": "s1", "project_path": "/repo"}]})
                    elif message.get("method") == "ui.state.set":
                        params = message["params"]
                        page = params["payload"]
                        reply(message)
                        if params.get("slot") == "page":
                            if predicate(page):
                                return page
                            published_pages.append(page)
                self.fail("worker did not publish the expected dashboard state")

            try:
                listing = receive()
                self.assertEqual(listing["method"], "sessions.list")
                reply(listing, {"sessions": [{"id": "s1", "project_path": "/repositories/attention-open"}]})
                loading = receive()
                self.assertEqual(loading["method"], "ui.state.set")
                reply(loading)
                proc.stdin.write(json.dumps({
                    "jsonrpc": "2.0", "id": "initial-refresh",
                    "method": "attention.refresh", "params": {},
                }) + "\n")
                proc.stdin.flush()
                self.assertEqual(receive()["id"], "initial-refresh")
                page_payload = None
                while page_payload is None:
                    first = receive()
                    if first.get("method") == "sessions.list":
                        reply(first, {"sessions": [{"id": "s1", "project_path": "/repo"}]})
                        continue
                    self.assertEqual(first.get("method"), "ui.state.set")
                    params = first["params"]
                    if params.get("slot") == "page":
                        page_payload = params["payload"]
                    reply(first)
                item = page_payload["items"][0]
                button = next(action for action in item["actions"]
                              if action["label"] == "Run QA")
                token = button["token"]
                self.assertNotIn("private-command", json.dumps(page_payload))

                send_action("wrong-session", token, "other")
                wrong = wait_response("wrong-session")
                self.assertFalse(wrong["result"]["ok"])
                self.assertIn("Unknown session", wrong["result"]["error"])

                send_action("run", token, None)
                accepted = wait_response("run")
                self.assertTrue(accepted["result"]["ok"])
                send_action("duplicate", token, "s1")
                duplicate = wait_response("duplicate")
                self.assertFalse(duplicate["result"]["ok"])

                deadline = time.monotonic() + 5
                completed_seen = False
                completed_page = None
                action_output = None
                while time.monotonic() < deadline and not completed_seen:
                    message = receive(max(0.01, deadline - time.monotonic()))
                    if message.get("method") == "sessions.list":
                        reply(message, {"sessions": [{"id": "s1", "project_path": "/repo"}]})
                        continue
                    self.assertEqual(message.get("method"), "ui.state.set")
                    params = message["params"]
                    page = params["payload"]
                    if params.get("slot") == "page":
                        completed_seen = "Completed: Run QA" in json.dumps(page)
                        if completed_seen:
                            completed_page = page
                            action_output = page.get("action_output")
                    reply(message)
                self.assertTrue(dispatched.exists(), "source action child was not invoked")
                self.assertEqual(json.loads(dispatched.read_text())["payload"]["command"],
                                 ["echo", "run"])
                self.assertTrue(completed_seen, "page omitted action completion feedback")
                self.assertEqual(action_output, "source action output")
                self.assertEqual(fetches.read_text(), "1", "action completion fetched implicitly")
                session_button = next(
                    action for action in completed_page["items"][0]["actions"]
                    if action["label"] == "New session"
                )
                new_session_token = session_button["token"]

                send_action("stale", token, "s1")
                stale = wait_response("stale")
                self.assertFalse(stale["result"]["ok"])
                self.assertIn("expired", stale["result"]["error"])
                self.assertIsNotNone(new_session_token)
                send_action("create-session", new_session_token, "s1", method="attention.new_session")
                rejected = wait_response("create-session")
                self.assertFalse(rejected["result"]["ok"])
                self.assertIn("Attention item has no AoE project path.",
                              rejected["result"]["error"])
                self.assertEqual(fetches.read_text(), "1", "session creation fetched implicitly")
                send_action("duplicate-session", new_session_token, "s1", method="attention.new_session")
                duplicate_session = wait_response("duplicate-session")
                self.assertFalse(duplicate_session["result"]["ok"])
                self.assertIn("Attention item has no AoE project path.",
                              duplicate_session["result"]["error"])

                prompt_button = next(
                    action for action in completed_page["items"][0]["actions"]
                    if action["label"] == "Prompt QA"
                )
                send_action("prompt", prompt_button["token"], "s1")
                prompt_response = wait_response("prompt")
                self.assertTrue(prompt_response["result"]["ok"])
                prompt_page = wait_page(lambda value: value.get("prompt") is not None)
                prompt = prompt_page["prompt"]
                self.assertEqual(prompt["text"], "Confirm action?")
                self.assertEqual(prompt["choices"], ["yes", "no"])

                proc.stdin.write(json.dumps({
                    "jsonrpc": "2.0", "id": "cancel-input",
                    "method": "attention.input",
                    "params": {
                        "token": prompt["token"], "prompt_id": prompt["id"], "value": None,
                    },
                }) + "\n")
                proc.stdin.flush()
                canceled = wait_response("cancel-input")
                self.assertTrue(canceled["result"]["ok"])
                canceled_page = wait_page(
                    lambda value: "Action canceled." in (value.get("status") or ""),
                )
                prompt_again = next(
                    action for action in canceled_page["items"][0]["actions"]
                    if action["label"] == "Prompt QA"
                )
                self.assertNotEqual(prompt_again["token"], prompt["token"])

                oversized = next(
                    action for action in canceled_page["items"][0]["actions"]
                    if action["label"] == "Oversized QA"
                )
                send_action("oversized-prompt", oversized["token"], "s1")
                oversized_response = wait_response("oversized-prompt")
                self.assertTrue(oversized_response["result"]["ok"])
                oversized_page = wait_page(
                    lambda value: "dashboard size limit" in (value.get("status") or ""),
                )
                self.assertTrue(any(
                    action["label"] == "Oversized QA"
                    for action in oversized_page["items"][0]["actions"]
                ))
                self.assertEqual(fetches.read_text(), "1", "actions fetched implicitly")
            finally:
                proc.kill()
                proc.wait(timeout=5)
                proc.stdin.close()
                proc.stdout.close()
                proc.stderr.close()


if __name__ == "__main__":
    unittest.main()
