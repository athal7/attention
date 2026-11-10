# attention

Prioritized multi-source triage dashboard. Pulls near-term calendar events,
open reminders, GitHub PRs/issues that need your attention, and open Linear
issues into one terminal UI, sorted by urgency, with per-item hotkeys to act
on a row immediately.

The core has no source-specific logic at all -- everything above is a
plugin (see [PLUGINS.md](PLUGINS.md)). Enable, disable, or reorder them
freely, or write your own and point `attention` at it.

## Install

```sh
brew install athal7/tap/attention
```

The dashboard uses Python's built-in curses UI. Everything else below is
optional, per plugin.

## Usage

```sh
attention             # run the interactive terminal dashboard (default)
attention list        # print the current prioritized list (tab-delimited)
attention expect-keys # print the action keys in the current item list
attention act K LINE  # dispatch a single hotkey press against one dashboard row
```

In the dashboard: `Enter` runs a row's primary action (open in browser for
GitHub/Linear items); every other hotkey acts immediately on the highlighted
row. Press `r` to refresh explicitly. `Esc` quits.

Press `/` to filter visible rows with lowercase letters or digits. Use Backspace
to edit the filter. Use Up/Down or `j`/`k` to move, `q` to quit, and `Enter`
to run a row's primary action. Press an action letter on the highlighted row.
Lowercase `q`, `j`, and `k` are reserved for dashboard controls.
For items with linked actions, press `?` to show or hide those actions in the
footer. Their hotkeys work even while the linked actions are hidden.
An item is marked work in progress by running a custom action configured with `"wip": true`, then unmarked by one configured with `"wip": "clear"` (see [Configuration](#configuration)). Either update lands only when the action completes successfully -- a canceled prompt or a failed command leaves the current state unchanged. Marked items show a `WORK IN PROGRESS` banner in their details. Marks persist in `$XDG_STATE_HOME/attention/wip.json`, defaulting to `~/.local/state/attention/wip.json`.

| Row type | Hotkeys |
|---|---|
| Calendar event | `y` yank to clipboard; `x` (+ digits for extra linked reminders) complete a reminder matched to this event by title |
| Reminder | `x` complete |
| GitHub PR/issue | `o` open · `a` approve · `m` merge (only for a verified Merge ready PR; confirms, rechecks state and pins the displayed head commit) · `c` comment · `g` add label. If the item cross-links to a Linear issue (title contains its identifier), that issue's open/comment/transition actions are folded in too, each labeled *(linked)*; any key that collides with an existing one is remapped to a digit, shown in the footer hint |
| Linear issue | `o` open · `c` comment · `t` transition (lists the issue's own team's workflow states) |

## Configuration

Config is JSON at `$XDG_CONFIG_HOME/attention/config.json`, falling back to
`~/.config/attention/config.json`.

```json
{
  "codeDir": "/Users/you/code",
  "plugins": ["calendar", "reminders", "github", "linear"],
  "calendar":  { "names": ["Work"] },
  "reminders": { "lists": ["Personal", "Work"] },
  "github":    {
    "actions": [
      {
        "key": "s",
        "label": "session",
        "background": true,
        "command": ["my-session", "-d", "{repo_path}", "-n", "{slug}", "{input}"],
        "input": { "prompt": "Session message", "default": "Work on issue {id} in this repo" }
      },
      {
        "key": "l",
        "label": "lumen",
        "command": ["lumen", "diff", "{url}"],
        "terminal": true
      }
    ]
  },
  "linear":    { "apiToken": "lin_api_..." },
  "session":   {
    "agents": [
      { "repo": "athal7/attention", "tool": "omp" },
      { "repo": "athal7/*", "tool": "freebuf" },
      { "path": "~/code/work", "tool": "omp" },
      { "path": "~/code/personal", "tool": "freebuf" }
    ]
  },
  "dashboard": {
    "groups": [
      { "name": "Pull Requests", "match": { "kinds": ["pull_request"] }, "columns": ["state"] },
      { "name": "Needs Attention", "match": { "statuses": ["OVERDUE"] }, "columns": ["due"] },
      { "name": "My Repositories", "match": { "contextPrefixes": ["athal7/"] } },
      { "name": "Ready for Something New", "fallback": true }
    ]
  }
}
```

| Key | Meaning |
|---|---|
| `plugins` | Which plugins to run, in order. A bare name (`"github"`) loads a bundled plugin; anything containing `/` or ending in `.py` is a path to your own -- see [PLUGINS.md](PLUGINS.md). Missing entirely = the five bundled plugins. |
| `codeDir` | Parent directory of local repo clones (default `~/code`). The `github` plugin matches a GitHub `owner/repo` identity against verified GitHub remotes in these checkouts and AoE registered projects. It prefers one registered match, resolves bare registrations through their default-branch worktree, and leaves missing or ambiguous identities unresolved rather than guessing from a folder name or scan order. |
| `calendar.names` | Calendar names to pull near-term events from (via [`ical`](https://github.com/BRO3886/ical) on `PATH`). Missing/empty = the plugin contributes nothing. |
| `reminders.lists` | Reminder list names to pull open items from (via [`remindctl`](https://github.com/steipete/remindctl) on `PATH`). Missing/empty = the plugin contributes nothing. |
| `github.trackAuthors` | GitHub usernames of teammates whose open PRs to also flag for failing checks, changes requested, merge conflicts, or review feedback and updates -- same check as your own authored PRs. Missing/empty = no extra queries. |
| `github.actions` / `linear.actions` | Optional custom actions to attach to items. Each action specifies `"key"`, `"label"`, `"command"` (with `{field}` placeholders like `{url}`, `{id}`, `{repo_path}`, `{slug}`, `{identifier}`, and `{input}`), optional `"terminal": true` to run in a separate terminal window, optional `"background": true` for fire-and-forget commands, optional `"wip"` to update work-in-progress state, optional `"read": true` for an action that only reads (open, copy) so running it leaves the item in place instead of moving it down the list, and optional `"input"`/`"inputs"` prompts (see [PLUGINS.md](PLUGINS.md)). GitHub action templates also expose `{head_branch}` and `{base_branch}` for PR worktrees (empty when unavailable). An action labeled `session` or `New session` also gets the agent `session.agents` resolves for the item, both as `AOE_TOOL` in the action process's environment and as the `{session_tool}` placeholder. |
| `linear.apiToken` | Your [Linear personal API key](https://linear.app/settings/account/security). Falls back to the `LINEAR_API_TOKEN` or `LINEAR_TOKEN` environment variable if omitted -- put it there instead if you'd rather not keep a secret in a config file. Missing entirely = the plugin contributes nothing (no error). |
| `session.agents` | Which agent AoE sessions start per repository or directory. Each entry pairs an AoE `tool` (`aoe add --tool`, e.g. `omp` or `freebuf`) with either a GitHub `repo` -- `"owner/name"` or an `"owner/*"` owner prefix, matched case-insensitively from the item's repository -- or a `path` (a directory; `~` is expanded). An exact repository match beats an owner prefix; the longest configured directory containing the item's project directory is the fallback. This drives both the generated `New session` action and configured actions labeled `session`/`New session`. Items matching no entry keep AoE's default agent. An invalid entry fails the refresh instead of starting the wrong agent. |
| `dashboard.groups` | Optional ordered terminal-dashboard groups. Without it, the dashboard groups pull requests, issues, reminders, events, and other items by type. Each entry has a unique `name` and either a `match` object (`plugins`, `kinds`, `contexts`, `contextPrefixes`, `statuses`, `authors`, and/or `any`) or `fallback: true`. `contextPrefixes` matches an item's `context` or the provider-qualified identity it reports as `repo`, so an organization prefix groups GitHub items even though their rows show only the repository name. `any` is a non-empty list of match objects; any one alternative may match, combined with the outer match fields. Optional `columns` is a non-empty list of shared indicator keys. Exactly one fallback is required when groups are configured. |

### Dashboard groups

The dashboard opens with type groups for pull requests, issues, reminders,
events, and other items. Use Up/Down or `j`/`k` to select a group and Enter
to expand or collapse it in place. Select an item and press Enter for its
primary action. Press `/` to filter, `r` to refresh, or Esc to quit. The
dashboard fetches once on open and only refreshes on request.

Group rules are evaluated in configuration order. Values within a rule field
are alternatives, while specified fields are combined: a rule with both
`kinds` and `statuses` matches only items satisfying both. Use `any` for OR
alternatives across fields; outer fields remain ANDed with the alternatives:

```json
{ "name": "My Work", "match": {
  "plugins": ["github"],
  "any": [
    { "kinds": ["pull_request"], "statuses": ["NEEDS"] },
    { "contexts": ["Release"] }
  ]
} }
```

The first matching group wins; the fallback receives every remaining item.
Invalid grouping configuration emits a warning and uses the existing flat
dashboard, so it cannot hide attention items.

`contextPrefixes` matches the start of an item's `context` or, when a source
reports one, its `repo` identity. The bundled `github` plugin shows only the
repository name as context but reports the full `owner/repo` as `repo`, so
organization prefixes such as `"myorg/"` group its items without widening
their rows.

`authors` matches the item's author login exactly, including case. GitHub pull
requests provide their actual author login when available, including for review
requests and notifications. GitHub bot logins use the canonical `[bot]` suffix:

```json
{ "name": "Dependabot", "match": {
  "plugins": ["github"],
  "kinds": ["pull_request"],
  "authors": ["dependabot[bot]"]
} }
```

Place this rule before broader pull-request rules and include a fallback group.
Items without author metadata do not match an author rule.

Each group can select a different table with `columns`. Bundled groups use one
compact `state` column by default. Its value is a short status label such as
`Ready`, `Draft`, `CI failing`, or `Review requested`. `Draft` replaces less
relevant failure or review labels. Status values are shown as glyphs without
words. Visible pull request stacks are shown as trees within each group.
GitHub pull requests also provide optional `ci`, `draft`, `review`, `merge`,
and `target` indicators for explicit column configurations.

Glyphs are the same one-character marks the aoe-github plugin paints on its
session rows: `✓` approved, ready, or merged; `✔`/`✘` CI passing/failing; `~`
running or happening now; `⇨` CI queued; `?` unknown; `✗` changes requested,
blocked, or overdue; `❝` commented or a review update; `↻` pushed since review;
`⧗` awaiting someone else's review or due soon; `◷` partially approved; `⊘` merge
conflict; `⇉` in a merge queue; `✉` unresolved review comments (reply needed);
`◌` draft or not started; `⇄` open; `◉` your review requested by someone else,
which is the plugin's own "awaiting your review" status. `⚑` (notification) and
`◍` (assigned or in progress) are local marks for states that plugin has no
equivalent for. `◉` never stands in for feedback you owe a reply to, so a request
for your review is distinguishable from a review that landed, and `⧗` only ever
means waiting on someone else. Every mark is a single narrow cell, and no two
distinct outcomes share a mark: synonyms such as `Ready`/`Merge ready` or
`Approved`/`Done`/`Merged` deliberately share one, because the mark names the
outcome rather than the word a source chose for it.

## What each bundled plugin surfaces

- **calendar**: events today/tomorrow on the configured calendars, weighted by
  how soon they start (declined/free/cancelled events excluded).
- **reminders**: open (incomplete) reminders on the configured lists, weighted
  by due date (overdue/due-today) or priority.
- **github**: PRs where your review is requested; your authored PRs that need
  attention; issues assigned to you; and unassigned issues in repositories you
  own, for triage. Issues assigned to someone else are excluded. Direct
  mentions and comments on your work are included. Passive subscription state
  changes and CI activity from watched repositories are excluded. Configured
  `github.trackAuthors` entries remain an explicit opt-in for teammate PRs.
  Review requests are omitted when another confirmed person has submitted a
  review, unless you have also submitted a review; a PR assigned to another
  user is still omitted. Bots, pending and dismissed reviews, and your own
  assignment do not count. A review whose author type is unknown does not hide
  a request, including when the detail fallback lacks bot metadata. PR details
  are fetched in bounded batches with per-PR fallback.
  Search and notification validation reuse complete PR and repository state
  within a refresh; missing state is fetched separately.
  Merge ready requires an open, non-draft PR with approved review, a clean
  merge state, a known mergeable result, and complete successful CI checks.
  It ranks above NOTIFIED. Unknown, incomplete, pending, or failing state is
  never treated as ready. The merge action is available only on ready authored
  or explicitly tracked PRs; it is user-triggered, rechecks readiness after
  confirmation, and pins the displayed head commit. No automatic merging.
  Eligible unread notifications appear in the default Other group unless they
  concern a pull request. Author notifications use NOTIFIED rather than claiming
  a reply is needed: the unread event can be your own or a bot's activity. Read
  threads, closed PR/issue threads, and threads from archived repositories are
  omitted. For an unread authored PR, an ordinary bot timeline comment is
  filtered only when `last_read_at` is a valid
  timezone-aware timestamp and the complete paginated PR timeline since that
  time contains at least one later event, all ordinary bot-authored comments.
  Reviews, commits, unknown activity, missing/equal comment timestamps, and
  failed or incomplete timelines remain visible. `latest_comment_url` selects
  candidates for this check, not the event that triggered the notification.
  CHANGES_REQUESTED reviews count regardless of reviewer type, including bots;
  APPROVED reviews do not. For COMMENTED reviews with complete detail, an
  unresolved review thread needs a reply only when its last commenter is not
  the PR author. Resolved threads and threads last answered by the author do
  not. A later PR conversation comment from the author also clears body-only
  review feedback. These comparisons use the PR author from detail, falling
  back to the current GitHub login only if that detail is unavailable.
  Body-only feedback with no thread history still counts unless the author
  has replied afterward. A review body not associated with the PR's other
  threads remains visible as REVIEW UPDATE without asserting that a reply is
  due. Missing or incomplete review detail also stays visible as REVIEW UPDATE
  Ordinary PR timeline comments are not reviews. Confirmed review feedback
  keeps NEEDS ATTENTION even when another review request is pending; a pending
  request without such feedback can show REVIEW REQUESTED. Items are
  de-duplicated by repository and number.
  Visible PRs that target another visible PR's head branch render as a tree,
  with the target PR above its child.
- **linear**: your assigned issues, not in a completed/canceled/duplicate
  state, in your current cycle. Its status always wins on a cross-linked
  dashboard row: a PR title/body mentioning the issue's identifier folds
  into one row showing the Linear workflow state, not the PR's review/CI
  status (see [PLUGINS.md](PLUGINS.md#cross-link-merging)).
- **generic**: config-only providers -- run a command, map its JSON output
  onto the item shape via `{dotted.path}` templates. See
  [PLUGINS.md](PLUGINS.md#config-only-providers-no-python-required) for
  the config shape; contributes nothing until you define a provider under
  `config["generic"]`.

Two generic, source-agnostic passes then cross-link items regardless of
which plugin produced them: an item whose title mentions another item's
ticket-shaped id (e.g. a GitHub PR title mentioning `ABC-123` absorbs the
Linear issue `ABC-123`), and an item whose title is a substring of
another's (e.g. a reminder titled "Prep for Team Dinner" absorbs into a
calendar event titled "Team Dinner"). See
[PLUGINS.md](PLUGINS.md#cross-link-merging) for the exact rules.

## Writing your own plugin

See [PLUGINS.md](PLUGINS.md) -- a plugin is a single `.py` file exposing
`fetch(config)` and `act(key, payload)`. No registration, no packaging;
just point `config["plugins"]` at it.

## Dependencies

- [`gh`](https://cli.github.com) -- `github` plugin (authenticated)
- [`ical`](https://github.com/BRO3886/ical) -- `calendar` plugin
- [`remindctl`](https://github.com/steipete/remindctl) -- `reminders` plugin


The `attention` GitHub source does not persist a snapshot locally. It uses
`gh search`; `gh api --cache` covers GET requests and does not cache those
searches. No stale snapshot is shown as current.
