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
import re
import sys
import tempfile
import threading
import time
from urllib.parse import quote, urlsplit
ATTENTION_PATH = Path(__file__).resolve().with_name("attention")
SESSION_POLL_SECONDS = 60
MAX_PANE_BYTES = 64 * 1024
MAX_ACTION_PROMPT_BYTES = 32 * 1024
ACTION_TIMEOUT_SECONDS = 120
INITIAL_MESSAGE_DELAY_SECONDS = 5
DASHBOARD_PATH = "/plugins/athal7.attention/dashboard"
STATE_VISUALS = (
    ("merge conflict", "triangle-alert", "danger"),
    ("changes requested", "circle-x", "danger"),
    ("ci failing", "circle-x", "danger"),
    ("error", "circle-alert", "danger"),
    ("failed", "circle-x", "danger"),
    ("overdue", "alarm-clock", "danger"),
    ("blocked", "circle-x", "danger"),
    ("review requested", "clock", "info"),
    ("awaiting review", "clock", "info"),
    ("waiting", "clock", "info"),
    ("running", "loader", "info"),
    ("due today", "clock", "warn"),
    ("draft", "git-pull-request-draft", "warn"),
    ("merge ready", "git-merge", "success"),
    ("approved", "badge-check", "success"),
    ("passed", "circle-check", "success"),
    ("completed", "circle-check", "success"),
)
SOURCE_ICONS = {
    "github": "git-branch",
    "linear": "circle-dot",
    "calendar": "calendar-days",
    "reminders": "list-todo",
    "generic": "inbox",
}
_input_buffer = bytearray()
_source_lock = threading.Lock()
_filter_lock = threading.Lock()
live_sessions_by_id = {}
hidden_sources = set()
hidden_providers = set()
dashboard_active = False
filter_refresh_requested = False
prompt_refresh_requested = False
snapshot_fetched_at = None
active_action = None
active_prompt = None
action_answers = queue.Queue()
consumed_action_ids = set()
consumed_session_item_ids = set()


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
                indicator_labels = getattr(module, "_INDICATOR_LABELS", {})
                for item in items:
                    group = next((group for group in groups if "match" in group
                                  and module._group_matches(item, group["match"])), fallback)
                    columns = group.get("columns") or ["state"]
                    item["_dashboard_group"] = group["name"]
                    item["_dashboard_columns"] = columns
                    item["_dashboard_column_labels"] = {
                        column: indicator_labels.get(column, column.replace("_", " ").title())
                        for column in columns
                    }
                    item["_dashboard_order"] = groups.index(group)
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


def _is_open_action(action):
    payload = action.get("payload")
    label = str(action.get("label") or "").strip().casefold()
    kind = str(payload.get("kind") or "").strip().casefold() if isinstance(payload, dict) else ""
    return kind == "open" or label == "open" or label.startswith("open ")


def _is_approve_action(action):
    payload = action.get("payload")
    label = str(action.get("label") or "").strip().casefold()
    kind = str(payload.get("kind") or "").strip().casefold() if isinstance(payload, dict) else ""
    return kind == "approve" or label == "approve"


def _open_action(item):
    for action in item.get("actions", []):
        if (not isinstance(action, dict) or id(action) in consumed_action_ids
                or not _is_open_action(action)):
            continue
        payload = action.get("payload")
        if isinstance(payload, dict) and not payload.get("inputs"):
            return action
    return None


def _primary_row(item, token_actions, token_factory):
    title = _display_text(item.get("title"), 240) or "Attention item"
    row_title = f"{item.get('_tree_prefix', '')}{title}"
    status = _display_text(item.get("status"), 100)
    context = _display_text(item.get("context"), 160)
    indicators = item.get("indicators")
    reason = item.get("attention_reason")
    if not reason and isinstance(indicators, dict):
        reason = indicators.get("state")
    reason = _display_text(reason, 160)

    summary = []
    seen = {" ".join(title.casefold().split())}
    for value in (status, context, reason):
        key = " ".join(value.casefold().split())
        if value and key not in seen:
            seen.add(key)
            summary.append(value)
    row = {
        "kind": "row", "label": row_title,
        "icon": SOURCE_ICONS.get(item.get("_plugin"), "inbox"),
    }
    if summary:
        row["sublabel"] = " · ".join(summary)[:300]
    state_values = [status, reason]
    if isinstance(indicators, dict):
        state_values.extend(indicators.values())
    state_text = " ".join(_display_text(value, 100) for value in state_values).casefold()
    visual = next((entry for entry in STATE_VISUALS if entry[0] in state_text), None)
    if item.get("kind") == "pull_request" and visual is None:
        visual = ("open PR", "git-pull-request-arrow", "success")
    if visual:
        state, icon, tone = visual
        row["badges"] = [{"icon": icon, "tone": tone, "tooltip": state}]

    open_action = _open_action(item)
    action_payload = open_action.get("payload") if open_action else None
    href = _safe_source_url(item.get("url"))
    if not href and isinstance(action_payload, dict):
        href = _safe_source_url(action_payload.get("url"))
    if href:
        row["href"] = href
    elif open_action:
        token = token_factory()
        token_actions[token] = open_action
        row.update({"method": "attention.action", "params": {"token": token}})
    return row


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


def _web_actions(item, token_actions, token_factory):
    """Expose configured actions not already represented in the primary row."""
    actions = item.get("actions", [])
    has_session_action = any(_is_session_action(action) for action in actions)
    row_open_action = _open_action(item)
    blocks = []
    for action in actions:
        if not isinstance(action, dict) or id(action) in consumed_action_ids:
            continue
        if action is row_open_action:
            continue
        if ((item.get("_is_own_pr") is True or action.get("_is_own_pr") is True)
                and _is_approve_action(action)):
            continue
        if not isinstance(action.get("payload"), dict):
            continue
        token = token_factory()
        token_actions[token] = action
        label = str(action.get("label") or "Action").strip()[:80]
        blocks.append({
            "kind": "action", "label": label[:1].upper() + label[1:],
            "method": "attention.action", "params": {"token": token},
            "variant": "secondary",
        })
    if not has_session_action and id(item) not in consumed_session_item_ids:
        action = _new_session_action(item)
        token = token_factory()
        token_actions[token] = action
        blocks.append({
            "kind": "action", "label": "New session",
            "method": "attention.new_session", "params": {"token": token},
            "variant": "secondary",
        })

    return blocks


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


def _item_cards(items, token_actions, token_factory):
    groups = []
    current_group = []
    for item in _tree_order(items):
        if current_group and not item.get("_tree_prefix"):
            groups.append(current_group)
            current_group = []
        current_group.append(item)
    if current_group:
        groups.append(current_group)

    cards = []
    for group in groups:
        item_cards = [_item_card(item, token_actions, token_factory) for item in group]
        cards.append(item_cards[0])
        if len(group) > 1:
            stacked_count = len(group) - 1
            count_label = "PR" if stacked_count == 1 else "PRs"
            cards.append({
                "kind": "section",
                "title": "Stacked pull requests",
                "value": f"{stacked_count} {count_label}",
                "boxed": True,
                "collapsible": True,
                "collapsed": True,
                "children": item_cards[1:],
            })
    return cards



def _item_card(item, token_actions, token_factory):
    row = _primary_row(item, token_actions, token_factory)
    actions = _web_actions(item, token_actions, token_factory)
    children = [row]
    for offset in range(0, len(actions), 2):
        children.append({"kind": "columns", "children": actions[offset:offset + 2]})
    return {"kind": "section", "boxed": True, "children": children}


def _walk_blocks(blocks):
    for block in blocks:
        if not isinstance(block, dict):
            continue
        yield block
        yield from _walk_blocks(block.get("children", []))


def _tokens_in_payload(payload):
    return {
        block.get("params", {}).get("token")
        for block in _walk_blocks(payload.get("blocks", []))
        if block.get("method") in {"attention.action", "attention.filter", "attention.new_session"}
        and isinstance(block.get("params", {}).get("token"), str)
    }


def _pane_toolbar():
    return {
        "kind": "columns",
        "children": [{
            "kind": "action", "label": "Refresh",
            "method": "attention.refresh", "icon": "refresh-cw",
            "variant": "secondary",
        }],
    }


def _filter_controls(items, token_actions, token_factory, hidden_sources, hidden_providers):
    options = []
    sources = sorted({
        item.get("_plugin") for item in items
        if isinstance(item.get("_plugin"), str) and item["_plugin"]
    })
    providers = sorted({
        item.get("_provider") for item in items
        if isinstance(item.get("_provider"), str) and item["_provider"]
    })
    for kind, names, hidden in (
        ("source", sources, hidden_sources),
        ("provider", providers, hidden_providers),
    ):
        for name in names:
            token = token_factory()
            token_actions[token] = {
                "_aoe_operation": "filter",
                "filter_kind": kind,
                "value": name,
            }
            options.append({
                "kind": "action",
                "label": f"{'Show' if name in hidden else 'Hide'} {kind}: {name}",
                "method": "attention.filter",
                "params": {"token": token},
                "variant": "secondary",
            })
    if not options:
        return None
    return {
        "kind": "section", "title": "Filters",
        "collapsible": True, "collapsed": True,
        "children": options,
    }


def _filter_item(item, hidden_sources, hidden_providers):
    source = item.get("_plugin")
    provider = item.get("_provider")
    return (
        source not in hidden_sources
        and provider not in hidden_providers
    )


def build_pane(items, token_factory=None, fetched_at=None, hidden_sources=(), hidden_providers=()):
    items = list(items)
    all_items = items
    items = [item for item in items if _filter_item(item, hidden_sources, hidden_providers)]
    token_factory = token_factory or (lambda: secrets.token_urlsafe(18))
    token_actions = {}
    failures = [item for item in items if item.get("_collector_failure")]
    cards = _item_cards(
        [item for item in items if not item.get("_collector_failure")],
        token_actions,
        token_factory,
    )
    toolbar = _pane_toolbar()
    filters = _filter_controls(
        all_items, token_actions, token_factory, hidden_sources, hidden_providers,
    )
    omission = {"kind": "note", "text": "Additional items omitted to fit the pane."}
    base = {"title": "Attention", "default_location": "right"}
    notices = [
        {"kind": "note", "text": f"{item['context']} collector failed: {item['details']}"}
        for item in failures
    ]
    notices = ([{"kind": "note", "text": f"Fetched at {fetched_at} · Refresh to update."}]
               if fetched_at else []) + notices
    blocks = [toolbar]
    if filters:
        blocks.append(filters)
    blocks.extend(notices)
    if not cards and not failures:
        blocks.append({"kind": "note", "text": "No attention items."})
    blocks.extend(cards)
    payload = {**base, "blocks": blocks}
    if len(json.dumps(payload, separators=(",", ":")).encode("utf-8")) <= MAX_PANE_BYTES:
        return payload, token_actions

    kept = []
    blocks = [toolbar]
    if filters:
        blocks.append(filters)
    blocks.extend(notices)
    blocks.append(omission)
    size = len(json.dumps({**base, "blocks": blocks}, separators=(",", ":")).encode("utf-8"))
    for card in cards:
        card_size = len(json.dumps(card, separators=(",", ":")).encode("utf-8")) + 1
        if size + card_size > MAX_PANE_BYTES:
            break
        kept.append(card)
        size += card_size
    payload = {**base, "blocks": [toolbar, *([filters] if filters else []), *notices, *kept, omission]}
    visible_tokens = _tokens_in_payload(payload)
    return payload, {token: action for token, action in token_actions.items() if token in visible_tokens}


def pane_payload(items):
    return build_pane(items)[0]

def _matching_sessions(item, sessions, allow_title=True):
    branch = item.get("head_branch")
    repo_path = item.get("repo_path")
    repo_path = os.path.realpath(repo_path) if isinstance(repo_path, str) and repo_path else None
    title = f"Attention: {_display_text(item.get('title'), 120) or 'Follow-up'}"
    matches = []
    for session in sessions:
        if (session.get("status") not in {"Running", "Waiting", "Starting", "Creating"}
                or any(session.get(flag) for flag in ("archived", "snoozed", "trashed"))):
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


def payload_with_sessions(base_payload, items, sessions):
    items = [item for item in items if not item.get("_collector_failure")]
    items = _tree_order(items)
    titles = [_display_text(item.get("title"), 120) or "Follow-up" for item in items]
    payload = json.loads(json.dumps(base_payload))
    cards = (
        block for block in _walk_blocks(payload["blocks"])
        if block.get("kind") == "section" and block.get("collapsible") is not True
    )
    for card, item, title in zip(cards, items, titles):
        matches = _matching_sessions(item, sessions, allow_title=titles.count(title) == 1)
        if matches:
            session = matches[0]
            card["children"].insert(1, {
                "kind": "row",
                "label": f"Session: {_display_text(session.get('title'), 120) or session['id']}",
                "sublabel": session["status"],
                "icon": "square-terminal",
                "tone": "info",
                "href": f"/session/{quote(session['id'], safe='')}",
            })
    return _fit_payload(payload)


def live_sessions():
    result = rpc("sessions.list")
    sessions = result.get("sessions", []) if isinstance(result, dict) else []
    sessions = [session for session in sessions if isinstance(session, dict) and isinstance(session.get("id"), str)]
    live_ids = {session["id"] for session in sessions}
    for stale_id in published_ids - live_ids:
        rpc("ui.state.remove", {"slot": "pane", "id": "attention", "session_id": stale_id})
        published_ids.discard(stale_id)
    return sessions





def status_payload(text):
    return {
        "title": "Attention",
        "default_location": "right",
        "blocks": [_pane_toolbar(), {"kind": "note", "text": text}],
    }


def _payload_action_tokens(payload):
    return _tokens_in_payload(payload)


def _remove_action_token(blocks, token):
    cleaned = []
    for block in blocks:
        params = block.get("params", {})
        if block.get("method") in {"attention.action", "attention.new_session"} and params.get("token") == token:
            continue
        copy = dict(block)
        if isinstance(copy.get("children"), list):
            copy["children"] = _remove_action_token(copy["children"], token)
        cleaned.append(copy)
    return cleaned


def payload_with_notice(base_payload, text, consumed_token=None):
    payload = json.loads(json.dumps(base_payload))
    if consumed_token is not None:
        payload["blocks"] = _remove_action_token(payload["blocks"], consumed_token)
    if text:
        blocks = payload["blocks"]
        insert_at = next((i for i, block in enumerate(blocks)
                          if block.get("kind") in {"section", "note"}), max(0, len(blocks) - 1))
        blocks.insert(insert_at, {"kind": "note", "text": text[:200]})
    return _fit_payload(payload)


def _fit_payload(payload):
    blocks = payload["blocks"]
    omission = {"kind": "note", "text": "Additional items omitted to fit the pane."}
    while len(json.dumps(payload, separators=(",", ":")).encode("utf-8")) > MAX_PANE_BYTES:
        card_index = next((i for i in range(len(blocks) - 1, -1, -1) if blocks[i].get("kind") == "section"), None)
        if card_index is None:
            break
        blocks.pop(card_index)
        if omission not in blocks:
            toolbar_index = next((i for i, block in enumerate(blocks)
                                  if block.get("kind") == "columns" and any(
                                      child.get("method") == "attention.refresh"
                                      for child in block.get("children", [])
                                  )), len(blocks))
            blocks.insert(toolbar_index, omission)
    return payload

def dashboard_payload(items, fetched_at, sessions, token_actions=None):
    token_actions = {} if token_actions is None else token_actions
    cards = []
    titles = [_display_text(item.get("title"), 120) or "Follow-up"
              for item in items if not item.get("_collector_failure")]
    for item in _tree_order([item for item in items if not item.get("_collector_failure")]):
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
            "title": title, "url": _safe_source_url(item.get("url")),
            "status": _display_text(item.get("status"), 100),
            "context": _display_text(item.get("context"), 160),
            "reason": _display_text(item.get("attention_reason"), 160),
            "details": _display_text(item.get("details"), 4000),
            "indicators": {str(k): _display_text(v, 120)
                           for k, v in (item.get("indicators") or {}).items()},
            "columns": [
                _display_text(column, 120)
                for column in item.get("_dashboard_columns", ["state"])
                if isinstance(column, str)
            ],
            "column_labels": {
                _display_text(key, 120): _display_text(value, 120)
                for key, value in item.get("_dashboard_column_labels", {}).items()
                if isinstance(key, str)
            } if isinstance(item.get("_dashboard_column_labels", {}), dict) else {},
            "source": _display_text(item.get("_plugin"), 100),
            "provider": _display_text(item.get("_provider"), 100),
            "group": _display_text(item.get("_dashboard_group", "Attention"), 120),
            "group_order": item.get("_dashboard_order", 0), "actions": actions,
            "session": ({"id": matches[0]["id"], "title": _display_text(
                matches[0].get("title"), 120) or matches[0]["id"]} if matches else None),
        })
    failures = [{"source": _display_text(item.get("context"), 100),
                 "error": _display_text(item.get("details"), 200)}
                for item in items if item.get("_collector_failure")]
    return {"items": cards, "failures": failures, "fetched_at": fetched_at,
            "busy": active_action is not None, "prompt": active_prompt,
            "omitted_items": 0, "omitted_failures": 0}


def _fit_dashboard(payload):
    encoded = lambda: len(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    while payload["failures"] and encoded() > MAX_PANE_BYTES:
        payload["failures"].pop()
        payload["omitted_failures"] += 1
    while payload["items"] and encoded() > MAX_PANE_BYTES:
        payload["items"].pop()
        payload["omitted_items"] += 1
    return payload


def _notice_text(payload):
    return next((block.get("text", "") for block in payload.get("blocks", [])
                 if block.get("kind") == "note" and block.get("text")), "")

def publish_payload(sessions, payload, only_new=False, items=(), action_output=""):
    global action_tokens
    visible = [item for item in items if _filter_item(item, hidden_sources, hidden_providers)]
    dashboard_tokens = set()
    if dashboard_active:
        page = dashboard_payload(
            visible, snapshot_fetched_at, sessions, action_tokens,
        )
        if action_output:
            page["action_output"] = action_output
        notice = _display_text(_notice_text(payload), 400)
        page["status"] = notice
        page["error"] = notice if ("failed" in notice.casefold()
                                   or notice.startswith("New session:")) else ""
        page = _fit_dashboard(page)
        dashboard_tokens = {
            action["token"]
            for card in page.get("items", [])
            for action in card.get("actions", [])
            if isinstance(action, dict) and isinstance(action.get("token"), str)
        }
        rpc("ui.state.set", {"slot": "page", "id": "dashboard", "payload": page})
    pane = payload_with_sessions(payload, visible, sessions)
    visible_tokens = _payload_action_tokens(pane)
    action_tokens = {token: action for token, action in action_tokens.items()
                     if token in visible_tokens or token in dashboard_tokens}
    for session in sessions:
        session_id = session["id"]
        if only_new and session_id in published_ids:
            continue
        rpc("ui.state.set", {
            "slot": "pane", "id": "attention", "session_id": session_id, "payload": pane,
        })
        published_ids.add(session_id)


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
            with _filter_lock:
                current_hidden_sources = set(hidden_sources)
                current_hidden_providers = set(hidden_providers)
            payload, actions = build_pane(
                items, fetched_at=fetched_at,
                hidden_sources=current_hidden_sources,
                hidden_providers=current_hidden_providers,
            )
        results.put((payload, actions, None, items, fetched_at))
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
                                prompt_data = response["prompt"]
                                prompt_id = secrets.token_urlsafe(18)
                                size = os.fstat(output.fileno()).st_size
                                context = os.pread(output.fileno(), min(size - offset, 4000),
                                                   max(offset, size - 4000)).decode("utf-8", errors="replace")
                                offset = size
                                prompt = {
                                    "id": prompt_id, "token": token,
                                    "text": _display_text(
                                        prompt_data.get("text") if isinstance(prompt_data, dict)
                                        else prompt_data, 1000,
                                    ) or "Input required",
                                    "context": context,
                                }
                                if isinstance(prompt_data, dict):
                                    choices = prompt_data.get("choices")
                                    if isinstance(choices, list):
                                        prompt["choices"] = [
                                            choice[:200] for choice in choices[:100]
                                            if isinstance(choice, str)
                                        ]
                                    default = prompt_data.get("default")
                                    if isinstance(default, str):
                                        prompt["default"] = default[:1000]
                                if (len(json.dumps(
                                        prompt, separators=(",", ":")
                                ).encode("utf-8")) > MAX_ACTION_PROMPT_BYTES):
                                    error = "Action prompt exceeds the dashboard size limit."
                                    process.kill()
                                    process.wait()
                                    buffer.clear()
                                    break
                                results.put({"token": token, "prompt": prompt})
                                try:
                                    answer = action_answers.get(
                                        timeout=max(0.1, deadline - time.monotonic())
                                    )
                                except queue.Empty as exc:
                                    raise TimeoutError from exc
                                if answer is None:
                                    error = "Action canceled."
                                process.stdin.write((json.dumps({"value": answer}) + "\n").encode())
                                process.stdin.flush()
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
    global refresh_requested, active_action, active_prompt, filter_refresh_requested
    global dashboard_active, prompt_refresh_requested
    method = message.get("method", "")
    request_id = message.get("id")
    params = message.get("params")
    if not isinstance(params, dict):
        params = {}
    session_id = params.get("session_id")
    if (session_id is None
            and method in {"attention.refresh", "attention.input", "attention.filter",
                           "attention.action", "attention.new_session"}):
        dashboard_active = True
    result = {"ok": False, "error": "Unknown session."}
    if session_id is not None and session_id not in live_session_ids:
        pass
    elif method == "attention.refresh":
        refresh_requested = True
        result = {"ok": True}
    elif method == "attention.input":
        if (not active_prompt or params.get("token") != active_prompt["token"]
                or params.get("prompt_id") != active_prompt["id"]
                or not (params.get("value") is None or isinstance(params.get("value"), str))):
            result = {"ok": False, "error": "This prompt expired."}
        else:
            active_prompt = None
            action_answers.put(params.get("value"))
            prompt_refresh_requested = True
            result = {"ok": True}
    elif method == "attention.filter":
        token = params.get("token")
        action = action_tokens.get(token) if isinstance(token, str) else None
        kind = action.get("filter_kind") if isinstance(action, dict) else None
        value = action.get("value") if isinstance(action, dict) else None
        if (not isinstance(action, dict) or action.get("_aoe_operation") != "filter"
                or kind not in {"source", "provider"} or not isinstance(value, str)):
            result = {"ok": False, "error": "This filter expired. Refresh and try again."}
        elif active_action is not None:
            result = {"ok": False, "error": "Another action is already running."}
        else:
            action_tokens.pop(token, None)
            with _filter_lock:
                filters = hidden_sources if kind == "source" else hidden_providers
                if value in filters:
                    filters.remove(value)
                else:
                    filters.add(value)
            filter_refresh_requested = True
            result = {"ok": True}
    elif method == "attention.action":
        token = params.get("token")
        action = action_tokens.get(token) if isinstance(token, str) else None
        if active_action is not None:
            result = {"ok": False, "error": "Another action is already running."}
        elif not isinstance(action, dict) or action.get("_aoe_operation"):
            result = {"ok": False, "error": "This action expired. Refresh and try again."}
        else:
            action_tokens.pop(token)
            consumed_action_ids.add(id(action))
            active_action = {"token": token, "action": action, "session_id": session_id}
            action_requests.put(active_action)
            result = {"ok": True}
    elif method == "attention.new_session":
        token = params.get("token")
        session_action = action_tokens.get(token) if isinstance(token, str) else None
        item_id = None
        if token is not None and (
            not isinstance(session_action, dict)
            or session_action.get("_aoe_operation") != "new_session"
        ):
            result = {"ok": False, "error": "This session action expired. Refresh and try again."}
        else:
            if token is not None:
                options = session_action.get("params")
                item_id = session_action.get("_item_id")
            else:
                options = {
                    "title": params.get("title", ""),
                    "path": params.get("path", ""),
                    "profile": params.get("profile", ""),
                    "tool": params.get("tool", ""),
                    "group": params.get("group", ""),
                    "worktree_branch": params.get("worktree_branch"),
                    "new_branch": params.get("new_branch", False),
                    "base_branch": params.get("base_branch"),
                    "scratch": params.get("scratch", False),
                }
            error = _session_options_error(options)
            if error:
                result = {"ok": False, "error": error}
            elif active_action is not None:
                result = {"ok": False, "error": "Another action is already running."}
            else:
                active_token = token or secrets.token_urlsafe(18)
                if token is not None:
                    action_tokens.pop(token, None)
                    if isinstance(item_id, int):
                        consumed_session_item_ids.add(item_id)
                active_action = {"token": active_token, "action": {
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

    global published_ids, refresh_requested, action_tokens, live_session_ids
    global action_requests, action_results, active_action
    global filter_refresh_requested, hidden_sources, hidden_providers, snapshot_fetched_at, dashboard_active
    global action_answers, active_prompt, prompt_refresh_requested, consumed_action_ids
    global consumed_session_item_ids
    dashboard_active = False
    hidden_sources = set()
    hidden_providers = set()
    snapshot_fetched_at = None
    published_ids = set()
    refresh_requested = False
    prompt_refresh_requested = False
    action_tokens = {}
    consumed_action_ids = set()
    consumed_session_item_ids = set()
    live_session_ids = set()
    action_requests = queue.Queue()
    action_results = queue.Queue()
    action_answers = queue.Queue()
    active_action = None
    active_prompt = None
    results = queue.Queue(maxsize=1)
    base_payload = status_payload("Select Refresh to load attention items.")
    payload = base_payload
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
            if prompt_refresh_requested:
                prompt_refresh_requested = False
                payload = payload_with_notice(base_payload, "Action running.")
                publish_payload(sessions, payload, items=snapshot_items)
            if filter_refresh_requested:
                filter_refresh_requested = False
                with _filter_lock:
                    current_hidden_sources = set(hidden_sources)
                    current_hidden_providers = set(hidden_providers)
                base_payload, action_tokens = build_pane(
                    snapshot_items, fetched_at=snapshot_fetched_at,
                    hidden_sources=current_hidden_sources,
                    hidden_providers=current_hidden_providers,
                )
                payload = base_payload
                publish_payload(sessions, payload, items=snapshot_items)

            now = time.monotonic()
            if now >= next_sessions:
                updated_sessions = live_sessions()
                sessions_changed = updated_sessions != sessions
                sessions = updated_sessions
                live_session_ids = {session["id"] for session in sessions}
                live_sessions_by_id.clear()
                live_sessions_by_id.update({session["id"]: session for session in sessions})
                publish_payload(sessions, payload, only_new=not (retry_publish or sessions_changed), items=snapshot_items)
                retry_publish = False
                next_sessions = now + SESSION_POLL_SECONDS

            try:
                request = action_requests.get_nowait()
            except queue.Empty:
                request = None
            if request is not None:
                label = request["action"].get("label", "Action")
                payload = payload_with_notice(base_payload, f"Running: {label}", request["token"])
                base_payload = payload_with_notice(base_payload, "", request["token"])
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
                if "prompt" in event:
                    active_prompt = event["prompt"]
                    payload = payload_with_notice(
                        base_payload, "Input required. Continue in the Attention dashboard."
                    )
                    action_in_flight = False
                    publish_payload(sessions, payload, items=snapshot_items)
                else:
                    completed_action = active_action["action"]
                    if completed_action.get("_aoe_operation") == "new_session":
                        item_id = completed_action.get("_item_id")
                        if not event["completed"] and not event.get("session_id"):
                            if isinstance(item_id, int):
                                consumed_session_item_ids.discard(item_id)
                    else:
                        consumed_action_ids.discard(id(completed_action))
                    active_action = None
                    active_prompt = None
                    action_in_flight = False
                    message_text = (
                        f"Created session: {event['session_id']}"
                        if event["completed"] and event.get("session_id")
                        else f"Completed: {event['label']}" if event["completed"]
                        else f"{event['label']}: {event['error']}"
                    )
                    with _filter_lock:
                        current_hidden_sources = set(hidden_sources)
                        current_hidden_providers = set(hidden_providers)
                    base_payload, action_tokens = build_pane(
                        snapshot_items, fetched_at=snapshot_fetched_at,
                        hidden_sources=current_hidden_sources,
                        hidden_providers=current_hidden_providers,
                    )
                    payload = payload_with_notice(base_payload, message_text)
                    next_sessions = time.monotonic()
                    publish_payload(
                        sessions, payload, items=snapshot_items,
                        action_output=event.get("output", ""),
                    )

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
                    base_payload = updated_payload
                    payload = base_payload
                    action_tokens = updated_actions
                    consumed_action_ids.clear()
                    consumed_session_item_ids.clear()
                    snapshot_items = updated_items
                    snapshot_fetched_at = fetched_at
                else:
                    payload = payload_with_notice(
                        base_payload, f"Refresh failed: {error}. Select Refresh to retry."
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
