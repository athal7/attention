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
[{"number": 1, "title": "GHTEST-review-me", "repository": {"name": "kb", "nameWithOwner": "myorg/kb"}, "url": "https://github.com/myorg/kb/pull/1"}]
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
{"mergeable": "CONFLICTING", "reviewDecision": "CHANGES_REQUESTED", "statusCheckRollup": [{"conclusion": "FAILURE"}], "comments": [{"author": {"login": "someone-else"}}]}
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
    "NEEDS ATTENTION"*"Changes Requested, Merge Conflict, Checks Failing, New Comments"*)
      ok "authored PR needing attention gets NEEDS ATTENTION status with reasons in details" ;;
    *) bad "authored PR needing attention gets NEEDS ATTENTION status with reasons in details (got: $authored_line)" ;;
  esac
  case "$repo_issue_line" in
    "OPEN"*"myorg/kb"*) ok "owned-repo issue (not assigned to me) appears with OPEN status" ;;
    *) bad "owned-repo issue (not assigned to me) appears with OPEN status (got: $repo_issue_line)" ;;
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
{"plugins": ["github"], "codeDir": "$FAKE_CODE_DIR", "github": {}}
JSON

test_repo_path_git_remote_autodetect() {
  local out repo_line
  out="$(HOME="$TEST_HOME" XDG_CONFIG_HOME="$XDG_CONFIG" PATH="$REPODIR_BIN:$PATH" python3 "$ATTENTION" list)"
  repo_line="$(grep 'REPODIRTEST-shorthand' <<<"$out" || true)"
  # repo_path isn't a visible column anymore -- decode the row's action
  # blob and check the "session" action's payload.repo_path instead.
  local decoded
  decoded="$(python3 -c "
import sys, base64, json
line = sys.argv[1]
blob = line.split(chr(9), 2)[1]
actions = json.loads(base64.b64decode(blob))
session = next(a for a in actions if a['payload'].get('kind') == 'session')
print(session['payload']['repo_path'])
" "$repo_line" 2>/dev/null || true)"
  check "repo_path matches the shorthand-named local clone via its git remote" "$decoded" "$FAKE_CODE_DIR/bigproj"
}
test_repo_path_git_remote_autodetect

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
  {"key": "alt-s", "label": "session", "payload": {"kind": "session", "repo_path": "/tmp/repo", "slug": "test-pr", "item_id": "42"}, "_plugin": "github"},
  {"key": "alt-l", "label": "lumen", "payload": {"kind": "lumen", "url": "https://github.com/myorg/kb/pull/42"}, "_plugin": "github"},
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
  {"key": "alt-s", "label": "session", "payload": {"kind": "session", "identifier": "ABC-1"}, "_plugin": "linear"}
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
echo "== run_dashboard(): the bare (no-args) interactive loop =="

DASH_BIN="$WORK/bin-dashboard"
mkdir -p "$DASH_BIN"
DASH_FZF_LOG="$WORK/dashboard-fzf-calls.log"
DASH_OPEN_LOG="$WORK/dashboard-open.log"
: > "$DASH_FZF_LOG"; : > "$DASH_OPEN_LOG"

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

# fzf stub: first call presses alt-o on the first (only) row fed to it via
# stdin; second call returns nothing (simulates Esc), ending the loop.
cat > "$DASH_BIN/fzf" <<STUB
#!/bin/sh
echo call >> "$DASH_FZF_LOG"
calls=\$(wc -l < "$DASH_FZF_LOG")
if [ "\$calls" -eq 1 ]; then
  first_line=\$(head -1)
  printf 'alt-o\n%s\n' "\$first_line"
fi
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

# ---------------------------------------------------------------------------
echo
echo "== --help =="

check "attention --help mentions Usage" \
  "$(python3 "$ATTENTION" --help | grep -c Usage)" "1"

# ---------------------------------------------------------------------------
echo
echo "== summary: $pass passed, $fail failed =="
[ "$fail" -eq 0 ]
