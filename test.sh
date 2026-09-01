#!/usr/bin/env bash
# Regression tests for the `attention` CLI's plugin architecture. Plain
# bash, no bats. Stubs gh/remindctl/ical/aoe-cmd/lumen/open/pbcopy so
# the script runs against fixed fixture data instead of live system
# data, and writes a JSON config file at a temp $XDG_CONFIG_HOME instead
# of touching the real one.
#
#   ./test.sh

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ATTENTION="$REPO_ROOT/attention"

WORK="$(mktemp -d "${TMPDIR:-/tmp}/attention-test.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT INT TERM

pass=0; fail=0
ok()   { printf '  ok   %s\n' "$1"; pass=$((pass + 1)); }
bad()  { printf '  FAIL %s\n' "$1"; fail=$((fail + 1)); }
check(){ if [ "$2" = "$3" ]; then ok "$1 ($2)"; else bad "$1 (want '$3' got '$2')"; fi; }
skip() { printf '  skip %s\n' "$1"; }

STUB_BIN="$WORK/bin"
mkdir -p "$STUB_BIN"

TEST_HOME="$WORK/home"
mkdir -p "$TEST_HOME"

XDG_CONFIG="$WORK/xdg-config"
mkdir -p "$XDG_CONFIG/attention"

write_config() {
  cat > "$XDG_CONFIG/attention/config.json"
}

# blob_for <<<'[{"key": "o", ...}, ...]' -> base64(JSON) of that exact
# actions array, matching render_rows()'s hidden second field. Lets tests
# build realistic dashboard rows without hand-typing base64.
blob_for() {
  python3 -c "import json,base64,sys; print(base64.b64encode(json.dumps(json.loads(sys.stdin.read())).encode()).decode())"
}

TAB=$'\t'

# Bootstraps for loading modules directly (unit-testing pure functions
# without going through the CLI/subprocess layer). Plugins need
# sources/ on sys.path first so their `from _util import ...` resolves.
LOAD_CORE="
import importlib.util
from importlib.machinery import SourceFileLoader
loader = SourceFileLoader('attention_core', '$ATTENTION')
spec = importlib.util.spec_from_loader('attention_core', loader)
m = importlib.util.module_from_spec(spec)
loader.exec_module(m)
"

LOAD_DASHBOARD="
import importlib.util
spec = importlib.util.spec_from_file_location('dashboard', '$REPO_ROOT/dashboard.py')
d = importlib.util.module_from_spec(spec)
spec.loader.exec_module(d)
"

# Test doubles for DashboardController: a fake Presenter driven entirely by
# threading primitives (never wall-clock sleeps) plus small fetch_plugin/
# build_snapshot/render_rows/act fakes closures can control. Concatenated
# after \$LOAD_DASHBOARD in every DashboardController test's python3 -c.
DASHBOARD_FIXTURES="
import base64
import json
import queue
import threading


class FakePresenter:
    def __init__(self):
        self._lock = threading.RLock()
        self._cond = threading.Condition(self._lock)
        self.calls = []
        self.launch_count = 0
        self.stopped = False
        self._results = queue.Queue()

    def launch(self):
        with self._lock:
            self.launch_count += 1
            self.calls.append(('launch',))
            self._cond.notify_all()

    def push_snapshot(self, rows, pending):
        with self._lock:
            self.calls.append(('push', list(rows), list(pending)))
            self._cond.notify_all()

    def push_calls(self):
        with self._lock:
            return [c for c in self.calls if c[0] == 'push']

    def launch_calls(self):
        with self._lock:
            return [c for c in self.calls if c[0] == 'launch']

    def wait_for_push_count(self, n, timeout=5):
        with self._cond:
            return self._cond.wait_for(lambda: len(self.push_calls()) >= n, timeout=timeout)

    def wait_for_launch_count(self, n, timeout=5):
        with self._cond:
            return self._cond.wait_for(lambda: self.launch_count >= n, timeout=timeout)

    def wait_for_exit(self, timeout):
        return self._results.get()

    def stop(self):
        with self._lock:
            self.stopped = True
            self.calls.append(('stop',))
        self._results.put(d.PresenterResult('', ''))

    def send_timeout(self):
        self._results.put(d.PresenterResult(None, ''))

    def send_result(self, key, row):
        self._results.put(d.PresenterResult(key, row))


class CallLog:
    def __init__(self):
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._items = []

    def append(self, item):
        with self._lock:
            self._items.append(item)
            self._cond.notify_all()

    def snapshot(self):
        with self._lock:
            return list(self._items)

    def wait_for_count(self, n, timeout=5):
        with self._cond:
            return self._cond.wait_for(lambda: len(self._items) >= n, timeout=timeout)


def gated_fetch_plugin(items_by_name, gates, calls):
    def fetch_plugin(name):
        calls.append(name)
        gate = gates.get(name)
        if gate is not None:
            gate.wait()
        return items_by_name.get(name, [])
    return fetch_plugin


def flatten_build_snapshot(items_by_plugin, recently_acted):
    items = []
    for name in items_by_plugin:
        for it in items_by_plugin[name]:
            it = dict(it)
            if it.get('id') in recently_acted:
                it['weight'] = max(0, it.get('weight', 0) - 20)
            items.append(it)
    items.sort(key=lambda x: x.get('weight', 0), reverse=True)
    return items


def titles_render_rows(items):
    return [it['title'] for it in items]


def blob_render_rows(items):
    rows = []
    for it in items:
        title = it['title']
        blob = base64.b64encode(json.dumps(it.get('actions', [])).encode()).decode()
        rows.append(f'{title}\t{blob}')
    return rows
"

load_plugin_py() {
  local name="$1"
  cat <<PY
import sys
sys.path.insert(0, "$REPO_ROOT/sources")
import importlib.util
from importlib.machinery import SourceFileLoader
loader = SourceFileLoader("attention_plugin_${name}", "$REPO_ROOT/sources/${name}.py")
spec = importlib.util.spec_from_loader("attention_plugin_${name}", loader)
p = importlib.util.module_from_spec(spec)
loader.exec_module(p)
PY
}

# Portable "clearly yesterday" ISO date (matches the yyyy-mm-dd prefix the
# reminders plugin parses via date.fromisoformat(due[:10])).
YESTERDAY="$(date -v-1d +%F 2>/dev/null || date -d yesterday +%F)"

# ---------------------------------------------------------------------------
echo "== reminders plugin: configured lists only, priority, overdue status =="

write_config <<'JSON'
{"plugins": ["reminders"], "reminders": {"lists": ["Personal", "Work"]}}
JSON

cat > "$STUB_BIN/remindctl" <<STUB
#!/bin/sh
if [ "\$1" = "show" ] && [ "\$2" = "all" ]; then
  cat <<JSON
[
  {"id": "r1", "title": "REGRESSION-open-personal", "listName": "Personal", "isCompleted": false, "priority": "none"},
  {"id": "r2", "title": "REGRESSION-completed-work", "listName": "Work", "isCompleted": true, "priority": "none"},
  {"id": "r3", "title": "REGRESSION-open-shopping", "listName": "Shopping", "isCompleted": false, "priority": "none"},
  {"id": "r4", "title": "REGRESSION-overdue-work", "listName": "Work", "isCompleted": false, "priority": "none", "dueDate": "${YESTERDAY}"}
]
JSON
  exit 0
fi
echo "fake remindctl: unexpected invocation: \$*" >&2
exit 99
STUB
chmod +x "$STUB_BIN/remindctl"

test_reminders() {
  local out
  out="$(HOME="$TEST_HOME" XDG_CONFIG_HOME="$XDG_CONFIG" PATH="$STUB_BIN:$PATH" python3 "$ATTENTION" list)"

  if grep -q 'REGRESSION-open-personal' <<<"$out"; then
    ok "open reminder in configured list (Personal) appears"
  else
    bad "open reminder in configured list (Personal) appears (got: $out)"
  fi
  if grep -q 'REGRESSION-completed-work' <<<"$out"; then
    bad "completed reminder must not appear"
  else
    ok "completed reminder does not appear"
  fi
  if grep -q 'REGRESSION-open-shopping' <<<"$out"; then
    bad "reminder in unconfigured list must not appear"
  else
    ok "reminder in unconfigured list does not appear"
  fi

  local overdue_line
  overdue_line="$(grep 'REGRESSION-overdue-work' <<<"$out" || true)"
  if grep -Eq '^OVERDUE\b' <<<"$overdue_line"; then
    ok "overdue reminder gets OVERDUE status (overdue logic engaged)"
  else
    bad "overdue reminder gets OVERDUE status (got: $overdue_line)"
  fi
}
test_reminders

# ---------------------------------------------------------------------------
echo
echo "== calendar plugin: configured names only, declined events excluded =="

cat > "$STUB_BIN/ical" <<'STUB'
#!/bin/sh
cal=""
prev=""
for a in "$@"; do
  if [ "$prev" = "-c" ]; then cal="$a"; fi
  prev="$a"
done
case "$cal" in
  Work)
    echo '[{"id": "e1", "title": "CALTEST-work-event", "status": "confirmed", "availability": "busy", "all_day": true}]'
    ;;
  Family)
    echo '[{"id": "e2", "title": "CALTEST-family-event", "status": "confirmed", "availability": "busy", "all_day": true}]'
    ;;
  *)
    echo "[]"
    ;;
esac
STUB
chmod +x "$STUB_BIN/ical"

write_config <<'JSON'
{"plugins": ["calendar"], "calendar": {"names": ["Work"]}}
JSON

test_calendar_configured_names() {
  local out
  out="$(HOME="$TEST_HOME" XDG_CONFIG_HOME="$XDG_CONFIG" PATH="$STUB_BIN:$PATH" python3 "$ATTENTION" list)"
  if grep -q 'CALTEST-work-event' <<<"$out"; then
    ok "event on a configured calendar (Work) appears"
  else
    bad "event on a configured calendar (Work) appears (got: $out)"
  fi
  if grep -q 'CALTEST-family-event' <<<"$out"; then
    bad "event on a non-configured calendar (Family) must not appear"
  else
    ok "event on a non-configured calendar (Family) does not appear"
  fi
}
test_calendar_configured_names

echo
echo "-- declined event exclusion --"

DECLINE_BIN="$WORK/bin-cal-decline"
mkdir -p "$DECLINE_BIN"
cat > "$DECLINE_BIN/ical" <<'STUB'
#!/bin/sh
cat <<'JSON'
[
  {"id": "e1", "title": "DECLINETEST-attending", "status": "confirmed", "availability": "busy", "all_day": true, "self_status": "accepted"},
  {"id": "e2", "title": "DECLINETEST-declined", "status": "confirmed", "availability": "busy", "all_day": true, "self_status": "declined"}
]
JSON
STUB
chmod +x "$DECLINE_BIN/ical"

test_declined_event_excluded() {
  local out
  out="$(HOME="$TEST_HOME" XDG_CONFIG_HOME="$XDG_CONFIG" PATH="$DECLINE_BIN:$PATH" python3 "$ATTENTION" list)"
  if grep -q 'DECLINETEST-attending' <<<"$out"; then
    ok "accepted event on a configured calendar appears"
  else
    bad "accepted event on a configured calendar appears (got: $out)"
  fi
  if grep -q 'DECLINETEST-declined' <<<"$out"; then
    bad "declined event must not appear even on a configured calendar"
  else
    ok "declined event does not appear"
  fi
}
test_declined_event_excluded

# ---------------------------------------------------------------------------
echo
echo "== github plugin: global search, authored-PR attention, owned-repo issues, de-dupe =="

GH_BIN="$WORK/bin-gh"
mkdir -p "$GH_BIN"
GH_LOG="$WORK/gh-invocations.log"
: > "$GH_LOG"

cat > "$GH_BIN/gh" <<STUB
#!/bin/sh
echo "\$*" >> "$GH_LOG"
case "\$*" in
  "search prs --review-requested=@me"*)
    cat <<'JSON'
[{"number": 1, "title": "GHTEST-review-me", "repository": {"name": "kb", "nameWithOwner": "myorg/kb"}, "url": "https://github.com/myorg/kb/pull/1"}, {"number": 5, "title": "GHTEST-draft-review", "repository": {"name": "kb", "nameWithOwner": "myorg/kb"}, "url": "https://github.com/myorg/kb/pull/5", "isDraft": true}]
JSON
    ;;
  "search prs --author=@me"*)
    cat <<'JSON'
[{"number": 3, "title": "GHTEST-authored-me", "repository": {"name": "kb", "nameWithOwner": "myorg/kb"}, "url": "https://github.com/myorg/kb/pull/3"}]
JSON
    ;;
  "search issues --assignee=@me"*)
    cat <<'JSON'
[{"number": 2, "title": "GHTEST-assigned-me", "repository": {"name": "kb", "nameWithOwner": "myorg/kb"}, "url": "https://github.com/myorg/kb/issues/2"}]
JSON
    ;;
  "search issues --owner=@me"*)
    cat <<'JSON'
[{"number": 2, "title": "GHTEST-assigned-me", "repository": {"name": "kb", "nameWithOwner": "myorg/kb"}, "url": "https://github.com/myorg/kb/issues/2"}, {"number": 4, "title": "GHTEST-repo-issue", "repository": {"name": "kb", "nameWithOwner": "myorg/kb"}, "url": "https://github.com/myorg/kb/issues/4"}]
JSON
    ;;
  "api user --jq .login")
    echo "ghtestuser"
    ;;
  "pr view 3 -R myorg/kb"*)
    cat <<'JSON'
{"mergeable": "CONFLICTING", "reviewDecision": "CHANGES_REQUESTED", "statusCheckRollup": [{"conclusion": "FAILURE"}], "latestReviews": [{"author": {"login": "someone-else"}, "state": "CHANGES_REQUESTED"}, {"author": {"login": "another-reviewer"}, "state": "COMMENTED"}]}
JSON
    ;;
  *)
    echo "[]"
    ;;
esac
exit 0
STUB
chmod +x "$GH_BIN/gh"

write_config <<'JSON'
{"plugins": ["github"], "codeDir": "/tmp/nonexistent-fakecode", "github": {}}
JSON

test_github_source() {
  local out
  out="$(HOME="$TEST_HOME" XDG_CONFIG_HOME="$XDG_CONFIG" PATH="$GH_BIN:$PATH" python3 "$ATTENTION" list)"

  check "gh search prs (review-requested) invoked exactly once" \
    "$(grep -c '^search prs --review-requested=@me' "$GH_LOG")" "1"
  check "gh search prs (author) invoked exactly once" \
    "$(grep -c '^search prs --author=@me' "$GH_LOG")" "1"
  check "gh search issues (assignee) invoked exactly once" \
    "$(grep -c '^search issues --assignee=@me' "$GH_LOG")" "1"
  check "gh search issues (owner) invoked exactly once" \
    "$(grep -c '^search issues --owner=@me' "$GH_LOG")" "1"

  if grep -Eq '^pr list|^issue list' "$GH_LOG"; then
    bad "gh never invoked with repo-scoped 'pr list'/'issue list' (got: $(cat "$GH_LOG"))"
  else
    ok "gh never invoked with repo-scoped 'pr list'/'issue list'"
  fi
  if grep -Eq 'org:|user:' "$GH_LOG"; then
    bad "no org:/user: qualifier anywhere in gh invocations (got: $(cat "$GH_LOG"))"
  else
    ok "no org:/user: qualifier anywhere in gh invocations"
  fi

  local pr_line issue_line authored_line repo_issue_line
  pr_line="$(grep 'GHTEST-review-me' <<<"$out" || true)"
  issue_line="$(grep 'GHTEST-assigned-me' <<<"$out" || true)"
  authored_line="$(grep 'GHTEST-authored-me' <<<"$out" || true)"
  local draft_line
  draft_line="$(grep 'GHTEST-draft-review' <<<"$out" || true)"
  repo_issue_line="$(grep 'GHTEST-repo-issue' <<<"$out" || true)"

  case "$pr_line" in
    "REVIEW REQUESTED"*"myorg/kb"*) ok "review-requested PR gets REVIEW REQUESTED status with repo as context" ;;
    *) bad "review-requested PR gets REVIEW REQUESTED status with repo as context (got: $pr_line)" ;;
  esac
  case "$issue_line" in
    "ASSIGNED"*"myorg/kb"*) ok "assigned issue gets ASSIGNED status with repo as context" ;;
    *) bad "assigned issue gets ASSIGNED status with repo as context (got: $issue_line)" ;;
  esac
  case "$authored_line" in
    "NEEDS ATTENTION"*"Changes Requested, Review Commented, Merge Conflict, Checks Failing"*)
      ok "authored PR review, conflict, and failing-check signals appear as attention reasons" ;;
    *) bad "authored PR needing attention gets NEEDS ATTENTION status with reasons in details (got: $authored_line)" ;;
  esac
  case "$repo_issue_line" in
    "OPEN"*"myorg/kb"*) ok "owned-repo issue (not assigned to me) appears with OPEN status" ;;
    *) bad "owned-repo issue (not assigned to me) appears with OPEN status (got: $repo_issue_line)" ;;
  esac
  case "$draft_line" in
    "DRAFT:"*"myorg/kb"*) ok "draft PR shown with DRAFT status, distinguishable from ready-to-review" ;;
    *) bad "draft PR shown with DRAFT status, distinguishable from ready-to-review (got: $draft_line)" ;;
  esac

  check "issue matching both assignee and owner queries appears exactly once" \
    "$(grep -c 'GHTEST-assigned-me' <<<"$out")" "1"
  case "$issue_line" in
    "ASSIGNED"*) ok "de-duped issue keeps the more specific ASSIGNED status, not OPEN" ;;
    *) bad "de-duped issue keeps the more specific ASSIGNED status, not OPEN (got: $issue_line)" ;;
  esac
}
test_github_source

echo
echo "-- repo_path resolves via git-remote auto-detection, not the repo's own name --"

FAKE_CODE_DIR="$WORK/fakecode"
mkdir -p "$FAKE_CODE_DIR/bigproj"
env -u GIT_DIR -u GIT_WORK_TREE -u GIT_INDEX_FILE -u GIT_PREFIX git -C "$FAKE_CODE_DIR/bigproj" init -q
env -u GIT_DIR -u GIT_WORK_TREE -u GIT_INDEX_FILE -u GIT_PREFIX git -C "$FAKE_CODE_DIR/bigproj" remote add origin "https://github.com/myorg/big-project-name.git"

REPODIR_BIN="$WORK/bin-repodirs"
mkdir -p "$REPODIR_BIN"
cat > "$REPODIR_BIN/gh" <<'STUB'
#!/bin/sh
case "$*" in
  "search prs --review-requested=@me"*)
    cat <<'JSON'
[{"number": 9, "title": "REPODIRTEST-shorthand", "repository": {"name": "big-project-name", "nameWithOwner": "myorg/big-project-name"}, "url": "https://github.com/myorg/big-project-name/pull/9"}]
JSON
    ;;
  *) echo "[]" ;;
esac
exit 0
STUB
chmod +x "$REPODIR_BIN/gh"

write_config <<JSON
{"plugins": ["github"], "codeDir": "$FAKE_CODE_DIR", "github": {"actions": [{"key": "s", "label": "session", "background": true, "command": ["aoe-cmd", "-d", "{repo_path}"]}]}}
JSON

test_repo_path_git_remote_autodetect() {
  local out repo_line
  out="$(HOME="$TEST_HOME" XDG_CONFIG_HOME="$XDG_CONFIG" PATH="$REPODIR_BIN:$PATH" python3 "$ATTENTION" list)"
  repo_line="$(grep 'REPODIRTEST-shorthand' <<<"$out" || true)"
  # repo_path isn't a visible column anymore -- decode the row's action
  # blob and check the "session" action's command payload instead.
  local decoded
  decoded="$(python3 -c "
import sys, base64, json
line = sys.argv[1]
blob = line.split(chr(9), 2)[1]
actions = json.loads(base64.b64decode(blob))
session = next(a for a in actions if a.get('key') == 's')
print(session['payload']['command'][2])
" "$repo_line" 2>/dev/null || true)"
  check "repo_path matches the shorthand-named local clone via its git remote" "$decoded" "$FAKE_CODE_DIR/bigproj"
}
test_repo_path_git_remote_autodetect

# ---------------------------------------------------------------------------
echo
echo "== github plugin: tracked-author PRs needing attention, config[\"github\"][\"trackAuthors\"] =="

JULES_BIN="$WORK/bin-jules"
mkdir -p "$JULES_BIN"
cat > "$JULES_BIN/gh" <<'STUB'
#!/bin/sh
case "$*" in
  "search prs --author=jules-bot"*)
    cat <<'JSON'
[{"number": 11, "title": "JULESTEST-needs-attention", "repository": {"name": "kb", "nameWithOwner": "myorg/kb"}, "url": "https://github.com/myorg/kb/pull/11"}]
JSON
    ;;
  "pr view 11 -R myorg/kb"*)
    cat <<'JSON'
{"mergeable": "MERGEABLE", "reviewDecision": "CHANGES_REQUESTED", "statusCheckRollup": [], "latestReviews": [{"author": {"login": "reviewer"}, "state": "CHANGES_REQUESTED"}]}
JSON
    ;;
  "api user --jq .login")
    echo "ghtestuser"
    ;;
  *) echo "[]" ;;
esac
exit 0
STUB
chmod +x "$JULES_BIN/gh"

write_config <<'JSON'
{"plugins": ["github"], "codeDir": "/tmp/nonexistent-fakecode", "github": {"trackAuthors": ["jules-bot"]}}
JSON

test_tracked_author_pr_with_configured_list() {
  local out jules_line
  out="$(HOME="$TEST_HOME" XDG_CONFIG_HOME="$XDG_CONFIG" PATH="$JULES_BIN:$PATH" python3 "$ATTENTION" list)"
  jules_line="$(grep 'JULESTEST-needs-attention' <<<"$out" || true)"
  case "$jules_line" in
    "JULES-BOT:"*"myorg/kb"*) ok "a tracked author's flagged PR appears with their username in the status" ;;
    *) bad "a tracked author's flagged PR appears with their username in the status (got: $jules_line)" ;;
  esac
}
test_tracked_author_pr_with_configured_list

write_config <<'JSON'
{"plugins": ["github"], "codeDir": "/tmp/nonexistent-fakecode", "github": {}}
JSON

test_tracked_author_skipped_without_configured_list() {
  local out
  out="$(HOME="$TEST_HOME" XDG_CONFIG_HOME="$XDG_CONFIG" PATH="$JULES_BIN:$PATH" python3 "$ATTENTION" list)"
  if grep -q 'JULESTEST-needs-attention' <<<"$out"; then
    bad "no tracked-author query is run when github.trackAuthors is unconfigured (got: $out)"
  else
    ok "no tracked-author query is run when github.trackAuthors is unconfigured"
  fi
}
test_tracked_author_skipped_without_configured_list

# ---------------------------------------------------------------------------
echo
echo "== config defaults: missing plugin config, plugin failure isolation =="

write_config <<'JSON'
{"plugins": ["github", "reminders"], "github": {}}
JSON

test_missing_plugin_config_is_not_fatal() {
  # reminders is listed but has no "reminders" config key at all --
  # must not crash the whole run, just contribute nothing.
  local out rc
  out="$(HOME="$TEST_HOME" XDG_CONFIG_HOME="$XDG_CONFIG" PATH="$GH_BIN:$PATH" python3 "$ATTENTION" list 2>&1)" && rc=0 || rc=$?
  check "missing plugin-specific config section does not crash the run" "$rc" "0"
  if grep -q 'GHTEST-review-me' <<<"$out"; then
    ok "the other configured plugin (github) still contributes normally"
  else
    bad "the other configured plugin (github) still contributes normally (got: $out)"
  fi
}
test_missing_plugin_config_is_not_fatal

BROKEN_BIN="$WORK/bin-broken-plugin"
mkdir -p "$BROKEN_BIN/attention-sources"
BROKEN_PLUGIN="$BROKEN_BIN/broken.py"
cat > "$BROKEN_PLUGIN" <<'PY'
def fetch(config):
    raise RuntimeError("boom")

def act(key, payload):
    pass
PY

write_config <<JSON
{"plugins": ["github", "$BROKEN_PLUGIN"], "codeDir": "/tmp/nonexistent-fakecode", "github": {}}
JSON

test_one_broken_plugin_does_not_take_down_others() {
  local out rc
  out="$(HOME="$TEST_HOME" XDG_CONFIG_HOME="$XDG_CONFIG" PATH="$GH_BIN:$PATH" python3 "$ATTENTION" list 2>&1)" && rc=0 || rc=$?
  check "a plugin whose fetch() raises does not crash the whole run" "$rc" "0"
  if grep -q 'GHTEST-review-me' <<<"$out"; then
    ok "other plugins still contribute when one plugin's fetch() raises"
  else
    bad "other plugins still contribute when one plugin's fetch() raises (got: $out)"
  fi
}
test_one_broken_plugin_does_not_take_down_others

# ---------------------------------------------------------------------------
echo
echo "== custom (third-party, path-based) plugin resolution =="

CUSTOM_PLUGIN="$WORK/custom_plugin.py"
cat > "$CUSTOM_PLUGIN" <<'PY'
def fetch(config):
    return [{
        "status": "CUSTOM", "context": "test-source", "title": "Custom item", "details": "",
        "weight": 200, "id": "",
        "actions": [{"key": "Z", "label": "zap", "primary": True, "payload": {"msg": "zapped!"}}],
    }]

def act(key, payload):
    print(payload["msg"])
PY

write_config <<JSON
{"plugins": ["$CUSTOM_PLUGIN"]}
JSON

test_custom_plugin_resolution_and_dispatch() {
  local out line
  out="$(HOME="$TEST_HOME" XDG_CONFIG_HOME="$XDG_CONFIG" python3 "$ATTENTION" list)"
  if grep -q 'CUSTOM' <<<"$out"; then
    ok "a bare filesystem path in config[\"plugins\"] resolves and contributes items"
  else
    bad "a bare filesystem path in config[\"plugins\"] resolves and contributes items (got: $out)"
  fi
  line="$(grep 'Custom item' <<<"$out")"
  local act_out
  act_out="$(HOME="$TEST_HOME" XDG_CONFIG_HOME="$XDG_CONFIG" python3 "$ATTENTION" act "Z" "$line")"
  check "act() dispatches to the custom plugin's own act()" "$act_out" "zapped!"
}
test_custom_plugin_resolution_and_dispatch


# ---------------------------------------------------------------------------
echo
echo "== generic plugin: config-only provider (command + field templates, no .py file) =="

GENERIC_BIN="$WORK/bin-generic"
mkdir -p "$GENERIC_BIN"
GENERIC_OPEN_LOG="$WORK/generic-open.log"; : > "$GENERIC_OPEN_LOG"
GENERIC_SESSION_LOG="$WORK/generic-session.log"; : > "$GENERIC_SESSION_LOG"

cat > "$GENERIC_BIN/my-source-cli" <<'STUB'
#!/bin/sh
cat <<'JSON'
[
  {"num": 7, "name": "GENERICTEST-first", "proj": {"slug": "backend"}, "link": "https://example.com/7", "prio": "12"},
  {"num": 8, "name": "GENERICTEST-second", "proj": {"slug": "frontend"}, "link": "https://example.com/8"}
]
JSON
STUB
chmod +x "$GENERIC_BIN/my-source-cli"

cat > "$GENERIC_BIN/open" <<STUB
#!/bin/sh
echo "\$*" >> "$GENERIC_OPEN_LOG"
STUB
chmod +x "$GENERIC_BIN/open"

cat > "$GENERIC_BIN/my-session-cli" <<STUB
#!/bin/sh
echo "\$*" >> "$GENERIC_SESSION_LOG"
STUB
chmod +x "$GENERIC_BIN/my-session-cli"

cat > "$GENERIC_BIN/failing-cli" <<'STUB'
#!/bin/sh
exit 1
STUB
chmod +x "$GENERIC_BIN/failing-cli"

write_config <<JSON
{
  "plugins": ["generic"],
  "generic": {
    "my-source": {
      "command": ["my-source-cli"],
      "status": "NEEDS REVIEW",
      "context": "{proj.slug}",
      "title": "{name}",
      "details": "prio {prio}",
      "id": "{num}",
      "weight": "{prio}",
      "indicators": {"ci": "{prio}"},
      "actions": [
        {"key": "o", "label": "open", "primary": true, "command": ["open", "{link}"]},
        {"key": "s", "label": "session", "background": true, "command": ["my-session-cli", "-n", "{num}"]}
      ]
    },
    "broken-source": {
      "command": ["failing-cli"]
    }
  }
}
JSON

test_generic_provider() {
  local out first_line second_line
  out="$(HOME="$TEST_HOME" XDG_CONFIG_HOME="$XDG_CONFIG" PATH="$GENERIC_BIN:$PATH" python3 "$ATTENTION" list)"
  first_line="$(grep 'GENERICTEST-first' <<<"$out" || true)"
  second_line="$(grep 'GENERICTEST-second' <<<"$out" || true)"

  case "$first_line" in
    "NEEDS REVIEW"*"GENERICTEST-first"*"backend"*"prio 12"*)
      ok "status/context/title/details resolve {dotted.path} templates against the record" ;;
    *) bad "status/context/title/details resolve {dotted.path} templates against the record (got: $first_line)" ;;
  esac

  case "$second_line" in
    *"{prio}"*) bad "a missing {path} substitutes empty string rather than leaving the brace unresolved (got: $second_line)" ;;
    *) ok "a missing {path} substitutes empty string rather than leaving the brace unresolved" ;;
  esac

  local weights
  weights="$(HOME="$TEST_HOME" XDG_CONFIG_HOME="$XDG_CONFIG" PATH="$GENERIC_BIN:$PATH" python3 -c "
$LOAD_CORE
items = m.build_prioritized_items(m.load_config())
for it in items:
    print(it['title'], it['weight'])
")"
  check "weight template {prio} resolves and parses as int" \
    "$(grep 'GENERICTEST-first' <<<"$weights" | awk '{print $NF}')" "12"
  check "weight template with no matching field falls back to the default" \
    "$(grep 'GENERICTEST-second' <<<"$weights" | awk '{print $NF}')" "50"

  local indicators
  indicators="$(HOME="$TEST_HOME" XDG_CONFIG_HOME="$XDG_CONFIG" PATH="$GENERIC_BIN:$PATH" python3 -c "
$LOAD_CORE
import json
item = next(item for item in m.build_prioritized_items(m.load_config()) if item['title'] == 'GENERICTEST-first')
print(json.dumps(item['indicators']))
")"
  check "generic provider resolves indicator templates against the record" \
    "$indicators" '{"ci": "12"}'

  : > "$GENERIC_OPEN_LOG"
  HOME="$TEST_HOME" XDG_CONFIG_HOME="$XDG_CONFIG" PATH="$GENERIC_BIN:$PATH" python3 "$ATTENTION" act "o" "$first_line" >/dev/null 2>&1
  check "action command template substitutes the record's field before dispatch" \
    "$(cat "$GENERIC_OPEN_LOG")" "https://example.com/7"

  : > "$GENERIC_SESSION_LOG"
  HOME="$TEST_HOME" XDG_CONFIG_HOME="$XDG_CONFIG" PATH="$GENERIC_BIN:$PATH" python3 "$ATTENTION" act "s" "$first_line" >/dev/null 2>&1
  sleep 0.3
  check "an action marked background dispatches via dispatch_background, not run_cmd" \
    "$(cat "$GENERIC_SESSION_LOG")" "-n 7"

  if grep -q 'GENERICTEST' <<<"$out" && [ "$(grep -c 'GENERICTEST' <<<"$out")" = "2" ]; then
    ok "one provider's failing command does not prevent another provider in the same config from contributing"
  else
    bad "one provider's failing command does not prevent another provider in the same config from contributing (got: $out)"
  fi
}
test_generic_provider

echo
echo "== configurable actions in plugin configs (github, linear, etc.) =="

test_plugin_configurable_actions() {
  local gh_out
  gh_out="$(python3 -c "
$(load_plugin_py github)
import json
config = {
    'codeDir': '/tmp/repo',
    'github': {
        'actions': [
            {'key': 's', 'label': 'session', 'background': True, 'command': ['my-session', '-d', '{repo_path}', '-n', '{slug}']},
            {'key': 'l', 'label': 'lumen', 'command': ['my-lumen', '{url}']},
            {'key': 'Z', 'label': 'custom', 'command': ['my-custom', '{url}']}
        ]
    }
}
p._fetch_raw = lambda cfg: [{'number': 42, 'title': 'Fix bug', 'repository': {'nameWithOwner': 'myorg/repo'}, 'url': 'https://github.com/myorg/repo/pull/42', 'type': 'review_request'}]
items = p.fetch(config)
print(json.dumps([a['key'] for a in items[0]['actions']]))
print(json.dumps(items[0]['actions'][5]['payload']['command']))
")"
  check "github fetch attaches lowercase defaults and preserves configured key casing" \
    "$(sed -n 1p <<<"$gh_out")" '["o", "a", "m", "c", "g", "s", "l", "Z"]'
  check "github fetch resolves template in configured action command" \
    "$(sed -n 2p <<<"$gh_out")" '["my-session", "-d", "/tmp/repo/repo", "-n", "fix-bug"]'
}
test_plugin_configurable_actions

echo
echo "== action input: text/choice prompts, {input} substitution, cancel =="

INPUT_BIN="$WORK/bin-action-input"
mkdir -p "$INPUT_BIN"
INPUT_LOG="$WORK/action-input.log"; : > "$INPUT_LOG"

cat > "$INPUT_BIN/record-args" <<STUB
#!/bin/sh
printf '%s\n' "\$*" >> "$INPUT_LOG"
STUB
chmod +x "$INPUT_BIN/record-args"

test_util_input_resolve() {
  local out
  out="$(python3 -c "
$(load_plugin_py _util)
record = {'id': '42', 'url': 'https://x/42'}
actions = p.resolve_configured_actions([
    {'key': 's', 'label': 'session', 'command': ['run', '-m', '{input}'], 'input': {'prompt': 'Msg', 'default': 'Work on issue {id}'}},
    {'key': 'p', 'label': 'prio', 'command': ['run', '--prio', '{input}'], 'input': {'prompt': 'Priority', 'choices': ['p0', 'p1']}},
    {'key': 'S2', 'label': 'multi', 'command': ['run', '--agent', '{input.tool}', '--msg', '{input.command}'], 'inputs': [
        {'name': 'tool', 'prompt': 'Agent', 'choices': ['opencode', 'omp']},
        {'name': 'command', 'prompt': 'Command', 'default': 'Work on issue {id}'},
    ]},
], record)
print(actions[0]['payload']['command'][2])
print(actions[0]['payload']['inputs'][0]['default'])
print(','.join(actions[1]['payload']['inputs'][0]['choices']))
print(actions[2]['payload']['command'][2])
print(actions[2]['payload']['command'][4])
print(actions[2]['payload']['inputs'][0]['name'])
print(actions[2]['payload']['inputs'][1]['default'])
")"
  check "resolve leaves the reserved {input} placeholder for act-time substitution" \
    "$(sed -n 1p <<<"$out")" '{input}'
  check "resolve resolves record placeholders in the input default" \
    "$(sed -n 2p <<<"$out")" 'Work on issue 42'
  check "resolve carries choice mode into the payload input spec" \
    "$(sed -n 3p <<<"$out")" 'p0,p1'
  check "resolve leaves named {input.<name>} placeholders verbatim" \
    "$(sed -n 4p <<<"$out")" '{input.tool}'
  check "resolve leaves a second named placeholder verbatim" \
    "$(sed -n 5p <<<"$out")" '{input.command}'
  check "resolve names each multi-input spec" \
    "$(sed -n 6p <<<"$out")" 'tool'
  check "resolve resolves record placeholders in a named input's default" \
    "$(sed -n 7p <<<"$out")" 'Work on issue 42'
}
test_util_input_resolve

test_util_input_prompt_and_run() {
  : > "$INPUT_LOG"
  PATH="$INPUT_BIN:$PATH" python3 -c "
$(load_plugin_py _util)
import io, sys
sys.stdin = io.StringIO('fix the login\n')
p.run_configured_action({'command': ['record-args', '--msg', '{input}'], 'inputs': [{'name': '', 'prompt': 'Message'}]})
" >/dev/null 2>&1
  check "text input is substituted into the command" "$(cat "$INPUT_LOG")" '--msg fix the login'

  : > "$INPUT_LOG"
  PATH="$INPUT_BIN:$PATH" python3 -c "
$(load_plugin_py _util)
import io, sys
sys.stdin = io.StringIO('\n')
p.run_configured_action({'command': ['record-args', '--msg', '{input}'], 'inputs': [{'name': '', 'prompt': 'Message', 'default': 'Work on issue 42'}]})
" >/dev/null 2>&1
  check "an empty answer falls back to the declared default" "$(cat "$INPUT_LOG")" '--msg Work on issue 42'

  : > "$INPUT_LOG"
  PATH="$INPUT_BIN:$PATH" python3 -c "
$(load_plugin_py _util)
import io, sys
sys.stdin = io.StringIO('\n')
p.run_configured_action({'command': ['record-args', '--before', '{input}', '--after'], 'inputs': [{'name': '', 'prompt': 'Message', 'default': ''}]})
" >/dev/null 2>&1
  check "an explicit empty default dispatches an empty argument" "$(cat "$INPUT_LOG")" '--before  --after'

  : > "$INPUT_LOG"
  PATH="$INPUT_BIN:$PATH" python3 -c "
$(load_plugin_py _util)
import io, sys
sys.stdin = io.StringIO('2\n')
p.run_configured_action({'command': ['record-args', '--prio', '{input}'], 'inputs': [{'name': '', 'prompt': 'Priority', 'choices': ['p0', 'p1', 'p2']}]})
" >/dev/null 2>&1
  check "choice input substitutes the chosen value" "$(cat "$INPUT_LOG")" '--prio p1'

  : > "$INPUT_LOG"
  PATH="$INPUT_BIN:$PATH" python3 -c "
$(load_plugin_py _util)
import io, sys
sys.stdin = io.StringIO('\n')
p.run_configured_action({'command': ['record-args', '--msg', '{input}'], 'inputs': [{'name': '', 'prompt': 'Message'}]})
" >/dev/null 2>&1
  if [ -s "$INPUT_LOG" ]; then
    bad "an empty answer with no default cancels without running the command (got: $(cat "$INPUT_LOG"))"
  else
    ok "an empty answer with no default cancels without running the command"
  fi

  : > "$INPUT_LOG"
  PATH="$INPUT_BIN:$PATH" python3 -c "
$(load_plugin_py _util)
import io, sys
sys.stdin = io.StringIO('9\n')
p.run_configured_action({'command': ['record-args', '--prio', '{input}'], 'inputs': [{'name': '', 'prompt': 'Priority', 'choices': ['p0', 'p1']}]})
" >/dev/null 2>&1
  if [ -s "$INPUT_LOG" ]; then
    bad "an out-of-range choice cancels without running the command (got: $(cat "$INPUT_LOG"))"
  else
    ok "an out-of-range choice cancels without running the command"
  fi

  : > "$INPUT_LOG"
  PATH="$INPUT_BIN:$PATH" python3 -c "
$(load_plugin_py _util)
import io, sys
sys.stdin = io.StringIO('')
p.run_configured_action({'command': ['record-args', '--msg', '{input}'], 'inputs': [{'name': '', 'prompt': 'Message'}]})
" >/dev/null 2>&1
  if [ -s "$INPUT_LOG" ]; then
    bad "EOF cancels without running the command (got: $(cat "$INPUT_LOG"))"
  else
    ok "EOF cancels without running the command"
  fi

  : > "$INPUT_LOG"
  PATH="$INPUT_BIN:$PATH" python3 -c "
$(load_plugin_py _util)
import io, sys
sys.stdin = io.StringIO('2\nmy custom command\n')
p.run_configured_action({'command': ['record-args', '--agent', '{input.tool}', '--msg', '{input.command}'], 'inputs': [
    {'name': 'tool', 'prompt': 'Agent', 'choices': ['opencode', 'omp']},
    {'name': 'command', 'prompt': 'Command', 'default': 'Work on issue 42'},
]})
" >/dev/null 2>&1
  check "multi-input substitutes each named prompt into the command" "$(cat "$INPUT_LOG")" '--agent omp --msg my custom command'

  : > "$INPUT_LOG"
  PATH="$INPUT_BIN:$PATH" python3 -c "
$(load_plugin_py _util)
import io, sys
sys.stdin = io.StringIO('\n')
p.run_configured_action({'command': ['record-args', '--agent', '{input.tool}'], 'inputs': [
    {'name': 'tool', 'prompt': 'Agent', 'choices': ['opencode', 'omp'], 'default': 'opencode'},
]})
" >/dev/null 2>&1
  check "choice mode with a default accepts Enter to pick it" "$(cat "$INPUT_LOG")" '--agent opencode'
}
test_util_input_prompt_and_run

test_generic_provider_input() {
  write_config <<JSON
{
  "plugins": ["generic"],
  "generic": {
    "input-source": {
      "command": ["my-source-cli"],
      "status": "TODO",
      "context": "input",
      "title": "{name}",
      "id": "{num}",
      "weight": 50,
      "actions": [
        {"key": "t", "label": "text", "command": ["record-args", "--msg", "{input}"], "input": {"prompt": "Message", "default": "default-msg-{num}"}},
        {"key": "c", "label": "choice", "command": ["record-args", "--prio", "{input}"], "input": {"prompt": "Priority", "choices": ["low", "high"]}}
      ]
    }
  }
}
JSON
  local line
  line="$(HOME="$TEST_HOME" XDG_CONFIG_HOME="$XDG_CONFIG" PATH="$GENERIC_BIN:$INPUT_BIN:$PATH" python3 "$ATTENTION" list | grep 'GENERICTEST-first')"

  : > "$INPUT_LOG"
  printf 'typed message\n' | HOME="$TEST_HOME" XDG_CONFIG_HOME="$XDG_CONFIG" PATH="$GENERIC_BIN:$INPUT_BIN:$PATH" python3 "$ATTENTION" act "t" "$line" >/dev/null 2>&1
  check "generic provider text input reaches the command" "$(cat "$INPUT_LOG")" '--msg typed message'

  : > "$INPUT_LOG"
  printf '\n' | HOME="$TEST_HOME" XDG_CONFIG_HOME="$XDG_CONFIG" PATH="$GENERIC_BIN:$INPUT_BIN:$PATH" python3 "$ATTENTION" act "t" "$line" >/dev/null 2>&1
  check "generic provider empty input uses the record-resolved default" "$(cat "$INPUT_LOG")" '--msg default-msg-7'

  : > "$INPUT_LOG"
  printf '2\n' | HOME="$TEST_HOME" XDG_CONFIG_HOME="$XDG_CONFIG" PATH="$GENERIC_BIN:$INPUT_BIN:$PATH" python3 "$ATTENTION" act "c" "$line" >/dev/null 2>&1
  check "generic provider choice input reaches the command" "$(cat "$INPUT_LOG")" '--prio high'
}
test_generic_provider_input

# ---------------------------------------------------------------------------
echo
echo "== core: build_prioritized_items() deprioritizes recently-acted items =="

DEPRIO_PLUGIN="$WORK/deprio_plugin.py"
cat > "$DEPRIO_PLUGIN" <<'PY'
def fetch(config):
    return [{
        "status": "PENDING", "context": "test-source", "title": "Deprio item", "details": "",
        "weight": 70, "id": "dp1",
        "actions": [{"key": "z", "label": "zap", "payload": {}}],
    }]

def act(key, payload):
    pass
PY

write_config <<JSON
{"plugins": ["$DEPRIO_PLUGIN"]}
JSON

test_recently_acted_deprioritized() {
  local before after blob item_id
  before="$(HOME="$TEST_HOME" XDG_CONFIG_HOME="$XDG_CONFIG" python3 -c "
$LOAD_CORE
items = m.build_prioritized_items(m.load_config())
print(items[0]['weight'])
")"
  check "weight before any action" "$before" "70"

  after="$(HOME="$TEST_HOME" XDG_CONFIG_HOME="$XDG_CONFIG" python3 -c "
$LOAD_CORE
items = m.build_prioritized_items(m.load_config(), recently_acted={'dp1'})
print(items[0]['weight'])
")"
  check "weight after its id is marked recently-acted (70 - 20 penalty)" "$after" "50"

  item_id="$(HOME="$TEST_HOME" XDG_CONFIG_HOME="$XDG_CONFIG" python3 -c "
$LOAD_CORE
items = m.fetch_all(m.load_config())
print(items[0]['actions'][0]['_item_id'])
")"
  check "fetch_all() tags each action with its item's id (so the dashboard loop can track it)" "$item_id" "dp1"
}
test_recently_acted_deprioritized


test_work_in_progress_reliability() {
  local out
  out="$(python3 -c "
$LOAD_CORE
import contextlib, io, multiprocessing, os, tempfile

os.environ['XDG_STATE_HOME'] = tempfile.mkdtemp()

def mark(item_id):
    m._wip_items = None
    m.mark_wip_item(item_id)

context = multiprocessing.get_context('fork')
first = context.Process(target=mark, args=('first',))
second = context.Process(target=mark, args=('second',))
first.start()
second.start()
first.join()
second.join()
m._wip_items = None
print(sorted(m.get_wip_items()))

item = {
    'status': 'OPEN', 'context': 'generic', 'title': 'No ID', 'details': '', 'weight': 1,
    'id': 'boom',
    'actions': [{'key': 'alt-x', 'label': 'do', 'wip': True,
                 'payload': {'command': ['true'], 'background': False},
                 '_plugin': 'generic', '_item_id': 'boom'}],
    '_plugin': 'generic',
}
m._wip_items = None
row = m.render_rows(m.build_snapshot({'generic': [item]}))[0]
blocked = tempfile.NamedTemporaryFile(delete=False)
blocked.close()
os.environ['XDG_STATE_HOME'] = blocked.name
m._wip_items = None
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    m.act('alt-x', row)
print(any(line.startswith('Action failed: ') for line in buf.getvalue().splitlines()))
")"
  check "concurrent WIP updates preserve both item markers" "$(sed -n 1p <<<"$out")" "['first', 'second']"
  check "WIP persistence failure stays in the action error boundary" "$(sed -n 2p <<<"$out")" "True"
}
test_work_in_progress_reliability

test_wip_on_action() {
  local out
  out="$(python3 -c "
$LOAD_CORE
import contextlib, io, os, tempfile

def build_row(item_id, wip, command=None):
    # _plugin/_item_id are normally stamped onto each action by fetch_all;
    # this test builds items directly (bypassing fetch_all), so set them here.
    item = {
        'status': 'OPEN', 'context': 'generic', 'title': 'WIP ' + item_id,
        'details': '', 'weight': 1, 'id': item_id,
        'actions': [{'key': 'alt-x', 'label': 'do', 'wip': wip,
                     'payload': {'command': command or ['true'], 'background': False},
                     '_plugin': 'generic', '_item_id': item_id}],
        '_plugin': 'generic',
    }
    m._wip_items = None
    return m.render_rows(m.build_snapshot({'generic': [item]}))[0]

def act(row):
    with contextlib.redirect_stdout(io.StringIO()):
        m._wip_items = None
        m.act('alt-x', row)
    m._wip_items = None
    print(sorted(m.get_wip_items()))

os.environ['XDG_STATE_HOME'] = tempfile.mkdtemp()
act(build_row('wipitem', True))
act(build_row('wipitem', True))
m._wip_items = None
print('WORK IN PROGRESS' in build_row('wipitem', True))
act(build_row('wipitem', 'clear'))
act(build_row('wipitem', 'clear'))

os.environ['XDG_STATE_HOME'] = tempfile.mkdtemp()
act(build_row('plain', False))

os.environ['XDG_STATE_HOME'] = tempfile.mkdtemp()
act(build_row('failed-mark', True, command=['false']))
act(build_row('failed-clear-target', True))
act(build_row('failed-clear-target', 'clear', command=['false']))
")"
  check "an action with wip:true marks the acted item work in progress" \
    "$(sed -n 1p <<<"$out")" "['generic:wipitem']"
  check "wip auto-mark is idempotent -- running again never clears it" \
    "$(sed -n 2p <<<"$out")" "['generic:wipitem']"
  check "a marked item shows the WORK IN PROGRESS banner in the next snapshot" \
    "$(sed -n 3p <<<"$out")" "True"
  check "wip:clear removes the acted item's mark" \
    "$(sed -n 4p <<<"$out")" "[]"
  check "wip:clear is conditional and idempotent when already unmarked" \
    "$(sed -n 5p <<<"$out")" "[]"
  check "an action without wip leaves the item unmarked" \
    "$(sed -n 6p <<<"$out")" "[]"
  check "a wip:true action whose command fails leaves the item unmarked" \
    "$(sed -n 7p <<<"$out")" "[]"
  check "a wip:clear action whose command fails keeps the item marked" \
    "$(sed -n 9p <<<"$out")" "['generic:failed-clear-target']"
}
test_wip_on_action

test_build_snapshot_matches_build_prioritized_items_flattened_equivalent() {
  local out
  out="$(HOME="$TEST_HOME" XDG_CONFIG_HOME="$XDG_CONFIG" python3 -c "
$LOAD_CORE
import copy

items = [
    {'status': 'S1', 'context': 'c1', 'title': 'Item A', 'details': '', 'weight': 5, 'id': 'a1', 'actions': [], '_plugin': 'p1'},
    {'status': 'S2', 'context': 'c2', 'title': 'Item B', 'details': '', 'weight': 10, 'id': 'b1', 'actions': [], '_plugin': 'p2'},
    {'status': 'S3', 'context': 'c2', 'title': 'Item C', 'details': '', 'weight': 1, 'id': 'c1', 'actions': [], '_plugin': 'p2'},
]
recently_acted = {'a1'}

m.fetch_all = lambda config: copy.deepcopy(items)
via_build_prioritized_items = m.build_prioritized_items({}, recently_acted)

items_by_plugin = {}
for it in copy.deepcopy(items):
    items_by_plugin.setdefault(it['_plugin'], []).append(it)
via_build_snapshot = m.build_snapshot(items_by_plugin, recently_acted)

print([(it['title'], it['weight']) for it in via_build_prioritized_items])
print([(it['title'], it['weight']) for it in via_build_prioritized_items] == [(it['title'], it['weight']) for it in via_build_snapshot])
")"
  check "build_snapshot(items_by_plugin, recently_acted) matches build_prioritized_items()'s flattened-equivalent output" \
    "$(sed -n 2p <<<"$out")" "True"
  check "the shared pipeline still applies the recently-acted penalty and sorts by weight (sanity check on the actual values)" \
    "$(sed -n 1p <<<"$out")" "[('Item B', 10), ('Item C', 1), ('Item A', 0)]"
}
test_build_snapshot_matches_build_prioritized_items_flattened_equivalent

test_build_snapshot_pure_over_stored_items_across_repeated_calls() {
  local out
  out="$(python3 -c "
$LOAD_CORE
import copy

items_by_plugin = {
    'p1': [{'status': 'S1', 'context': 'c1', 'title': 'Fix ABC-123 today', 'details': '', 'weight': 5, 'id': '', 'absorb_note': '', 'created_at': '', 'actions': [{'key': 'o', 'label': 'open', 'primary': False, 'payload': {}}], '_plugin': 'p1'}],
    'p2': [{'status': 'S2', 'context': 'c2', 'title': 'Ticket', 'details': '', 'weight': 10, 'id': 'ABC-123', 'absorb_note': '', 'created_at': '', 'actions': [{'key': 's', 'label': 'session', 'primary': False, 'payload': {}}], '_plugin': 'p2'}],
}
baseline = copy.deepcopy(items_by_plugin)

def rendered(items):
    return [(it['title'], it['weight'], it['details'], [(a['key'], a['label']) for a in it['actions']]) for it in items]

first = rendered(m.build_snapshot(items_by_plugin))
second = rendered(m.build_snapshot(items_by_plugin))

print(first == second)
print(items_by_plugin == baseline)
")"
  check "build_snapshot() called twice on the same items_by_plugin mapping returns identical rendered/action results both times" \
    "$(sed -n 1p <<<"$out")" "True"
  check "build_snapshot() never mutates the stored provider item objects it was given (source objects unchanged after two calls)" \
    "$(sed -n 2p <<<"$out")" "True"
}
test_build_snapshot_pure_over_stored_items_across_repeated_calls

test_build_prioritized_items_has_no_unreachable_dead_code() {
  local body
  body="$(python3 -c "
src = open('$ATTENTION').read()
start = src.index('def build_prioritized_items(')
end = src.index('\ndef ', start + 1)
print(src[start:end])
")"
  case "$body" in
    *"    return items"*) bad "build_prioritized_items() has no unreachable 'return items' after its real return statement" ;;
    *) ok "build_prioritized_items() has no unreachable 'return items' after its real return statement" ;;
  esac
}
test_build_prioritized_items_has_no_unreachable_dead_code


# ---------------------------------------------------------------------------
echo
echo "== core: build_prioritized_items() ages items by real created_at, not by id =="

AGE_PLUGIN="$WORK/age_plugin.py"
cat > "$AGE_PLUGIN" <<'PY'
from datetime import datetime, timedelta, timezone

def fetch(config):
    now = datetime.now(timezone.utc)
    old_ts = (now - timedelta(days=10)).isoformat().replace("+00:00", "Z")
    new_ts = (now - timedelta(hours=1)).isoformat().replace("+00:00", "Z")
    return [
        {"status": "OPEN", "context": "org/new-repo", "title": "New repo PR3", "details": "",
         "weight": 70, "id": "3", "created_at": new_ts, "actions": []},
        {"status": "OPEN", "context": "org/old-repo", "title": "Old repo PR3", "details": "",
         "weight": 70, "id": "3", "created_at": old_ts, "actions": []},
        {"status": "OPEN", "context": "no-ts", "title": "No timestamp item", "details": "",
         "weight": 70, "id": "3", "actions": []},
    ]

def act(key, payload):
    pass
PY

write_config <<JSON
{"plugins": ["$AGE_PLUGIN"]}
JSON

test_age_boost_uses_created_at_not_id() {
  local weights
  weights="$(HOME="$TEST_HOME" XDG_CONFIG_HOME="$XDG_CONFIG" python3 -c "
$LOAD_CORE
items = m.build_prioritized_items(m.load_config())
for it in items:
    print(it['title'], it['weight'])
")"
  check "10-day-old item (id=3) gets the full +3 age boost" \
    "$(grep 'Old repo PR3' <<<"$weights" | awk '{print $NF}')" "73"
  check "1-hour-old item sharing the same id=3 gets no age boost (age is by timestamp, not id)" \
    "$(grep 'New repo PR3' <<<"$weights" | awk '{print $NF}')" "70"
  check "item with no created_at at all (id=3) gets no age boost" \
    "$(grep 'No timestamp item' <<<"$weights" | awk '{print $NF}')" "70"
  local first_title
  first_title="$(head -1 <<<"$weights" | cut -d' ' -f1-3)"
  check "the actually-older item sorts first despite sharing an id with a newer one" \
    "$first_title" "Old repo PR3"
}
test_age_boost_uses_created_at_not_id

# ---------------------------------------------------------------------------
echo
echo "== core: validate_and_normalize_item() enforces the plugin boundary shape =="

test_runtime_validation() {
  local err
  err="$(python3 -c "
$LOAD_CORE
item = {'context': 'ctx', 'title': 't', 'details': 'd', 'weight': 10}
try:
    m.validate_and_normalize_item(item, 'badplugin')
except ValueError as e:
    print('err:', e)
" 2>/dev/null || true)"
  check "detects missing required item key status" "$err" "err: plugin 'badplugin' returned a malformed item: missing required key 'status'"

  err="$(python3 -c "
$LOAD_CORE
item = {'status': 'S', 'context': 'ctx', 'title': 't', 'details': 'd', 'weight': '10'}
try:
    m.validate_and_normalize_item(item, 'badplugin')
except ValueError as e:
    print('err:', e)
" 2>/dev/null || true)"
  check "detects wrong type for weight (str instead of int)" "$err" "err: plugin 'badplugin' returned a malformed item: 'weight' must be of type int, got str"

  err="$(python3 -c "
$LOAD_CORE
item = {'status': 'S', 'context': 'ctx', 'title': 't', 'details': 'd', 'weight': True}
try:
    m.validate_and_normalize_item(item, 'badplugin')
except ValueError as e:
    print('err:', e)
" 2>/dev/null || true)"
  check "detects wrong type for weight (bool instead of int, bool is an int subclass)" "$err" "err: plugin 'badplugin' returned a malformed item: 'weight' must be of type int, got bool"

  err="$(python3 -c "
$LOAD_CORE
item = {'status': 'S', 'context': 'ctx', 'title': 't', 'details': 'd', 'weight': 10, 'actions': [{'label': 'open'}]}
try:
    m.validate_and_normalize_item(item, 'badplugin')
except ValueError as e:
    print('err:', e)
" 2>/dev/null || true)"
  check "detects action missing required key 'key'" "$err" "err: plugin 'badplugin' returned a malformed item: action missing required key 'key'"

  err="$(python3 -c "
$LOAD_CORE
item = {'status': 'S', 'context': 'ctx', 'title': 't', 'details': 'd', 'weight': 10, 'actions': [{'key': 'o', 'label': 123}]}
try:
    m.validate_and_normalize_item(item, 'badplugin')
except ValueError as e:
    print('err:', e)
" 2>/dev/null || true)"
  check "detects wrong type for action label" "$err" "err: plugin 'badplugin' returned a malformed item: action 'label' must be of type str, got int"

  err="$(python3 -c "
$LOAD_CORE
item = {'status': 'S', 'context': 'ctx', 'title': 't', 'details': 'd', 'weight': 10, 'actions': [{'key': 'ctrl-o', 'label': 'open'}]}
try:
    m.validate_and_normalize_item(item, 'badplugin')
except ValueError as e:
    print('err:', e)
" 2>/dev/null || true)"
  check "rejects action keys that the terminal UI cannot receive" "$err" "err: plugin 'badplugin' returned a malformed item: action 'key' must be one letter or digit"

  local accepted_keys
  accepted_keys="$(python3 -c "
$LOAD_CORE
import string
for key in string.ascii_lowercase + string.ascii_uppercase + string.digits:
    item = {'status': 'S', 'context': 'ctx', 'title': 't', 'details': 'd', 'weight': 10, 'actions': [{'key': key, 'label': 'open'}]}
    m.validate_and_normalize_item(item, 'goodplugin')
print('ok')
")"
  check "accepts lowercase, uppercase, and digit action keys" "$accepted_keys" "ok"

  err="$(python3 -c "
$LOAD_CORE
item = {'status': 'S', 'context': 'ctx', 'title': 't', 'details': 'd', 'weight': 10, 'actions': [{'key': 'o', 'label': 'open', 'wip': 'toggle'}]}
try:
    m.validate_and_normalize_item(item, 'badplugin')
except ValueError as e:
    print('err:', e)
" 2>/dev/null || true)"
  check "rejects unsupported action wip modes" "$err" "err: plugin 'badplugin' returned a malformed item: action 'wip' must be true, false, or \"clear\", got 'toggle'"

  err="$(python3 -c "
$LOAD_CORE
item = {'status': 'S', 'context': 'ctx', 'title': 't', 'details': 'd', 'weight': 10, 'created_at': 1700000000}
try:
    m.validate_and_normalize_item(item, 'badplugin')
except ValueError as e:
    print('err:', e)
" 2>/dev/null || true)"
  check "detects wrong type for created_at (int instead of str)" "$err" "err: plugin 'badplugin' returned a malformed item: 'created_at' must be of type str, got int"

  local defaults
  defaults="$(python3 -c "
$LOAD_CORE
import json
item = {'status': 'S', 'context': 'ctx', 'title': 't', 'details': 'd', 'weight': 10, 'actions': [{'key': 'o', 'label': 'o'}]}
m.validate_and_normalize_item(item, 'goodplugin')
print(json.dumps(item))
")"
  check "defaults optional item fields (id, absorb_note, created_at) to empty string" \
    "$(python3 -c "import json,sys; item=json.loads(sys.argv[1]); print(repr(item['id']), repr(item['absorb_note']), repr(item['created_at']))" "$defaults")" \
    "'' '' ''"
  check "defaults optional action fields (primary=False, wip=False, payload={})" \
    "$(python3 -c "import json,sys; item=json.loads(sys.argv[1]); print(item['actions'][0]['primary'], item['actions'][0]['wip'], item['actions'][0]['payload'])" "$defaults")" \
    "False False {}"

  local act_err
  act_err="$(python3 -c "
$LOAD_CORE
line = 'STATUS\t' + m.base64.b64encode(m.json.dumps([{'key': 'o', 'label': 'o', 'payload': 'not-a-dict', '_plugin': 'github'}]).encode()).decode()
m.act('o', line)
" 2>/dev/null || true)"
  check "act() rejects a non-dict payload before dispatching to the plugin" "$act_err" "Action failed: plugin 'github' act() received a malformed payload: expected a dictionary, got str"
}
test_runtime_validation

VALIDATION_ISOLATION_PLUGIN="$WORK/validation_isolation_plugin.py"
cat > "$VALIDATION_ISOLATION_PLUGIN" <<'PY'
def fetch(config):
    return [{"status": "OK", "context": "ctx", "title": "t", "details": "", "weight": "not-an-int"}]

def act(key, payload):
    pass
PY

write_config <<JSON
{"plugins": ["github", "$VALIDATION_ISOLATION_PLUGIN"], "codeDir": "/tmp/nonexistent-fakecode", "github": {}}
JSON

test_malformed_item_isolated_to_its_own_plugin() {
  local out err rc
  out="$(HOME="$TEST_HOME" XDG_CONFIG_HOME="$XDG_CONFIG" PATH="$GH_BIN:$PATH" python3 "$ATTENTION" list 2>"$WORK/validation-stderr.log")" && rc=0 || rc=$?
  check "a plugin returning a malformed item does not crash the whole run" "$rc" "0"
  if grep -q 'GHTEST-review-me' <<<"$out"; then
    ok "other plugins still contribute when one plugin's item fails validation"
  else
    bad "other plugins still contribute when one plugin's item fails validation (got: $out)"
  fi
  err="$(cat "$WORK/validation-stderr.log")"
  case "$err" in
    *"plugin '$VALIDATION_ISOLATION_PLUGIN' returned a malformed item: 'weight' must be of type int, got str"*)
      ok "the validation failure is printed to stderr, naming the plugin and the exact problem" ;;
    *) bad "the validation failure is printed to stderr, naming the plugin and the exact problem (got: $err)" ;;
  esac
}
test_malformed_item_isolated_to_its_own_plugin

# ---------------------------------------------------------------------------
echo
echo "== core: fetch_all() preserves config plugin order regardless of completion timing =="

ORDER_SLOW_PLUGIN="$WORK/order_slow_plugin.py"
cat > "$ORDER_SLOW_PLUGIN" <<'PY'
import time
def fetch(config):
    time.sleep(0.3)
    return [{"status": "S", "context": "c", "title": "OrderA-slowest", "details": "", "weight": 50}]
def act(key, payload):
    pass
PY

ORDER_FAST_PLUGIN="$WORK/order_fast_plugin.py"
cat > "$ORDER_FAST_PLUGIN" <<'PY'
def fetch(config):
    return [{"status": "S", "context": "c", "title": "OrderB-fastest", "details": "", "weight": 50}]
def act(key, payload):
    pass
PY

ORDER_MID_PLUGIN="$WORK/order_mid_plugin.py"
cat > "$ORDER_MID_PLUGIN" <<'PY'
import time
def fetch(config):
    time.sleep(0.1)
    return [{"status": "S", "context": "c", "title": "OrderC-mid", "details": "", "weight": 50}]
def act(key, payload):
    pass
PY

write_config <<JSON
{"plugins": ["$ORDER_SLOW_PLUGIN", "$ORDER_FAST_PLUGIN", "$ORDER_MID_PLUGIN"]}
JSON

test_fetch_all_deterministic_order() {
  local titles
  titles="$(HOME="$TEST_HOME" XDG_CONFIG_HOME="$XDG_CONFIG" python3 -c "
$LOAD_CORE
items = m.fetch_all(m.load_config())
for it in items:
    print(it['title'])
")"
  check "items come back in config (submission) order, not completion order, even though the slowest plugin is listed first" \
    "$(tr '\n' ',' <<<"$titles")" "OrderA-slowest,OrderB-fastest,OrderC-mid,"
}
test_fetch_all_deterministic_order

test_fetch_all_tags_item_with_plugin() {
  local plugins
  plugins="$(HOME="$TEST_HOME" XDG_CONFIG_HOME="$XDG_CONFIG" python3 -c "
$LOAD_CORE
items = m.fetch_all(m.load_config())
for it in items:
    print(it['_plugin'])
")"
  check "fetch_all() tags every item (not just its actions) with its originating plugin" \
    "$(tr '\n' ',' <<<"$plugins")" \
    "$ORDER_SLOW_PLUGIN,$ORDER_FAST_PLUGIN,$ORDER_MID_PLUGIN,"
}
test_fetch_all_tags_item_with_plugin

echo
echo "== github plugin: PR-detail fetches share one aggregate 32-worker budget across authors =="

test_pr_detail_aggregate_concurrency_capped_at_32_across_authors() {
  local out
  out="$(python3 -c "
$(load_plugin_py github)
import threading

overflow_barrier = threading.Barrier(33)
cap_barrier = threading.Barrier(32)
state = {'overflow_succeeded': False, 'cap_succeeded': False}
lock = threading.Lock()


def fake_gh_json(args):
    if args[:2] == ['search', 'prs'] and any(a.startswith('--author=') for a in args):
        return [{'number': n, 'repository': {'nameWithOwner': 'owner/repo'}} for n in range(16)]
    if args[:2] == ['search', 'prs']:
        return []
    if args[:2] == ['search', 'issues']:
        return []
    if args[:2] == ['api', '/notifications']:
        return []
    if args[:2] == ['pr', 'view']:
        try:
            overflow_barrier.wait(timeout=0.5)
            with lock:
                state['overflow_succeeded'] = True
        except threading.BrokenBarrierError:
            pass
        try:
            cap_barrier.wait(timeout=5)
            with lock:
                state['cap_succeeded'] = True
        except threading.BrokenBarrierError:
            pass
        return {'mergeable': 'MERGEABLE', 'reviewDecision': None, 'statusCheckRollup': [], 'comments': []}
    raise AssertionError(args)


p._gh_json = fake_gh_json
p._get_gh_login = lambda: 'me'

combined = p._fetch_raw({'github': {'trackAuthors': ['alice', 'bob']}})
print(state['overflow_succeeded'])
print(state['cap_succeeded'])
print(combined)
")"
  check "33 callers (one @me + two tracked authors' 16-candidate pages, 48 total) never simultaneously gather -- the aggregate budget across all authors never exceeds 32" \
    "$(sed -n 1p <<<"$out")" "False"
  check "32 callers do simultaneously gather -- the aggregate budget is a real, fully-utilized 32, not accidentally smaller" \
    "$(sed -n 2p <<<"$out")" "True"
  check "every one of the 48 candidates across all 3 authors was still processed without error" \
    "$(sed -n 3p <<<"$out")" "[]"
}
test_pr_detail_aggregate_concurrency_capped_at_32_across_authors

test_pr_detail_preserves_submission_order_and_isolates_per_candidate_failures() {
  local out
  out="$(python3 -c "
$(load_plugin_py github)
import concurrent.futures

prs = [{'number': n, 'repository': {'nameWithOwner': 'owner/repo'}} for n in range(5)]


def fake_gh_json(args):
    if args[:2] == ['search', 'prs']:
        return prs
    if args[:2] == ['pr', 'view']:
        number = int(args[2])
        if number == 2:
            return []
        return {
            'mergeable': 'MERGEABLE', 'reviewDecision': 'CHANGES_REQUESTED',
            'statusCheckRollup': [],
            'latestReviews': [{'author': {'login': 'reviewer'}, 'state': 'CHANGES_REQUESTED'}],
        }
    if args[:2] == ['api', '/notifications']:
        return []
    if args[:2] == ['api', 'graphql']:
        return {'data': {'repository': {'pullRequest': {'latestReviews': {'nodes': [
            {'author': {'__typename': 'User', 'login': 'reviewer'}}
        ]}}}}}
    raise AssertionError(args)


p._gh_json = fake_gh_json
p._get_gh_login = lambda: 'me'
with concurrent.futures.ThreadPoolExecutor(max_workers=32) as detail_pool:
    result = p._fetch_pr_attention('@me', detail_pool)
print([r['number'] for r in result])
")"
  check "results keep the search page's submission order, and a single candidate's failed detail fetch is isolated (excluded) rather than aborting the batch" \
    "$out" "[0, 1, 3, 4]"
}
test_pr_detail_preserves_submission_order_and_isolates_per_candidate_failures

test_pr_attention_uses_reviews_not_timeline_comments() {
  local out
  out="$(python3 -c "
$(load_plugin_py github)
import concurrent.futures

prs = [
    {'number': 1, 'repository': {'nameWithOwner': 'owner/repo'}},
    {'number': 2, 'repository': {'nameWithOwner': 'owner/repo'}},
    {'number': 3, 'repository': {'nameWithOwner': 'owner/repo'}},
]
requested_detail_fields = []



def fake_gh_json(args):
    if args[:2] == ['search', 'prs']:
        return prs
    if args[:2] == ['pr', 'view']:
        requested_detail_fields.append(args[-1])

        if args[2] == '1':
            return {
                'mergeable': 'MERGEABLE', 'reviewDecision': None,
                'statusCheckRollup': [],
                'comments': [{'author': {'login': 'reviewer'}}],
                'latestReviews': [],
            }
        if args[2] == '3':
            return {
                'mergeable': 'MERGEABLE', 'reviewDecision': None,
                'statusCheckRollup': [],
                'latestReviews': [
                    {'author': {'login': 'reviewer'}, 'state': 'APPROVED'},
                ],
            }
        return {
            'mergeable': 'MERGEABLE', 'reviewDecision': None,
            'statusCheckRollup': [],
            'comments': [],
            'latestReviews': [{'author': {'login': 'reviewer'}, 'state': 'COMMENTED'}],
        }
    if args[:2] == ['api', '/notifications']:
        return []
    if args[:2] == ['api', 'graphql']:
        return {'data': {'repository': {'pullRequest': {'latestReviews': {'nodes': [
            {'author': {'__typename': 'User', 'login': 'reviewer'}}
        ]}}}}}
    raise AssertionError(args)


p._gh_json = fake_gh_json
p._get_gh_login = lambda: 'author'
with concurrent.futures.ThreadPoolExecutor(max_workers=32) as detail_pool:
    result = p._fetch_pr_attention('@me', detail_pool)
print([(r['number'], r['attention_reasons']) for r in result])
print(all('latestReviews' in fields.split(',') for fields in requested_detail_fields))

")"
  check "ordinary comments do not flag authored PRs while non-author COMMENTED latest reviews do" \
    "$(sed -n 1p <<<"$out")" "[(2, ['Review Commented'])]"
  check "PR attention requests each reviewer's latest review" \
    "$(sed -n 2p <<<"$out")" "True"
}
test_pr_attention_uses_reviews_not_timeline_comments

test_pr_attention_ignores_bot_review_comments_unless_allowlisted() {
  local out
  out="$(python3 -c "
$(load_plugin_py github)
import concurrent.futures

prs = [
    {'number': 1, 'repository': {'nameWithOwner': 'owner/repo'}},
    {'number': 2, 'repository': {'nameWithOwner': 'owner/repo'}},
    {'number': 3, 'repository': {'nameWithOwner': 'owner/repo'}},
    {'number': 4, 'repository': {'nameWithOwner': 'owner/repo'}},
]

# Real \`gh pr view --json latestReviews\` never exposes __typename, and a
# Bot actor's bare login there never carries a \"[bot]\" suffix (verified
# against live PRs reviewed by Copilot/CodeRabbit/dependabot) -- only the
# separate \`api graphql\` lookup in _fetch_review_bot_flags() can tell a
# bot review from a human one.
reviewer_by_number = {'1': 'dependabot', '2': 'coderabbitai', '3': 'human-reviewer', '4': 'legacy-bot[bot]'}
typename_by_number = {'1': 'Bot', '2': 'Bot', '3': 'User'}


def fake_gh_json(args):
    if args[:2] == ['search', 'prs']:
        return prs
    if args[:2] == ['pr', 'view']:
        reviewer = reviewer_by_number[args[2]]
        return {
            'mergeable': 'MERGEABLE', 'reviewDecision': None,
            'statusCheckRollup': [],
            'latestReviews': [{'author': {'login': reviewer}, 'state': 'COMMENTED'}],
        }
    if args[:2] == ['api', '/notifications']:
        return []
    if args[:2] == ['api', 'graphql']:
        number = next(a.split('=', 1)[1] for a in args if a.startswith('number='))
        if number == '4':
            return []  # simulate the graphql lookup itself being unreachable
        return {'data': {'repository': {'pullRequest': {'latestReviews': {'nodes': [
            {'author': {'__typename': typename_by_number[number], 'login': reviewer_by_number[number]}}
        ]}}}}}
    raise AssertionError(args)


p._gh_json = fake_gh_json
p._get_gh_login = lambda: 'author'
with concurrent.futures.ThreadPoolExecutor(max_workers=32) as detail_pool:
    default_result = p._fetch_pr_attention('@me', detail_pool)
    allowlisted_result = p._fetch_pr_attention('@me', detail_pool, frozenset(['coderabbitai[bot]']))
print([(r['number'], r['attention_reasons']) for r in default_result])
print([(r['number'], r['attention_reasons']) for r in allowlisted_result])
")"
  check "unlisted bots (bare GraphQL Bot login, no [bot] suffix) never flag a PR by default -- only the human reviewer does" \
    "$(sed -n 1p <<<"$out")" "[(3, ['Review Commented'])]"
  check "allowlisting a bot login (with the [bot] suffix, as documented) admits its bare-login review too" \
    "$(sed -n 2p <<<"$out")" "[(2, ['Review Commented']), (3, ['Review Commented'])]"
}
test_pr_attention_ignores_bot_review_comments_unless_allowlisted

test_pr_attention_flags_review_requested_from_pending_requests() {
  local out
  out="$(python3 -c "
$(load_plugin_py github)
import concurrent.futures

prs = [
    {'number': 1, 'repository': {'nameWithOwner': 'owner/repo'}},
    {'number': 2, 'repository': {'nameWithOwner': 'owner/repo'}},
]


def fake_gh_json(args):
    if args[:2] == ['search', 'prs']:
        return prs
    if args[:2] == ['pr', 'view']:
        detail = {
            'mergeable': 'CONFLICTING', 'reviewDecision': None,
            'statusCheckRollup': [], 'latestReviews': [],
        }
        # gh flattens each reviewRequests entry to a top-level login, never
        # a nested reviewer object -- PR 1 has a pending reviewer, PR 2 none.
        detail['reviewRequests'] = (
            [{'__typename': 'User', 'login': 'some-reviewer'}] if args[2] == '1' else []
        )
        return detail
    raise AssertionError(args)


p._gh_json = fake_gh_json
p._get_gh_login = lambda: 'author'
with concurrent.futures.ThreadPoolExecutor(max_workers=32) as detail_pool:
    result = p._fetch_pr_attention('@me', detail_pool)
print([(r['number'], r['reviewRequested']) for r in sorted(result, key=lambda r: r['number'])])
")"
  check "a PR with a pending review request is flagged reviewRequested; one without is not" \
    "$out" "[(1, True), (2, False)]"
}
test_pr_attention_flags_review_requested_from_pending_requests

test_fetch_shows_review_requested_status_on_authored_and_tracked_prs() {
  local out
  out="$(python3 -c "
$(load_plugin_py github)
import json
p._fetch_raw = lambda cfg: [
    {'number': 1, 'title': 'Authored waiting on review', 'repository': {'nameWithOwner': 'myorg/repo'}, 'url': 'https://github.com/myorg/repo/pull/1', 'type': 'authored_attention', 'attention_reasons': ['Merge Conflict'], 'reviewRequested': True},
    {'number': 2, 'title': 'Authored no reviewer yet', 'repository': {'nameWithOwner': 'myorg/repo'}, 'url': 'https://github.com/myorg/repo/pull/2', 'type': 'authored_attention', 'attention_reasons': ['Merge Conflict'], 'reviewRequested': False},
    {'number': 3, 'title': 'Tracked waiting on review', 'repository': {'nameWithOwner': 'myorg/repo'}, 'url': 'https://github.com/myorg/repo/pull/3', 'type': 'tracked_attention', 'tracked_author': 'teammate', 'attention_reasons': ['Merge Conflict'], 'reviewRequested': True},
]
items = p.fetch({'codeDir': '/tmp/nonexistent'})
print(json.dumps({i['title']: i['status'] for i in items}))
")"
  check "authored PR with a pending review request shows REVIEW REQUESTED" \
    "$(python3 -c "import sys,json; print(json.load(sys.stdin)['Authored waiting on review'])" <<<"$out")" "REVIEW REQUESTED"
  check "authored PR with no pending review request keeps NEEDS ATTENTION" \
    "$(python3 -c "import sys,json; print(json.load(sys.stdin)['Authored no reviewer yet'])" <<<"$out")" "NEEDS ATTENTION"
  check "tracked PR with a pending review request keeps the author prefix" \
    "$(python3 -c "import sys,json; print(json.load(sys.stdin)['Tracked waiting on review'])" <<<"$out")" "TEAMMATE: REVIEW REQUESTED"
}
test_fetch_shows_review_requested_status_on_authored_and_tracked_prs

test_tracked_attention_wins_over_duplicate_review_request() {
  local out
  out="$(python3 -c "
$(load_plugin_py github)

pr = {'number': 7, 'title': 'Tracked PR', 'repository': {'nameWithOwner': 'owner/repo'}, 'url': 'https://github.com/owner/repo/pull/7'}


def fake_gh_json(args):
    if args[:2] == ['search', 'prs']:
        return [dict(pr)]
    if args[:2] == ['pr', 'view']:
        return {'closingIssuesReferences': []}
    return []


p._gh_json = fake_gh_json
p._fetch_my_repo_issues = lambda: []
p._fetch_pr_attention = lambda author, *_: [dict(pr)] if author == 'teammate' else []
items = p._fetch_raw({'github': {'trackAuthors': ['teammate']}})
print([(item['type'], item.get('tracked_author')) for item in items])
")"
  check "tracked attention keeps its author context when it duplicates a review request" \
    "$out" "[('tracked_attention', 'teammate')]"
}
test_tracked_attention_wins_over_duplicate_review_request
test_github_session_prompt_state_aware() {
  local out
  out="$(python3 -c "
$(load_plugin_py github)
f = p._session_prompt
print(f('authored_attention', ['Checks Failing']))
print(f('authored_attention', ['Changes Requested']))
print(f('authored_attention', ['Changes Requested', 'Checks Failing']))
print(f('authored_attention', ['Merge Conflict']))
print(f('authored_attention', ['Review Commented']))
print(f('review_request', []))
print(f('tracked_attention', ['Checks Failing']))
print(f('tracked_attention', ['Changes Requested']))
print(f('assigned_issue', []))
print(f('repo_issue', []))
")"
  check "my PR with failing CI tells me to fix CI" \
    "$(sed -n 1p <<<"$out")" "Fix the failing CI checks."
  check "my PR with changes requested tells me to address them" \
    "$(sed -n 2p <<<"$out")" "Address the requested changes."
  check "changes requested outranks failing CI on my own PR" \
    "$(sed -n 3p <<<"$out")" "Address the requested changes."
  check "my PR with a merge conflict tells me to resolve it" \
    "$(sed -n 4p <<<"$out")" "Resolve the merge conflict."
  check "my PR with only review comments tells me to respond" \
    "$(sed -n 5p <<<"$out")" "Respond to the review comments."
  check "a PR someone asked me to review tells me to review it" \
    "$(sed -n 6p <<<"$out")" "Review it."
  check "a teammate's failing PR tells me to follow up, not fix their CI" \
    "$(sed -n 7p <<<"$out")" "Follow up with the author."
  check "a teammate's changes-requested PR tells me to follow up, not address their changes" \
    "$(sed -n 8p <<<"$out")" "Follow up with the author."
  check "an assigned issue tells me to work on it" \
    "$(sed -n 9p <<<"$out")" "Work on it."
  check "an owned-repo issue tells me to work on it" \
    "$(sed -n 10p <<<"$out")" "Work on it."
}
test_github_session_prompt_state_aware

test_pull_request_indicators_distinguish_draft_ci_review_and_stack_state() {
  local out
  out="$(python3 -c "
$(load_plugin_py github)
import json
detail = {
    'statusCheckRollup': [{'conclusion': 'SUCCESS'}],
    'latestReviews': [{'state': 'APPROVED'}],
    'reviewDecision': 'APPROVED',
    'reviewRequests': [],
    'baseRefName': 'feature/base',
}
print(json.dumps(p._pr_indicators(detail, True, 'main'), sort_keys=True))
detail['statusCheckRollup'] = [{'conclusion': 'FAILURE'}]
detail['latestReviews'] = [{'state': 'CHANGES_REQUESTED'}]
detail['reviewDecision'] = 'CHANGES_REQUESTED'
detail['baseRefName'] = 'main'
print(json.dumps(p._pr_indicators(detail, False, 'main'), sort_keys=True))
")"
  check "draft stacked PR shows ready false, passed CI, approved review, and stacked true" \
    "$(sed -n 1p <<<"$out")" '{"ci": "\u2713", "ready": "\u00d7", "review": "\u2713", "stacked": "\u2713"}'
  check "base-branch PR shows failing CI, changes requested, and stacked false" \
    "$(sed -n 2p <<<"$out")" '{"ci": "\u00d7", "ready": "\u2713", "review": "\u00d7", "stacked": "\u00d7"}'
}
test_pull_request_indicators_distinguish_draft_ci_review_and_stack_state

# ---------------------------------------------------------------------------
echo
echo "== linear plugin: state.type filter, no pagination truncation, project as context =="

# fetch() hits the network directly (urllib), which a bash-level PATH stub
# can't intercept, so this loads the module in-process and monkeypatches
# urlopen with a fake response, capturing the outgoing GraphQL request body.
test_fetch_linear_functional() {
  local out
  out="$(python3 -c "
$(load_plugin_py linear)
import json

captured = {}

class FakeResp:
    def __init__(self, data):
        self._data = data
    def read(self):
        return self._data
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False

def fake_urlopen(req, timeout=10):
    captured['query'] = json.loads(req.data.decode())['query']
    payload = {
        'data': {'viewer': {'assignedIssues': {'nodes': [
            {'id': 'u1', 'identifier': 'ABC-1', 'title': 'Fix it',
             'url': 'https://linear.app/x/issue/ABC-1',
             'state': {'name': 'In Progress'}, 'project': {'name': 'Backend'}}
        ]}}}
    }
    return FakeResp(json.dumps(payload).encode())

p.urllib.request.urlopen = fake_urlopen

result = p.fetch({'linear': {'apiToken': 'fake-token'}})
print(json.dumps({'query': captured['query'], 'result': result}))
")"

  case "$out" in
    *'type: { nin: [\"completed\", \"canceled\", \"duplicate\"] }'*)
      ok "assignedIssues filters by state.type (not the fragile literal name \"Done\")" ;;
    *) bad "assignedIssues filters by state.type (got: $out)" ;;
  esac
  case "$out" in
    *'cycle: { isActive: { eq: true } }'*)
      ok "assignedIssues is scoped to the current cycle" ;;
    *) bad "assignedIssues is scoped to the current cycle (got: $out)" ;;
  esac
  case "$out" in
    *'first: 250'*) ok "assignedIssues raises the page size past the 50-item default" ;;
    *) bad "assignedIssues raises the page size past the 50-item default (got: $out)" ;;
  esac
  case "$out" in
    *'"context": "Backend"'*'"id": "ABC-1"'*)
      ok "fetch() returns the issue with id=identifier and project as CONTEXT" ;;
    *) bad "fetch() returns the issue with id=identifier and project as CONTEXT (got: $out)" ;;
  esac
  case "$out" in
    *'"status_priority": 10'*)
      ok "Linear issues outrank a cross-linked host's status on merge" ;;
    *) bad "Linear issues outrank a cross-linked host's status on merge (got: $out)" ;;
  esac
  case "$out" in
    *'"indicators": {"state": "IN PROGRESS"}'*'"kind": "issue"'*)
      ok "Linear issues expose an issue type and state indicator" ;;
    *) bad "Linear issues expose an issue type and state indicator (got: $out)" ;;
  esac
}
test_fetch_linear_functional

echo
echo "-- linear token: config apiToken, env var fallback, no keychain coupling --"

test_linear_token_from_config() {
  local token
  token="$(python3 -c "
$(load_plugin_py linear)
print(p._get_token({'linear': {'apiToken': 'config-token'}}))
")"
  check "_get_token() returns linear.apiToken from config" "$token" "config-token"
}
test_linear_token_from_config

test_linear_token_env_fallback() {
  local token
  token="$(LINEAR_API_TOKEN="env-token" python3 -c "
$(load_plugin_py linear)
print(p._get_token({'linear': {}}))
")"
  check "falls back to LINEAR_API_TOKEN env var when config has no apiToken" "$token" "env-token"
}
test_linear_token_env_fallback

test_linear_token_config_takes_precedence_over_env() {
  local token
  token="$(LINEAR_API_TOKEN="env-token" python3 -c "
$(load_plugin_py linear)
print(p._get_token({'linear': {'apiToken': 'config-token'}}))
")"
  check "config apiToken takes precedence over the env var" "$token" "config-token"
}
test_linear_token_config_takes_precedence_over_env

# ---------------------------------------------------------------------------
echo
echo "== _util.slugify() title-to-slug =="

check "slugify: lowercases and hyphenates" \
  "$(python3 -c "
$(load_plugin_py github)
print(p.slugify('Fix the login bug!'))")" \
  "fix-the-login-bug"
check "slugify: collapses repeated punctuation/whitespace" \
  "$(python3 -c "
$(load_plugin_py github)
print(p.slugify('  Weird---Title!!  '))")" \
  "weird-title"
check "slugify: trims to max_len at a hyphen boundary, never mid-word" \
  "$(python3 -c "
$(load_plugin_py github)
print(p.slugify('one two three four five six seven', max_len=15))")" \
  "one-two-three"
check "slugify: empty/non-alnum input falls back to a generic slug" \
  "$(python3 -c "
$(load_plugin_py github)
print(p.slugify('!!!'))")" \
  "session"

# ---------------------------------------------------------------------------
echo
echo "== core: merge_cross_links() cross-source, generic merging =="

test_id_shaped_cross_link_merge() {
  local out
  out="$(python3 -c "
$LOAD_CORE
gh_item = {
    'status': 'REVIEW REQUESTED', 'context': 'myorg/kb', 'title': 'Fix the thing ABC-1', 'details': '',
    'weight': 90, 'id': '1',
    'actions': [
        {'key': 'o', 'label': 'open', 'primary': True, 'payload': {}, '_plugin': 'github'},
        {'key': 'c', 'label': 'comment', 'payload': {}, '_plugin': 'github'},
    ],
}
lin_item = {
    'status': 'IN PROGRESS', 'context': 'Backend', 'title': 'Fix it', 'details': '',
    'weight': 80, 'id': 'ABC-1', 'absorb_note': 'Linear ABC-1: IN PROGRESS',
    'actions': [
        {'key': 'o', 'label': 'open', 'primary': True, 'payload': {}, '_plugin': 'linear'},
        {'key': 'c', 'label': 'comment', 'payload': {}, '_plugin': 'linear'},
        {'key': 't', 'label': 'transition', 'payload': {}, '_plugin': 'linear'},
    ],
}
merged = m.merge_cross_links([gh_item, lin_item])
import json
print(json.dumps(merged))
")"

  check "an id-shaped title match (GH title mentions ABC-1) merges down to one item" \
    "$(python3 -c "import json,sys; print(len(json.loads(sys.argv[1])))" "$out")" "1"

  case "$out" in
    *'"details": "Linear ABC-1: IN PROGRESS"'*) ok "absorbed item's absorb_note lands in the host's details" ;;
    *) bad "absorbed item's absorb_note lands in the host's details (got: $out)" ;;
  esac
  case "$out" in
    *'"weight": 95'*) ok "host weight becomes max(host, guest) + 5" ;;
    *) bad "host weight becomes max(host, guest) + 5 (got: $out)" ;;
  esac
  case "$out" in
    *'"key": "1"'*'"label": "open (linked)"'*) ok "colliding open key remaps to the lowest free digit, labeled (linked)" ;;
    *) bad "colliding open key remaps to a digit (got: $out)" ;;
  esac
  case "$out" in
    *'"key": "2"'*'"label": "comment (linked)"'*) ok "colliding comment key remaps to the next free digit" ;;
    *) bad "colliding comment key remaps to a digit (got: $out)" ;;
  esac
  case "$out" in
    *'"key": "t"'*'"label": "transition (linked)"'*)
      ok "non-colliding t key is kept as-is, still labeled (linked)" ;;
    *) bad "non-colliding t key is kept as-is (got: $out)" ;;
  esac

  local primary_count
  primary_count="$(python3 -c "
import json,sys
merged = json.loads(sys.argv[1])
print(sum(1 for a in merged[0]['actions'] if a.get('primary')))
" "$out")"
  check "only the host's own action keeps primary=true after merging" "$primary_count" "1"
}
test_id_shaped_cross_link_merge

test_status_priority_swaps_visible_status_on_merge() {
  local out
  out="$(python3 -c "
$LOAD_CORE
gh_item = {
    'status': 'REVIEW REQUESTED', 'context': 'myorg/kb', 'title': 'Fix the thing ABC-1', 'details': 'Review Commented',
    'weight': 90, 'id': '1', 'actions': [],
}
lin_item = {
    'status': 'IN PROGRESS', 'context': 'Backend', 'title': 'Fix it', 'details': '',
    'weight': 80, 'id': 'ABC-1', 'absorb_note': 'Linear ABC-1: IN PROGRESS',
    'status_priority': 10, 'actions': [],
}
merged = m.merge_cross_links([gh_item, lin_item])
import json
print(json.dumps(merged))
")"

  case "$out" in
    *'"status": "IN PROGRESS"'*) ok "guest's higher status_priority makes its status the merged row's visible status" ;;
    *) bad "guest's higher status_priority makes its status the merged row's visible status (got: $out)" ;;
  esac
  case "$out" in
    *'"details": "REVIEW REQUESTED: Review Commented"'*)
      ok "the demoted host status/details survive as a note instead of the guest's absorb_note" ;;
    *) bad "the demoted host status/details survive as a note (got: $out)" ;;
  esac
  case "$out" in
    *'"status_priority": 10'*) ok "the merged row's status_priority carries the winning side's value" ;;
    *) bad "the merged row's status_priority carries the winning side's value (got: $out)" ;;
  esac
}
test_status_priority_swaps_visible_status_on_merge

test_declared_association_key_merge() {
  local out
  out="$(python3 -c "
$LOAD_CORE
pr = {'status': 'REVIEW REQUESTED', 'context': 'owner/repo', 'title': 'Fix it', 'details': '', 'weight': 90, 'id': '7', 'identity_key': 'github:owner/repo#7', 'association_keys': ['github:owner/repo#42'], 'actions': []}
issue = {'status': 'OPEN', 'context': 'owner/repo', 'title': 'Tracked issue', 'details': '', 'weight': 70, 'id': '42', 'identity_key': 'github:owner/repo#42', 'actions': []}
same_number_elsewhere = {'status': 'OPEN', 'context': 'other/repo', 'title': 'Other issue', 'details': '', 'weight': 70, 'id': '42', 'identity_key': 'github:other/repo#42', 'actions': []}
import json
merged = m.merge_cross_links([pr, issue, same_number_elsewhere])
print(json.dumps(sorted(item['identity_key'] for item in merged)))
")"
  check "declared repository-qualified association preserves the equal-number issue in another repository" \
    "$out" '["github:other/repo#42", "github:owner/repo#7"]'
}
test_declared_association_key_merge

test_title_substring_cross_link_merge() {
  local out
  out="$(python3 -c "
$LOAD_CORE
cal_item = {
    'status': 'ALL DAY', 'context': 'Work', 'title': 'Team Dinner', 'details': '',
    'weight': 50, 'id': 'e1',
    'actions': [{'key': 'y', 'label': 'yank', 'payload': {}, '_plugin': 'calendar'}],
}
rem_item = {
    'status': 'PENDING', 'context': 'Personal', 'title': 'Book babysitter for Team Dinner', 'details': '',
    'weight': 15, 'id': 'r1', 'absorb_note': 'Reminder: Book babysitter for Team Dinner',
    'actions': [{'key': 'x', 'label': 'complete', 'payload': {'id': 'r1'}, '_plugin': 'reminders'}],
}
merged = m.merge_cross_links([cal_item, rem_item])
import json
print(json.dumps(merged))
")"
  check "a title-substring match (reminder title contains event title) merges to one item" \
    "$(python3 -c "import json,sys; print(len(json.loads(sys.argv[1])))" "$out")" "1"
  case "$out" in
    *'"key": "x"'*'"label": "complete (linked)"'*)
      ok "reminder's complete action carries over unchanged (no collision with CAL's Y)" ;;
    *) bad "reminder's complete action carries over unchanged (got: $out)" ;;
  esac
}
test_title_substring_cross_link_merge

test_short_or_untitled_titles_never_match() {
  local out
  out="$(python3 -c "
$LOAD_CORE
a = {'status': 'X', 'context': '', 'title': 'ok', 'details': '', 'weight': 1, 'id': '', 'actions': []}
b = {'status': 'X', 'context': '', 'title': 'this contains ok somewhere', 'details': '', 'weight': 1, 'id': '', 'actions': []}
c = {'status': 'X', 'context': '', 'title': 'Untitled', 'details': '', 'weight': 1, 'id': '', 'actions': []}
d = {'status': 'X', 'context': '', 'title': 'Untitled event happened', 'details': '', 'weight': 1, 'id': '', 'actions': []}
merged = m.merge_cross_links([a, b, c, d])
print(len(merged))
")"
  check "titles under 4 chars, and the literal 'untitled', never act as a merge host" "$out" "4"
}
test_short_or_untitled_titles_never_match

# ---------------------------------------------------------------------------
echo
echo "== core: expect_keys_for() / hint_for_actions() =="

test_expect_keys_for_is_union_of_present_items() {
  local out
  out="$(python3 -c "
$LOAD_CORE
items = [
    {'actions': [{'key': 'o', 'label': 'x'}, {'key': 's', 'label': 'x'}]},
    {'actions': [{'key': 'o', 'label': 'x'}, {'key': 'x', 'label': 'x'}]},
]
print(','.join(m.expect_keys_for(items)))
")"
  check "expect_keys_for() is the de-duped union of every action key actually present" "$out" "o,s,x"
}
test_expect_keys_for_is_union_of_present_items

test_expect_keys_for_empty_items_is_empty() {
  local out
  out="$(python3 -c "
$LOAD_CORE
print(','.join(m.expect_keys_for([])))
")"
  check "expect_keys_for([]) is empty (no static hypothetical table anymore)" "$out" ""
}
test_expect_keys_for_empty_items_is_empty

check "hint_for_actions() renders 'key label' pairs joined by two spaces" \
  "$(python3 -c "
$LOAD_CORE
print(m.hint_for_actions([{'key': 'o', 'label': 'open'}, {'key': 'o', 'label': 'open linear'}]))")" \
  "o open  o open linear"

# ---------------------------------------------------------------------------
echo
echo "== core: mobile width support (dynamic column scaling and adaptive headers) =="

test_mobile_width_support() {
  local out
  out="$(python3 -c "
$LOAD_CORE
$LOAD_DASHBOARD
import shutil

class DummySize:
    def __init__(self, columns):
        self.columns = columns

items = [
    {'status': 'VERY_LONG_STATUS_HERE', 'context': 'very/long/context/here', 'title': 'Very long title that goes on and on and on', 'details': 'More details than will fit'}
]

# Case 1: Terminal width 50
shutil.get_terminal_size = lambda: DummySize(50)
cols_50 = m._row_columns(items)
print('cols_50:', cols_50)

# Case 2: Terminal width 40
shutil.get_terminal_size = lambda: DummySize(40)
cols_40 = m._row_columns(items)
print('cols_40:', cols_40)

# Case 3: Terminal width 100
shutil.get_terminal_size = lambda: DummySize(100)
cols_100 = m._row_columns(items)
print('cols_100:', cols_100)

# Case 4: Idle header at width 70
shutil.get_terminal_size = lambda: DummySize(70)
print('idle_70:', d._pending_header([]))

# Case 5: Idle header at width 60
shutil.get_terminal_size = lambda: DummySize(60)
print('idle_60:', d._pending_header([]))

# Case 6: Idle header at width 40
shutil.get_terminal_size = lambda: DummySize(40)
print('idle_40:', d._pending_header([]))

# Case 7: Pending list truncation at width 40
shutil.get_terminal_size = lambda: DummySize(40)
print('pending_40:', d._pending_header(['calendar', 'reminders', 'github', 'linear']))

# Case 8: Pending list truncation at width 30
shutil.get_terminal_size = lambda: DummySize(30)
print('pending_30:', d._pending_header(['calendar', 'reminders', 'github', 'linear']))

# Case 9: Rendered rows remain visible within narrow terminals
shutil.get_terminal_size = lambda: DummySize(40)
print('visible_40_fits:', len(m.render_rows(items)[0].split('\t', 1)[0]) <= 40)
shutil.get_terminal_size = lambda: DummySize(30)
print('visible_30_fits:', len(m.render_rows(items)[0].split('\t', 1)[0]) <= 30)

# Case 10: Idle header remains visible within the narrowest terminal
print('idle_30_fits:', len(d._pending_header([])) <= 30)
")"

  check "adaptive column widths at w=50" \
    "$(grep 'cols_50:' <<<"$out")" "cols_50: (8, 10, 22)"
  check "adaptive column widths at w=40" \
    "$(grep 'cols_40:' <<<"$out")" "cols_40: (8, 10, 15)"
  check "standard column widths at w=100" \
    "$(grep 'cols_100:' <<<"$out")" "cols_100: (20, 22, 42)"
  check "adaptive idle header at w=70" \
    "$(grep 'idle_70:' <<<"$out")" "idle_70: Hotkeys act immediately · Enter = primary · Esc = quit"
  check "adaptive idle header at w=60" \
    "$(grep 'idle_60:' <<<"$out")" "idle_60: Keys act immediately · Enter=primary · Esc=quit"
  check "adaptive idle header at w=40" \
    "$(grep 'idle_40:' <<<"$out")" "idle_40: Keys act immediately · Enter/Esc"
  check "adaptive pending list at w=40" \
    "$(grep 'pending_40:' <<<"$out")" "pending_40: Loading: calendar, reminders, github...…"
  check "adaptive pending list at w=30" \
    "$(grep 'pending_30:' <<<"$out")" "pending_30: Loading…"
  check "visible row fits at w=40" \
    "$(grep 'visible_40_fits:' <<<"$out")" "visible_40_fits: True"
  check "visible row fits at w=30" \
    "$(grep 'visible_30_fits:' <<<"$out")" "visible_30_fits: True"
  check "idle header fits at w=30" \
    "$(grep 'idle_30_fits:' <<<"$out")" "idle_30_fits: True"
}
test_mobile_width_support

# ---------------------------------------------------------------------------
echo
echo "== core: render_dashboard_rows() / render_rows() compatibility =="


test_render_rows_byte_for_byte_unchanged() {
  local out
  out="$(python3 -c "
$LOAD_CORE
items = [{'status': 'REVIEW REQUESTED', 'context': 'myorg/kb', 'title': 'Fix the login bug', 'details': '', 'weight': 90, '_plugin': 'github', 'actions': [{'key': 'o', 'label': 'open', 'primary': True, 'payload': {}}]}]
rows = m.render_rows(items)
print(len(rows))
fields = rows[0].split(chr(9))
print(len(fields))
print(fields[0].strip())
import base64, json
print(json.loads(base64.b64decode(fields[1]).decode())[0]['key'])
print(fields[2])
")"
  check "render_rows() still emits exactly one row per item" "$(sed -n 1p <<<"$out")" "1"
  check "render_rows() output is still exactly 3 tab-delimited fields (unchanged by the dashboard renderer's addition)" \
    "$(sed -n 2p <<<"$out")" "3"
  check "render_rows() field 1 (visible columns) is unchanged" "$(sed -n 3p <<<"$out")" "REVIEW REQUESTED  Fix the login bug  myorg/kb"
  check "render_rows() field 2 (actions blob) is unchanged" "$(sed -n 4p <<<"$out")" "o"
  check "render_rows() field 3 (hint) is unchanged" "$(sed -n 5p <<<"$out")" "o open"
}
test_render_rows_byte_for_byte_unchanged

test_render_dashboard_rows_omits_status_and_retains_action_fields() {
  local out
  out="$(python3 -c "
$LOAD_CORE
items = [{
    'status': 'REVIEW REQUESTED', 'context': 'myorg/kb', 'title': 'Fix the login bug',
    'details': '', 'weight': 90, 'id': '42', '_plugin': 'github',
    'actions': [
        {'key': 'o', 'label': 'open', 'primary': True, 'payload': {}},
        {'key': 's', 'label': 'session', 'payload': {}},
    ],
}]
rows = m.render_dashboard_rows(items)
print(len(rows))
fields = rows[0].split(chr(9))
print(len(fields))
print(fields[0].strip())
print(fields[2])
print(fields[3])
list_fields = m.render_rows([dict(items[0])])[0].split(chr(9))
print(fields[1] == list_fields[1])
")"
  check "render_dashboard_rows() emits exactly one row per item" "$(sed -n 1p <<<"$out")" "1"
  check "render_dashboard_rows() emits exactly 4 tab-delimited fields" "$(sed -n 2p <<<"$out")" "4"
  check "render_dashboard_rows() field 1 omits status and begins with title" "$(sed -n 3p <<<"$out")" "Fix the login bug  myorg/kb"
  check "render_dashboard_rows() field 3 is the comma-joined CSV of this item's own action keys" "$(sed -n 4p <<<"$out")" "o,s"
  check "render_dashboard_rows() field 4 is the same hint text render_rows() puts in field 3" "$(sed -n 5p <<<"$out")" "o open  s session"
  check "render_dashboard_rows() shares render_rows()'s hidden actions-blob field (field 2)" "$(sed -n 6p <<<"$out")" "True"
}
test_render_dashboard_rows_omits_status_and_retains_action_fields

test_dashboard_indicator_columns_are_shared_per_row_set() {
  local out
  out="$(python3 -c "
$LOAD_CORE
$LOAD_DASHBOARD
items = [{
    'status': 'NEEDS ATTENTION', 'context': 'myorg/kb', 'title': 'Fix the login bug',
    'details': '', 'weight': 90, 'id': '42',
    'indicators': {'ci': '✓', 'ready': '✓', 'review': '×', 'stacked': '✓'},
    'actions': [{'key': 'o', 'label': 'open', 'primary': True, 'payload': {}}],
}]
rows = m.render_dashboard_rows(items, ['ci', 'ready', 'review', 'stacked'])
fields = rows[0].split(chr(9))
print(len(fields))
print(m.json.loads(fields[4]))
print(d.CursesPresenter()._indicator_header(rows))
print(fields[0].index('Fix the login bug') < fields[0].rfind('✓'))
")"
  check "dashboard indicator rows add one metadata field for their shared table columns" \
    "$(sed -n 1p <<<"$out")" "5"
  check "dashboard indicator metadata keeps the CI, ready, review, and stacked table definition" \
    "$(sed -n 2p <<<"$out")" "[['CI', 2], ['READY', 5], ['REVIEW', 6], ['STACKED', 7]]"
  check "curses dashboard renders the shared indicator table header" \
    "$(sed -n 3p <<<"$out")" "CI  READY  REVIEW  STACKED"
  check "dashboard indicator values follow the item content" \
    "$(sed -n 4p <<<"$out")" "True"
}
test_dashboard_indicator_columns_are_shared_per_row_set

test_dashboard_action_hints_wrap_at_footer_width() {
  local out
  out="$(python3 -c "
$LOAD_CORE
import shutil

class DummySize:
    columns = 40

shutil.get_terminal_size = lambda: DummySize()
actions = [
    {'key': 'o', 'label': 'open'},
    {'key': 'a', 'label': 'approve'},
    {'key': 'm', 'label': 'merge'},
    {'key': 'c', 'label': 'comment'},
    {'key': 'l', 'label': 'label'},
]
hint = m._dashboard_hint_for_actions(actions)
print(repr(hint))
print(hint.split(chr(11)))
")"
  check "dashboard action hints use explicit footer lines that fit a 40-column terminal" \
    "$(sed -n 1p <<<"$out")" "'o open  a approve  m merge  c comment\\x0bl label'"
  check "the curses presenter receives one footer line per wrapped action-hint line" \
    "$(sed -n 2p <<<"$out")" "['o open  a approve  m merge  c comment', 'l label']"
}
test_dashboard_action_hints_wrap_at_footer_width


test_plugins_md_documents_terminal_key_constraints() {
  local body
  body="$(python3 -c "
src = open('$REPO_ROOT/PLUGINS.md').read()
print('ASCII letter or digit' in src and 'lowercase' in src)
")"
  check "PLUGINS.md documents terminal action key syntax" "$body" "True"
}
test_plugins_md_documents_terminal_key_constraints



# ---------------------------------------------------------------------------
echo
echo "== core: act(key, line) dispatch =="

INTERACT_BIN="$WORK/bin-act-interact"
mkdir -p "$INTERACT_BIN"

OPEN_LOG="$WORK/open-invocations.log"; : > "$OPEN_LOG"
REMINDCTL_ACT_LOG="$WORK/remindctl-act-invocations.log"; : > "$REMINDCTL_ACT_LOG"
GH_ACT_LOG="$WORK/gh-act-invocations.log"; : > "$GH_ACT_LOG"
PBCOPY_LOG="$WORK/pbcopy-invocations.log"; : > "$PBCOPY_LOG"
AOE_CMD_LOG="$WORK/aoe-cmd-invocations.log"; : > "$AOE_CMD_LOG"
LUMEN_LOG="$WORK/lumen-invocations.log"; : > "$LUMEN_LOG"

cat > "$INTERACT_BIN/open" <<STUB
#!/bin/sh
echo "\$*" >> "$OPEN_LOG"
exit 0
STUB
chmod +x "$INTERACT_BIN/open"

cat > "$INTERACT_BIN/remindctl" <<STUB
#!/bin/sh
echo "\$*" >> "$REMINDCTL_ACT_LOG"
exit 0
STUB
chmod +x "$INTERACT_BIN/remindctl"

cat > "$INTERACT_BIN/gh" <<STUB
#!/bin/sh
echo "\$*" >> "$GH_ACT_LOG"
exit 0
STUB
chmod +x "$INTERACT_BIN/gh"

cat > "$INTERACT_BIN/pbcopy" <<STUB
#!/bin/sh
cat >> "$PBCOPY_LOG"
exit 0
STUB
chmod +x "$INTERACT_BIN/pbcopy"

cat > "$INTERACT_BIN/lumen" <<STUB
#!/bin/sh
echo "\$*" >> "$LUMEN_LOG"
exit 0
STUB
chmod +x "$INTERACT_BIN/lumen"

cat > "$INTERACT_BIN/aoe-cmd" <<STUB
#!/bin/sh
# Deliberately slow (mirrors aoe-cmd's real ~2-30s worktree+readiness-poll
# latency) -- proves the session action dispatches this in the background
# instead of blocking the caller on it.
sleep 1
echo "\$*" >> "$AOE_CMD_LOG"
exit 0
STUB
chmod +x "$INTERACT_BIN/aoe-cmd"

GH_OPEN_ACTIONS='[{"key": "o", "label": "open", "primary": true, "payload": {"kind": "open", "url": "https://github.com/myorg/kb/pull/42"}, "_plugin": "github"}]'
GH_FULL_ACTIONS='[
  {"key": "o", "label": "open", "primary": true, "payload": {"kind": "open", "url": "https://github.com/myorg/kb/pull/42"}, "_plugin": "github"},
  {"key": "s", "label": "session", "payload": {"command": ["aoe-cmd", "-d", "/tmp/repo", "-n", "test-pr", "-b", "-w", "test-pr", "Work on issue 42 in this repo"], "background": true}, "_plugin": "github"},
  {"key": "l", "label": "lumen", "payload": {"command": ["lumen", "diff", "--pr", "https://github.com/myorg/kb/pull/42"]}, "_plugin": "github"},
  {"key": "a", "label": "approve", "payload": {"kind": "approve", "id": "42", "url": "https://github.com/myorg/kb/pull/42"}, "_plugin": "github"},
  {"key": "m", "label": "merge", "payload": {"kind": "merge", "id": "42", "url": "https://github.com/myorg/kb/pull/42"}, "_plugin": "github"},
  {"key": "c", "label": "comment", "payload": {"kind": "comment", "id": "42", "url": "https://github.com/myorg/kb/pull/42"}, "_plugin": "github"},
  {"key": "g", "label": "label", "payload": {"kind": "label", "id": "42", "url": "https://github.com/myorg/kb/pull/42"}, "_plugin": "github"},
  {"key": "1", "label": "open (linked)", "primary": false, "payload": {"kind": "open", "url": "https://linear.app/abc/issue/ABC-1"}, "_plugin": "linear", "_original_key": "o"}
]'
REM_ACTIONS='[{"key": "x", "label": "complete", "payload": {"id": "r1"}, "_plugin": "reminders"}]'
CAL_ACTIONS='[{"key": "y", "label": "yank", "payload": {"text": "Team Sync - 10:00 AM"}, "_plugin": "calendar"}]'
CAL_MULTI_ACTIONS='[
  {"key": "y", "label": "yank", "payload": {"text": "Team Sync"}, "_plugin": "calendar"},
  {"key": "x", "label": "complete (linked)", "payload": {"id": "r1"}, "_plugin": "reminders", "_original_key": "x"},
  {"key": "1", "label": "complete (linked)", "payload": {"id": "r2"}, "_plugin": "reminders", "_original_key": "x"}
]'
LIN_ACTIONS='[
  {"key": "o", "label": "open", "primary": true, "payload": {"kind": "open", "url": "https://linear.app/abc/issue/ABC-1"}, "_plugin": "linear"},
  {"key": "s", "label": "session", "payload": {"command": ["aoe-cmd", "-d", ".", "-n", "abc-1", "Work on Linear issue ABC-1"], "background": true}, "_plugin": "linear"}
]'

FIX_GH_LINE="REVIEW REQUESTED  myorg/kb  Test PR   ${TAB}$(echo "$GH_OPEN_ACTIONS" | blob_for)${TAB}hint"
FIX_GH_FULL_LINE="REVIEW REQUESTED  myorg/kb  Test PR   ${TAB}$(echo "$GH_FULL_ACTIONS" | blob_for)${TAB}hint"
FIX_REM_LINE="HIGH  Personal  Buy milk   ${TAB}$(echo "$REM_ACTIONS" | blob_for)${TAB}hint"
FIX_CAL_LINE="ALL DAY  Work  Team Sync   ${TAB}$(echo "$CAL_ACTIONS" | blob_for)${TAB}hint"
FIX_CAL_MULTI_LINE="ALL DAY  Work  Team Sync   ${TAB}$(echo "$CAL_MULTI_ACTIONS" | blob_for)${TAB}hint"
FIX_LIN_LINE="TODO  MyProject  Fix bug   ${TAB}$(echo "$LIN_ACTIONS" | blob_for)${TAB}hint"

run_act() {
  local key="$1" line="$2" stdin="${3-}"
  ACT_OUTPUT="$(printf '%s' "$stdin" | HOME="$TEST_HOME" XDG_CONFIG_HOME="$XDG_CONFIG" PATH="$INTERACT_BIN:$PATH" \
    python3 "$ATTENTION" act "$key" "$line" 2>&1)" && ACT_RC=0 || ACT_RC=$?
  if [ "$ACT_RC" -ne 0 ]; then
    printf '  (act output: %s)\n' "$ACT_OUTPUT" >&2
  fi
}

echo
echo "-- action hotkey dispatch invokes the correct downstream command --"

: > "$OPEN_LOG"
run_act "o" "$FIX_GH_LINE"
check "GH o exits 0" "$ACT_RC" "0"
if grep -q 'https://github.com/myorg/kb/pull/42' "$OPEN_LOG"; then
  ok "GH o invokes open with the item URL"
else
  bad "GH o invokes open with the item URL (got: $(cat "$OPEN_LOG"))"
fi

: > "$REMINDCTL_ACT_LOG"
run_act "x" "$FIX_REM_LINE"
check "REM x exits 0" "$ACT_RC" "0"
if grep -q 'complete r1' "$REMINDCTL_ACT_LOG"; then
  ok "REM x invokes remindctl complete on the item's own ID"
else
  bad "REM x invokes remindctl complete on the item's own ID (got: $(cat "$REMINDCTL_ACT_LOG"))"
fi

: > "$PBCOPY_LOG"
run_act "y" "$FIX_CAL_LINE"
check "CAL y exits 0" "$ACT_RC" "0"
if grep -q 'Team Sync' "$PBCOPY_LOG"; then
  ok "CAL y copies the payload's precomputed text to the clipboard"
else
  bad "CAL y copies the payload's precomputed text to the clipboard (got: $(cat "$PBCOPY_LOG"))"
fi

: > "$LUMEN_LOG"
run_act "l" "$FIX_GH_FULL_LINE"
check "GH l exits 0" "$ACT_RC" "0"
if grep -q 'diff --pr https://github.com/myorg/kb/pull/42' "$LUMEN_LOG"; then
  ok "GH l invokes lumen diff --pr with the item URL"
else
  bad "GH l invokes lumen diff --pr with the item URL (got: $(cat "$LUMEN_LOG"))"
fi

: > "$GH_ACT_LOG"
run_act "a" "$FIX_GH_FULL_LINE"
check "GH a exits 0" "$ACT_RC" "0"
if grep -q 'pr review --approve 42 --repo myorg/kb' "$GH_ACT_LOG"; then
  ok "GH a invokes gh pr review --approve"
else
  bad "GH a invokes gh pr review --approve (got: $(cat "$GH_ACT_LOG"))"
fi

: > "$GH_ACT_LOG"
run_act "c" "$FIX_GH_FULL_LINE" "a nice comment
"
check "GH c exits 0" "$ACT_RC" "0"
if grep -q 'issue comment 42 -R myorg/kb -b a nice comment' "$GH_ACT_LOG"; then
  ok "GH c invokes gh issue comment with the entered body"
else
  bad "GH c invokes gh issue comment with the entered body (got: $(cat "$GH_ACT_LOG"))"
fi

: > "$GH_ACT_LOG"
run_act "g" "$FIX_GH_FULL_LINE" "bug
"
check "GH g exits 0" "$ACT_RC" "0"
if grep -q 'issue edit 42 -R myorg/kb --add-label bug' "$GH_ACT_LOG"; then
  ok "GH g invokes gh issue edit --add-label with the entered label"
else
  bad "GH g invokes gh issue edit --add-label with the entered label (got: $(cat "$GH_ACT_LOG"))"
fi

echo
echo "-- CAL multi-reminder overflow: linked reminders keep their own routed key --"

: > "$REMINDCTL_ACT_LOG"
run_act "x" "$FIX_CAL_MULTI_LINE"
check "CAL x (first linked reminder) exits 0" "$ACT_RC" "0"
if grep -q 'complete r1' "$REMINDCTL_ACT_LOG"; then
  ok "CAL x completes the first linked reminder (r1)"
else
  bad "CAL x completes the first linked reminder (r1) (got: $(cat "$REMINDCTL_ACT_LOG"))"
fi

: > "$REMINDCTL_ACT_LOG"
run_act "1" "$FIX_CAL_MULTI_LINE"
check "CAL digit-overflow key '1' exits 0" "$ACT_RC" "0"
if grep -q 'complete r2' "$REMINDCTL_ACT_LOG"; then
  ok "CAL digit-overflow key '1' completes the second linked reminder (r2)"
else
  bad "CAL digit-overflow key '1' completes the second linked reminder (r2) (got: $(cat "$REMINDCTL_ACT_LOG"))"
fi

echo
echo "-- Linear cross-link remapped digit key routes to the linear plugin, not github --"

: > "$OPEN_LOG"
run_act "1" "$FIX_GH_FULL_LINE"
check "GH linked item, key '1' exits 0" "$ACT_RC" "0"
if grep -q 'https://linear.app/abc/issue/ABC-1' "$OPEN_LOG"; then
  ok "the remapped digit key opens the linked Linear issue, not the GH item"
else
  bad "the remapped digit key opens the linked Linear issue (got: $(cat "$OPEN_LOG"))"
fi

: > "$OPEN_LOG"
run_act "o" "$FIX_GH_FULL_LINE"
check "GH linked item, key 'o' exits 0" "$ACT_RC" "0"
if grep -q 'https://github.com/myorg/kb/pull/42' "$OPEN_LOG"; then
  ok "key 'O' opens the GH item's own open action"
else
  bad "key 'O' opens the GH item's own open action (got: $(cat "$OPEN_LOG"))"
fi

echo
echo "-- Enter (empty key): whichever action is primary=true, no-op if none --"

: > "$OPEN_LOG"
run_act "" "$FIX_GH_LINE"
check "Enter on GH exits 0" "$ACT_RC" "0"
if grep -q 'https://github.com/myorg/kb/pull/42' "$OPEN_LOG"; then
  ok "Enter on GH runs its primary=true action (open)"
else
  bad "Enter on GH runs its primary=true action (got: $(cat "$OPEN_LOG"))"
fi

: > "$OPEN_LOG"
run_act "" "$FIX_LIN_LINE"
check "Enter on LIN exits 0" "$ACT_RC" "0"
if grep -q 'https://linear.app/abc/issue/ABC-1' "$OPEN_LOG"; then
  ok "Enter on LIN runs its primary=true action (open)"
else
  bad "Enter on LIN runs its primary=true action (got: $(cat "$OPEN_LOG"))"
fi

: > "$PBCOPY_LOG"; : > "$REMINDCTL_ACT_LOG"
run_act "" "$FIX_CAL_LINE"
check "Enter on CAL exits 0 (no-op, no action is primary)" "$ACT_RC" "0"
if [ -s "$PBCOPY_LOG" ] || [ -s "$REMINDCTL_ACT_LOG" ]; then
  bad "Enter on CAL must not invoke any action (pbcopy: $(cat "$PBCOPY_LOG"); remindctl: $(cat "$REMINDCTL_ACT_LOG"))"
else
  ok "Enter on CAL takes no action"
fi

: > "$REMINDCTL_ACT_LOG"
run_act "" "$FIX_REM_LINE"
check "Enter on REM exits 0 (no-op, no action is primary)" "$ACT_RC" "0"
if [ -s "$REMINDCTL_ACT_LOG" ]; then
  bad "Enter on REM must not invoke any action (got: $(cat "$REMINDCTL_ACT_LOG"))"
else
  ok "Enter on REM takes no action"
fi

echo
echo "-- unmapped key for a row: brief note, exit 0, no traceback --"

: > "$OPEN_LOG"
run_act "z" "$FIX_GH_LINE"
check "unmapped key exits 0 (not an error)" "$ACT_RC" "0"
case "$ACT_OUTPUT" in
  *Traceback*) bad "unmapped key must not print a traceback (got: $ACT_OUTPUT)" ;;
  *) ok "unmapped key prints a note instead of crashing (got: $ACT_OUTPUT)" ;;
esac
if [ -s "$OPEN_LOG" ]; then
  bad "unmapped key must not invoke any downstream action command (got: $(cat "$OPEN_LOG"))"
else
  ok "unmapped key does not invoke any downstream action command"
fi

echo
echo "-- no redundant quit key: 'q' is not a bound hotkey anywhere --"

: > "$OPEN_LOG"
run_act "q" "$FIX_GH_LINE"
check "'q' exits 0 like any other unmapped key (no special quit exit code)" "$ACT_RC" "0"
if [ -s "$OPEN_LOG" ]; then
  bad "'q' must not invoke any downstream action command (got: $(cat "$OPEN_LOG"))"
else
  ok "'q' does not invoke any downstream action command"
fi

echo
echo "-- merge gate (m): confirm_and_merge runs as a plain input() prompt --"

: > "$GH_ACT_LOG"
run_act "m" "$FIX_GH_FULL_LINE" "y
"
check "merge confirm 'y' exits 0" "$ACT_RC" "0"
if grep -q 'pr merge --squash --delete-branch 42 --repo myorg/kb' "$GH_ACT_LOG"; then
  ok "merge confirm 'y' invokes gh pr merge --squash --delete-branch"
else
  bad "merge confirm 'y' invokes gh pr merge --squash --delete-branch (got: $(cat "$GH_ACT_LOG"))"
fi

: > "$GH_ACT_LOG"
run_act "m" "$FIX_GH_FULL_LINE" "n
"
check "merge confirm 'n' exits 0" "$ACT_RC" "0"
if [ -s "$GH_ACT_LOG" ]; then
  bad "merge confirm 'n' must not invoke gh pr merge (got: $(cat "$GH_ACT_LOG"))"
else
  ok "merge confirm 'n' does not invoke gh pr merge"
fi

: > "$GH_ACT_LOG"
run_act "m" "$FIX_GH_FULL_LINE" ""
check "merge confirm EOF (no stdin) exits 0, canceled gracefully" "$ACT_RC" "0"
if [ -s "$GH_ACT_LOG" ]; then
  bad "merge confirm EOF must not invoke gh pr merge (got: $(cat "$GH_ACT_LOG"))"
else
  ok "merge confirm EOF does not invoke gh pr merge"
fi

echo
echo "-- session dispatch (s): backgrounded (doesn't block the caller) --"

wait_for_aoe_cmd_log() {
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    [ -s "$AOE_CMD_LOG" ] && return 0
    sleep 0.3
  done
  return 1
}

: > "$AOE_CMD_LOG"
start_ts=$(date +%s)
run_act "s" "$FIX_GH_FULL_LINE"
elapsed=$(( $(date +%s) - start_ts ))
check "GH s exits 0" "$ACT_RC" "0"
if [ "$elapsed" -le 1 ]; then
  ok "GH s returns without waiting for the dispatched process (${elapsed}s; stub sleeps 1s)"
else
  bad "GH s returns without waiting for the dispatched process (took ${elapsed}s; stub sleeps 1s)"
fi
wait_for_aoe_cmd_log
if grep -q -- '-n test-pr -b -w test-pr ' "$AOE_CMD_LOG"; then
  ok "GH s names session/worktree branch from the title slug 'test-pr' (got: $(cat "$AOE_CMD_LOG"))"
else
  bad "GH s names session/worktree branch from the title slug 'test-pr' (got: $(cat "$AOE_CMD_LOG"))"
fi

: > "$AOE_CMD_LOG"
run_act "s" "$FIX_LIN_LINE"
check "LIN s exits 0" "$ACT_RC" "0"
wait_for_aoe_cmd_log
if grep -q -- '-n abc-1 ' "$AOE_CMD_LOG"; then
  ok "LIN S names the session from the issue identifier"
else
  bad "LIN S names the session from the issue identifier (got: $(cat "$AOE_CMD_LOG"))"
fi

# ---------------------------------------------------------------------------
echo
echo "== dashboard.py: Presenter seam, injected callbacks, single in-flight round =="

test_dashboard_module_has_no_forbidden_imports() {
  local src
  src="$(cat "$REPO_ROOT/dashboard.py")"
  case "$src" in
    *"import attention"*) bad "dashboard.py does not import attention anywhere" ;;
    *) ok "dashboard.py does not import attention anywhere" ;;
  esac
  local controller_src
  controller_src="$(python3 -c "
src = open('$REPO_ROOT/dashboard.py').read()
start = src.index('class DashboardController:')
end = src.index('class CursesPresenter:')
print(src[start:end])
")"
  case "$controller_src" in
    *"subprocess"*) bad "DashboardController itself never references subprocess" ;;
    *) ok "DashboardController itself never references subprocess" ;;
  esac
  case "$controller_src" in
    *"socket"*) bad "DashboardController itself never references socket" ;;
    *) ok "DashboardController itself never references socket" ;;
  esac
  case "$controller_src" in
    *"http.client"*) bad "DashboardController itself never references http.client" ;;
    *) ok "DashboardController itself never references http.client" ;;
  esac
  case "$controller_src" in
    *"ThreadPoolExecutor("*) bad "DashboardController no longer uses ThreadPoolExecutor (its non-daemon workers are joined at interpreter shutdown)" ;;
    *) ok "DashboardController no longer uses ThreadPoolExecutor (its non-daemon workers are joined at interpreter shutdown)" ;;
  esac
  case "$controller_src" in
    *"daemon=True"*) ok "DashboardController dispatches per-plugin fetches on daemon threads (never block interpreter exit)" ;;
    *) bad "DashboardController dispatches per-plugin fetches on daemon threads (never block interpreter exit)" ;;
  esac
}
test_dashboard_module_has_no_forbidden_imports

test_dashboard_controller_constructs_from_injected_callables() {
  local out
  out="$(python3 -c "
$LOAD_DASHBOARD
$DASHBOARD_FIXTURES
presenter = FakePresenter()
controller = d.DashboardController(
    ['a', 'b'], presenter,
    fetch_plugin=lambda name: [],
    build_snapshot=flatten_build_snapshot,
    render_rows=titles_render_rows,
    act=lambda key, row: None,
)
print(isinstance(controller, d.DashboardController))
print(controller.plugin_names)
")"
  check "DashboardController constructs from plugin_names/presenter + injected fetch_plugin/build_snapshot/render_rows/act" \
    "$(sed -n 1p <<<"$out")" "True"
  check "DashboardController keeps the configured plugin_names" "$(sed -n 2p <<<"$out")" "['a', 'b']"
}
test_dashboard_controller_constructs_from_injected_callables

test_fake_presenter_records_ordered_pushes_and_blocks_wait_for_exit() {
  local out
  out="$(python3 -c "
$LOAD_DASHBOARD
$DASHBOARD_FIXTURES
presenter = FakePresenter()
presenter.launch()
presenter.push_snapshot(['row1'], ['b'])
presenter.push_snapshot(['row1', 'row2'], [])
print([c[1] for c in presenter.push_calls()])
result_holder = []
def waiter():
    result_holder.append(presenter.wait_for_exit(3600))
th = threading.Thread(target=waiter)
th.start()
started_blocked = not th.join(timeout=0.2) and th.is_alive()
print(started_blocked)
presenter.send_result('o', 'row1')
th.join(timeout=5)
print(result_holder[0].key, repr(result_holder[0].row))
")"
  check "FakePresenter.push_snapshot() records every call's rows in order" \
    "$(sed -n 1p <<<"$out")" "[['row1'], ['row1', 'row2']]"
  check "FakePresenter.wait_for_exit() blocks until the test supplies a result (no wall-clock sleep)" \
    "$(sed -n 2p <<<"$out")" "True"
  check "FakePresenter.wait_for_exit() returns exactly the result the test sent" \
    "$(sed -n 3p <<<"$out")" "o 'row1'"
}
test_fake_presenter_records_ordered_pushes_and_blocks_wait_for_exit

test_dashboard_controller_from_fakes_touches_nothing_real() {
  local out
  out="$(python3 -c "
$LOAD_DASHBOARD
$DASHBOARD_FIXTURES
import sys
presenter = FakePresenter()
controller = d.DashboardController(
    ['solo'], presenter,
    fetch_plugin=lambda name: [{'status': 'S', 'context': 'c', 'title': 'X', 'details': '', 'weight': 1, 'id': 'x'}],
    build_snapshot=flatten_build_snapshot,
    render_rows=titles_render_rows,
    act=lambda key, row: None,
)
th = threading.Thread(target=controller.run, args=(3600,))
th.start()
presenter.wait_for_push_count(1, timeout=5)
presenter.send_result('', '')
th.join(timeout=5)
print('attention_core' not in sys.modules)
print('attention' not in sys.modules)
")"
  check "a DashboardController built entirely from fakes never imports attention_core, real plugin loading, config, or gh" \
    "$(sed -n 1p <<<"$out")" "True"
  check "a DashboardController built entirely from fakes never imports the attention module itself" \
    "$(sed -n 2p <<<"$out")" "True"
}
test_dashboard_controller_from_fakes_touches_nothing_real

# ---------------------------------------------------------------------------
echo
echo "== DashboardController: progressive publish, pending visibility, single-round invariant =="

test_progressive_publish_order_never_skips_earlier_providers() {
  local out
  out="$(python3 -c "
$LOAD_DASHBOARD
$DASHBOARD_FIXTURES
gate_b = threading.Event()
items_by_name = {
    'a': [{'status': 'S', 'context': 'c', 'title': 'ItemA', 'details': '', 'weight': 10, 'id': 'a1'}],
    'b': [{'status': 'S', 'context': 'c', 'title': 'ItemB', 'details': '', 'weight': 5, 'id': 'b1'}],
}
calls = []
fetch_plugin = gated_fetch_plugin(items_by_name, {'b': gate_b}, calls)
presenter = FakePresenter()
controller = d.DashboardController(
    ['a', 'b'], presenter,
    fetch_plugin=fetch_plugin, build_snapshot=flatten_build_snapshot,
    render_rows=titles_render_rows, act=lambda key, row: None,
)
th = threading.Thread(target=controller.run, args=(3600,))
th.start()
# push #1 is the round's initial (empty, all-pending) priming snapshot;
# push #2 is 'a' finishing.
presenter.wait_for_push_count(2, timeout=5)
first_rows = presenter.push_calls()[-1][1]
gate_b.set()
presenter.wait_for_push_count(3, timeout=5)
second_rows = presenter.push_calls()[-1][1]
presenter.send_result('', '')
th.join(timeout=5)
print(first_rows)
print(second_rows)
print(any(c[1] == ['ItemB'] for c in presenter.push_calls()))
")"
  check "first push_snapshot shows only the provider that finished first" "$(sed -n 1p <<<"$out")" "['ItemA']"
  check "second push_snapshot is a freshly recomputed full snapshot of both providers, sorted, not an append" \
    "$(sed -n 2p <<<"$out")" "['ItemA', 'ItemB']"
  check "the slower provider's item is never published alone" "$(sed -n 3p <<<"$out")" "False"
}
test_progressive_publish_order_never_skips_earlier_providers

test_pending_provider_visibility_names_exactly_the_unfinished_ones() {
  local out
  out="$(python3 -c "
$LOAD_DASHBOARD
$DASHBOARD_FIXTURES
gate_b = threading.Event()
gate_c = threading.Event()
items_by_name = {
    'a': [{'status': 'S', 'context': 'c', 'title': 'A', 'details': '', 'weight': 1, 'id': 'a'}],
    'b': [{'status': 'S', 'context': 'c', 'title': 'B', 'details': '', 'weight': 1, 'id': 'b'}],
    'c': [{'status': 'S', 'context': 'c', 'title': 'C', 'details': '', 'weight': 1, 'id': 'c'}],
}
calls = []
fetch_plugin = gated_fetch_plugin(items_by_name, {'b': gate_b, 'c': gate_c}, calls)
presenter = FakePresenter()
controller = d.DashboardController(
    ['a', 'b', 'c'], presenter,
    fetch_plugin=fetch_plugin, build_snapshot=flatten_build_snapshot,
    render_rows=titles_render_rows, act=lambda key, row: None,
)
th = threading.Thread(target=controller.run, args=(3600,))
th.start()
presenter.wait_for_launch_count(1, timeout=5)
presenter.wait_for_push_count(2, timeout=5)
pending_after_a = presenter.push_calls()[-1][2]
gate_b.set()
presenter.wait_for_push_count(3, timeout=5)
pending_after_b = presenter.push_calls()[-1][2]
gate_c.set()
presenter.wait_for_push_count(4, timeout=5)
pending_after_c = presenter.push_calls()[-1][2]
presenter.send_result('', '')
th.join(timeout=5)
print(pending_after_a)
print(pending_after_b)
print(pending_after_c)
")"
  check "pending after 'a' finishes names exactly the still-unfinished providers" "$(sed -n 1p <<<"$out")" "['b', 'c']"
  check "pending after 'b' also finishes shrinks to just the remaining provider" "$(sed -n 2p <<<"$out")" "['c']"
  check "pending is empty once every provider has finished" "$(sed -n 3p <<<"$out")" "[]"
}
test_pending_provider_visibility_names_exactly_the_unfinished_ones

test_round_goes_terminal_only_once_every_plugin_returns() {
  local out
  out="$(python3 -c "
$LOAD_DASHBOARD
$DASHBOARD_FIXTURES
gate_a = threading.Event()
items_by_name = {
    'a': [{'status': 'S', 'context': 'c', 'title': 'A', 'details': '', 'weight': 1, 'id': 'a'}],
    'b': [{'status': 'S', 'context': 'c', 'title': 'B', 'details': '', 'weight': 1, 'id': 'b'}],
}
calls = CallLog()
fetch_plugin = gated_fetch_plugin(items_by_name, {'a': gate_a}, calls)
presenter = FakePresenter()
controller = d.DashboardController(
    ['a', 'b'], presenter,
    fetch_plugin=fetch_plugin, build_snapshot=flatten_build_snapshot,
    render_rows=titles_render_rows, act=lambda key, row: None,
)
th = threading.Thread(target=controller.run, args=(3600,))
th.start()
calls.wait_for_count(2, timeout=5)
presenter.wait_for_push_count(2, timeout=5)
calls_before = sorted(calls.snapshot())
launches = presenter.launch_count
for _ in range(5):
    presenter.send_timeout()
    presenter.wait_for_launch_count(launches + 1, timeout=5)
    launches = presenter.launch_count
calls_during_gate = sorted(calls.snapshot())
pushes_before_release = len(presenter.push_calls())
gate_a.set()
presenter.wait_for_push_count(pushes_before_release + 1, timeout=5)
presenter.send_timeout()
presenter.wait_for_launch_count(launches + 1, timeout=5)
pushes_after_new_round_launch = len(presenter.push_calls())
presenter.wait_for_push_count(pushes_after_new_round_launch + 1, timeout=5)
calls_after_release_and_timeout = sorted(calls.snapshot())
presenter.send_result('', '')
th.join(timeout=5)
print(calls_before)
print(calls_during_gate == calls_before)
print(len(calls_after_release_and_timeout) > len(calls_during_gate))
")"
  check "while one plugin is still gated, repeated timeouts submit no second round's fetch_plugin calls" \
    "$(sed -n 2p <<<"$out")" "True"
  check "once the gated plugin finally returns, the round goes terminal and a fresh round becomes eligible" \
    "$(sed -n 3p <<<"$out")" "True"
}
test_round_goes_terminal_only_once_every_plugin_returns

test_timeout_and_accept_relaunch_presenter_without_new_fetch_calls_mid_round() {
  local out
  out="$(python3 -c "
$LOAD_DASHBOARD
$DASHBOARD_FIXTURES
gate_b = threading.Event()
items_by_name = {
    'a': [{'status': 'S', 'context': 'c', 'title': 'A', 'details': '', 'weight': 1, 'id': 'a',
           'actions': [{'key': 'o', 'label': 'x', 'primary': True, '_item_id': 'a'}]}],
    'b': [{'status': 'S', 'context': 'c', 'title': 'B', 'details': '', 'weight': 1, 'id': 'b'}],
}
calls = CallLog()
fetch_plugin = gated_fetch_plugin(items_by_name, {'b': gate_b}, calls)
events = []
presenter = FakePresenter()
controller = d.DashboardController(
    ['a', 'b'], presenter,
    fetch_plugin=fetch_plugin, build_snapshot=flatten_build_snapshot,
    render_rows=blob_render_rows,
    act=lambda key, row: events.append('act'),
    acknowledge_action=lambda: events.append('acknowledge'),
)
th = threading.Thread(target=controller.run, args=(3600,))
th.start()
calls.wait_for_count(2, timeout=5)
presenter.wait_for_push_count(2, timeout=5)
row_for_a = presenter.push_calls()[-1][1][0]
presenter.send_result('o', row_for_a)
presenter.wait_for_launch_count(2, timeout=5)
calls_after_accept = sorted(calls.snapshot())
pushes_before_release = len(presenter.push_calls())
gate_b.set()
presenter.wait_for_push_count(pushes_before_release + 1, timeout=5)
pending_after_b = presenter.push_calls()[-1][2]
titles_after_b = [r.split(chr(9))[0] for r in presenter.push_calls()[-1][1]]
presenter.send_result('', '')
th.join(timeout=5)
print(events.count('act'))
print(calls_after_accept)
print(pending_after_b)
print(titles_after_b)
print(events)
")"
  check "the accepted hotkey dispatched through the injected act() callable" "$(sed -n 1p <<<"$out")" "1"
  check "an accept mid-round relaunches the presenter but submits no second round's fetch_plugin calls" \
    "$(sed -n 2p <<<"$out")" "['a', 'b']"
  check "the gated plugin's completion still reaches the presenter after the relaunch" "$(sed -n 3p <<<"$out")" "[]"
  check "the acted-on item is deprioritized below the newly-arrived item in the very next snapshot" \
    "$(sed -n 4p <<<"$out")" "['B', 'A']"
  check "the controller acknowledges action output before it relaunches the dashboard" \
    "$(sed -n 5p <<<"$out")" "['act', 'acknowledge']"
}
test_timeout_and_accept_relaunch_presenter_without_new_fetch_calls_mid_round

test_terminal_action_relaunches_existing_list_before_refresh() {
  local out
  out="$(python3 -c "
$LOAD_DASHBOARD
$DASHBOARD_FIXTURES
items = {
    'solo': [{'status': 'S', 'context': 'c', 'title': 'A', 'details': '', 'weight': 1, 'id': 'a',
              'actions': [{'key': 's', 'label': 'session', 'primary': False, '_item_id': 'a'}]}],
}
calls = CallLog()
presenter = FakePresenter()
controller = d.DashboardController(
    ['solo'], presenter,
    fetch_plugin=gated_fetch_plugin(items, {}, calls),
    build_snapshot=flatten_build_snapshot,
    render_rows=blob_render_rows,
    act=lambda key, row: None,
)
th = threading.Thread(target=controller.run, args=(3600,))
th.start()
calls.wait_for_count(1, timeout=5)
presenter.wait_for_push_count(2, timeout=5)
row = presenter.push_calls()[-1][1][0]
presenter.send_result('s', row)
presenter.wait_for_launch_count(2, timeout=5)
print(calls.snapshot())
print([r.split(chr(9))[0] for r in presenter.push_calls()[-1][1]])
presenter.send_result('', '')
th.join(timeout=5)
")"
  check "a terminal session action does not start a fresh fetch before the refresh interval" \
    "$(sed -n 1p <<<"$out")" "['solo']"
  check "a terminal session action relaunches the same item list" \
    "$(sed -n 2p <<<"$out")" "['A']"
}
test_terminal_action_relaunches_existing_list_before_refresh

test_invariant_has_no_override_across_many_cycles() {
  local out
  out="$(python3 -c "
$LOAD_DASHBOARD
$DASHBOARD_FIXTURES
gate_a = threading.Event()
items_by_name = {
    'a': [{'status': 'S', 'context': 'c', 'title': 'A', 'details': '', 'weight': 1, 'id': 'a'}],
    'b': [{'status': 'S', 'context': 'c', 'title': 'B', 'details': '', 'weight': 1, 'id': 'b'}],
}
calls = CallLog()
fetch_plugin = gated_fetch_plugin(items_by_name, {'a': gate_a}, calls)
presenter = FakePresenter()
controller = d.DashboardController(
    ['a', 'b'], presenter,
    fetch_plugin=fetch_plugin, build_snapshot=flatten_build_snapshot,
    render_rows=titles_render_rows, act=lambda key, row: None,
)
th = threading.Thread(target=controller.run, args=(3600,))
th.start()
calls.wait_for_count(2, timeout=5)
presenter.wait_for_push_count(2, timeout=5)
never_grew = True
launches = presenter.launch_count
for _ in range(50):
    calls_snapshot = sorted(calls.snapshot())
    presenter.send_timeout()
    presenter.wait_for_launch_count(launches + 1, timeout=5)
    launches = presenter.launch_count
    if sorted(calls.snapshot()) != calls_snapshot or presenter.push_calls()[-1][2] != ['a']:
        never_grew = False
pushes_before_release = len(presenter.push_calls())
gate_a.set()
presenter.wait_for_push_count(pushes_before_release + 1, timeout=5)
presenter.send_timeout()
presenter.wait_for_launch_count(launches + 1, timeout=5)
pushes_after_new_round_launch = len(presenter.push_calls())
presenter.wait_for_push_count(pushes_after_new_round_launch + 1, timeout=5)
became_eligible_again = len(calls.snapshot()) > 2
presenter.send_result('', '')
th.join(timeout=5)
print(never_grew)
print(became_eligible_again)
")"
  check "across 50 consecutive periodic-timeout cycles with one plugin permanently gated, no new round ever starts (no bounded escape hatch)" \
    "$(sed -n 1p <<<"$out")" "True"
  check "releasing the gated plugin finally lets the round go terminal and a new round becomes possible" \
    "$(sed -n 2p <<<"$out")" "True"
}
test_invariant_has_no_override_across_many_cycles

test_empty_final_snapshot_stops_presenter_and_reports_nothing_to_show() {
  local out
  out="$(python3 -c "
$LOAD_DASHBOARD
$DASHBOARD_FIXTURES
fetch_plugin = gated_fetch_plugin({'a': [], 'b': []}, {}, [])
presenter = FakePresenter()
controller = d.DashboardController(
    ['a', 'b'], presenter,
    fetch_plugin=fetch_plugin, build_snapshot=flatten_build_snapshot,
    render_rows=titles_render_rows, act=lambda key, row: None,
)
result_holder = []
def runner():
    result_holder.append(controller.run(3600))
th = threading.Thread(target=runner)
th.start()
th.join(timeout=5)
print(result_holder[0])
print(presenter.stopped)
")"
  check "run() returns False when every provider finishes with a permanently empty merged snapshot" \
    "$(sed -n 1p <<<"$out")" "False"
  check "the presenter is stopped without requiring the user to press Esc on an empty list" \
    "$(sed -n 2p <<<"$out")" "True"
}
test_empty_final_snapshot_stops_presenter_and_reports_nothing_to_show

test_quit_returns_promptly_and_discards_a_later_gated_completion() {
  local out
  out="$(python3 -c "
$LOAD_DASHBOARD
$DASHBOARD_FIXTURES
gate_a = threading.Event()
started = threading.Event()
thread_holder = []


def fetch_plugin(name):
    thread_holder.append(threading.current_thread())
    started.set()
    gate_a.wait()
    return [{'status': 'S', 'context': 'c', 'title': 'A', 'details': '', 'weight': 1, 'id': 'a'}]


presenter = FakePresenter()
controller = d.DashboardController(
    ['a'], presenter,
    fetch_plugin=fetch_plugin, build_snapshot=flatten_build_snapshot,
    render_rows=titles_render_rows, act=lambda key, row: None,
)
th = threading.Thread(target=controller.run, args=(3600,))
th.start()
started.wait(timeout=5)
presenter.send_result('', '')
th.join(timeout=5)
joined_promptly = not th.is_alive()
gate_still_unset = not gate_a.is_set()
closed_after_quit = controller._closed
calls_after_quit = list(presenter.calls)
gate_a.set()
thread_holder[0].join(timeout=5)
calls_after_release = list(presenter.calls)
print(joined_promptly)
print(gate_still_unset)
print(closed_after_quit)
print(calls_after_quit == calls_after_release)
")"
  check "run() returns (Esc/quit) within 5s while its one plugin's fetch is still gated forever, never releasing the gate itself" \
    "$(sed -n 1p <<<"$out")" "True"
  check "the gate is confirmed still unset when run() has already returned -- the gated fetch_plugin call is genuinely still blocked, not coincidentally finished" \
    "$(sed -n 2p <<<"$out")" "True"
  check "the controller marks itself closed as part of quitting" "$(sed -n 3p <<<"$out")" "True"
  check "releasing the gate after quitting lets the plugin's own daemon thread finish, but its late result never reaches the presenter (no new push/launch/stop call)" \
    "$(sed -n 4p <<<"$out")" "True"
}
test_quit_returns_promptly_and_discards_a_later_gated_completion

# ---------------------------------------------------------------------------
echo
echo "== dashboard.py: curses presenter row handling =="

test_curses_presenter_reads_row_actions() {
  local out
  out="$(python3 -c "
$LOAD_DASHBOARD
rows = [
    'Fix login bug' + chr(9) + 'blob1' + chr(9) + 'o,o' + chr(9) + 'o open' + chr(11) + 'o merge',
    'Review release notes' + chr(9) + 'blob2' + chr(9) + 's' + chr(9) + 's session',
]
print(d.CursesPresenter._action_keys(rows[0]))
print(d.CursesPresenter._hint_lines(rows[0]))
")"
  check "curses presenter reads only the selected row's action keys" "$(sed -n 1p <<<"$out")" "['o', 'o']"
  check "curses presenter splits wrapped action hints into footer lines" \
    "$(sed -n 2p <<<"$out")" "['o open', 'o merge']"
}
test_curses_presenter_reads_row_actions

test_curses_presenter_dispatches_filtered_and_alt_actions() {
  local out
  out="$(python3 -c "
$LOAD_DASHBOARD
import time

class FakeScreen:
    def __init__(self, keys):
        self.keys = list(keys)
    def keypad(self, value):
        pass
    def timeout(self, value):
        pass
    def getmaxyx(self):
        return (24, 80)
    def erase(self):
        pass
    def addnstr(self, *args):
        pass
    def refresh(self):
        pass
    def getch(self):
        return self.keys.pop(0) if self.keys else -1

class RecordingScreen(FakeScreen):
    def __init__(self, keys):
        super().__init__(keys)
        self.drawn = []
    def addnstr(self, *args):
        self.drawn.append(args[2])

rows = [
    'Fix login bug' + chr(9) + 'blob1' + chr(9) + 'o' + chr(9) + 'o open',
    'Review release notes' + chr(9) + 'blob2' + chr(9) + 'o' + chr(9) + 'o merge',
]
filtered = d.CursesPresenter()
filtered.launch()
filtered.push_snapshot(rows, [])
filtered_result = filtered._run(FakeScreen([ord('/'), *(ord(c) for c in 'review'), 10]), time.monotonic() + 1)
alt = d.CursesPresenter()
alt.launch()
alt.push_snapshot(rows, [])
alt_result = alt._run(FakeScreen([ord('o')]), time.monotonic() + 1)
reserved_results = []
for key in ('q', 'j', 'k'):
    row = 'Reserved ' + key + chr(9) + 'blob' + chr(9) + key + chr(9) + key + ' action'
    reserved = d.CursesPresenter()
    reserved.launch()
    reserved.push_snapshot([row], [])
    result = reserved._run(FakeScreen([ord(key), 27]), time.monotonic() + 1)
    reserved_results.append(result == d.PresenterResult('', ''))
unbound_lowercase = d.CursesPresenter()
unbound_lowercase.launch()
unbound_lowercase.push_snapshot(rows, [])
lowercase_screen = RecordingScreen([ord('z'), 27])
unbound_lowercase._run(lowercase_screen, time.monotonic() + 1)
print(filtered_result == d.PresenterResult('', rows[1]))
print(alt_result == d.PresenterResult('o', rows[0]))
print(all(reserved_results))
print('Filter: z' not in lowercase_screen.drawn)
")"
  check "curses presenter filters after slash before Enter selects the filtered row" "$(sed -n 1p <<<"$out")" "True"
  check "curses presenter dispatches a lowercase action on the selected row" "$(sed -n 2p <<<"$out")" "True"
  check "curses presenter reserves q, j, and k before action dispatch" "$(sed -n 3p <<<"$out")" "True"
  check "curses presenter does not filter on an unbound lowercase key without slash" "$(sed -n 4p <<<"$out")" "True"
}
test_curses_presenter_preserves_selection_after_action() {
  local out
  out="$(python3 -c "
$LOAD_DASHBOARD
import time

class FakeScreen:
    def __init__(self, keys):
        self._keys = list(keys)
    def keypad(self, v):
        pass
    def timeout(self, v):
        pass
    def getmaxyx(self):
        return (24, 80)
    def erase(self):
        pass
    def addnstr(self, *a, **k):
        pass
    def refresh(self):
        pass
    def getch(self):
        return self._keys.pop(0) if self._keys else -1

rows = [
    'Fix login bug' + chr(9) + 'blob1' + chr(9) + 'alt-o' + chr(9) + 'hint',
    'Review release notes' + chr(9) + 'blob2' + chr(9) + 'O' + chr(9) + 'hint',
    'Deploy hotfix' + chr(9) + 'blob3' + chr(9) + 'D' + chr(9) + 'hint',
]
presenter = d.CursesPresenter()
presenter.launch()
presenter.push_snapshot(rows, [])
# Navigate to the third row (index 2), then press its action key 'D'.
result = presenter._run(FakeScreen([ord('j'), ord('j'), ord('D')]), time.monotonic() + 1)
# Selection should be preserved at index 2 after the action.
print(presenter._last_selection)
print(result == d.PresenterResult('D', rows[2]))
")"
  check "flat presenter preserves selection index after action key" "$(sed -n 1p <<<"$out")" "2"
  check "flat presenter dispatches the action on the selected row" "$(sed -n 2p <<<"$out")" "True"
}
test_curses_presenter_detects_inflight_snapshot_update() {
  local out
  out="$(python3 -c "
$LOAD_DASHBOARD
import time

class FakeScreen:
    def __init__(self, keys):
        self._keys = list(keys)
    def keypad(self, v):
        pass
    def timeout(self, v):
        pass
    def getmaxyx(self):
        return (24, 80)
    def erase(self):
        pass
    def addnstr(self, *a, **k):
        pass
    def refresh(self):
        pass
    def getch(self):
        return self._keys.pop(0) if self._keys else -1

presenter = d.CursesPresenter()
presenter.launch()
rows1 = ['Item A' + chr(9) + 'blob1' + chr(9) + 'a' + chr(9) + 'hint']
presenter.push_snapshot(rows1, [])
# Simulate a snapshot update arriving while the presenter is waiting.
rows2 = ['Item A' + chr(9) + 'blob1' + chr(9) + 'a' + chr(9) + 'hint', 'Item B' + chr(9) + 'blob2' + chr(9) + 'b' + chr(9) + 'hint']
presenter.push_snapshot(rows2, [])
# No keys to press; presenter should detect the version change and loop.
# After deadline, it returns None.
result = presenter._run(FakeScreen([]), time.monotonic() + 0.5)
print(result.key is None)
print(presenter._snapshot_version >= 2)
")"
  check "presenter detects in-flight snapshot version change" "$(sed -n 1p <<<"$out")" "True"
  check "presenter snapshot version incremented after push" "$(sed -n 2p <<<"$out")" "True"
}
test_curses_presenter_dispatches_filtered_and_alt_actions
test_curses_presenter_preserves_selection_after_action
test_curses_presenter_detects_inflight_snapshot_update
echo "== dashboard groups =="

test_dashboard_group_rules_and_rows() {
  local out
  out="$(python3 -c "
$LOAD_CORE
groups, error = m.dashboard_groups({
    'dashboard': {
        'groups': [
            {'name': 'Needs Attention', 'match': {'statuses': ['NEEDS']}},
            {'name': 'My Repositories', 'match': {'contextPrefixes': ['athal7/']}},
            {'name': 'Ready to Ship', 'match': {'contexts': ['Release']}},
            {'name': 'Ready for Something New', 'fallback': True},
        ],
    },
})
items = [
    {'status': 'RELEASE', 'context': 'Release', 'title': 'Ship it', 'details': '', 'weight': 90, 'id': 'ship', 'actions': [], '_plugin': 'github'},
    {'status': 'NEEDS', 'context': 'Inbox', 'title': 'Review it', 'details': '', 'weight': 80, 'id': 'review', 'actions': [], '_plugin': 'github'},
    {'status': 'PENDING', 'context': 'Inbox', 'title': 'Plan it', 'details': '', 'weight': 70, 'id': 'plan', 'actions': [], '_plugin': 'reminders'},
    {'status': 'PENDING', 'context': 'athal7/attention', 'title': 'Prefix it', 'details': '', 'weight': 60, 'id': 'prefix', 'actions': [], '_plugin': 'generic'},
]
rows = m.render_grouped_dashboard_rows(items, groups)
print(error)
print(','.join(row.rpartition(chr(9))[2] for row in rows))
print(all(len(row.split(chr(9))) == 5 for row in rows))
first_match, first_error = m.dashboard_groups({
    'dashboard': {
        'groups': [
            {'name': 'First', 'match': {'plugins': ['github']}},
            {'name': 'Second', 'match': {'statuses': ['NEEDS']}},
            {'name': 'Other', 'fallback': True},
        ],
    },
})
first_row = m.render_grouped_dashboard_rows([items[1]], first_match)[0]
print(first_error)
print(first_row.rpartition(chr(9))[2])
_, invalid_error = m.dashboard_groups({
    'dashboard': {'groups': [{'name': 'A', 'fallback': True}, {'name': 'B', 'fallback': True}]},
})
_, empty_prefix_error = m.dashboard_groups({
    'dashboard': {'groups': [{'name': 'A', 'match': {'contextPrefixes': []}}, {'name': 'B', 'fallback': True}]},
})
_, typed_prefix_error = m.dashboard_groups({
    'dashboard': {'groups': [{'name': 'A', 'match': {'contextPrefixes': ['athal7/', 7]}}, {'name': 'B', 'fallback': True}]},
})
print(invalid_error)
print(empty_prefix_error)
print(typed_prefix_error)
")"
  check "valid dashboard group configuration has no validation error" "$(sed -n 1p <<<"$out")" "None"
  check "context prefixes group matching uses item context startswith" "$(sed -n 2p <<<"$out")" "Needs Attention,My Repositories,Ready to Ship,Ready for Something New"
  check "grouped dashboard rows add exactly one hidden group field" "$(sed -n 3p <<<"$out")" "True"
  check "first matching dashboard group wins" "$(sed -n 5p <<<"$out")" "First"
  check "multiple fallback groups are rejected" "$(sed -n 6p <<<"$out")" "dashboard.groups must declare exactly one fallback group"
  check "empty context prefix list is rejected" "$(sed -n 7p <<<"$out")" "dashboard.groups[1].match.contextPrefixes must be a non-empty list of strings"
  check "non-string context prefix is rejected" "$(sed -n 8p <<<"$out")" "dashboard.groups[1].match.contextPrefixes must be a non-empty list of strings"
}
test_dashboard_group_rules_and_rows

test_default_dashboard_groups_items_by_type() {
  local out
  out="$(python3 -c "
$LOAD_CORE
groups, error = m.dashboard_groups({})
items = [
    {'status': 'S', 'context': 'c', 'title': 'PR', 'details': '', 'weight': 1, 'kind': 'pull_request', 'indicators': {'ci': '✓'}, 'actions': []},
    {'status': 'S', 'context': 'c', 'title': 'Issue', 'details': '', 'weight': 1, 'kind': 'issue', 'indicators': {'state': 'OPEN'}, 'actions': []},
    {'status': 'S', 'context': 'c', 'title': 'Reminder', 'details': '', 'weight': 1, 'kind': 'reminder', 'indicators': {'due': '×'}, 'actions': []},
    {'status': 'S', 'context': 'c', 'title': 'Event', 'details': '', 'weight': 1, 'kind': 'event', 'actions': []},
]
rows = m.render_grouped_dashboard_rows(items, groups)
print(error)
print([group['name'] for group in groups])
print([row.rpartition(chr(9))[2] for row in rows])
")"
  check "dashboard uses typed groups when no groups configuration exists" \
    "$(sed -n 1p <<<"$out")" "None"
  check "default dashboard groups define pull request, issue, reminder, event, and fallback sections" \
    "$(sed -n 2p <<<"$out")" "['Pull Requests', 'Issues', 'Reminders', 'Events', 'Other']"
  check "default dashboard groups route each built-in item type to its own section" \
    "$(sed -n 3p <<<"$out")" "['Pull Requests', 'Issues', 'Reminders', 'Events']"
}
test_default_dashboard_groups_items_by_type

test_curses_group_presenter_scopes_rows() {
  local out
  out="$(python3 -c "
$LOAD_DASHBOARD
class Child:
    def __init__(self):
        self.calls = []
    def push_snapshot(self, rows, pending):
        self.calls.append((rows, pending))

presenter = d.CursesGroupPresenter(['Needs Attention', 'Other'])
child = Child()
presenter._active_group = 'Needs Attention'
presenter._active_presenter = child
presenter.push_snapshot([
    'needs' + chr(9) + 'blob' + chr(9) + 'o' + chr(9) + 'hint' + chr(9) + 'Needs Attention',
    'other' + chr(9) + 'blob' + chr(9) + 'o' + chr(9) + 'hint' + chr(9) + 'Other',
], ['github'])
print(child.calls)
")"
  check "curses group presenter forwards only the selected group's rows to its terminal list" \
    "$(sed -n 1p <<<"$out")" "[(['needs\tblob\to\thint'], ['github'])]"
}
test_curses_group_presenter_scopes_rows

test_curses_presenter_reopens_group_after_action_or_refresh_instead_of_overview() {
  local out
  out="$(python3 -c "
$LOAD_DASHBOARD
import time

class FakeScreen:
    def __init__(self, keys):
        self._keys = list(keys)
    def keypad(self, v):
        pass
    def timeout(self, v):
        pass
    def getmaxyx(self):
        return (24, 80)
    def erase(self):
        pass
    def addnstr(self, *a, **k):
        pass
    def refresh(self):
        pass
    def getch(self):
        return self._keys.pop(0) if self._keys else -1

presenter = d.CursesGroupPresenter(['A', 'B'])
presenter._rows = [
    'a-item' + chr(9) + 'blob' + chr(9) + 'o' + chr(9) + 'hint' + chr(9) + 'A',
    'b-item' + chr(9) + 'blob' + chr(9) + 'o' + chr(9) + 'hint' + chr(9) + 'B',
]
deadline = time.monotonic() + 5

# Shared FakeScreen drives both outer (overview) and inner (group) loops.
# Key sequence: Enter(10) opens group A, 'o'(111) dispatches action,
# timeout(-1) triggers snapshot check then Esc(27) exits group,
# Esc(27) quits overview.
screen = FakeScreen([10, 111, -1, 27, 27])

# 1) Enter on the overview's first row opens 'A' and dispatches 'o'.
result1 = presenter._run(screen, deadline)
reopen_after_action = presenter._reopen_group
# 2) The next relaunch (e.g. a periodic refresh timeout) reopens 'A'
#    directly -- no keypress needed to pick it again.
result2 = presenter._run(screen, deadline)
reopen_after_refresh = presenter._reopen_group
# 3) An explicit Esc inside the reopened group falls through to the
#    overview, which then consumes a real keypress (Esc) to quit.
result3 = presenter._run(screen, deadline)

print((result1.key, result1.row))
print(reopen_after_action)
print((result2.key, result2.row))
print(reopen_after_refresh)
print((result3.key, result3.row))
")"
  check "the dispatched action's key/row surface unchanged" \
    "$(sed -n 1p <<<"$out")" "('o', 'a-item\tblob\to\thint')"
  check "acting on an item remembers its group instead of resetting to the overview" \
    "$(sed -n 2p <<<"$out")" "A"
  check "the next relaunch reopens the same group with no overview keypress" \
    "$(sed -n 3p <<<"$out")" "('', '')"
  check "a periodic-refresh relaunch inside a group keeps remembering it too" \
    "$(sed -n 4p <<<"$out")" "None"
  check "explicit Esc inside the group still returns to the overview" \
    "$(sed -n 5p <<<"$out")" "(None, '')"
}
test_curses_presenter_reopens_group_after_action_or_refresh_instead_of_overview

test_dashboard_startup_handles_curses_error() {
  local out
  out="$(python3 -c "
$LOAD_CORE
import contextlib
import io


class FailingController:
    def __init__(self, *args, **kwargs):
        pass

    def run(self, refresh_interval):
        raise m.curses.error('terminal unavailable')


m.load_config = lambda: {'plugins': ['x']}
m.load_plugin = lambda name: object()
m.dashboard.DashboardController = FailingController
stderr = io.StringIO()
try:
    with contextlib.redirect_stderr(stderr):
        m.run_dashboard()
except SystemExit as error:
    print(error.code)
print(stderr.getvalue().strip())
")"
  check "dashboard startup handles curses setup errors without a traceback" "$(sed -n 1p <<<"$out")" "1"
  check "dashboard startup reports the curses setup error" \
    "$(sed -n 2p <<<"$out")" "attention: could not start the interactive dashboard: terminal unavailable"
}
test_dashboard_startup_handles_curses_error

# ---------------------------------------------------------------------------
echo
echo "== --help =="

check "attention --help mentions Usage" \
  "$(python3 "$ATTENTION" --help | grep -c Usage)" "1"

# ---------------------------------------------------------------------------
echo
echo "== summary: $pass passed, $fail failed =="
[ "$fail" -eq 0 ]
