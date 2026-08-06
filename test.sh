#!/usr/bin/env bash
# Regression tests for the `attention` CLI. Plain bash, no bats. Stubs
# gh/remindctl/ical/security/fzf/aoe-cmd so the script runs against fixed
# fixture data instead of live system state, and writes a JSON config file
# at a temp $XDG_CONFIG_HOME instead of touching the real one.
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

# Portable "clearly yesterday" ISO date (matches the yyyy-mm-dd prefix the
# script parses via date.fromisoformat(due[:10])).
YESTERDAY="$(date -v-1d +%F 2>/dev/null || date -d yesterday +%F)"

# ---------------------------------------------------------------------------
echo "== reminder source: enabled lists, priority, overdue status =="

write_config <<JSON
{
  "sources": {
    "calendar": {"enabled": false},
    "reminders": {"enabled": true, "lists": ["Personal", "Work"]},
    "github": {"enabled": false},
    "linear": {"enabled": false}
  }
}
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
  out="$(HOME="$TEST_HOME" XDG_CONFIG_HOME="$XDG_CONFIG" PATH="$STUB_BIN:$PATH" env -u LINEAR_API_TOKEN -u LINEAR_TOKEN python3 "$ATTENTION" list)"

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
echo "== calendar source: configured names only, declined events excluded =="

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
{
  "sources": {
    "calendar": {"enabled": true, "names": ["Work"]},
    "reminders": {"enabled": false},
    "github": {"enabled": false},
    "linear": {"enabled": false}
  }
}
JSON

test_calendar_configured_names() {
  local out
  out="$(HOME="$TEST_HOME" XDG_CONFIG_HOME="$XDG_CONFIG" PATH="$STUB_BIN:$PATH" env -u LINEAR_API_TOKEN -u LINEAR_TOKEN python3 "$ATTENTION" list)"

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
  out="$(HOME="$TEST_HOME" XDG_CONFIG_HOME="$XDG_CONFIG" PATH="$DECLINE_BIN:$PATH" env -u LINEAR_API_TOKEN -u LINEAR_TOKEN python3 "$ATTENTION" list)"

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
echo "== github source: global search, authored-PR attention, owned-repo issues, de-dupe =="

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
{
  "sources": {
    "calendar": {"enabled": false},
    "reminders": {"enabled": false},
    "github": {"enabled": true},
    "linear": {"enabled": false}
  }
}
JSON

test_github_source() {
  local out
  out="$(HOME="$TEST_HOME" XDG_CONFIG_HOME="$XDG_CONFIG" PATH="$GH_BIN:$PATH" env -u LINEAR_API_TOKEN -u LINEAR_TOKEN python3 "$ATTENTION" list)"

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
{
  "codeDir": "$FAKE_CODE_DIR",
  "sources": {
    "calendar": {"enabled": false},
    "reminders": {"enabled": false},
    "github": {"enabled": true},
    "linear": {"enabled": false}
  }
}
JSON

test_repo_path_git_remote_autodetect() {
  local out repo_line
  out="$(HOME="$TEST_HOME" XDG_CONFIG_HOME="$XDG_CONFIG" PATH="$REPODIR_BIN:$PATH" env -u LINEAR_API_TOKEN -u LINEAR_TOKEN python3 "$ATTENTION" list)"
  repo_line="$(grep 'REPODIRTEST-shorthand' <<<"$out" || true)"
  case "$repo_line" in
    *"REPO_PATH:$FAKE_CODE_DIR/bigproj"*) ok "repo_path matches the shorthand-named local clone via its git remote" ;;
    *) bad "repo_path matches the shorthand-named local clone via its git remote (got: $repo_line)" ;;
  esac
}
test_repo_path_git_remote_autodetect

# ---------------------------------------------------------------------------
echo
echo "== config defaults: missing config file, missing sources key =="

rm -f "$XDG_CONFIG/attention/config.json"

test_no_config_defaults_github_enabled() {
  local out
  out="$(HOME="$TEST_HOME" XDG_CONFIG_HOME="$XDG_CONFIG" PATH="$GH_BIN:$PATH" env -u LINEAR_API_TOKEN -u LINEAR_TOKEN python3 "$ATTENTION" list)"
  if grep -q 'GHTEST-review-me' <<<"$out"; then
    ok "with no config file at all, github source defaults to enabled"
  else
    bad "with no config file at all, github source defaults to enabled (got: $out)"
  fi
}
test_no_config_defaults_github_enabled

# ---------------------------------------------------------------------------
echo
echo "== linear source: state.type filter (not fragile name match), no pagination truncation, project as context =="

# fetch_linear() hits the network directly (urllib), which a bash-level PATH
# stub can't intercept, so this loads the module in-process and monkeypatches
# urlopen with a fake response, capturing the outgoing GraphQL request body.
LOAD_MODULE="
import importlib.util
from importlib.machinery import SourceFileLoader
loader = SourceFileLoader('attention_mod', '$ATTENTION')
spec = importlib.util.spec_from_loader('attention_mod', loader)
m = importlib.util.module_from_spec(spec)
loader.exec_module(m)
"

test_fetch_linear_functional() {
  local out
  out="$(python3 -c "
$LOAD_MODULE
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

m.get_linear_token = lambda: 'fake-token'
m.urllib.request.urlopen = fake_urlopen

result = m.fetch_linear()
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
    *'"identifier": "ABC-1"'*'"name": "Backend"'*)
      ok "fetch_linear() returns the issue with its project available for CONTEXT" ;;
    *) bad "fetch_linear() returns the issue with its project available for CONTEXT (got: $out)" ;;
  esac
}
test_fetch_linear_functional

# ---------------------------------------------------------------------------
echo
echo "== get_linear_token(): config apiToken, env var fallback, no keychain coupling =="

test_linear_token_from_config() {
  write_config <<'JSON'
{"sources": {"linear": {"enabled": true, "apiToken": "config-token"}}}
JSON
  local token
  token="$(HOME="$TEST_HOME" XDG_CONFIG_HOME="$XDG_CONFIG" env -u LINEAR_API_TOKEN -u LINEAR_TOKEN python3 -c "
$LOAD_MODULE
print(m.get_linear_token())
")"
  check "get_linear_token() returns sources.linear.apiToken from config" "$token" "config-token"
}
test_linear_token_from_config

test_linear_token_env_fallback() {
  write_config <<'JSON'
{"sources": {"linear": {"enabled": true}}}
JSON
  local token
  token="$(HOME="$TEST_HOME" XDG_CONFIG_HOME="$XDG_CONFIG" LINEAR_API_TOKEN="env-token" python3 -c "
$LOAD_MODULE
print(m.get_linear_token())
")"
  check "falls back to LINEAR_API_TOKEN env var when config has no apiToken" "$token" "env-token"
}
test_linear_token_env_fallback

test_linear_token_config_takes_precedence_over_env() {
  write_config <<'JSON'
{"sources": {"linear": {"enabled": true, "apiToken": "config-token"}}}
JSON
  local token
  token="$(HOME="$TEST_HOME" XDG_CONFIG_HOME="$XDG_CONFIG" LINEAR_API_TOKEN="env-token" python3 -c "
$LOAD_MODULE
print(m.get_linear_token())
")"
  check "config apiToken takes precedence over the env var" "$token" "config-token"
}
test_linear_token_config_takes_precedence_over_env

echo
echo "== slugify() title-to-slug =="

check "slugify: lowercases and hyphenates" \
  "$(python3 -c "$LOAD_MODULE
print(m.slugify('Fix the login bug!'))")" \
  "fix-the-login-bug"
check "slugify: collapses repeated punctuation/whitespace" \
  "$(python3 -c "$LOAD_MODULE
print(m.slugify('  Weird---Title!!  '))")" \
  "weird-title"
check "slugify: trims to max_len at a hyphen boundary, never mid-word" \
  "$(python3 -c "$LOAD_MODULE
print(m.slugify('one two three four five six seven', max_len=15))")" \
  "one-two-three"
check "slugify: empty/non-alnum input falls back to a generic slug" \
  "$(python3 -c "$LOAD_MODULE
print(m.slugify('!!!'))")" \
  "session"

# ---------------------------------------------------------------------------
echo
echo "== actions_for()/expect_keys() single-source-of-truth parity =="

check_keys() {
  local label="$1" expected="$2" py_call="$3"
  local actual
  actual="$(python3 -c "
$LOAD_MODULE
print(','.join(k for k, _ in $py_call))
")"
  check "$label" "$actual" "$expected"
}

check_keys "CAL (no linked reminders) keys" "alt-y" \
  "m.actions_for('CAL')"
check_keys "GH (no Linear link) keys" "alt-o,alt-s,alt-l,alt-a,alt-m,alt-c,alt-g" \
  "m.actions_for('GH')"
check_keys "GH (with Linear cross-link) keys" "alt-o,alt-s,alt-l,alt-a,alt-m,alt-c,alt-g,O,C,T" \
  "m.actions_for('GH', has_linear=True)"
check_keys "LIN keys" "alt-o,alt-s,alt-c,alt-t" \
  "m.actions_for('LIN')"

test_expect_keys_is_union() {
  local expected actual
  expected="$(python3 -c "
$LOAD_MODULE
contexts = [
    m.actions_for('CAL', rem_ids=[f'r{i}' for i in range(1, 10)]),
    m.actions_for('REM'),
    m.actions_for('GH', has_linear=False),
    m.actions_for('GH', has_linear=True),
    m.actions_for('LIN'),
]
seen = set(); keys = []
for actions in contexts:
    for key, _ in actions:
        if key not in seen:
            seen.add(key); keys.append(key)
print(','.join(keys))
")"
  actual="$(PATH="$STUB_BIN:$PATH" python3 "$ATTENTION" expect-keys)"
  check "expect-keys output equals the union of all actions_for() keys" "$actual" "$expected"
}
test_expect_keys_is_union

# ---------------------------------------------------------------------------
echo
echo "== act(key, line) dispatch =="

INTERACT_BIN="$WORK/bin-act-interact"
mkdir -p "$INTERACT_BIN"

OPEN_LOG="$WORK/open-invocations.log"; : > "$OPEN_LOG"
REMINDCTL_ACT_LOG="$WORK/remindctl-act-invocations.log"; : > "$REMINDCTL_ACT_LOG"
GH_ACT_LOG="$WORK/gh-act-invocations.log"; : > "$GH_ACT_LOG"
PBCOPY_LOG="$WORK/pbcopy-invocations.log"; : > "$PBCOPY_LOG"
AOE_CMD_LOG="$WORK/aoe-cmd-invocations.log"; : > "$AOE_CMD_LOG"

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

cat > "$INTERACT_BIN/aoe-cmd" <<STUB
#!/bin/sh
# Deliberately slow (mirrors aoe-cmd's real ~2-30s worktree+readiness-poll
# latency) -- proves act() dispatches this in the background instead of
# blocking the caller on it.
sleep 1
echo "\$*" >> "$AOE_CMD_LOG"
exit 0
STUB
chmod +x "$INTERACT_BIN/aoe-cmd"

TAB=$'\t'
FIX_GH_LINE="REVIEW REQUESTED  myorg/kb  Test PR   ${TAB}| TYPE:GH | ID:42 | SLUG:test-pr | DATABASE_ID: | URL:https://github.com/myorg/kb/pull/42 | REPO_PATH:/tmp/repo | LINEAR_DB_ID: | LINEAR_URL: | REMINDER_ID:${TAB}hint"
FIX_GH_LINKED_LINE="REVIEW REQUESTED  myorg/kb  Test PR  Linear ABC-1: TODO ${TAB}| TYPE:GH | ID:42 | SLUG:test-pr | DATABASE_ID: | URL:https://github.com/myorg/kb/pull/42 | REPO_PATH:/tmp/repo | LINEAR_DB_ID:db-uuid | LINEAR_URL:https://linear.app/abc/issue/ABC-1 | REMINDER_ID:${TAB}hint"
FIX_REM_LINE="HIGH  Personal  Buy milk   ${TAB}| TYPE:REM | ID:r1 | SLUG:buy-milk | DATABASE_ID: | URL: | REPO_PATH: | LINEAR_DB_ID: | LINEAR_URL: | REMINDER_ID:${TAB}hint"
FIX_CAL_LINE="ALL DAY  Work  Team Sync   ${TAB}| TYPE:CAL | ID:e1 | SLUG:team-sync | DATABASE_ID: | URL: | REPO_PATH: | LINEAR_DB_ID: | LINEAR_URL: | REMINDER_ID:${TAB}hint"
FIX_CAL_MULTI_LINE="ALL DAY  Work  Team Sync  Reminder: prep ${TAB}| TYPE:CAL | ID:e1 | SLUG:team-sync | DATABASE_ID: | URL: | REPO_PATH: | LINEAR_DB_ID: | LINEAR_URL: | REMINDER_ID:r1,r2,r3${TAB}hint"
FIX_LIN_LINE="TODO  MyProject  Fix bug   ${TAB}| TYPE:LIN | ID:ABC-1 | SLUG:fix-bug | DATABASE_ID:db-uuid | URL:https://linear.app/abc/issue/ABC-1 | REPO_PATH: | LINEAR_DB_ID: | LINEAR_URL: | REMINDER_ID:${TAB}hint"

run_act() {
  local key="$1" line="$2" stdin="${3-}"
  ACT_OUTPUT="$(printf '%s' "$stdin" | HOME="$TEST_HOME" XDG_CONFIG_HOME="$XDG_CONFIG" PATH="$INTERACT_BIN:$PATH" \
    env -u LINEAR_API_TOKEN -u LINEAR_TOKEN python3 "$ATTENTION" act "$key" "$line" 2>&1)" && ACT_RC=0 || ACT_RC=$?
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
  ok "CAL alt-y copies the row to the clipboard"
else
  bad "CAL alt-y copies the row to the clipboard (got: $(cat "$PBCOPY_LOG"))"
fi

echo
echo "-- CAL multi-reminder overflow: alt-x is first reminder, digits are the rest --"

: > "$REMINDCTL_ACT_LOG"
run_act "alt-x" "$FIX_CAL_MULTI_LINE"
check "CAL alt-x (first reminder) exits 0" "$ACT_RC" "0"
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
echo "-- Linear cross-link plain-uppercase keys (O/C/T) --"

: > "$OPEN_LOG"
run_act "O" "$FIX_GH_LINKED_LINE"
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
  ok "key 'O' with no Linear link is a no-op"
fi

echo
echo "-- Enter (empty key): primary action for GH/LIN, no-op for CAL/REM --"

: > "$OPEN_LOG"
run_act "" "$FIX_GH_LINE"
check "Enter on GH exits 0" "$ACT_RC" "0"
if grep -q 'https://github.com/myorg/kb/pull/42' "$OPEN_LOG"; then
  ok "Enter on GH opens in browser (same as alt-o)"
else
  bad "Enter on GH opens in browser (same as alt-o) (got: $(cat "$OPEN_LOG"))"
fi

: > "$OPEN_LOG"
run_act "" "$FIX_LIN_LINE"
check "Enter on LIN exits 0" "$ACT_RC" "0"
if grep -q 'https://linear.app/abc/issue/ABC-1' "$OPEN_LOG"; then
  ok "Enter on LIN opens in browser (same as alt-o)"
else
  bad "Enter on LIN opens in browser (same as alt-o) (got: $(cat "$OPEN_LOG"))"
fi

: > "$PBCOPY_LOG"; : > "$REMINDCTL_ACT_LOG"
run_act "" "$FIX_CAL_LINE"
check "Enter on CAL exits 0 (no-op)" "$ACT_RC" "0"
if [ -s "$PBCOPY_LOG" ] || [ -s "$REMINDCTL_ACT_LOG" ]; then
  bad "Enter on CAL must not invoke any action (pbcopy: $(cat "$PBCOPY_LOG"); remindctl: $(cat "$REMINDCTL_ACT_LOG"))"
else
  ok "Enter on CAL takes no action"
fi

: > "$REMINDCTL_ACT_LOG"
run_act "" "$FIX_REM_LINE"
check "Enter on REM exits 0 (no-op)" "$ACT_RC" "0"
if [ -s "$REMINDCTL_ACT_LOG" ]; then
  bad "Enter on REM must not invoke any action (got: $(cat "$REMINDCTL_ACT_LOG"))"
else
  ok "Enter on REM takes no action"
fi

echo
echo "-- unmapped key for a row's type: brief note, exit 0, no traceback --"

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
echo "-- merge gate (alt-m): confirm_and_merge() runs as a plain input() prompt --"

: > "$GH_ACT_LOG"
run_act "alt-m" "$FIX_GH_LINE" "y
"
check "merge confirm 'y' exits 0" "$ACT_RC" "0"
if grep -q 'pr merge --squash --delete-branch 42 --repo myorg/kb' "$GH_ACT_LOG"; then
  ok "merge confirm 'y' invokes gh pr merge --squash --delete-branch"
else
  bad "merge confirm 'y' invokes gh pr merge --squash --delete-branch (got: $(cat "$GH_ACT_LOG"))"
fi

: > "$GH_ACT_LOG"
run_act "alt-m" "$FIX_GH_LINE" "n
"
check "merge confirm 'n' exits 0" "$ACT_RC" "0"
if [ -s "$GH_ACT_LOG" ]; then
  bad "merge confirm 'n' must not invoke gh pr merge (got: $(cat "$GH_ACT_LOG"))"
else
  ok "merge confirm 'n' does not invoke gh pr merge"
fi

: > "$GH_ACT_LOG"
run_act "alt-m" "$FIX_GH_LINE" ""
check "merge confirm EOF (no stdin) exits 0, canceled gracefully" "$ACT_RC" "0"
if [ -s "$GH_ACT_LOG" ]; then
  bad "merge confirm EOF must not invoke gh pr merge (got: $(cat "$GH_ACT_LOG"))"
else
  ok "merge confirm EOF does not invoke gh pr merge"
fi

echo
echo "-- session dispatch (alt-s): backgrounded (doesn't block the caller), title-based slug --"

wait_for_aoe_cmd_log() {
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    [ -s "$AOE_CMD_LOG" ] && return 0
    sleep 0.3
  done
  return 1
}

: > "$AOE_CMD_LOG"
start_ts=$(date +%s)
run_act "alt-s" "$FIX_GH_LINE"
elapsed=$(( $(date +%s) - start_ts ))
check "GH alt-s exits 0" "$ACT_RC" "0"
if [ "$elapsed" -le 1 ]; then
  ok "GH alt-s returns without waiting for the dispatched process (${elapsed}s; stub sleeps 1s)"
else
  bad "GH alt-s returns without waiting for the dispatched process (took ${elapsed}s; stub sleeps 1s)"
fi

wait_for_aoe_cmd_log
gh_session_call="$(cat "$AOE_CMD_LOG")"
case "$gh_session_call" in
  *"-n test-pr -b -w test-pr "*) ok "GH alt-s names session/worktree branch from the title slug 'test-pr'" ;;
  *) bad "GH alt-s names session/worktree branch from the title slug 'test-pr' (got: $gh_session_call)" ;;
esac
case "$gh_session_call" in
  *"issue-42"*) bad "GH alt-s must not name the session/branch after the issue number (got: $gh_session_call)" ;;
  *) ok "GH alt-s does not name the session/branch after the issue number" ;;
esac

: > "$AOE_CMD_LOG"
run_act "alt-s" "$FIX_LIN_LINE"
check "LIN alt-s exits 0" "$ACT_RC" "0"
wait_for_aoe_cmd_log
if grep -q -- '-n fix-bug ' "$AOE_CMD_LOG"; then
  ok "LIN alt-s names the session from the title slug 'fix-bug'"
else
  bad "LIN alt-s names the session from the title slug 'fix-bug' (got: $(cat "$AOE_CMD_LOG"))"
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
{
  "sources": {
    "calendar": {"enabled": false},
    "reminders": {"enabled": false},
    "github": {"enabled": true},
    "linear": {"enabled": false}
  }
}
JSON

test_run_dashboard_loop() {
  local rc
  HOME="$TEST_HOME" XDG_CONFIG_HOME="$XDG_CONFIG" PATH="$DASH_BIN:$PATH" \
    env -u LINEAR_API_TOKEN -u LINEAR_TOKEN python3 "$ATTENTION" >/dev/null 2>&1 && rc=0 || rc=$?
  check "bare invocation (no args) exits 0" "$rc" "0"
  check "fzf invoked exactly twice (render, act, re-render, Esc)" "$(cat "$DASH_FZF_LOG" | wc -l | tr -d ' ')" "2"
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
{
  "sources": {
    "calendar": {"enabled": false},
    "reminders": {"enabled": false},
    "github": {"enabled": false},
    "linear": {"enabled": false}
  }
}
JSON

test_run_dashboard_empty() {
  local out rc
  : > "$DASH_FZF_LOG"
  out="$(HOME="$TEST_HOME" XDG_CONFIG_HOME="$XDG_CONFIG" PATH="$DASH_BIN:$PATH" \
    env -u LINEAR_API_TOKEN -u LINEAR_TOKEN python3 "$ATTENTION" 2>&1)" && rc=0 || rc=$?
  check "bare invocation with nothing to show exits 0" "$rc" "0"
  check "no fzf process spawned for an empty list" "$(wc -l < "$DASH_FZF_LOG" | tr -d ' ')" "0"
  case "$out" in
    *"Nothing needs attention"*) ok "prints a friendly empty-list message" ;;
    *) bad "prints a friendly empty-list message (got: $out)" ;;
  esac
}
test_run_dashboard_empty

echo
echo "== --help =="

check "attention --help mentions Usage" \
  "$(python3 "$ATTENTION" --help | grep -c Usage)" "1"

# ---------------------------------------------------------------------------
echo
echo "== summary: $pass passed, $fail failed =="
[ "$fail" -eq 0 ]
