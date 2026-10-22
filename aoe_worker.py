#!/usr/bin/env python3
"""Agent of Empires worker publishing attention items into session panes."""
from datetime import datetime, timezone
import importlib.machinery
import importlib.util
import json
import os
import re
import queue
from pathlib import Path
import hashlib
import secrets
import select
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import urlsplit, urlunsplit
from urllib.request import urlopen
ATTENTION_PATH = Path(__file__).resolve().with_name("attention")
SESSION_POLL_SECONDS = 60
MAX_PANE_BYTES = 64 * 1024
ACTION_TIMEOUT_SECONDS = 120
INITIAL_MESSAGE_DELAY_SECONDS = 5
_input_buffer = bytearray()
_source_lock = threading.Lock()
live_sessions_by_id = {}
consumed_action_ids = set()
consumed_session_item_ids = set()
selected_item_keys = {}
action_token_sessions = {}
pane_refresh_requested = False


def send(message):
    sys.stdout.write(json.dumps(message, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def read_message(timeout=None):
    while True:
        newline = _input_buffer.find(b"\n")
        if newline >= 0:
            line = bytes(_input_buffer[:newline])
            del _input_buffer[:newline + 1]
            return json.loads(line)
        ready, _, _ = select.select([sys.stdin.fileno()], [], [], timeout)
        if not ready:
            return None
        chunk = os.read(sys.stdin.fileno(), 4096)
        if not chunk:
            raise EOFError
        _input_buffer.extend(chunk)


def rpc(method, params=None):
    request_id = rpc.next_id
    rpc.next_id += 1
    send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}})
    while True:
        message = read_message()
        if not isinstance(message, dict):
            continue
        if "method" in message:
            dispatch(message)
            continue
        if message.get("id") == request_id:
            if "error" in message:
                raise RuntimeError(message["error"].get("message", "host RPC failed"))
            return message.get("result", {})


rpc.next_id = 1


def attention_items():
    name = "_attention_plugin_" + str(time.monotonic_ns())
    loader = importlib.machinery.SourceFileLoader(name, str(ATTENTION_PATH))
    module = importlib.util.module_from_spec(importlib.util.spec_from_loader(name, loader))
    try:
        loader.exec_module(module)
        config = module.load_config()
        items = module.build_prioritized_items(config)
        dashboard_groups = getattr(module, "dashboard_groups", None)
        if dashboard_groups:
            groups, group_error = dashboard_groups(config)
            if group_error:
                raise RuntimeError(group_error)
            if groups:
                fallback = next(group for group in groups if group.get("fallback"))
                for item in items:
                    group = next((group for group in groups if "match" in group
                                  and module._group_matches(item, group["match"])), fallback)
                    item["_attention_group"] = group["name"]
                    item["_attention_group_order"] = groups.index(group)
        return items
    finally:
        sys.modules.pop(name, None)


def attention_host_profile():
    session_ids = set(live_sessions_by_id)
    if not session_ids:
        return None
    try:
        result = subprocess.run(
            ["aoe", "list", "--all", "--json"], capture_output=True,
            text=True, timeout=20,
        )
        sessions = json.loads(result.stdout) if result.returncode == 0 else []
    except (OSError, subprocess.SubprocessError, ValueError, TypeError):
        return None
    profiles = {
        session["profile"] for session in sessions
        if isinstance(session, dict) and session.get("id") in session_ids
        and isinstance(session.get("profile"), str) and session["profile"]
    }
    return next(iter(profiles)) if len(profiles) == 1 else None


def _safe_source_url(value):
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or len(value) > 2048 or any(ord(char) < 32 or ord(char) == 127 for char in value):
        return None
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    if (parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname
            or parsed.username or parsed.password):
        return None
    return value


def _display_text(value, limit):
    return " ".join(str(value).split())[:limit] if value is not None else ""




def _is_approve_action(action):
    payload = action.get("payload")
    label = str(action.get("label") or "").strip().casefold()
    kind = str(payload.get("kind") or "").strip().casefold() if isinstance(payload, dict) else ""
    return kind == "approve" or label == "approve"






def _new_session_action(item):
    title = _display_text(item.get("title"), 120) or "Follow-up"
    is_github_item = item.get("_plugin") == "github"
    is_pull_request = is_github_item and item.get("kind") == "pull_request"
    repo_path = (
        item.get("repo_project_path") or item.get("repo_path")
        if is_github_item else item.get("repo_path")
    )
    path = repo_path.strip() if isinstance(repo_path, str) else ""
    options = {
        "title": f"Attention: {title}",
        "path": path,
        "scratch": not path and not is_github_item,
        "is_pull_request": is_pull_request,
    }
    if is_github_item:
        resolution_error = item.get("repo_resolution_error")
        if not path and not resolution_error:
            resolution_error = "No unique local GitHub checkout."
        if resolution_error:
            options["repo_resolution_error"] = resolution_error
    branch = item.get("head_branch")
    if is_pull_request:
        options["worktree_branch"] = branch
        target_branch = item.get("base_branch")
        if target_branch is not None:
            options["base_branch"] = target_branch
    initial_turn = item.get("session_prompt")
    if isinstance(initial_turn, str) and initial_turn:
        options["initial_turn"] = initial_turn
    return {
        "_aoe_operation": "new_session",
        "_item_id": id(item),
        "params": options,
        "label": "New session",
    }


def _is_session_action(action):
    """Recognize configured actions that replace the generated session action."""
    return (
        isinstance(action, dict)
        and str(action.get("label", "")).strip().casefold()
        in {"session", "new session"}
        and isinstance(action.get("payload"), dict)
    )




def _tree_order(items):
    by_identity = {
        item["identity_key"]: item
        for item in items
        if item.get("identity_key")
    }
    children = {}
    attached = set()
    for item in items:
        parent = item.get("parent_identity_key")
        if parent in by_identity and parent != item.get("identity_key"):
            children.setdefault(parent, []).append(item)
            attached.add(id(item))
    if not children:
        return items

    ordered = []
    visited = set()

    def append_item(item, prefix="", connector=""):
        marker = id(item)
        if marker in visited:
            return
        visited.add(marker)
        row = dict(item)
        if connector:
            row["_tree_prefix"] = f"{prefix}{connector}"
        ordered.append(row)
        descendants = children.get(item.get("identity_key"), [])
        descendant_prefix = prefix + (
            "   " if connector == "└─ " else "│  " if connector else ""
        )
        for index, child in enumerate(descendants):
            child_connector = "└─ " if index == len(descendants) - 1 else "├─ "
            append_item(child, descendant_prefix, child_connector)

    for item in items:
        if id(item) not in attached:
            append_item(item)
    for item in items:
        append_item(item)
    return ordered




















def _open_session(session):
    return (session.get("status") in {"Running", "Idle", "Waiting", "Starting", "Creating"}
            and not any(session.get(flag) for flag in ("archived", "snoozed", "trashed")))


def _item_identity(url):
    url = _safe_source_url(url)
    if not url:
        return None
    parsed = urlsplit(url)
    parts = parsed.path.strip("/").split("/")
    if (parsed.hostname.lower() == "github.com" and len(parts) == 4
            and parts[2] in {"pull", "issues"} and parts[3].isdigit()):
        return ("github", parts[0].casefold(), parts[1].casefold(), parts[2], int(parts[3]))
    if (parsed.hostname.lower() == "linear.app" and len(parts) in {3, 4}
            and parts[1] == "issue" and re.fullmatch(r"[A-Za-z][A-Za-z0-9]*-\d+", parts[2])):
        return ("linear", parts[0].casefold(), parts[2].upper())
    return None


def session_ui_state():
    """Read the daemon's shared plugin snapshot once, never once per item."""
    try:
        result = subprocess.run(
            ["aoe", "url", "--no-autostart"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode:
            return []
        lines = result.stdout.strip().splitlines()
        if not lines:
            return []
        address = lines[0]
        if "\t" in address:
            label, address = address.split("\t", 1)
            if label != "primary":
                return []
        endpoint = urlsplit(address)
        if endpoint.scheme not in {"http", "https"} or not endpoint.netloc:
            return []
        url = urlunsplit((endpoint.scheme, endpoint.netloc, "/api/plugins/ui-state",
                          endpoint.query, ""))
        with urlopen(url, timeout=5) as response:
            data = response.read(4 * 1024 * 1024 + 1)
        if len(data) > 4 * 1024 * 1024:
            return []
        snapshot = json.loads(data)
        entries = snapshot.get("entries", []) if isinstance(snapshot, dict) else []
        return entries if isinstance(entries, list) else []
    except (OSError, ValueError, subprocess.SubprocessError):
        return []


def _session_item_identities(entries):
    identities = {}
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("session_id"), str):
            continue
        plugin_id = entry.get("plugin_id")
        payload = entry.get("payload")
        if not isinstance(payload, dict):
            continue
        candidates = []
        related = payload.get("items")
        related = related if isinstance(related, list) else []
        if (plugin_id == "agent-of-empires.github" and entry.get("slot") == "row-badge"
                and entry.get("id") == "github_pr_badge"):
            candidates = [payload, *related]
        elif (plugin_id == "community.linear" and entry.get("slot") == "row-badge"
              and entry.get("id") == "linear_issue_badge"):
            candidates = [payload, *related]
        elif (plugin_id == "community.linear" and entry.get("slot") == "pane"
              and entry.get("id") == "linear_issue_pane"):
            # Only issue-selector rows, not comments or linked resources.
            blocks = payload.get("blocks")
            pending = list(blocks) if isinstance(blocks, list) else []
            while pending:
                block = pending.pop()
                if not isinstance(block, dict):
                    continue
                if block.get("method") == "linear.select_issue":
                    candidates.append(block)
                children = block.get("children")
                if isinstance(children, list):
                    pending.extend(children)
        for candidate in candidates:
            if isinstance(candidate, dict):
                identity = _item_identity(candidate.get("href"))
                if identity:
                    identities.setdefault(entry["session_id"], set()).add(identity)
    return {sid: sorted(values) for sid, values in identities.items()}


def _unowned_items(items, sessions):
    owned = {
        identity for session in sessions if _open_session(session)
        for identity in session.get("_attention_items", [])
    }
    visible = []
    for item in items:
        if item.get("kind") in {"pull_request", "issue"}:
            urls = [item.get("url"), *item.get("_linked_item_urls", [])]
            if any(_item_identity(url) in owned for url in urls):
                continue
        visible.append(item)
    return visible


def _matching_sessions(item, sessions, allow_title=True):
    branch = item.get("head_branch")
    repo_path = item.get("repo_path")
    repo_path = os.path.realpath(repo_path) if isinstance(repo_path, str) and repo_path else None
    title = f"Attention: {_display_text(item.get('title'), 120) or 'Follow-up'}"
    matches = []
    for session in sessions:
        if not _open_session(session):
            continue
        repos = list(session.get("workspace_repos") or [])
        worktree = session.get("worktree")
        if isinstance(worktree, dict):
            repos.append({"source_path": worktree.get("main_repo_path"), "branch": worktree.get("branch")})
        branch_match = bool(repo_path and branch and any(
            isinstance(repo, dict) and repo.get("branch") == branch
            and isinstance(repo.get("source_path"), str) and repo["source_path"]
            and os.path.realpath(repo["source_path"]) == repo_path
            for repo in repos
        ))
        title_match = allow_title and session.get("title") == title
        if title_match and repo_path:
            paths = [session.get("project_path"), *(repo.get("source_path") for repo in repos if isinstance(repo, dict))]
            title_match = any(isinstance(path, str) and path and os.path.realpath(path) == repo_path for path in paths)
        if branch_match or title_match:
            matches.append(session)
    return sorted(matches, key=lambda session: session["id"])




def live_sessions():
    result = rpc("sessions.list")
    sessions = result.get("sessions", []) if isinstance(result, dict) else []
    sessions = [session for session in sessions if isinstance(session, dict) and isinstance(session.get("id"), str)]
    identities = _session_item_identities(session_ui_state())
    sessions = [dict(session, _attention_items=identities.get(session["id"], []))
                for session in sessions]
    live_ids = {session["id"] for session in sessions}
    for stale_id in published_ids - live_ids:
        rpc("ui.state.remove", {"slot": "pane", "id": "attention", "session_id": stale_id})
        published_ids.discard(stale_id)
    return sessions





def status_payload(text=""):
    blocks = [{
        "kind": "row", "label": "Refresh Attention", "icon": "refresh",
        "method": "attention.refresh",
    }]
    if text:
        blocks.append({"kind": "note", "text": _display_text(text, 200)})
    return {"title": "Attention", "icon": "inbox", "default_location": "right",
            "blocks": blocks}


def _item_key(item):
    identity = item.get("identity_key")
    if not identity:
        identity = (
            item.get("_plugin"), item.get("kind"), item.get("id"),
            item.get("url"), item.get("title"), item.get("context"),
            item.get("repo_path"), item.get("head_branch"),
        )
    encoded = json.dumps(
        identity, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:20]


def attention_payload(items, sessions, token_actions=None):
    token_actions = {} if token_actions is None else token_actions
    items = _unowned_items(items, sessions)
    cards = []
    titles = [_display_text(item.get("title"), 120) or "Follow-up"
              for item in items if not item.get("_collector_failure")]
    for item in _tree_order([item for item in items if not item.get("_collector_failure")]):
        item_key = _item_key(item)
        actions = []
        for action in item.get("actions", []):
            if not isinstance(action, dict) or id(action) in consumed_action_ids:
                continue
            if ((item.get("_is_own_pr") is True or action.get("_is_own_pr") is True)
                    and _is_approve_action(action)):
                continue
            token = next((key for key, value in token_actions.items() if value is action), None)
            if token is None:
                token = secrets.token_urlsafe(18)
                token_actions[token] = action
            actions.append({
                "label": _display_text(action.get("label"), 80) or "Action",
                "token": token, "method": "attention.action",
            })
        has_session_action = any(
            _is_session_action(action) for action in item.get("actions", [])
        )
        if not has_session_action and id(item) not in consumed_session_item_ids:
            token = next((
                key for key, value in token_actions.items()
                if isinstance(value, dict)
                and value.get("_aoe_operation") == "new_session"
                and value.get("_item_id") == id(item)
            ), None)
            if token is None:
                token = secrets.token_urlsafe(18)
                token_actions[token] = _new_session_action(item)
            actions.append({
                "label": "New session", "token": token,
                "method": "attention.new_session",
            })
        title = _display_text(item.get("title"), 120) or "Follow-up"
        matches = _matching_sessions(item, sessions, allow_title=titles.count(title) == 1)
        cards.append({
            "key": item_key,
            "prefix": _display_text(item.get("_tree_prefix") or item.get("id"), 48),
            "title": title, "url": _safe_source_url(item.get("url")),
            "status": _display_text(item.get("status"), 100),
            "context": _display_text(item.get("context"), 160),
            "reason": _display_text(item.get("attention_reason"), 160),
            "details": _display_text(item.get("details"), 1000),
            "source": _display_text(item.get("_plugin"), 100),
            "group": _display_text(item.get("_attention_group", "Attention"), 120),
            "group_order": item.get("_attention_group_order", 0),
            "actions": actions,
            "session": ({"id": matches[0]["id"], "title": _display_text(
                matches[0].get("title"), 120) or matches[0]["id"]} if matches else None),
        })
    failures = [{"source": _display_text(item.get("context"), 100),
                 "error": _display_text(item.get("details"), 200)}
                for item in items if item.get("_collector_failure")]
    return {"items": cards, "failures": failures}


def _source_icon(source):
    return {
        "github": "git-branch", "linear": "circle", "calendar": "calendar",
        "reminders": "check-square", "generic": "inbox",
    }.get((source or "").casefold(), "inbox")


def _status_tone(status):
    status = (status or "").casefold()
    if any(word in status for word in ("fail", "overdue", "blocked", "error")):
        return "danger"
    if any(word in status for word in ("review", "assigned", "waiting", "due")):
        return "warn"
    if any(word in status for word in ("approved", "complete", "success")):
        return "success"
    return "neutral"


def _pane_payload(snapshot, notice="", selected_key=None):
    blocks = [{
        "kind": "row", "label": "Refresh Attention", "icon": "refresh",
        "method": "attention.refresh",
    }]
    if notice:
        blocks.append({"kind": "note", "text": _display_text(notice, 200)})

    cards = snapshot["items"]
    if cards:
        selected = next((card for card in cards if card["key"] == selected_key), cards[0])
        rows = []
        for card in cards:
            row = {
                "kind": "row", "label": card["title"],
                "icon": _source_icon(card["source"]),
                "method": "attention.select_item",
                "params": {"item_key": card["key"]},
                "selected": card["key"] == selected["key"],
            }
            if card["prefix"]:
                row["prefix"] = card["prefix"]
            if card["context"]:
                row["sublabel"] = card["context"]
            if card["status"]:
                row["value"] = card["status"]
                row["value_tone"] = _status_tone(card["status"])
            if card["url"]:
                row["href"] = card["url"]
            rows.append(row)
        blocks.append({
            "kind": "section", "title": "Work queue",
            "badges": [{"text": str(len(rows)), "tone": "neutral"}],
            "children": rows, "scroll": True,
        })

        callout = {
            "kind": "callout",
            "title": selected["reason"] or selected["status"] or "Follow-up",
            "icon": _source_icon(selected["source"]),
            "tone": _status_tone(selected["status"]),
        }
        if selected["details"]:
            callout["detail"] = selected["details"]
        if selected["url"]:
            callout["actions"] = [{
                "kind": "action", "label": "Open source",
                "href": selected["url"], "icon": "external-link",
                "variant": "secondary",
            }]
        blocks.append(callout)
        if selected["session"]:
            blocks.append({
                "kind": "row", "label": "Session",
                "value": selected["session"]["title"], "icon": "bot",
            })
        if selected["actions"]:
            blocks.append({
                "kind": "section", "title": "Actions",
                "badges": [{"text": str(len(selected["actions"])), "tone": "neutral"}],
                "icon": "zap", "boxed": True,
                "collapsible": True, "collapsed": True,
                "children": [{
                    "kind": "row", "label": action["label"],
                    "method": action["method"], "params": {"token": action["token"]},
                } for action in selected["actions"]],
            })
    else:
        blocks.append({
            "kind": "callout", "title": "Nothing needs attention",
            "detail": "Refresh to check for new items.",
            "icon": "circle-check", "tone": "success",
        })

    if snapshot["failures"]:
        blocks.append({
            "kind": "section", "title": "Collector failures",
            "icon": "triangle-alert", "tone": "danger", "boxed": True,
            "children": [{
                "kind": "note",
                "text": f'{failure["source"]}: {failure["error"]}',
                "tone": "danger",
            } for failure in snapshot["failures"]],
        })
    return {"title": "Attention", "icon": "inbox", "default_location": "right",
            "blocks": blocks}


def _fit_pane(payload):
    def size():
        return len(json.dumps(payload, separators=(",", ":")).encode("utf-8"))

    omitted = 0
    while size() > MAX_PANE_BYTES - 100:
        queue_section = next((
            block for block in payload["blocks"]
            if block.get("kind") == "section" and block.get("title") == "Work queue"
        ), None)
        rows = queue_section.get("children", []) if queue_section else []
        removable = next((
            index for index in range(len(rows) - 1, -1, -1)
            if rows[index].get("selected") is not True
        ), None)
        if removable is not None:
            rows.pop(removable)
            omitted += 1
            continue

        actions = next((
            block for block in reversed(payload["blocks"])
            if block.get("kind") == "section"
            and block.get("title") == "Actions"
            and block.get("children")
        ), None)
        if actions is not None:
            actions["children"].pop()
            omitted += 1
            if not actions["children"]:
                payload["blocks"].remove(actions)
            continue

        failures = next((
            block for block in reversed(payload["blocks"])
            if block.get("kind") == "section"
            and block.get("title") == "Collector failures"
            and block.get("children")
        ), None)
        if failures is None:
            break
        failures["children"].pop()
        omitted += 1
        if not failures["children"]:
            payload["blocks"].remove(failures)
    if omitted:
        payload["blocks"].append({
            "kind": "note", "text": f"{omitted} lower-priority rows or actions omitted."
        })
    return payload


def _status_notice(payload):
    return next((block.get("text", "") for block in payload.get("blocks", [])
                 if block.get("kind") == "note" and block.get("text")), "")


def _pane_action_tokens(payload):
    tokens = set()
    pending = list(payload.get("blocks", []))
    while pending:
        block = pending.pop()
        if not isinstance(block, dict):
            continue
        params = block.get("params")
        if isinstance(params, dict) and isinstance(params.get("token"), str):
            tokens.add(params["token"])
        children = block.get("children")
        if isinstance(children, list):
            pending.extend(children)
    return tokens


def publish_payload(sessions, payload, only_new=False, items=()):
    global action_tokens, action_token_sessions
    notice = _display_text(_status_notice(payload), 200)
    next_action_tokens = {}
    next_token_sessions = {}
    pane_updates = []
    live_ids = set()
    for session in sessions:
        session_id = session["id"]
        live_ids.add(session_id)
        if only_new and session_id in published_ids:
            for token, owner in action_token_sessions.items():
                if owner == session_id and token in action_tokens:
                    next_action_tokens[token] = action_tokens[token]
                    next_token_sessions[token] = session_id
            continue

        session_tokens = {}
        snapshot = attention_payload(items, sessions, session_tokens)
        keys = {card["key"] for card in snapshot["items"]}
        selected_key = selected_item_keys.get(session_id)
        if selected_key not in keys:
            selected_key = snapshot["items"][0]["key"] if snapshot["items"] else None
        if selected_key is None:
            selected_item_keys.pop(session_id, None)
        else:
            selected_item_keys[session_id] = selected_key
        pane = _fit_pane(_pane_payload(snapshot, notice, selected_key))
        for token in _pane_action_tokens(pane):
            if token in session_tokens:
                next_action_tokens[token] = session_tokens[token]
                next_token_sessions[token] = session_id
        pane_updates.append((session_id, pane))
        published_ids.add(session_id)
    for stale_id in set(selected_item_keys) - live_ids:
        selected_item_keys.pop(stale_id, None)
    action_tokens = next_action_tokens
    action_token_sessions = next_token_sessions
    for session_id, pane in pane_updates:
        rpc("ui.state.set", {
            "slot": "pane", "id": "attention", "session_id": session_id, "payload": pane,
        })

def fetch_snapshot(results):
    try:
        with _source_lock:
            profile = attention_host_profile()
            previous_profile = os.environ.pop("AGENT_OF_EMPIRES_PROFILE", None)
            if profile:
                os.environ["AGENT_OF_EMPIRES_PROFILE"] = profile
            try:
                items = attention_items()
            finally:
                if previous_profile is not None:
                    os.environ["AGENT_OF_EMPIRES_PROFILE"] = previous_profile
            fetched_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        results.put((status_payload(), {}, None, items, fetched_at))
    except Exception as exc:
        results.put((None, {}, str(exc)[:400], [], None))


def start_fetch(results):
    threading.Thread(target=fetch_snapshot, args=(results,), daemon=True).start()


def _read_action_output(output):
    size = os.fstat(output.fileno()).st_size
    limit = 4000
    start = max(0, size - limit)
    value = os.pread(output.fileno(), size - start, start).decode(
        "utf-8", errors="replace",
    )
    value = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", value)
    return "".join(
        char for char in value
        if char in "\n\t" or ord(char) >= 32 and ord(char) != 127
    ).strip()


def execute_action(token, action, label, results):
    """Run the CLI dispatcher, forwarding prompts without a second action implementation."""
    completed = False
    error = "Action failed."
    action_output = ""
    try:
        with _source_lock, tempfile.TemporaryFile() as output:
            with subprocess.Popen(
                [sys.executable, str(ATTENTION_PATH), "__worker_action_interactive__"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=output,
            ) as process:
                try:
                    process.stdin.write((json.dumps(action) + "\n").encode())
                    process.stdin.flush()
                    deadline = time.monotonic() + ACTION_TIMEOUT_SECONDS
                    offset = 0
                    buffer = bytearray()
                    while True:
                        ready, _, _ = select.select([process.stdout.fileno()], [], [],
                                                    max(0, deadline - time.monotonic()))
                        if not ready:
                            raise TimeoutError
                        chunk = os.read(process.stdout.fileno(), 4096)
                        if not chunk:
                            break
                        buffer.extend(chunk)
                        while b"\n" in buffer:
                            line, _, remainder = buffer.partition(b"\n")
                            buffer[:] = remainder
                            response = json.loads(line)
                            if "prompt" in response:
                                error = (
                                    "This action requires interactive input; "
                                    "run it from the CLI."
                                )
                                process.kill()
                                process.wait()
                                buffer.clear()
                                break
                            if response.get("ok") is True:
                                completed = True
                            else:
                                error = _display_text(response.get("error"), 300) or "Action failed."
                            if response.get("error") == "Action canceled.":
                                error = "Action canceled."
                    process.wait(timeout=max(0.1, deadline - time.monotonic()))
                    action_output = _read_action_output(output)
                    completed = completed and process.returncode == 0
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.wait()
    except TimeoutError:
        error = "Action timed out waiting for input or completion."
    except Exception as exc:
        error = f"Action failed ({type(exc).__name__})."
    results.put({
        "token": token, "label": label, "completed": completed,
        "error": error, "output": action_output,
    })


def start_action(token, action, label, results):
    threading.Thread(target=execute_action, args=(token, action, label, results), daemon=True).start()


def _session_options_error(options):
    if (not isinstance(options, dict)
            or not isinstance(options.get("title", ""), str)
            or not isinstance(options.get("path", ""), str)
            or not isinstance(options.get("profile", ""), str)
            or not isinstance(options.get("tool", ""), str)
            or not isinstance(options.get("group", ""), str)
            or not isinstance(options.get("repo_resolution_error", ""), str)
            or not isinstance(options.get("scratch", False), bool)
            or not isinstance(options.get("new_branch", False), bool)
            or not isinstance(options.get("is_pull_request", False), bool)
            or not isinstance(options.get("initial_turn", ""), str)):
        return "Invalid session options."
    resolution_error = options.get("repo_resolution_error", "").strip()
    if resolution_error:
        return _display_text(resolution_error, 300) or "GitHub repository resolution failed."
    path = options.get("path", "").strip()
    scratch = options.get("scratch", False)
    is_pull_request = options.get("is_pull_request", False)
    new_branch = options.get("new_branch", False)
    branch = options.get("worktree_branch")
    base_branch = options.get("base_branch")
    if is_pull_request and not path:
        return "This pull request has no repository path."
    if is_pull_request and (
        not isinstance(branch, str) or not branch or branch != branch.strip()
        or any(ord(char) < 32 or ord(char) == 127 for char in branch)
    ):
        return "This pull request has no valid head branch."
    if is_pull_request and base_branch is not None and (
        not isinstance(base_branch, str) or base_branch != base_branch.strip()
        or any(ord(char) < 32 or ord(char) == 127 for char in base_branch)
    ):
        return "This pull request has no valid target branch."
    if branch is not None and (
        not isinstance(branch, str) or not branch or branch != branch.strip()
        or any(ord(char) < 32 or ord(char) == 127 for char in branch)
    ):
        return "Invalid worktree branch."
    if base_branch is not None and (
        not isinstance(base_branch, str) or not base_branch
        or base_branch != base_branch.strip()
        or any(ord(char) < 32 or ord(char) == 127 for char in base_branch)
    ):
        return "Invalid worktree base branch."
    if new_branch and not branch:
        return "Choose a worktree branch before creating it."
    if base_branch and not (new_branch or is_pull_request):
        return "A base branch requires a new worktree branch."
    if scratch and (branch or new_branch or base_branch):
        return "Scratch sessions cannot use worktree options."
    if not scratch and not path:
        return "Choose a project path or create a scratch session."
    if len(options.get("initial_turn", "")) > 10000:
        return "The initial session message is too long."
    return None


def _pr_worktree_args(path, head_branch, base_branch):
    head_ref = subprocess.run(
        ["git", "-C", path, "check-ref-format", "--branch", head_branch],
        capture_output=True, text=True, timeout=ACTION_TIMEOUT_SECONDS, check=False,
    )
    if head_ref.returncode:
        raise ValueError("Invalid pull request head branch.")
    local_head = subprocess.run(
        ["git", "-C", path, "show-ref", "--verify", "--quiet",
         f"refs/heads/{head_branch}"],
        capture_output=True, text=True, timeout=ACTION_TIMEOUT_SECONDS, check=False,
    )
    if local_head.returncode == 0:
        return ["--worktree", head_branch]
    if local_head.returncode != 1:
        raise RuntimeError("Could not inspect local pull request branches.")
    if not base_branch:
        raise ValueError("Pull request target branch is unavailable.")
    base_ref = subprocess.run(
        ["git", "-C", path, "check-ref-format", "--branch", base_branch],
        capture_output=True, text=True, timeout=ACTION_TIMEOUT_SECONDS, check=False,
    )
    if base_ref.returncode:
        raise ValueError("Invalid pull request target branch.")
    return [
        "--worktree", head_branch, "--new-branch",
        "--base-branch", base_branch,
    ]


def start_new_session(token, params, results):
    def create():
        option_error = _session_options_error(params)
        if option_error:
            results.put({"token": token, "label": "New session",
                         "completed": False, "error": option_error})
            return
        title = params.get("title", "").strip()
        path = params.get("path", "").strip()
        profile = params.get("profile", "").strip()
        tool_name = params.get("tool", "").strip()
        group = params.get("group", "").strip()
        branch = params.get("worktree_branch")
        base_branch = params.get("base_branch")
        initial_turn = params.get("initial_turn", "")
        command = ["aoe", "add"]
        if profile:
            command.extend(["--profile", profile])
        if tool_name:
            command.extend(["--tool", tool_name])
        if title:
            command.extend(["--title", title])
        if group:
            command.extend(["--group", group])
        try:
            if branch:
                if params.get("is_pull_request"):
                    command.extend(_pr_worktree_args(path, branch, base_branch))
                else:
                    command.extend(["--worktree", branch])
                    if params.get("new_branch"):
                        command.append("--new-branch")
                        if base_branch:
                            command.extend(["--base-branch", base_branch])
            if params.get("scratch") is True:
                command.append("--scratch")
            else:
                if path.startswith("-"):
                    command.append("--")
                command.append(path)
            created = subprocess.run(command, capture_output=True, text=True,
                                     timeout=ACTION_TIMEOUT_SECONDS, check=False)
            if created.returncode:
                error = (created.stderr or created.stdout or "aoe add failed").strip()[:300]
                results.put({"token": token, "label": "New session",
                             "completed": False, "error": error})
                return
            match = re.search(r"(?m)^\s*ID:\s*(\S+)\s*$", created.stdout)
            if not match:
                results.put({"token": token, "label": "New session",
                             "completed": False, "error": "aoe add did not report a session ID"})
                return
            session_id = match.group(1)
            start_command = ["aoe", "session", "start"]
            if profile:
                start_command.extend(["--profile", profile])
            start_command.append(session_id)
            started = subprocess.run(start_command, capture_output=True, text=True,
                                     timeout=ACTION_TIMEOUT_SECONDS, check=False)
            if started.returncode:
                error = (started.stderr or started.stdout or "aoe session start failed").strip()[:300]
                results.put({"token": token, "label": "New session",
                             "completed": False,
                             "error": f"Created {session_id}, but start failed: {error}",
                             "session_id": session_id})
                return
            if initial_turn:
                time.sleep(INITIAL_MESSAGE_DELAY_SECONDS)
                send_command = ["aoe", "send", "--no-revive"]
                if profile:
                    send_command.extend(["--profile", profile])
                send_command.extend([session_id, initial_turn])
                sent = subprocess.run(send_command, capture_output=True, text=True,
                                      timeout=ACTION_TIMEOUT_SECONDS, check=False)
                if sent.returncode:
                    error = (sent.stderr or sent.stdout or "aoe send failed").strip()[:300]
                    results.put({
                        "token": token, "label": "New session", "completed": False,
                        "error": f"Created {session_id}, but initial message failed: {error}",
                        "session_id": session_id,
                    })
                    return
            results.put({"token": token, "label": "New session",
                         "completed": True, "error": "", "session_id": session_id})
        except Exception as exc:
            detail = str(exc).strip()[:300] or type(exc).__name__
            results.put({"token": token, "label": "New session",
                         "completed": False,
                         "error": f"Session creation failed: {detail}"})

    threading.Thread(target=create, daemon=True).start()


def dispatch(message):
    global refresh_requested, active_action, pane_refresh_requested
    method = message.get("method", "")
    request_id = message.get("id")
    params = message.get("params")
    if not isinstance(params, dict):
        params = {}
    session_id = params.get("session_id")
    result = {"ok": False, "error": "Unknown session."}
    if session_id is not None and session_id not in live_session_ids:
        pass
    elif method == "attention.select_item":
        item_key = params.get("item_key")
        if (not isinstance(session_id, str) or not isinstance(item_key, str)
                or not item_key or len(item_key) > 128):
            result = {"ok": False, "error": "Invalid item selection."}
        else:
            selected_item_keys[session_id] = item_key
            pane_refresh_requested = True
            result = {"ok": True}
    elif method == "attention.refresh":
        refresh_requested = True
        result = {"ok": True}
    elif method == "attention.action":
        token = params.get("token")
        action = (
            action_tokens.get(token)
            if isinstance(token, str) and action_token_sessions.get(token) == session_id
            else None
        )
        if active_action is not None:
            result = {"ok": False, "error": "Another action is already running."}
        elif not isinstance(action, dict) or action.get("_aoe_operation"):
            result = {"ok": False, "error": "This action expired. Refresh and try again."}
        else:
            action_tokens.pop(token)
            action_token_sessions.pop(token, None)
            consumed_action_ids.add(id(action))
            active_action = {"token": token, "action": action, "session_id": session_id}
            action_requests.put(active_action)
            result = {"ok": True}
    elif method == "attention.new_session":
        token = params.get("token")
        session_action = (
            action_tokens.get(token)
            if isinstance(token, str) and action_token_sessions.get(token) == session_id
            else None
        )
        if (not isinstance(session_action, dict)
                or session_action.get("_aoe_operation") != "new_session"):
            result = {
                "ok": False, "error": "This session action expired. Refresh and try again.",
            }
        elif active_action is not None:
            result = {"ok": False, "error": "Another action is already running."}
        else:
            options = session_action.get("params")
            error = _session_options_error(options)
            if error:
                result = {"ok": False, "error": error}
            else:
                item_id = session_action.get("_item_id")
                action_tokens.pop(token, None)
                action_token_sessions.pop(token, None)
                if isinstance(item_id, int):
                    consumed_session_item_ids.add(item_id)
                active_action = {"token": token, "action": {
                    "_aoe_operation": "new_session", "_item_id": item_id,
                    "params": options, "label": "New session",
                }, "session_id": session_id}
                action_requests.put(active_action)
                result = {"ok": True}
    else:
        result = {"ok": False, "error": f"Unsupported method: {method}"}
    if request_id is not None:
        send({"jsonrpc": "2.0", "id": request_id, "result": result})

def main():
    global published_ids, refresh_requested, action_tokens, action_token_sessions
    global live_session_ids, action_requests, action_results, active_action
    global consumed_action_ids, consumed_session_item_ids, selected_item_keys
    global pane_refresh_requested
    published_ids = set()
    refresh_requested = False
    action_tokens = {}
    action_token_sessions = {}
    consumed_action_ids = set()
    consumed_session_item_ids = set()
    live_session_ids = set()
    selected_item_keys = {}
    pane_refresh_requested = False
    action_requests = queue.Queue()
    action_results = queue.Queue()
    active_action = None
    results = queue.Queue(maxsize=1)
    payload = status_payload()
    snapshot_items = []
    sessions = []
    fetch_in_flight = False
    action_in_flight = False
    refresh_pending = False
    retry_publish = True
    next_sessions = time.monotonic()

    while True:
        try:
            message = read_message(max(0, min(0.25, next_sessions - time.monotonic())))
            if isinstance(message, dict) and "method" in message:
                dispatch(message)

            now = time.monotonic()
            if now >= next_sessions:
                updated_sessions = live_sessions()
                sessions_changed = updated_sessions != sessions
                sessions = updated_sessions
                live_session_ids = {session["id"] for session in sessions}
                live_sessions_by_id.clear()
                live_sessions_by_id.update({session["id"]: session for session in sessions})
                for stale_id in set(selected_item_keys) - live_session_ids:
                    selected_item_keys.pop(stale_id, None)
                publish_payload(
                    sessions, payload,
                    only_new=not (retry_publish or sessions_changed), items=snapshot_items,
                )
                retry_publish = False
                next_sessions = now + SESSION_POLL_SECONDS
            if pane_refresh_requested:
                pane_refresh_requested = False
                publish_payload(sessions, payload, items=snapshot_items)

            try:
                request = action_requests.get_nowait()
            except queue.Empty:
                request = None
            if request is not None:
                label = request["action"].get("label", "Action")
                payload = status_payload(f"Running: {label}")
                publish_payload(sessions, payload, items=snapshot_items)
                if request["action"].get("_aoe_operation") == "new_session":
                    start_new_session(request["token"], request["action"]["params"], action_results)
                else:
                    start_action(request["token"], request["action"], label, action_results)
                action_in_flight = True

            try:
                event = action_results.get_nowait()
            except queue.Empty:
                event = None
            if event is not None and active_action and active_action["token"] == event["token"]:
                completed_action = active_action["action"]
                if completed_action.get("_aoe_operation") == "new_session":
                    item_id = completed_action.get("_item_id")
                    if not event["completed"] and not event.get("session_id"):
                        if isinstance(item_id, int):
                            consumed_session_item_ids.discard(item_id)
                else:
                    consumed_action_ids.discard(id(completed_action))
                active_action = None
                action_in_flight = False
                message_text = (
                    f"Created session: {event['session_id']}"
                    if event["completed"] and event.get("session_id")
                    else f"Completed: {event['label']}" if event["completed"]
                    else f"Failed: {event['label']}: {event['error']}"
                )
                output = _display_text(event.get("output"), 120)
                if output:
                    message_text += f": {output}"
                payload = status_payload(message_text)
                next_sessions = time.monotonic()
                publish_payload(sessions, payload, items=snapshot_items)

            if refresh_requested:
                refresh_requested = False
                if fetch_in_flight or action_in_flight:
                    refresh_pending = True
                else:
                    start_fetch(results)
                    fetch_in_flight = True

            try:
                updated_payload, updated_actions, error, updated_items, fetched_at = results.get_nowait()
            except queue.Empty:
                updated_payload = None
                updated_actions = None
                error = None
                fetched_at = None
            if updated_payload is not None or updated_actions is not None:
                fetch_in_flight = False
                if error is None:
                    payload = updated_payload
                    action_tokens = updated_actions
                    consumed_action_ids.clear()
                    consumed_session_item_ids.clear()
                    snapshot_items = updated_items
                else:
                    payload = status_payload(
                        f"Refresh failed: {error}. Use Refresh to retry."
                    )
                publish_payload(sessions, payload, items=snapshot_items)
            if refresh_pending and not fetch_in_flight and not action_in_flight:
                refresh_pending = False
                start_fetch(results)
                fetch_in_flight = True
        except EOFError:
            return
        except Exception as exc:
            print(f"attention AoE worker: {exc}", file=sys.stderr, flush=True)
            retry_publish = True
            retry_at = time.monotonic() + SESSION_POLL_SECONDS
            next_sessions = retry_at


if __name__ == "__main__":
    main()
