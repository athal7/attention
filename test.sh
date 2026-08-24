#!/usr/bin/env bash
# Regression tests for the `attention` CLI's plugin architecture. Plain
# bash, no bats. Stubs gh/remindctl/ical/fzf/aoe-cmd/lumen/open/pbcopy so
# the script runs against fixed fixture data instead of live system
# state, and writes a JSON config file at a temp $XDG_CONFIG_HOME instead
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

# blob_for <<<'[{"key": "alt-o", ...}, ...]' -> base64(JSON) of that exact
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

    def launch(self, expect_keys, header):
        with self._lock:
            self.launch_count += 1
            self.calls.append(('launch', list(expect_keys), header))
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
{"plugins": ["github"], "codeDir": "$FAKE_CODE_DIR", "github": {"actions": [{"key": "alt-s", "label": "session", "background": true, "command": ["aoe-cmd", "-d", "{repo_path}"]}]}}
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
session = next(a for a in actions if a.get('key') == 'alt-s')
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
        "actions": [{"key": "alt-z", "label": "zap", "primary": True, "payload": {"msg": "zapped!"}}],
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
  act_out="$(HOME="$TEST_HOME" XDG_CONFIG_HOME="$XDG_CONFIG" python3 "$ATTENTION" act "alt-z" "$line")"
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
      "actions": [
        {"key": "alt-o", "label": "open", "primary": true, "command": ["open", "{link}"]},
        {"key": "alt-s", "label": "session", "background": true, "command": ["my-session-cli", "-n", "{num}"]}
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

  : > "$GENERIC_OPEN_LOG"
  HOME="$TEST_HOME" XDG_CONFIG_HOME="$XDG_CONFIG" PATH="$GENERIC_BIN:$PATH" python3 "$ATTENTION" act "alt-o" "$first_line" >/dev/null 2>&1
  check "action command template substitutes the record's field before dispatch" \
    "$(cat "$GENERIC_OPEN_LOG")" "https://example.com/7"

  : > "$GENERIC_SESSION_LOG"
  HOME="$TEST_HOME" XDG_CONFIG_HOME="$XDG_CONFIG" PATH="$GENERIC_BIN:$PATH" python3 "$ATTENTION" act "alt-s" "$first_line" >/dev/null 2>&1
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
            {'key': 'alt-s', 'label': 'session', 'background': True, 'command': ['my-session', '-d', '{repo_path}', '-n', '{slug}']},
            {'key': 'alt-l', 'label': 'lumen', 'command': ['my-lumen', '{url}']}
        ]
    }
}
p._fetch_raw = lambda cfg: [{'number': 42, 'title': 'Fix bug', 'repository': {'nameWithOwner': 'myorg/repo'}, 'url': 'https://github.com/myorg/repo/pull/42', 'type': 'review_request'}]
items = p.fetch(config)
keys = p.declared_action_keys(config)
print(','.join(keys))
print(json.dumps([a['key'] for a in items[0]['actions']]))
print(json.dumps(items[0]['actions'][5]['payload']['command']))
")"
  check "github declared_action_keys includes configured keys" \
    "$(sed -n 1p <<<"$gh_out")" "alt-o,alt-a,alt-m,alt-c,alt-g,alt-s,alt-l"
  check "github fetch attaches configured actions" \
    "$(sed -n 2p <<<"$gh_out")" '["alt-o", "alt-a", "alt-m", "alt-c", "alt-g", "alt-s", "alt-l"]'
  check "github fetch resolves template in configured action command" \
    "$(sed -n 3p <<<"$gh_out")" '["my-session", "-d", "/tmp/repo/repo", "-n", "fix-bug"]'
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
    {'key': 'alt-s', 'label': 'session', 'command': ['run', '-m', '{input}'], 'input': {'prompt': 'Msg', 'default': 'Work on issue {id}'}},
    {'key': 'alt-p', 'label': 'prio', 'command': ['run', '--prio', '{input}'], 'input': {'prompt': 'Priority', 'choices': ['p0', 'p1']}},
    {'key': 'alt-s2', 'label': 'multi', 'command': ['run', '--agent', '{input.tool}', '--msg', '{input.command}'], 'inputs': [
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
        {"key": "alt-t", "label": "text", "command": ["record-args", "--msg", "{input}"], "input": {"prompt": "Message", "default": "default-msg-{num}"}},
        {"key": "alt-c", "label": "choice", "command": ["record-args", "--prio", "{input}"], "input": {"prompt": "Priority", "choices": ["low", "high"]}}
      ]
    }
  }
}
JSON
  local line
  line="$(HOME="$TEST_HOME" XDG_CONFIG_HOME="$XDG_CONFIG" PATH="$GENERIC_BIN:$INPUT_BIN:$PATH" python3 "$ATTENTION" list | grep 'GENERICTEST-first')"

  : > "$INPUT_LOG"
  printf 'typed message\n' | HOME="$TEST_HOME" XDG_CONFIG_HOME="$XDG_CONFIG" PATH="$GENERIC_BIN:$INPUT_BIN:$PATH" python3 "$ATTENTION" act "alt-t" "$line" >/dev/null 2>&1
  check "generic provider text input reaches the command" "$(cat "$INPUT_LOG")" '--msg typed message'

  : > "$INPUT_LOG"
  printf '\n' | HOME="$TEST_HOME" XDG_CONFIG_HOME="$XDG_CONFIG" PATH="$GENERIC_BIN:$INPUT_BIN:$PATH" python3 "$ATTENTION" act "alt-t" "$line" >/dev/null 2>&1
  check "generic provider empty input uses the record-resolved default" "$(cat "$INPUT_LOG")" '--msg default-msg-7'

  : > "$INPUT_LOG"
  printf '2\n' | HOME="$TEST_HOME" XDG_CONFIG_HOME="$XDG_CONFIG" PATH="$GENERIC_BIN:$INPUT_BIN:$PATH" python3 "$ATTENTION" act "alt-c" "$line" >/dev/null 2>&1
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
        "actions": [{"key": "alt-z", "label": "zap", "payload": {}}],
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

test_work_in_progress_marker() {
  local row marked_row cleared_row
  row="$(HOME="$TEST_HOME" XDG_CONFIG_HOME="$XDG_CONFIG" XDG_STATE_HOME="$WORK/state" python3 "$ATTENTION" list | grep 'Deprio item')"
  HOME="$TEST_HOME" XDG_CONFIG_HOME="$XDG_CONFIG" XDG_STATE_HOME="$WORK/state" python3 "$ATTENTION" act "alt-w" "$row" >/dev/null
  marked_row="$(HOME="$TEST_HOME" XDG_CONFIG_HOME="$XDG_CONFIG" XDG_STATE_HOME="$WORK/state" python3 "$ATTENTION" list | grep 'Deprio item')"
  case "$marked_row" in
    *"WORK IN PROGRESS"*) ok "work-in-progress action persists and marks the item in the next dashboard snapshot" ;;
    *) bad "work-in-progress action persists and marks the item in the next dashboard snapshot (got: $marked_row)" ;;
  esac

  HOME="$TEST_HOME" XDG_CONFIG_HOME="$XDG_CONFIG" XDG_STATE_HOME="$WORK/state" python3 "$ATTENTION" act "alt-w" "$marked_row" >/dev/null
  cleared_row="$(HOME="$TEST_HOME" XDG_CONFIG_HOME="$XDG_CONFIG" XDG_STATE_HOME="$WORK/state" python3 "$ATTENTION" list | grep 'Deprio item')"
  case "$cleared_row" in
    *"WORK IN PROGRESS"*) bad "work-in-progress action clears the persisted mark (got: $cleared_row)" ;;
    *) ok "work-in-progress action clears the persisted mark" ;;
  esac
}
test_work_in_progress_marker

test_work_in_progress_reliability() {
  local out
  out="$(python3 -c "
$LOAD_CORE
import multiprocessing
import os
import tempfile

state_dir = tempfile.mkdtemp()
os.environ['XDG_STATE_HOME'] = state_dir
m._wip_items = None
item = {
    'status': 'OPEN', 'context': 'generic', 'title': 'No ID', 'details': '', 'weight': 1,
    'id': '', 'actions': [{'key': 'alt-w', 'label': 'custom action', 'payload': {}}],
    '_plugin': 'generic',
}
snapshot = m.build_snapshot({'generic': [item]})
print([(action['key'], action['label']) for action in snapshot[0]['actions']])

def mark(item_id):
    m._wip_items = None
    m.toggle_wip_item(item_id)

context = multiprocessing.get_context('fork')
first = context.Process(target=mark, args=('first',))
second = context.Process(target=mark, args=('second',))
first.start()
second.start()
first.join()
second.join()
m._wip_items = None
print(sorted(m.get_wip_items()))

blocked_state_home = tempfile.NamedTemporaryFile(delete=False)
blocked_state_home.close()
os.environ['XDG_STATE_HOME'] = blocked_state_home.name
m._wip_items = None
m.act('alt-w', m.render_rows(snapshot)[0])
")"
  check "WIP supports items without an explicit id and remaps a colliding alt-w action" \
    "$(sed -n 1p <<<"$out")" "[('alt-w', 'work in progress'), ('W', 'custom action (remapped)')]"
  check "concurrent WIP updates preserve both item markers" "$(sed -n 2p <<<"$out")" "['first', 'second']"
  case "$(sed -n 3p <<<"$out")" in
    "Action failed: "*) ok "WIP persistence failure stays in the action error boundary" ;;
    *) bad "WIP persistence failure stays in the action error boundary (got: $(sed -n 3p <<<"$out"))" ;;
  esac
}
test_work_in_progress_reliability

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
    'p1': [{'status': 'S1', 'context': 'c1', 'title': 'Fix ABC-123 today', 'details': '', 'weight': 5, 'id': '', 'absorb_note': '', 'created_at': '', 'actions': [{'key': 'alt-o', 'label': 'open', 'primary': False, 'payload': {}}], '_plugin': 'p1'}],
    'p2': [{'status': 'S2', 'context': 'c2', 'title': 'Ticket', 'details': '', 'weight': 10, 'id': 'ABC-123', 'absorb_note': '', 'created_at': '', 'actions': [{'key': 'alt-s', 'label': 'session', 'primary': False, 'payload': {}}], '_plugin': 'p2'}],
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
item = {'status': 'S', 'context': 'ctx', 'title': 't', 'details': 'd', 'weight': 10, 'actions': [{'key': 'alt-o', 'label': 123}]}
try:
    m.validate_and_normalize_item(item, 'badplugin')
except ValueError as e:
    print('err:', e)
" 2>/dev/null || true)"
  check "detects wrong type for action label" "$err" "err: plugin 'badplugin' returned a malformed item: action 'label' must be of type str, got int"

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
item = {'status': 'S', 'context': 'ctx', 'title': 't', 'details': 'd', 'weight': 10, 'actions': [{'key': 'alt-o', 'label': 'o'}]}
m.validate_and_normalize_item(item, 'goodplugin')
print(json.dumps(item))
")"
  check "defaults optional item fields (id, absorb_note, created_at) to empty string" \
    "$(python3 -c "import json,sys; item=json.loads(sys.argv[1]); print(repr(item['id']), repr(item['absorb_note']), repr(item['created_at']))" "$defaults")" \
    "'' '' ''"
  check "defaults optional action fields (primary=False, payload={})" \
    "$(python3 -c "import json,sys; item=json.loads(sys.argv[1]); print(item['actions'][0]['primary'], item['actions'][0]['payload'])" "$defaults")" \
    "False {}"

  local act_err
  act_err="$(python3 -c "
$LOAD_CORE
line = 'STATUS\t' + m.base64.b64encode(m.json.dumps([{'key': 'alt-o', 'label': 'o', 'payload': 'not-a-dict', '_plugin': 'github'}]).encode()).decode()
m.act('alt-o', line)
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
    *'first: 250'*) ok "assignedIssues raises the page size past the 50-item default" ;;
    *) bad "assignedIssues raises the page size past the 50-item default (got: $out)" ;;
  esac
  case "$out" in
    *'"context": "Backend"'*'"id": "ABC-1"'*)
      ok "fetch() returns the issue with id=identifier and project as CONTEXT" ;;
    *) bad "fetch() returns the issue with id=identifier and project as CONTEXT (got: $out)" ;;
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
        {'key': 'alt-o', 'label': 'open', 'primary': True, 'payload': {}, '_plugin': 'github'},
        {'key': 'alt-c', 'label': 'comment', 'payload': {}, '_plugin': 'github'},
    ],
}
lin_item = {
    'status': 'IN PROGRESS', 'context': 'Backend', 'title': 'Fix it', 'details': '',
    'weight': 80, 'id': 'ABC-1', 'absorb_note': 'Linear ABC-1: IN PROGRESS',
    'actions': [
        {'key': 'alt-o', 'label': 'open', 'primary': True, 'payload': {}, '_plugin': 'linear'},
        {'key': 'alt-c', 'label': 'comment', 'payload': {}, '_plugin': 'linear'},
        {'key': 'alt-t', 'label': 'transition', 'payload': {}, '_plugin': 'linear'},
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
    *'"key": "O"'*'"label": "open (linked)"'*) ok "colliding alt-o key transforms to uppercase O, labeled (linked)" ;;
    *) bad "colliding alt-o key transforms to uppercase O (got: $out)" ;;
  esac
  case "$out" in
    *'"key": "C"'*) ok "colliding alt-c key transforms to uppercase C" ;;
    *) bad "colliding alt-c key transforms to uppercase C (got: $out)" ;;
  esac
  case "$out" in
    *'"key": "alt-t"'*'"label": "transition (linked)"'*)
      ok "non-colliding alt-t key is kept as-is, still labeled (linked)" ;;
    *) bad "non-colliding alt-t key is kept as-is (got: $out)" ;;
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
    'actions': [{'key': 'alt-y', 'label': 'yank', 'payload': {}, '_plugin': 'calendar'}],
}
rem_item = {
    'status': 'PENDING', 'context': 'Personal', 'title': 'Book babysitter for Team Dinner', 'details': '',
    'weight': 15, 'id': 'r1', 'absorb_note': 'Reminder: Book babysitter for Team Dinner',
    'actions': [{'key': 'alt-x', 'label': 'complete', 'payload': {'id': 'r1'}, '_plugin': 'reminders'}],
}
merged = m.merge_cross_links([cal_item, rem_item])
import json
print(json.dumps(merged))
")"
  check "a title-substring match (reminder title contains event title) merges to one item" \
    "$(python3 -c "import json,sys; print(len(json.loads(sys.argv[1])))" "$out")" "1"
  case "$out" in
    *'"key": "alt-x"'*'"label": "complete (linked)"'*)
      ok "reminder's complete action carries over unchanged (no collision with CAL's alt-y)" ;;
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
    {'actions': [{'key': 'alt-o', 'label': 'x'}, {'key': 'alt-s', 'label': 'x'}]},
    {'actions': [{'key': 'alt-o', 'label': 'x'}, {'key': 'alt-x', 'label': 'x'}]},
]
print(','.join(m.expect_keys_for(items)))
")"
  check "expect_keys_for() is the de-duped union of every action key actually present" "$out" "alt-o,alt-s,alt-x"
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

check "hint_for_actions() renders '⌥key label' pairs, alt- stripped to the option symbol" \
  "$(python3 -c "
$LOAD_CORE
print(m.hint_for_actions([{'key': 'alt-o', 'label': 'open'}, {'key': 'O', 'label': 'open linear'}]))")" \
  "⌥o open  O open linear"

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
items = [{'status': 'REVIEW REQUESTED', 'context': 'myorg/kb', 'title': 'Fix the login bug', 'details': '', 'weight': 90, '_plugin': 'github', 'actions': [{'key': 'alt-o', 'label': 'open', 'primary': True, 'payload': {}}]}]
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
  check "render_rows() field 2 (actions blob) is unchanged" "$(sed -n 4p <<<"$out")" "alt-o"
  check "render_rows() field 3 (hint) is unchanged" "$(sed -n 5p <<<"$out")" "⌥o open"
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
        {'key': 'alt-o', 'label': 'open', 'primary': True, 'payload': {}},
        {'key': 'alt-s', 'label': 'session', 'payload': {}},
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
  check "render_dashboard_rows() field 3 is the comma-joined CSV of this item's own action keys" "$(sed -n 4p <<<"$out")" "alt-o,alt-s"
  check "render_dashboard_rows() field 4 is the same hint text render_rows() puts in field 3" "$(sed -n 5p <<<"$out")" "⌥o open  ⌥s session"
  check "render_dashboard_rows() shares render_rows()'s hidden actions-blob field (field 2)" "$(sed -n 6p <<<"$out")" "True"
}
test_render_dashboard_rows_omits_status_and_retains_action_fields

test_dashboard_action_hints_wrap_at_footer_width() {
  local out
  out="$(python3 -c "
$LOAD_CORE
$LOAD_DASHBOARD
import shlex
import subprocess
import shutil

class DummySize:
    columns = 40

shutil.get_terminal_size = lambda: DummySize()
actions = [
    {'key': 'alt-o', 'label': 'open'},
    {'key': 'alt-a', 'label': 'approve'},
    {'key': 'alt-m', 'label': 'merge'},
    {'key': 'alt-c', 'label': 'comment'},
    {'key': 'alt-g', 'label': 'label'},
]
print(repr(m._dashboard_hint_for_actions(actions)))
cmd = d.build_footer_transform().replace('{4}', shlex.quote(m._dashboard_hint_for_actions(actions)))
print(repr(subprocess.run(['sh', '-c', cmd], capture_output=True, text=True, check=True).stdout))
")"
  check "dashboard action hints use explicit footer lines that fit a 40-column terminal" \
    "$(sed -n 1p <<<"$out")" "'⌥o open  ⌥a approve  ⌥m merge\\x0b⌥c comment  ⌥g label'"
  check "the fzf footer transform turns each dashboard action-hint line into a visible footer row" \
    "$(sed -n 2p <<<"$out")" "'⌥o open  ⌥a approve  ⌥m merge\n⌥c comment  ⌥g label\n'"
}
test_dashboard_action_hints_wrap_at_footer_width

DECL_KEYS_A_PLUGIN="$WORK/decl_keys_a_plugin.py"
cat > "$DECL_KEYS_A_PLUGIN" <<'PY'
ACTION_KEYS = ["alt-o", "alt-b"]
def fetch(config):
    raise AssertionError("declared_action_keys() must never call fetch()")
def act(key, payload):
    pass
PY

DECL_KEYS_B_PLUGIN="$WORK/decl_keys_b_plugin.py"
cat > "$DECL_KEYS_B_PLUGIN" <<'PY'
def declared_action_keys(config):
    return ["alt-q"]
def fetch(config):
    raise AssertionError("declared_action_keys() must never call fetch()")
def act(key, payload):
    pass
PY

write_config <<JSON
{"plugins": ["$DECL_KEYS_A_PLUGIN", "$DECL_KEYS_B_PLUGIN"]}
JSON

test_declared_action_keys_unions_and_expands_fallbacks() {
  local out
  out="$(HOME="$TEST_HOME" XDG_CONFIG_HOME="$XDG_CONFIG" python3 -c "
$LOAD_CORE
print(','.join(m.declared_action_keys(m.load_config())))
")"
  check "declared_action_keys(): includes the core WIP key, de-duped plugin keys, uppercase fallbacks, and digit fallbacks without fetch calls" \
    "$out" "alt-w,alt-o,alt-b,alt-q,W,O,B,Q,1,2,3,4,5,6,7,8,9"
}
test_declared_action_keys_unions_and_expands_fallbacks

NO_HOOK_PLUGIN="$WORK/no_hook_plugin.py"
cat > "$NO_HOOK_PLUGIN" <<'PY'
def fetch(config):
    return []
def act(key, payload):
    pass
PY

write_config <<JSON
{"plugins": ["$NO_HOOK_PLUGIN"]}
JSON

test_declared_action_keys_missing_hook_warns_once_per_run() {
  local out warnings keys
  out="$(HOME="$TEST_HOME" XDG_CONFIG_HOME="$XDG_CONFIG" python3 -c "
import contextlib, io
$LOAD_CORE
config = m.load_config()
buf = io.StringIO()
with contextlib.redirect_stderr(buf):
    keys1 = m.declared_action_keys(config)
    keys2 = m.declared_action_keys(config)
print(','.join(keys1))
warnings = buf.getvalue().strip().splitlines()
print(len(warnings))
print('$NO_HOOK_PLUGIN' in warnings[0] if warnings else '')
" 2>&1)"
  keys="$(sed -n 1p <<<"$out")"
  warnings="$(sed -n 2p <<<"$out")"
  check "a plugin with neither ACTION_KEYS nor declared_action_keys() contributes no keys of its own (only the core WIP and digit fallback universe remains)" "$keys" "alt-w,W,1,2,3,4,5,6,7,8,9"
  check "exactly one stderr warning fires per dashboard run (not per call), regardless of how many times declared_action_keys() runs" "$warnings" "1"
  check "the warning names the plugin lacking the hook" "$(sed -n 3p <<<"$out")" "True"
}
test_declared_action_keys_missing_hook_warns_once_per_run

BROKEN_HOOK_PLUGIN="$WORK/broken_hook_plugin.py"
cat > "$BROKEN_HOOK_PLUGIN" <<'PY'
def declared_action_keys(config):
    return config["generic"]["boom"]
def fetch(config):
    return []
def act(key, payload):
    pass
PY

MALFORMED_KEYS_PLUGIN="$WORK/malformed_keys_plugin.py"
cat > "$MALFORMED_KEYS_PLUGIN" <<'PY'
ACTION_KEYS = "alt-o"
def fetch(config):
    return []
def act(key, payload):
    pass
PY

write_config <<JSON
{"plugins": ["$BROKEN_HOOK_PLUGIN", "$MALFORMED_KEYS_PLUGIN", "$DECL_KEYS_A_PLUGIN"]}
JSON

test_declared_action_keys_isolates_broken_plugin_hooks() {
  local out keys warnings
  out="$(HOME="$TEST_HOME" XDG_CONFIG_HOME="$XDG_CONFIG" python3 -c "
import contextlib, io
$LOAD_CORE
config = m.load_config()
buf = io.StringIO()
with contextlib.redirect_stderr(buf):
    keys = m.declared_action_keys(config)
print(','.join(keys))
warnings = buf.getvalue().strip().splitlines()
print(len(warnings))
print('$BROKEN_HOOK_PLUGIN' in warnings[0] if len(warnings) > 0 else '')
print('$MALFORMED_KEYS_PLUGIN' in warnings[1] if len(warnings) > 1 else '')
" 2>&1)"
  keys="$(sed -n 1p <<<"$out")"
  warnings="$(sed -n 2p <<<"$out")"
  check "a plugin whose declared_action_keys() raises contributes no keys, and a plugin whose ACTION_KEYS is a bare string ('alt-o') also contributes no keys -- neither aborts the dashboard, the core and good plugin keys still land" \
    "$keys" "alt-w,alt-o,alt-b,W,O,B,1,2,3,4,5,6,7,8,9"
  check "declared_action_keys() warns exactly once for the raising hook and once for the malformed-string ACTION_KEYS (two isolated plugins, two warnings)" \
    "$warnings" "2"
  check "the first warning names the plugin whose hook raised" "$(sed -n 3p <<<"$out")" "True"
  check "the second warning names the plugin whose ACTION_KEYS was a bare string" "$(sed -n 4p <<<"$out")" "True"
}
test_declared_action_keys_isolates_broken_plugin_hooks

MIXED_KEYS_PLUGIN="$WORK/mixed_keys_plugin.py"
cat > "$MIXED_KEYS_PLUGIN" <<'PY'
ACTION_KEYS = ["alt-z", 5]
def fetch(config):
    return []
def act(key, payload):
    pass
PY

write_config <<JSON
{"plugins": ["$MIXED_KEYS_PLUGIN", "$DECL_KEYS_A_PLUGIN"]}
JSON

test_declared_action_keys_rejects_mixed_type_list_and_warns_once() {
  local out keys1 keys2 warnings
  out="$(HOME="$TEST_HOME" XDG_CONFIG_HOME="$XDG_CONFIG" python3 -c "
import contextlib, io
$LOAD_CORE
config = m.load_config()
buf = io.StringIO()
with contextlib.redirect_stderr(buf):
    keys1 = m.declared_action_keys(config)
    keys2 = m.declared_action_keys(config)
print(','.join(keys1))
print(','.join(keys2))
warnings = buf.getvalue().strip().splitlines()
print(len(warnings))
print('$MIXED_KEYS_PLUGIN' in warnings[0] if len(warnings) > 0 else '')
" 2>&1)"
  keys1="$(sed -n 1p <<<"$out")"
  keys2="$(sed -n 2p <<<"$out")"
  warnings="$(sed -n 3p <<<"$out")"
  check "a plugin whose ACTION_KEYS mixes strings with non-strings (['alt-z', 5]) contributes zero keys, not just the non-string entries stripped" \
    "$keys1" "alt-w,alt-o,alt-b,W,O,B,1,2,3,4,5,6,7,8,9"
  check "declared_action_keys() is stable across repeated calls for a mixed-type-list plugin" "$keys2" "$keys1"
  check "a mixed-type ACTION_KEYS list warns exactly once even though declared_action_keys() ran twice" "$warnings" "1"
  check "the warning names the plugin whose ACTION_KEYS was a mixed-type list" "$(sed -n 4p <<<"$out")" "True"
}
test_declared_action_keys_rejects_mixed_type_list_and_warns_once

test_action_keys_constants_match_literal_keys_in_fetch() {
  local plugin expected actual
  for plugin in calendar reminders linear github; do
    expected="$(python3 -c "
$(load_plugin_py $plugin)
print(','.join(sorted(p.ACTION_KEYS)))
")"
    actual="$(python3 -c "
import re
src = open('$REPO_ROOT/sources/${plugin}.py').read()
print(','.join(sorted(set(re.findall(r'\"key\":\s*\"([^\"]+)\"', src)))))
")"
    check "sources/${plugin}.py's ACTION_KEYS matches every literal action key in its source" "$expected" "$actual"
  done
}
test_action_keys_constants_match_literal_keys_in_fetch

test_generic_declared_action_keys_reads_config_only() {
  local out
  out="$(python3 -c "
$(load_plugin_py generic)
config = {
    'generic': {
        'with-actions': {'command': ['irrelevant'], 'actions': [
            {'key': 'alt-o', 'label': 'open'}, {'key': 'alt-s', 'label': 'session'},
        ]},
        'no-actions-key': {'command': ['irrelevant']},
    },
}
print(','.join(p.declared_action_keys(config)))
")"
  check "generic.py's declared_action_keys() reads actions[].key straight from config, de-duped, ignoring a provider with no actions key" \
    "$out" "alt-o,alt-s"
}
test_generic_declared_action_keys_reads_config_only

test_generic_declared_action_keys_guards_malformed_config() {
  local out
  out="$(python3 -c "
$(load_plugin_py generic)
configs = [
    {'generic': ['not', 'a', 'dict']},
    {'generic': 'not-even-a-list'},
    {'generic': {'bad-actions-type': {'command': ['x'], 'actions': 'alt-o'}}},
    {'generic': {'bad-action-entry': {'command': ['x'], 'actions': ['not-a-dict', {'key': 'alt-s', 'label': 'session'}]}}},
]
for config in configs:
    print(','.join(p.declared_action_keys(config)))
")"
  check "declared_action_keys() ignores a non-dict config['generic'] value (list) instead of raising" "$(sed -n 1p <<<"$out")" ""
  check "declared_action_keys() ignores a non-dict config['generic'] value (string) instead of raising" "$(sed -n 2p <<<"$out")" ""
  check "declared_action_keys() ignores a provider whose actions value is a string instead of raising" "$(sed -n 3p <<<"$out")" ""
  check "declared_action_keys() skips a non-dict entry in actions[] but still reads the valid sibling entry" "$(sed -n 4p <<<"$out")" "alt-s"
}
test_generic_declared_action_keys_guards_malformed_config

test_plugins_md_documents_declared_key_character_constraints() {
  local body
  body="$(python3 -c "
src = open('$REPO_ROOT/PLUGINS.md').read()
start = src.index('### Declaring hotkeys for the interactive dashboard')
end = src.index('### Item shape', start)
print(src[start:end])
")"
  case "$body" in
    *"alt-<letter>"*"bare letter/digit"*)
      case "$body" in
        *","*":"*"+"*"("*")"*) ok "PLUGINS.md's declared-hotkeys section documents the plain-fzf-key-token constraint and the exact breaking characters (',', ':', '+', '(', ')')" ;;
        *) bad "PLUGINS.md's declared-hotkeys section documents the plain-fzf-key-token constraint and the exact breaking characters (',', ':', '+', '(', ')')" ;;
      esac
      ;;
    *) bad "PLUGINS.md's declared-hotkeys section documents the plain-fzf-key-token constraint (alt-<letter> or a bare letter/digit)" ;;
  esac
}
test_plugins_md_documents_declared_key_character_constraints



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

GH_OPEN_ACTIONS='[{"key": "alt-o", "label": "open", "primary": true, "payload": {"kind": "open", "url": "https://github.com/myorg/kb/pull/42"}, "_plugin": "github"}]'
GH_FULL_ACTIONS='[
  {"key": "alt-o", "label": "open", "primary": true, "payload": {"kind": "open", "url": "https://github.com/myorg/kb/pull/42"}, "_plugin": "github"},
  {"key": "alt-s", "label": "session", "payload": {"command": ["aoe-cmd", "-d", "/tmp/repo", "-n", "test-pr", "-b", "-w", "test-pr", "Work on issue 42 in this repo"], "background": true}, "_plugin": "github"},
  {"key": "alt-l", "label": "lumen", "payload": {"command": ["lumen", "diff", "--pr", "https://github.com/myorg/kb/pull/42"]}, "_plugin": "github"},
  {"key": "alt-a", "label": "approve", "payload": {"kind": "approve", "id": "42", "url": "https://github.com/myorg/kb/pull/42"}, "_plugin": "github"},
  {"key": "alt-m", "label": "merge", "payload": {"kind": "merge", "id": "42", "url": "https://github.com/myorg/kb/pull/42"}, "_plugin": "github"},
  {"key": "alt-c", "label": "comment", "payload": {"kind": "comment", "id": "42", "url": "https://github.com/myorg/kb/pull/42"}, "_plugin": "github"},
  {"key": "alt-g", "label": "label", "payload": {"kind": "label", "id": "42", "url": "https://github.com/myorg/kb/pull/42"}, "_plugin": "github"},
  {"key": "O", "label": "open (linked)", "primary": false, "payload": {"kind": "open", "url": "https://linear.app/abc/issue/ABC-1"}, "_plugin": "linear"}
]'
REM_ACTIONS='[{"key": "alt-x", "label": "complete", "payload": {"id": "r1"}, "_plugin": "reminders"}]'
CAL_ACTIONS='[{"key": "alt-y", "label": "yank", "payload": {"text": "Team Sync - 10:00 AM"}, "_plugin": "calendar"}]'
CAL_MULTI_ACTIONS='[
  {"key": "alt-y", "label": "yank", "payload": {"text": "Team Sync"}, "_plugin": "calendar"},
  {"key": "alt-x", "label": "complete (linked)", "payload": {"id": "r1"}, "_plugin": "reminders", "_original_key": "alt-x"},
  {"key": "1", "label": "complete (linked)", "payload": {"id": "r2"}, "_plugin": "reminders", "_original_key": "alt-x"}
]'
LIN_ACTIONS='[
  {"key": "alt-o", "label": "open", "primary": true, "payload": {"kind": "open", "url": "https://linear.app/abc/issue/ABC-1"}, "_plugin": "linear"},
  {"key": "alt-s", "label": "session", "payload": {"command": ["aoe-cmd", "-d", ".", "-n", "abc-1", "Work on Linear issue ABC-1"], "background": true}, "_plugin": "linear"}
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
echo "-- alt-key hotkey dispatch invokes the correct downstream command --"

: > "$OPEN_LOG"
run_act "alt-o" "$FIX_GH_LINE"
check "GH alt-o exits 0" "$ACT_RC" "0"
if grep -q 'https://github.com/myorg/kb/pull/42' "$OPEN_LOG"; then
  ok "GH alt-o invokes open with the item URL"
else
  bad "GH alt-o invokes open with the item URL (got: $(cat "$OPEN_LOG"))"
fi

: > "$REMINDCTL_ACT_LOG"
run_act "alt-x" "$FIX_REM_LINE"
check "REM alt-x exits 0" "$ACT_RC" "0"
if grep -q 'complete r1' "$REMINDCTL_ACT_LOG"; then
  ok "REM alt-x invokes remindctl complete on the item's own ID"
else
  bad "REM alt-x invokes remindctl complete on the item's own ID (got: $(cat "$REMINDCTL_ACT_LOG"))"
fi

: > "$PBCOPY_LOG"
run_act "alt-y" "$FIX_CAL_LINE"
check "CAL alt-y exits 0" "$ACT_RC" "0"
if grep -q 'Team Sync' "$PBCOPY_LOG"; then
  ok "CAL alt-y copies the payload's precomputed text to the clipboard"
else
  bad "CAL alt-y copies the payload's precomputed text to the clipboard (got: $(cat "$PBCOPY_LOG"))"
fi

: > "$LUMEN_LOG"
run_act "alt-l" "$FIX_GH_FULL_LINE"
check "GH alt-l exits 0" "$ACT_RC" "0"
if grep -q 'diff --pr https://github.com/myorg/kb/pull/42' "$LUMEN_LOG"; then
  ok "GH alt-l invokes lumen diff --pr with the item URL"
else
  bad "GH alt-l invokes lumen diff --pr with the item URL (got: $(cat "$LUMEN_LOG"))"
fi

: > "$GH_ACT_LOG"
run_act "alt-a" "$FIX_GH_FULL_LINE"
check "GH alt-a exits 0" "$ACT_RC" "0"
if grep -q 'pr review --approve 42 --repo myorg/kb' "$GH_ACT_LOG"; then
  ok "GH alt-a invokes gh pr review --approve"
else
  bad "GH alt-a invokes gh pr review --approve (got: $(cat "$GH_ACT_LOG"))"
fi

: > "$GH_ACT_LOG"
run_act "alt-c" "$FIX_GH_FULL_LINE" "a nice comment
"
check "GH alt-c exits 0" "$ACT_RC" "0"
if grep -q 'issue comment 42 -R myorg/kb -b a nice comment' "$GH_ACT_LOG"; then
  ok "GH alt-c invokes gh issue comment with the entered body"
else
  bad "GH alt-c invokes gh issue comment with the entered body (got: $(cat "$GH_ACT_LOG"))"
fi

: > "$GH_ACT_LOG"
run_act "alt-g" "$FIX_GH_FULL_LINE" "bug
"
check "GH alt-g exits 0" "$ACT_RC" "0"
if grep -q 'issue edit 42 -R myorg/kb --add-label bug' "$GH_ACT_LOG"; then
  ok "GH alt-g invokes gh issue edit --add-label with the entered label"
else
  bad "GH alt-g invokes gh issue edit --add-label with the entered label (got: $(cat "$GH_ACT_LOG"))"
fi

echo
echo "-- CAL multi-reminder overflow: linked reminders keep their own routed key --"

: > "$REMINDCTL_ACT_LOG"
run_act "alt-x" "$FIX_CAL_MULTI_LINE"
check "CAL alt-x (first linked reminder) exits 0" "$ACT_RC" "0"
if grep -q 'complete r1' "$REMINDCTL_ACT_LOG"; then
  ok "CAL alt-x completes the first linked reminder (r1)"
else
  bad "CAL alt-x completes the first linked reminder (r1) (got: $(cat "$REMINDCTL_ACT_LOG"))"
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
echo "-- Linear cross-link plain-uppercase key (O) routes to the linear plugin, not github --"

: > "$OPEN_LOG"
run_act "O" "$FIX_GH_FULL_LINE"
check "GH linked item, key 'O' exits 0" "$ACT_RC" "0"
if grep -q 'https://linear.app/abc/issue/ABC-1' "$OPEN_LOG"; then
  ok "key 'O' opens the linked Linear issue, not the GH item"
else
  bad "key 'O' opens the linked Linear issue, not the GH item (got: $(cat "$OPEN_LOG"))"
fi

: > "$OPEN_LOG"
run_act "O" "$FIX_GH_LINE"
check "GH item with no Linear link, key 'O' exits 0 (no-op, not a crash)" "$ACT_RC" "0"
if [ -s "$OPEN_LOG" ]; then
  bad "key 'O' with no Linear link must not invoke open (got: $(cat "$OPEN_LOG"))"
else
  ok "key 'O' with no Linear link is a no-op (not bound on this row)"
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
run_act "alt-z" "$FIX_GH_LINE"
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
echo "-- merge gate (alt-m): confirm_and_merge runs as a plain input() prompt --"

: > "$GH_ACT_LOG"
run_act "alt-m" "$FIX_GH_FULL_LINE" "y
"
check "merge confirm 'y' exits 0" "$ACT_RC" "0"
if grep -q 'pr merge --squash --delete-branch 42 --repo myorg/kb' "$GH_ACT_LOG"; then
  ok "merge confirm 'y' invokes gh pr merge --squash --delete-branch"
else
  bad "merge confirm 'y' invokes gh pr merge --squash --delete-branch (got: $(cat "$GH_ACT_LOG"))"
fi

: > "$GH_ACT_LOG"
run_act "alt-m" "$FIX_GH_FULL_LINE" "n
"
check "merge confirm 'n' exits 0" "$ACT_RC" "0"
if [ -s "$GH_ACT_LOG" ]; then
  bad "merge confirm 'n' must not invoke gh pr merge (got: $(cat "$GH_ACT_LOG"))"
else
  ok "merge confirm 'n' does not invoke gh pr merge"
fi

: > "$GH_ACT_LOG"
run_act "alt-m" "$FIX_GH_FULL_LINE" ""
check "merge confirm EOF (no stdin) exits 0, canceled gracefully" "$ACT_RC" "0"
if [ -s "$GH_ACT_LOG" ]; then
  bad "merge confirm EOF must not invoke gh pr merge (got: $(cat "$GH_ACT_LOG"))"
else
  ok "merge confirm EOF does not invoke gh pr merge"
fi

echo
echo "-- session dispatch (alt-s): backgrounded (doesn't block the caller) --"

wait_for_aoe_cmd_log() {
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    [ -s "$AOE_CMD_LOG" ] && return 0
    sleep 0.3
  done
  return 1
}

: > "$AOE_CMD_LOG"
start_ts=$(date +%s)
run_act "alt-s" "$FIX_GH_FULL_LINE"
elapsed=$(( $(date +%s) - start_ts ))
check "GH alt-s exits 0" "$ACT_RC" "0"
if [ "$elapsed" -le 1 ]; then
  ok "GH alt-s returns without waiting for the dispatched process (${elapsed}s; stub sleeps 1s)"
else
  bad "GH alt-s returns without waiting for the dispatched process (took ${elapsed}s; stub sleeps 1s)"
fi
wait_for_aoe_cmd_log
if grep -q -- '-n test-pr -b -w test-pr ' "$AOE_CMD_LOG"; then
  ok "GH alt-s names session/worktree branch from the title slug 'test-pr' (got: $(cat "$AOE_CMD_LOG"))"
else
  bad "GH alt-s names session/worktree branch from the title slug 'test-pr' (got: $(cat "$AOE_CMD_LOG"))"
fi

: > "$AOE_CMD_LOG"
run_act "alt-s" "$FIX_LIN_LINE"
check "LIN alt-s exits 0" "$ACT_RC" "0"
wait_for_aoe_cmd_log
if grep -q -- '-n abc-1 ' "$AOE_CMD_LOG"; then
  ok "LIN alt-s names the session from the issue identifier"
else
  bad "LIN alt-s names the session from the issue identifier (got: $(cat "$AOE_CMD_LOG"))"
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
end = src.index('class UnixHTTPConnection(')
print(src[start:end])
")"
  case "$controller_src" in
    *"subprocess"*) bad "DashboardController itself never references subprocess (only FzfPresenter does)" ;;
    *) ok "DashboardController itself never references subprocess (only FzfPresenter does)" ;;
  esac
  case "$controller_src" in
    *"socket"*) bad "DashboardController itself never references socket (only FzfPresenter does)" ;;
    *) ok "DashboardController itself never references socket (only FzfPresenter does)" ;;
  esac
  case "$controller_src" in
    *"http.client"*) bad "DashboardController itself never references http.client (only FzfPresenter does)" ;;
    *) ok "DashboardController itself never references http.client (only FzfPresenter does)" ;;
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
    ['a', 'b'], presenter, ['alt-o'],
    fetch_plugin=lambda name: [],
    build_snapshot=flatten_build_snapshot,
    render_rows=titles_render_rows,
    act=lambda key, row: None,
)
print(isinstance(controller, d.DashboardController))
print(controller.plugin_names)
print(controller.expect_keys)
")"
  check "DashboardController constructs from plugin_names/presenter/expect_keys + injected fetch_plugin/build_snapshot/render_rows/act" \
    "$(sed -n 1p <<<"$out")" "True"
  check "DashboardController keeps the configured plugin_names" "$(sed -n 2p <<<"$out")" "['a', 'b']"
  check "DashboardController keeps the fixed expect_keys" "$(sed -n 3p <<<"$out")" "['alt-o']"
}
test_dashboard_controller_constructs_from_injected_callables

test_fake_presenter_records_ordered_pushes_and_blocks_wait_for_exit() {
  local out
  out="$(python3 -c "
$LOAD_DASHBOARD
$DASHBOARD_FIXTURES
presenter = FakePresenter()
presenter.launch(['alt-o'], 'header')
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
presenter.send_result('alt-o', 'row1')
th.join(timeout=5)
print(result_holder[0].key, repr(result_holder[0].row))
")"
  check "FakePresenter.push_snapshot() records every call's rows in order" \
    "$(sed -n 1p <<<"$out")" "[['row1'], ['row1', 'row2']]"
  check "FakePresenter.wait_for_exit() blocks until the test supplies a result (no wall-clock sleep)" \
    "$(sed -n 2p <<<"$out")" "True"
  check "FakePresenter.wait_for_exit() returns exactly the result the test sent" \
    "$(sed -n 3p <<<"$out")" "alt-o 'row1'"
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
    ['solo'], presenter, [],
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
  check "a DashboardController built entirely from fakes never imports attention_core, real plugin loading, config, gh, or fzf" \
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
    ['a', 'b'], presenter, [],
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
    ['a', 'b', 'c'], presenter, [],
    fetch_plugin=fetch_plugin, build_snapshot=flatten_build_snapshot,
    render_rows=titles_render_rows, act=lambda key, row: None,
)
th = threading.Thread(target=controller.run, args=(3600,))
th.start()
presenter.wait_for_launch_count(1, timeout=5)
initial_header = presenter.launch_calls()[0][2]
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
print(initial_header)
print(pending_after_a)
print(pending_after_b)
print(pending_after_c)
")"
  check "initial launch header names every configured provider before any has finished" \
    "$(sed -n 1p <<<"$out")" "Loading: a, b, c…"
  check "pending after 'a' finishes names exactly the still-unfinished providers" "$(sed -n 2p <<<"$out")" "['b', 'c']"
  check "pending after 'b' also finishes shrinks to just the remaining provider" "$(sed -n 3p <<<"$out")" "['c']"
  check "pending is empty once every provider has finished" "$(sed -n 4p <<<"$out")" "[]"
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
    ['a', 'b'], presenter, [],
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
           'actions': [{'key': 'alt-o', 'label': 'x', 'primary': True, '_item_id': 'a'}]}],
    'b': [{'status': 'S', 'context': 'c', 'title': 'B', 'details': '', 'weight': 1, 'id': 'b'}],
}
calls = CallLog()
fetch_plugin = gated_fetch_plugin(items_by_name, {'b': gate_b}, calls)
acted = []
presenter = FakePresenter()
controller = d.DashboardController(
    ['a', 'b'], presenter, [],
    fetch_plugin=fetch_plugin, build_snapshot=flatten_build_snapshot,
    render_rows=blob_render_rows, act=lambda key, row: acted.append((key, row)),
)
th = threading.Thread(target=controller.run, args=(3600,))
th.start()
calls.wait_for_count(2, timeout=5)
presenter.wait_for_push_count(2, timeout=5)
row_for_a = presenter.push_calls()[-1][1][0]
presenter.send_result('alt-o', row_for_a)
presenter.wait_for_launch_count(2, timeout=5)
calls_after_accept = sorted(calls.snapshot())
pushes_before_release = len(presenter.push_calls())
gate_b.set()
presenter.wait_for_push_count(pushes_before_release + 1, timeout=5)
pending_after_b = presenter.push_calls()[-1][2]
titles_after_b = [r.split(chr(9))[0] for r in presenter.push_calls()[-1][1]]
presenter.send_result('', '')
th.join(timeout=5)
print(len(acted))
print(calls_after_accept)
print(pending_after_b)
print(titles_after_b)
")"
  check "the accepted hotkey dispatched through the injected act() callable" "$(sed -n 1p <<<"$out")" "1"
  check "an accept mid-round relaunches the presenter but submits no second round's fetch_plugin calls" \
    "$(sed -n 2p <<<"$out")" "['a', 'b']"
  check "the gated plugin's completion still reaches the presenter after the relaunch" "$(sed -n 3p <<<"$out")" "[]"
  check "the acted-on item is deprioritized below the newly-arrived item in the very next snapshot" \
    "$(sed -n 4p <<<"$out")" "['B', 'A']"
}
test_timeout_and_accept_relaunch_presenter_without_new_fetch_calls_mid_round

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
    ['a', 'b'], presenter, [],
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
    ['a', 'b'], presenter, [],
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
    ['a'], presenter, [],
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
echo "== dashboard.py: build_launch_binds() / build_focus_transform() pure helpers =="

test_build_launch_binds_one_print_accept_per_key_plus_enter() {
  local out
  out="$(python3 -c "
$LOAD_DASHBOARD
print(d.build_launch_binds(['alt-o', 'alt-s', 'O']))
")"
  check "build_launch_binds() returns KEY:print(KEY)+accept per declared key, plus enter:print()+accept last" \
    "$out" "['alt-o:print(alt-o)+accept', 'alt-s:print(alt-s)+accept', 'O:print(O)+accept', 'enter:print()+accept']"
}
test_build_launch_binds_one_print_accept_per_key_plus_enter

test_build_focus_transform_unbind_then_rebind_from_field3() {
  local out
  out="$(python3 -c "
$LOAD_DASHBOARD
print(repr(d.build_focus_transform(['alt-o', 'alt-s', 'O', 'S', '1'])))
")"
  check "build_focus_transform() emits a shell snippet unbinding the whole universe then rebinding only {3}'s keys when {3} is non-empty" \
    "$out" "'k={3}; if [ -z \"\$k\" ]; then printf \"unbind(alt-o,alt-s,O,S,1)\"; else printf \"unbind(alt-o,alt-s,O,S,1)+rebind(%s)\" \"\$k\"; fi'"
}
test_build_focus_transform_unbind_then_rebind_from_field3

test_build_focus_transform_emits_unbind_only_when_row_has_no_keys() {
  local out
  out="$(python3 -c "
$LOAD_DASHBOARD
import shlex, subprocess

cmd = d.build_focus_transform(['alt-o', 'alt-s', 'O', 'S', '1'])

def run_for_field3(value):
    substituted = cmd.replace('{3}', shlex.quote(value))
    return subprocess.run(['sh', '-c', substituted], capture_output=True, text=True, check=True).stdout

print(repr(run_for_field3('alt-o,O')))
print(repr(run_for_field3('')))
")"
  check "a row with keys in field 3 gets unbind(universe)+rebind(that row's keys)" \
    "$(sed -n 1p <<<"$out")" "'unbind(alt-o,alt-s,O,S,1)+rebind(alt-o,O)'"
  check "a row with no keys in field 3 gets unbind(universe) only -- never an empty rebind() fzf would reject, so the previous row's binding cannot persist" \
    "$(sed -n 2p <<<"$out")" "'unbind(alt-o,alt-s,O,S,1)'"
}
test_build_focus_transform_emits_unbind_only_when_row_has_no_keys

test_build_focus_transform_no_shell_breaking_chars_for_any_declarable_key() {
  local out
  out="$(python3 -c "
$LOAD_DASHBOARD
universe = ['alt-o', 'alt-b', 'alt-q', 'O', 'B', 'Q'] + list('123456789')
cmd = d.build_focus_transform(universe)
binds = d.build_launch_binds(universe)
universe_csv = ','.join(universe)
print(all(c not in universe_csv for c in ['\`', '\$', ';', '|', '&', chr(39)]))
print(universe_csv in cmd)
print(all(':' in b and '(' not in b.split(':', 1)[0] for b in binds[:-1]))
")"
  check "the key-derived universe CSV embedded in build_focus_transform()'s command contains no shell-breaking characters for any key declared_action_keys() can produce" \
    "$(sed -n 1p <<<"$out")" "True"
  check "that clean universe CSV is embedded verbatim in the command (no extra escaping mangles it)" \
    "$(sed -n 2p <<<"$out")" "True"
  check "build_launch_binds() keys are plain KEY:action tokens (no stray characters before the colon)" \
    "$(sed -n 3p <<<"$out")" "True"
}
test_build_focus_transform_no_shell_breaking_chars_for_any_declarable_key

# ---------------------------------------------------------------------------
echo
echo "== dashboard.py: real fzf pty smoke test -- row-scoped hotkeys (task 5.3) =="

PTY_TEST_PY="
$LOAD_DASHBOARD
import fcntl, json, os, pty, shutil, struct, tempfile, termios, threading, time

UNIVERSE = ['alt-o', 'alt-s', 'O', 'S', '1', '2']
ROWS_TEXT = 'row1' + chr(9) + 'blob1' + chr(9) + 'alt-o,O' + chr(9) + 'hint1' + chr(10) + 'row2' + chr(9) + 'blob2' + chr(9) + 'alt-s,1' + chr(9) + 'hint2'
RELOADED_ROWS_TEXT = 'new-first' + chr(9) + 'blob3' + chr(9) + 'alt-o,O' + chr(9) + 'hint3' + chr(10) + 'row2' + chr(9) + 'blob2' + chr(9) + 'alt-s,1' + chr(9) + 'hint2'
ROW1_LINE = ROWS_TEXT.split(chr(10))[0]
ROW2_LINE = ROWS_TEXT.split(chr(10))[1]
RELOADED_ROW1_LINE = RELOADED_ROWS_TEXT.split(chr(10))[0]


def spawn_session():
    tmpdir = tempfile.mkdtemp(prefix='attention-test-pty-')
    sock_path = os.path.join(tmpdir, 'fzf.sock')
    rows_path = os.path.join(tmpdir, 'rows.tsv')
    with open(rows_path, 'w') as f:
        f.write(ROWS_TEXT)
    master_fd, slave_fd = pty.openpty()
    fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, struct.pack('HHHH', 24, 80, 0, 0))
    out_r, out_w = os.pipe()
    args = ['fzf', '--ansi', '--layout=reverse', '--height', '10', '-d', chr(9),
            '--with-nth', '1', '--listen', sock_path, '--footer-border=line']
    for b in d.build_launch_binds(UNIVERSE):
        args += ['--bind', b]
    args += ['--bind', 'start,focus:transform[' + d.build_focus_transform(UNIVERSE) + ']']
    pid = os.fork()
    if pid == 0:
        try:
            os.setsid()
            fcntl.ioctl(slave_fd, termios.TIOCSCTTY, 0)
            os.close(master_fd)
            os.close(out_r)
            devnull = os.open(os.devnull, os.O_RDONLY)
            os.dup2(devnull, 0)
            os.dup2(out_w, 1)
            os.dup2(slave_fd, 2)
            for fd in (devnull, out_w, slave_fd):
                if fd > 2:
                    os.close(fd)
            os.execvp('fzf', args)
        finally:
            os._exit(127)
    os.close(slave_fd)
    os.close(out_w)
    stop = threading.Event()

    def drain():
        buf = b''
        while not stop.is_set():
            try:
                chunk = os.read(master_fd, 65536)
            except OSError:
                break
            if not chunk:
                break
            buf += chunk
            if b'\x1b[6n' in buf:
                try:
                    os.write(master_fd, b'\x1b[24;1R')
                except OSError:
                    break
                buf = b''

    thread = threading.Thread(target=drain, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while not os.path.exists(sock_path) and time.monotonic() < deadline:
        time.sleep(0.02)
    reload_body = ('reload[cat ' + chr(34) + rows_path + chr(34) + ']+first').encode()
    d.unix_request(sock_path, 'POST', '/', body=reload_body)
    return {'pid': pid, 'master_fd': master_fd, 'out_r': out_r, 'sock_path': sock_path,
            'tmpdir': tmpdir, 'stop': stop, 'thread': thread}


def wait_for_state(session, predicate, timeout=5):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        status, body = d.unix_request(session['sock_path'], 'GET', '/')
        if status == 200:
            last = json.loads(body)
            if predicate(last):
                return last
        time.sleep(0.1)
    return last


def still_running(session):
    try:
        return os.waitpid(session['pid'], os.WNOHANG) == (0, 0)
    except ChildProcessError:
        return False


def wait_exit(session, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        pid_done, status = os.waitpid(session['pid'], os.WNOHANG)
        if pid_done != 0:
            return status
        time.sleep(0.05)
    return None


def read_output(session):
    session['stop'].set()
    try:
        os.close(session['master_fd'])
    except OSError:
        pass
    chunks = []
    while True:
        try:
            chunk = os.read(session['out_r'], 65536)
        except OSError:
            break
        if not chunk:
            break
        chunks.append(chunk)
    return b''.join(chunks).decode()


def cleanup(session):
    session['stop'].set()
    if still_running(session):
        try:
            os.kill(session['pid'], 9)
            os.waitpid(session['pid'], 0)
        except OSError:
            pass
    for key in ('master_fd', 'out_r'):
        try:
            os.close(session[key])
        except OSError:
            pass
    shutil.rmtree(session['tmpdir'], ignore_errors=True)


results = []

session = spawn_session()
state = wait_for_state(session, lambda s: s.get('totalCount') == 2)
results.append(state is not None and state['current']['index'] == 0)

os.write(session['master_fd'], b'S')
state = wait_for_state(session, lambda s: s.get('query') == 'S')
results.append(state is not None and state.get('query') == 'S')
results.append(still_running(session))

os.write(session['master_fd'], b'1')
state = wait_for_state(session, lambda s: s.get('query') == 'S1')
results.append(state is not None and state.get('query') == 'S1')
results.append(still_running(session))

os.write(session['master_fd'], b'\x7f\x7f')
wait_for_state(session, lambda s: s.get('query') == '' and s.get('matchCount') == 2 and not s.get('reading'))

os.write(session['master_fd'], b'O')
results.append(wait_exit(session, timeout=5) is not None)
results.append(read_output(session) == 'O' + chr(10) + ROW1_LINE + chr(10))
cleanup(session)

session2 = spawn_session()
wait_for_state(session2, lambda s: s.get('totalCount') == 2)
os.write(session2['master_fd'], b'\x0e')
state = wait_for_state(session2, lambda s: s.get('current') and s['current']['index'] == 1)
results.append(state is not None and state['current']['index'] == 1)

os.write(session2['master_fd'], b'1')
results.append(wait_exit(session2, timeout=5) is not None)
results.append(read_output(session2) == '1' + chr(10) + ROW2_LINE + chr(10))
cleanup(session2)

session3 = spawn_session()
wait_for_state(session3, lambda s: s.get('totalCount') == 2)
os.write(session3['master_fd'], b'\x0e')
wait_for_state(session3, lambda s: s.get('current') and s['current']['index'] == 1)
reloaded_rows_path = os.path.join(session3['tmpdir'], 'reloaded-rows.tsv')
with open(reloaded_rows_path, 'w') as f:
    f.write(RELOADED_ROWS_TEXT)
reload_body = ('reload[cat ' + chr(34) + reloaded_rows_path + chr(34) + ']+first').encode()
d.unix_request(session3['sock_path'], 'POST', '/', body=reload_body)
state = wait_for_state(
    session3,
    lambda s: s.get('current') and s['current']['index'] == 0 and
    s['current']['text'] == RELOADED_ROW1_LINE,
)
results.append(state is not None and state['current']['index'] == 0 and state['current']['text'] == RELOADED_ROW1_LINE)
cleanup(session3)

for r in results:
    print(r)
"

test_real_pty_row_scoped_hotkeys() {
  local out
  out="$(python3 -c "$PTY_TEST_PY")"
  check "row 1 is focused immediately after the initial reload (index 0)" "$(sed -n 1p <<<"$out")" "True"
  check "a universe key absent from the focused row's CSV (S) appends to the query instead of exiting" "$(sed -n 2p <<<"$out")" "True"
  check "fzf is still running after typing the unbound key S" "$(sed -n 3p <<<"$out")" "True"
  check "a second universe key absent from the focused row's CSV (1) also appends to the query instead of exiting" "$(sed -n 4p <<<"$out")" "True"
  check "fzf is still running after typing the unbound key 1" "$(sed -n 5p <<<"$out")" "True"
  check "a universe key present on the focused row's CSV (O) exits fzf" "$(sed -n 6p <<<"$out")" "True"
  check "the exit output is exactly KEY\\n<row 1's line>" "$(sed -n 7p <<<"$out")" "True"
  check "moving focus to row 2 updates the --listen state's current index" "$(sed -n 8p <<<"$out")" "True"
  check "the key inert on row 1 (1) exits fzf once focus moves to row 2, where it is bound" "$(sed -n 9p <<<"$out")" "True"
  check "the row-2 exit output is exactly KEY\\n<row 2's line>" "$(sed -n 10p <<<"$out")" "True"
  check "a snapshot reload resets focus to its newly first row" "$(sed -n 11p <<<"$out")" "True"
}

if command -v fzf >/dev/null 2>&1; then
  test_real_pty_row_scoped_hotkeys
else
  skip "test_real_pty_row_scoped_hotkeys (fzf not on PATH)"
fi

# ---------------------------------------------------------------------------
echo
echo "== dashboard.py: UnixHTTPConnection / unix_request() over a stdlib AF_UNIX server (task 6.1) =="

test_unix_request_get_post_framing_against_stdlib_server() {
  local out
  out="$(python3 -c "
$LOAD_DASHBOARD
import http.server, os, shutil, socketserver, tempfile, threading

received = []


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = b'{' + chr(34).encode() + b'ok' + chr(34).encode() + b': true}'
        self.send_response(200)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get('Content-Length', 0))
        received.append(self.rfile.read(length))
        self.send_response(200)
        self.send_header('Content-Length', '0')
        self.end_headers()

    def log_message(self, *a):
        pass


tmpdir = tempfile.mkdtemp(prefix='attention-test-unixhttp-')
sock_path = os.path.join(tmpdir, 'test.sock')
server = socketserver.UnixStreamServer(sock_path, Handler)
thread = threading.Thread(target=server.serve_forever, daemon=True)
thread.start()

status, body = d.unix_request(sock_path, 'GET', '/')
print(status)
print(body.decode())

post_body = b'reload[cat ' + chr(34).encode() + b'/tmp/x' + chr(34).encode() + b']+change-header:hi'
status2, body2 = d.unix_request(sock_path, 'POST', '/', body=post_body)
print(status2)
print(received[-1] == post_body)

server.shutdown()
server.server_close()
thread.join(timeout=5)
shutil.rmtree(tmpdir, ignore_errors=True)
")"
  check "GET / over a Unix domain socket returns the server's status" "$(sed -n 1p <<<"$out")" "200"
  check "GET / over a Unix domain socket returns the server's body intact" "$(sed -n 2p <<<"$out")" "{\"ok\": true}"
  check "POST / over a Unix domain socket returns the server's status" "$(sed -n 3p <<<"$out")" "200"
  check "POST / over a Unix domain socket transmits the exact request body the server received" "$(sed -n 4p <<<"$out")" "True"
}
test_unix_request_get_post_framing_against_stdlib_server

# ---------------------------------------------------------------------------
echo
echo "== dashboard.py: FzfPresenter -- real fzf lifecycle over --listen (tasks 6.2-6.4) =="

FZF_PRESENTER_TEST_PY="
$LOAD_DASHBOARD
import fcntl, json, os, pty, struct, termios, threading, time


def run_in_pty_session(child_main):
    master_fd, slave_fd = pty.openpty()
    fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, struct.pack('HHHH', 24, 80, 0, 0))
    result_r, result_w = os.pipe()
    pid = os.fork()
    if pid == 0:
        try:
            os.setsid()
            fcntl.ioctl(slave_fd, termios.TIOCSCTTY, 0)
            os.close(master_fd)
            os.close(result_r)
            try:
                payload = json.dumps(child_main()).encode()
            except Exception as e:
                payload = json.dumps({'error': repr(e)}).encode()
            os.write(result_w, payload)
        finally:
            os.close(result_w)
            os._exit(0)
    os.close(slave_fd)
    os.close(result_w)
    stop = threading.Event()

    def drain():
        buf = b''
        while not stop.is_set():
            try:
                chunk = os.read(master_fd, 65536)
            except OSError:
                break
            if not chunk:
                break
            buf += chunk
            if b'\x1b[6n' in buf:
                try:
                    os.write(master_fd, b'\x1b[24;1R')
                except OSError:
                    break
                buf = b''

    thread = threading.Thread(target=drain, daemon=True)
    thread.start()
    chunks = []
    while True:
        chunk = os.read(result_r, 65536)
        if not chunk:
            break
        chunks.append(chunk)
    os.close(result_r)
    stop.set()
    try:
        os.close(master_fd)
    except OSError:
        pass
    os.waitpid(pid, 0)
    data = b''.join(chunks)
    return json.loads(data) if data else {'error': 'no result'}


def wait_for_state(presenter, predicate, timeout=5):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        status, body = d.unix_request(presenter._sock_path, 'GET', '/')
        if status == 200:
            last = json.loads(body)
            if predicate(last):
                return last
        time.sleep(0.1)
    return last


def child_launch_push_stop():
    presenter = d.FzfPresenter()
    presenter.launch(['alt-o'], 'Loading: gh…')
    sock_path, tmpdir, proc = presenter._sock_path, presenter._tmpdir, presenter._proc
    sock_exists = os.path.exists(sock_path)
    proc_running = proc.poll() is None
    no_expect_flag = '--expect' not in proc.args
    has_no_tracking_flags = '--track' not in proc.args and '--id-nth' not in proc.args

    presenter.push_snapshot(['rowA' + chr(9) + 'blobA' + chr(9) + 'alt-o' + chr(9) + 'hintA'], ['gh'])
    state1 = wait_for_state(presenter, lambda s: s.get('totalCount') == 1)
    first_push_reflected = state1 is not None and [m['text'] for m in state1['matches']] == [
        'rowA' + chr(9) + 'blobA' + chr(9) + 'alt-o' + chr(9) + 'hintA',
    ]

    presenter.push_snapshot([
        'rowA' + chr(9) + 'blobA' + chr(9) + 'alt-o' + chr(9) + 'hintA',
        'rowB' + chr(9) + 'blobB' + chr(9) + 'alt-o' + chr(9) + 'hintB',
    ], [])
    state2 = wait_for_state(presenter, lambda s: s.get('totalCount') == 2)
    second_push_replaces = state2 is not None and [m['text'] for m in state2['matches']] == [
        'rowA' + chr(9) + 'blobA' + chr(9) + 'alt-o' + chr(9) + 'hintA',
        'rowB' + chr(9) + 'blobB' + chr(9) + 'alt-o' + chr(9) + 'hintB',
    ]

    presenter.stop()
    return {
        'sock_exists_after_launch': sock_exists,
        'proc_running_after_launch': proc_running,
        'launch_has_no_expect_flag': no_expect_flag,
        'launch_has_no_tracking_flags': has_no_tracking_flags,
        'first_push_reflected': first_push_reflected,
        'second_push_replaces_not_appends': second_push_replaces,
        'proc_exited_after_stop': proc.poll() is not None,
        'tmpdir_removed_after_stop': not os.path.exists(tmpdir),
    }


def child_wait_for_exit_timeout_escalates():
    presenter = d.FzfPresenter()
    presenter.launch(['alt-o'], 'header')
    tmpdir, proc = presenter._tmpdir, presenter._proc
    presenter.push_snapshot(['rowA' + chr(9) + 'blobA' + chr(9) + 'alt-o' + chr(9) + 'hintA'], [])
    wait_for_state(presenter, lambda s: s.get('totalCount') == 1)

    start = time.monotonic()
    result = presenter.wait_for_exit(0.5)
    elapsed = time.monotonic() - start
    return {
        'timeout_returns_none_key': result.key is None,
        'elapsed_bounded_by_the_requested_timeout': elapsed < 5,
        'proc_terminated_after_timeout_escalation': proc.poll() is not None,
        'tmpdir_removed_after_timeout': not os.path.exists(tmpdir),
        'presenter_state_cleared_after_timeout': (
            presenter._proc is None and presenter._tmpdir is None and
            presenter._sock_path is None and presenter._snapshot_path is None
        ),
    }


def child_stop_after_process_already_exited():
    presenter = d.FzfPresenter()
    presenter.launch(['alt-o'], 'header')
    tmpdir, proc, sock_path = presenter._tmpdir, presenter._proc, presenter._sock_path
    presenter.push_snapshot(['rowA' + chr(9) + 'blobA' + chr(9) + 'alt-o' + chr(9) + 'hintA'], [])
    wait_for_state(presenter, lambda s: s.get('totalCount') == 1)
    d.unix_request(sock_path, 'POST', '/', body=b'abort', timeout=2)
    presenter.wait_for_exit(5)

    stop_raised = False
    try:
        presenter.stop()
    except Exception:
        stop_raised = True
    return {
        'process_already_exited_before_stop': proc.poll() is not None,
        'stop_after_already_exited_does_not_raise': not stop_raised,
        'tmpdir_still_removed': not os.path.exists(tmpdir),
    }


def child_push_snapshot_survives_teardown_race():
    presenter = d.FzfPresenter()
    presenter.launch(['alt-o'], 'header')

    reached = threading.Event()
    release = threading.Event()
    original_mkstemp = d.tempfile.mkstemp

    def gated_mkstemp(*args, **kwargs):
        reached.set()
        release.wait(timeout=5)
        return original_mkstemp(*args, **kwargs)

    d.tempfile.mkstemp = gated_mkstemp
    push_raised = []

    def call_push():
        try:
            presenter.push_snapshot(
                ['rowA' + chr(9) + 'blobA' + chr(9) + 'alt-o' + chr(9) + 'hintA'], [],
            )
        except Exception as e:
            push_raised.append(repr(e))

    # Deterministic barrier/event race, no wall-clock sleeps: push_snapshot
    # reads the (still-live) tmpdir/sock_path under its own lock, then blocks
    # right before its real mkstemp call via the gate below. Only once it's
    # gated do we run wait_for_exit()'s timeout path on a second thread --
    # the exact concurrent teardown FzfPresenter must survive -- and only
    # after that full teardown completes do we release the gate, so the
    # real mkstemp() runs against a directory wait_for_exit has already
    # removed.
    push_thread = threading.Thread(target=call_push)
    push_thread.start()
    reached.wait(timeout=5)

    timeout_thread = threading.Thread(target=presenter.wait_for_exit, args=(0.1,))
    timeout_thread.start()
    timeout_thread.join(timeout=5)

    release.set()
    push_thread.join(timeout=5)
    d.tempfile.mkstemp = original_mkstemp

    return {
        'push_snapshot_did_not_raise_into_removed_tmpdir': push_raised == [],
        'push_thread_finished': not push_thread.is_alive(),
        'presenter_state_cleared_after_race': (
            presenter._proc is None and presenter._tmpdir is None and
            presenter._sock_path is None and presenter._snapshot_path is None
        ),
    }


r1 = run_in_pty_session(child_launch_push_stop)
r2 = run_in_pty_session(child_wait_for_exit_timeout_escalates)
r3 = run_in_pty_session(child_stop_after_process_already_exited)
r4 = run_in_pty_session(child_push_snapshot_survives_teardown_race)
for key in (
    'sock_exists_after_launch', 'proc_running_after_launch', 'launch_has_no_expect_flag',
    'launch_has_no_tracking_flags', 'first_push_reflected', 'second_push_replaces_not_appends',
    'proc_exited_after_stop', 'tmpdir_removed_after_stop',
):
    print(r1.get(key))
for key in (
    'timeout_returns_none_key', 'elapsed_bounded_by_the_requested_timeout',
    'proc_terminated_after_timeout_escalation', 'tmpdir_removed_after_timeout',
    'presenter_state_cleared_after_timeout',
):
    print(r2.get(key))
for key in (
    'process_already_exited_before_stop', 'stop_after_already_exited_does_not_raise', 'tmpdir_still_removed',
):
    print(r3.get(key))
for key in (
    'push_snapshot_did_not_raise_into_removed_tmpdir', 'push_thread_finished',
    'presenter_state_cleared_after_race',
):
    print(r4.get(key))
"

test_fzf_presenter_real_lifecycle() {
  local out
  out="$(python3 -c "$FZF_PRESENTER_TEST_PY")"
  check "FzfPresenter.launch() waits for the --listen socket to exist before returning" "$(sed -n 1p <<<"$out")" "True"
  check "FzfPresenter.launch() leaves a live fzf process running" "$(sed -n 2p <<<"$out")" "True"
  check "FzfPresenter.launch() never passes --expect" "$(sed -n 3p <<<"$out")" "True"
  check "FzfPresenter.launch() omits selection-tracking flags" "$(sed -n 4p <<<"$out")" "True"
  check "FzfPresenter.push_snapshot() reload is reflected in the --listen state" "$(sed -n 5p <<<"$out")" "True"
  check "a second push_snapshot() replaces the list rather than appending to it" "$(sed -n 6p <<<"$out")" "True"
  check "FzfPresenter.stop() terminates the still-running fzf process" "$(sed -n 7p <<<"$out")" "True"
  check "FzfPresenter.stop() removes the per-session temp directory" "$(sed -n 8p <<<"$out")" "True"
  check "FzfPresenter.wait_for_exit(timeout) on an idle presenter returns key=None on timeout" "$(sed -n 9p <<<"$out")" "True"
  check "wait_for_exit()'s timeout escalation returns within the requested bound, not the full session" "$(sed -n 10p <<<"$out")" "True"
  check "wait_for_exit()'s timeout escalation (SIGTERM-then-kill) actually terminates the process" "$(sed -n 11p <<<"$out")" "True"
  check "wait_for_exit()'s timeout escalation removes the per-session temp directory" "$(sed -n 12p <<<"$out")" "True"
  check "wait_for_exit()'s timeout escalation clears _proc/_tmpdir/_sock_path/_snapshot_path (no stale presenter state)" "$(sed -n 13p <<<"$out")" "True"
  check "the process has already exited (via abort) before stop() is called" "$(sed -n 14p <<<"$out")" "True"
  check "stop() called after the process already exited is a safe no-op" "$(sed -n 15p <<<"$out")" "True"
  check "stop() still removes the temp directory when the process had already exited" "$(sed -n 16p <<<"$out")" "True"
  check "push_snapshot() racing wait_for_exit()'s timeout teardown never raises into a removed tmpdir (no plugin-thread traceback)" "$(sed -n 17p <<<"$out")" "True"
  check "the racing push_snapshot() thread returns cleanly (never hangs)" "$(sed -n 18p <<<"$out")" "True"
  check "presenter state stays fully cleared after the race, not left stale for a later push" "$(sed -n 19p <<<"$out")" "True"
}

if command -v fzf >/dev/null 2>&1; then
  test_fzf_presenter_real_lifecycle
else
  skip "test_fzf_presenter_real_lifecycle (fzf not on PATH)"
fi

# ---------------------------------------------------------------------------
echo
echo "== dashboard.py: FzfPresenter.launch() readiness-timeout raise + cleanup (no fzf required) =="

FAKE_FZF_BIN="$WORK/bin-fake-fzf"
mkdir -p "$FAKE_FZF_BIN"
FAKE_FZF_PIDFILE="$WORK/fake-fzf-nonlistening.pid"
cat > "$FAKE_FZF_BIN/fzf" <<STUB
#!/usr/bin/env python3
import os, time
with open("$FAKE_FZF_PIDFILE", "w") as f:
    f.write(str(os.getpid()))
    f.flush()
time.sleep(60)
STUB
chmod +x "$FAKE_FZF_BIN/fzf"

test_fzf_presenter_launch_readiness_timeout_raises_and_cleans_up() {
  local out
  rm -f "$FAKE_FZF_PIDFILE"
  out="$(PATH="$FAKE_FZF_BIN:$PATH" python3 -c "
$LOAD_DASHBOARD
import os, time

presenter = d.FzfPresenter()
raised_type = None
try:
    presenter.launch(['alt-o'], 'header', timeout=0.2)
except Exception as e:
    raised_type = type(e).__name__

deadline = time.monotonic() + 5
pid = None
while time.monotonic() < deadline and pid is None:
    if os.path.exists('$FAKE_FZF_PIDFILE'):
        pid = int(open('$FAKE_FZF_PIDFILE').read())
    else:
        time.sleep(0.02)

reaped = False
if pid is not None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            reaped = True
            break
        time.sleep(0.02)

print(raised_type)
print(presenter._proc is None)
print(presenter._tmpdir is None)
print(pid is not None)
print(reaped)
")"
  check "launch() raises PresenterLaunchTimeout when fzf never creates its --listen socket within the readiness timeout" \
    "$(sed -n 1p <<<"$out")" "PresenterLaunchTimeout"
  check "launch() never adopts the process/tmpdir it's about to raise past (no live presenter state left behind)" \
    "$(sed -n 2p <<<"$out")" "True"
  check "launch() never adopts the process/tmpdir it's about to raise past (no live presenter state left behind) (tmpdir)" \
    "$(sed -n 3p <<<"$out")" "True"
  check "the never-ready fzf process actually started (proving the next check is a real reap, not a no-op)" \
    "$(sed -n 4p <<<"$out")" "True"
  check "launch() terminates and reaps the never-ready fzf process instead of leaving it running" \
    "$(sed -n 5p <<<"$out")" "True"
}
test_fzf_presenter_launch_readiness_timeout_raises_and_cleans_up

# ---------------------------------------------------------------------------
echo
echo "== dashboard.py: FzfPresenter.push_snapshot() keeps at most one live snapshot file =="

test_fzf_presenter_push_snapshot_bounds_temp_files_to_one() {
  local out
  out="$(python3 -c "
$LOAD_DASHBOARD
import http.server, os, socketserver, tempfile, threading


class Handler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get('Content-Length', 0))
        self.rfile.read(length)
        self.send_response(200)
        self.send_header('Content-Length', '0')
        self.end_headers()

    def log_message(self, *a):
        pass


tmpdir = tempfile.mkdtemp(prefix='attention-test-snapshot-bound-')
sock_path = os.path.join(tmpdir, 'test.sock')
server = socketserver.UnixStreamServer(sock_path, Handler)
thread = threading.Thread(target=server.serve_forever, daemon=True)
thread.start()

presenter = d.FzfPresenter()
with presenter._lock:
    presenter._tmpdir = tmpdir
    presenter._sock_path = sock_path

counts = []
for i in range(5):
    presenter.push_snapshot([f'row{i}'], [])
    counts.append(len([p for p in os.listdir(tmpdir) if p.startswith('snapshot-')]))

presenter.stop()
snapshot_path_reset = presenter._snapshot_path is None
tmpdir_removed = not os.path.exists(tmpdir)

server.shutdown()
server.server_close()
thread.join(timeout=5)

print(max(counts))
print(counts[-1])
print(snapshot_path_reset)
print(tmpdir_removed)
")"
  check "at no point across 5 successive push_snapshot() calls do more than 1 snapshot-*.tsv files exist at once" \
    "$(sed -n 1p <<<"$out")" "1"
  check "exactly 1 snapshot file remains live after the 5th push (the one fzf is currently reading)" \
    "$(sed -n 2p <<<"$out")" "1"
  check "stop() resets the tracked current-snapshot path" "$(sed -n 3p <<<"$out")" "True"
  check "stop() removes the per-session temp directory (and whatever snapshot file was still in it)" \
    "$(sed -n 4p <<<"$out")" "True"
}
test_fzf_presenter_push_snapshot_bounds_temp_files_to_one

SIMPLE_DASH_PLUGIN="$WORK/simple_dash_plugin.py"
cat > "$SIMPLE_DASH_PLUGIN" <<'PY'
def fetch(config):
    return []
def act(key, payload):
    pass
PY

write_config <<JSON
{"plugins": ["$SIMPLE_DASH_PLUGIN"]}
JSON

test_run_dashboard_handles_presenter_launch_timeout_and_oserror() {
  local out
  out="$(HOME="$TEST_HOME" XDG_CONFIG_HOME="$XDG_CONFIG" python3 -c "
import contextlib, io
$LOAD_CORE

class RaisingController:
    def __init__(self, *args, **kwargs):
        pass
    def run(self, refresh_interval=60):
        raise RaisingController.exc

for exc, label in (
    (m.dashboard.PresenterLaunchTimeout('fzf never created its --listen socket at /tmp/x within 5s'), 'timeout'),
    (PermissionError('Permission denied'), 'oserror'),
):
    RaisingController.exc = exc
    m.dashboard.DashboardController = RaisingController
    buf = io.StringIO()
    exit_code = None
    crashed = None
    with contextlib.redirect_stderr(buf):
        try:
            m.run_dashboard()
        except SystemExit as e:
            exit_code = e.code
        except Exception as e:
            crashed = repr(e)
    stderr_lines = buf.getvalue().strip().splitlines()
    named = any('attention:' in line and str(exc) in line for line in stderr_lines)
    print(label, exit_code, crashed, named)
")"
  check "PresenterLaunchTimeout from controller.run(): run_dashboard() exits 1 instead of crashing" \
    "$(sed -n 1p <<<"$out" | awk '{print $2}')" "1"
  check "PresenterLaunchTimeout: run_dashboard() never lets the exception propagate uncaught" \
    "$(sed -n 1p <<<"$out" | awk '{print $3}')" "None"
  check "PresenterLaunchTimeout: stderr carries one line naming the failure with the attention: prefix and the underlying message" \
    "$(sed -n 1p <<<"$out" | awk '{print $4}')" "True"
  check "a general OSError (e.g. PermissionError) from launch(): run_dashboard() also exits 1 instead of crashing" \
    "$(sed -n 2p <<<"$out" | awk '{print $2}')" "1"
  check "OSError: run_dashboard() never lets the exception propagate uncaught" \
    "$(sed -n 2p <<<"$out" | awk '{print $3}')" "None"
  check "OSError: stderr carries one line naming the failure with the attention: prefix and the underlying message" \
    "$(sed -n 2p <<<"$out" | awk '{print $4}')" "True"
}
test_run_dashboard_handles_presenter_launch_timeout_and_oserror



# ---------------------------------------------------------------------------
echo
echo "== run_dashboard(): the bare (no-args) interactive loop =="

DASH_BIN="$WORK/bin-dashboard"
mkdir -p "$DASH_BIN"
DASH_FZF_LOG="$WORK/dashboard-fzf-calls.log"
DASH_OPEN_LOG="$WORK/dashboard-open.log"
: > "$DASH_FZF_LOG"; : > "$DASH_OPEN_LOG"
DASH_FZF_ARGS_LOG="$WORK/dashboard-fzf-args.log"
: > "$DASH_FZF_ARGS_LOG"

cat > "$DASH_BIN/gh" <<'STUB'
#!/bin/sh
case "$*" in
  "search prs --review-requested=@me"*)
    echo '[{"number": 1, "title": "DASHTEST-pr", "repository": {"name": "kb", "nameWithOwner": "myorg/kb"}, "url": "https://github.com/myorg/kb/pull/1"}]'
    ;;
  *) echo "[]" ;;
esac
STUB
chmod +x "$DASH_BIN/gh"

cat > "$DASH_BIN/open" <<STUB
#!/bin/sh
echo "\$*" >> "$DASH_OPEN_LOG"
STUB
chmod +x "$DASH_BIN/open"

# fzf stub: a real (but scripted) --listen HTTP-over-AF_UNIX server, since
# run_dashboard() now goes through FzfPresenter for real. First call
# accepts pushes, ignores the initial empty/pending one, and on the first
# push carrying actual row content presses alt-o and exits; later calls
# just bind the socket (so launch()'s readiness wait succeeds) and exit
# immediately with no output, simulating Esc.
cat > "$DASH_BIN/fzf" <<STUB
#!/usr/bin/env python3
import os, socket, sys

args = sys.argv[1:]
sock_path = None
for i, a in enumerate(args):
    if a == '--listen':
        sock_path = args[i + 1]
        break

log_path = "$DASH_FZF_LOG"
with open(log_path, 'a') as f:
    f.write('call\n')
with open(log_path) as f:
    calls = sum(1 for _ in f)

with open("$DASH_FZF_ARGS_LOG", 'a') as f:
    f.write(' '.join(args) + '\n')

try:
    os.unlink(sock_path)
except FileNotFoundError:
    pass
srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
srv.bind(sock_path)
srv.listen(5)

if calls != 1:
    sys.exit(0)


def handle(conn):
    data = b''
    conn.settimeout(5)
    while b'\r\n\r\n' not in data:
        chunk = conn.recv(4096)
        if not chunk:
            break
        data += chunk
    header, _, rest = data.partition(b'\r\n\r\n')
    length = 0
    for line in header.split(b'\r\n')[1:]:
        if line.lower().startswith(b'content-length:'):
            length = int(line.split(b':', 1)[1])
    while len(rest) < length:
        chunk = conn.recv(4096)
        if not chunk:
            break
        rest += chunk
    body = rest[:length]
    conn.sendall(b'HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n')
    conn.close()
    return body


marker = b'reload[cat "'
while True:
    conn, _ = srv.accept()
    body = handle(conn)
    if marker in body:
        path = body.split(marker, 1)[1].split(b'"', 1)[0].decode()
        try:
            content = open(path).read().strip()
        except OSError:
            content = ''
        if content:
            print('alt-o')
            print(content.splitlines()[0])
            sys.exit(0)
STUB
chmod +x "$DASH_BIN/fzf"

write_config <<'JSON'
{"plugins": ["github"], "codeDir": "/tmp/nonexistent-fakecode", "github": {}}
JSON

test_run_dashboard_loop() {
  local rc
  HOME="$TEST_HOME" XDG_CONFIG_HOME="$XDG_CONFIG" PATH="$DASH_BIN:$PATH" \
    python3 "$ATTENTION" >/dev/null 2>&1 && rc=0 || rc=$?
  check "bare invocation (no args) exits 0" "$rc" "0"
  check "fzf invoked exactly twice (render, act, re-render, Esc)" "$(wc -l < "$DASH_FZF_LOG" | tr -d ' ')" "2"
  if grep -q 'https://github.com/myorg/kb/pull/1' "$DASH_OPEN_LOG"; then
    ok "the hotkey pressed on the fzf-selected row actually dispatched (alt-o -> open)"
  else
    bad "the hotkey pressed on the fzf-selected row actually dispatched (got: $(cat "$DASH_OPEN_LOG"))"
  fi
  if grep -q -- '--height 10' "$DASH_FZF_ARGS_LOG"; then
    ok "fzf invoked with a fixed --height (shows ~5 items regardless of terminal size, not a % of it)"
  else
    bad "fzf invoked with a fixed --height (shows ~5 items regardless of terminal size, not a % of it) (got: $(cat "$DASH_FZF_ARGS_LOG"))"
  fi
}
test_run_dashboard_loop

echo
echo "-- empty list: prints a message, never invokes fzf at all --"

write_config <<'JSON'
{"plugins": []}
JSON

test_run_dashboard_empty() {
  local out rc
  : > "$DASH_FZF_LOG"
  out="$(HOME="$TEST_HOME" XDG_CONFIG_HOME="$XDG_CONFIG" PATH="$DASH_BIN:$PATH" python3 "$ATTENTION" 2>&1)" && rc=0 || rc=$?
  check "bare invocation with nothing to show exits 0" "$rc" "0"
  check "no fzf process spawned for an empty list" "$(wc -l < "$DASH_FZF_LOG" | tr -d ' ')" "0"
  case "$out" in
    *"Nothing needs attention"*) ok "prints a friendly empty-list message" ;;
    *) bad "prints a friendly empty-list message (got: $out)" ;;
  esac
}
test_run_dashboard_empty

echo
echo "-- periodic refresh: a stuck fzf is interrupted and re-rendered, not waited out --"

REFRESH_BIN="$WORK/bin-refresh"
mkdir -p "$REFRESH_BIN"
REFRESH_FZF_LOG="$WORK/refresh-fzf-calls.log"
: > "$REFRESH_FZF_LOG"

cat > "$REFRESH_BIN/gh" <<'STUB'
#!/bin/sh
case "$*" in
  "search prs --review-requested=@me"*)
    echo '[{"number": 1, "title": "REFRESHTEST-pr", "repository": {"name": "kb", "nameWithOwner": "myorg/kb"}, "url": "https://github.com/myorg/kb/pull/1"}]'
    ;;
  *) echo "[]" ;;
esac
STUB
chmod +x "$REFRESH_BIN/gh"

# First call: binds the --listen socket (so launch()'s readiness wait
# succeeds) then accepts and acks pushes forever without ever printing to
# stdout or exiting -- exactly what a genuinely stuck fzf looks like from
# wait_for_exit()'s side, requiring its SIGTERM-then-kill escalation to
# actually end it. Second call: binds the socket and exits immediately
# with no output (simulates Esc), ending the loop.
cat > "$REFRESH_BIN/fzf" <<STUB
#!/usr/bin/env python3
import os, socket, sys

args = sys.argv[1:]
sock_path = None
for i, a in enumerate(args):
    if a == '--listen':
        sock_path = args[i + 1]
        break

log_path = "$REFRESH_FZF_LOG"
with open(log_path, 'a') as f:
    f.write('call\n')
with open(log_path) as f:
    calls = sum(1 for _ in f)

try:
    os.unlink(sock_path)
except FileNotFoundError:
    pass
srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
srv.bind(sock_path)
srv.listen(5)

if calls != 1:
    sys.exit(0)


def handle(conn):
    data = b''
    conn.settimeout(5)
    try:
        while b'\r\n\r\n' not in data:
            chunk = conn.recv(4096)
            if not chunk:
                break
            data += chunk
        conn.sendall(b'HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n')
    except OSError:
        pass
    finally:
        conn.close()


while True:
    conn, _ = srv.accept()
    handle(conn)
STUB
chmod +x "$REFRESH_BIN/fzf"

write_config <<'JSON'
{"plugins": ["github"], "codeDir": "/tmp/nonexistent-fakecode", "github": {}}
JSON

test_periodic_refresh_interrupts_stuck_fzf() {
  local start_ts elapsed
  start_ts=$(date +%s)
  HOME="$TEST_HOME" XDG_CONFIG_HOME="$XDG_CONFIG" PATH="$REFRESH_BIN:$PATH" python3 -c "
$LOAD_CORE
m.run_dashboard(refresh_interval=1)
" >/dev/null 2>&1
  elapsed=$(( $(date +%s) - start_ts ))
  check "fzf invoked exactly twice (stuck render interrupted, then a fresh one)" \
    "$(wc -l < "$REFRESH_FZF_LOG" | tr -d ' ')" "2"
  if [ "$elapsed" -le 3 ]; then
    ok "the loop moved on after refresh_interval (1s) instead of waiting out the stuck fzf's 5s sleep (${elapsed}s)"
  else
    bad "the loop moved on after refresh_interval (1s) instead of waiting out the stuck fzf's 5s sleep (took ${elapsed}s)"
  fi
}
test_periodic_refresh_interrupts_stuck_fzf

echo
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

test_curses_presenter_scopes_fzf_rows() {
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
presenter._active_fzf = child
presenter.push_snapshot([
    'needs' + chr(9) + 'blob' + chr(9) + 'alt-o' + chr(9) + 'hint' + chr(9) + 'Needs Attention',
    'other' + chr(9) + 'blob' + chr(9) + 'alt-o' + chr(9) + 'hint' + chr(9) + 'Other',
], ['github'])
print(child.calls)
")"
  check "curses group presenter forwards only the selected group's rows to fzf" \
    "$(sed -n 1p <<<"$out")" "[(['needs\tblob\talt-o\thint'], ['github'])]"
}
test_curses_presenter_scopes_fzf_rows

# ---------------------------------------------------------------------------
echo
echo "== --help =="

check "attention --help mentions Usage" \
  "$(python3 "$ATTENTION" --help | grep -c Usage)" "1"

# ---------------------------------------------------------------------------
echo
echo "== summary: $pass passed, $fail failed =="
[ "$fail" -eq 0 ]
