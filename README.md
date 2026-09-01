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
row. Press Enter after an action to return to the dashboard. `Esc` quits.

Press `/` to filter visible rows with lowercase letters or digits. Use Backspace
to edit the filter. Use Up/Down or `j`/`k` to move, `q` to quit, and `Enter`
to run a row's primary action. Press an action letter on the highlighted row.
Lowercase `q`, `j`, and `k` are reserved for dashboard controls.
An item is marked work in progress by running a custom action configured with `"wip": true`, then unmarked by one configured with `"wip": "clear"` (see [Configuration](#configuration)). Either update lands only when the action completes successfully -- a canceled prompt or a failed command leaves the current state unchanged. Marked items show a `WORK IN PROGRESS` banner in their details. Marks persist in `$XDG_STATE_HOME/attention/wip.json`, defaulting to `~/.local/state/attention/wip.json`.

| Row type | Hotkeys |
|---|---|
| Calendar event | `y` yank to clipboard; `x` (+ digits for extra linked reminders) complete a reminder matched to this event by title |
| Reminder | `x` complete |
| GitHub PR/issue | `o` open · `a` approve · `m` merge (squash + delete branch, confirms first) · `c` comment · `g` add label. If the item cross-links to a Linear issue (title contains its identifier), that issue's open/comment/transition actions are folded in too, each labeled *(linked)*; any key that collides with an existing one is remapped to a digit, shown in the footer hint |
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
        "command": ["lumen", "diff", "{url}"]
      }
    ]
  },
  "linear":    { "apiToken": "lin_api_..." },
  "dashboard": {
    "groups": [
      { "name": "Pull Requests", "match": { "plugins": ["github"] }, "columns": ["ci", "ready", "review", "stacked"] },
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
| `codeDir` | Parent directory of your local repo clones (default `~/code`). Used by the `github` plugin, which resolves each item's local checkout by matching each subdirectory's own `git remote origin` against the item's repo -- not by name, so a repo cloned under a shorthand directory name still resolves. |
| `calendar.names` | Calendar names to pull near-term events from (via [`ical`](https://github.com/BRO3886/ical) on `PATH`). Missing/empty = the plugin contributes nothing. |
| `reminders.lists` | Reminder list names to pull open items from (via [`remindctl`](https://github.com/steipete/remindctl) on `PATH`). Missing/empty = the plugin contributes nothing. |
| `github.trackAuthors` | GitHub usernames of teammates whose open PRs to also flag when they need attention (failing checks, changes requested, a merge conflict, or a new review comment) -- same check as your own authored PRs. Missing/empty = no extra queries. |
| `github.botReviewAllowlist` | GitHub bot logins (e.g. `"coderabbitai[bot]"`, with or without the suffix) whose review comments still count toward "needs attention". Every other reviewer GitHub's API reports as a bot actor is ignored by default -- automated review noise doesn't inflate a PR's attention score. Missing/empty = no bot reviews count. |
| `github.actions` / `linear.actions` | Optional custom actions to attach to items. Each action specifies `"key"`, `"label"`, `"command"` (with `{field}` template placeholders like `{url}`, `{id}`, `{repo_path}`, `{slug}`, `{identifier}`, and `{input}` for a prompted value), optional `"background": true`, optional `"wip": true` to mark or `"wip": "clear"` to unmark the item when it runs successfully, and optional `"input"` to prompt for text or pick-one input before running (see [PLUGINS.md](PLUGINS.md)). |
| `linear.apiToken` | Your [Linear personal API key](https://linear.app/settings/account/security). Falls back to the `LINEAR_API_TOKEN` or `LINEAR_TOKEN` environment variable if omitted -- put it there instead if you'd rather not keep a secret in a config file. Missing entirely = the plugin contributes nothing (no error). |
| `dashboard.groups` | Optional ordered terminal-dashboard groups. Each entry has a unique `name` and either a `match` object (`plugins`, `contexts`, `contextPrefixes`, and/or `statuses`) or `fallback: true`. `contextPrefixes` matches item contexts that start with one of its values. Optional `columns` is a non-empty list of shared indicator keys. Exactly one fallback is required when groups are configured. |

### Dashboard groups

When `dashboard.groups` is configured, `attention` opens a curses overview.
It shows the non-empty groups and their current item counts. Use Up/Down or
`j`/`k` to select a group. Press Enter to open its scoped terminal list. Esc
in a scoped list returns to the overview. An action or background refresh
reopens the same group list. This lets you act on an item or section
repeatedly without returning to the overview.

Group rules are evaluated in configuration order. Values within a rule field
are alternatives, while specified fields are combined: a rule with both
`plugins` and `statuses` matches only items satisfying both. The first matching
group wins; the fallback receives every remaining item. Invalid grouping
configuration emits a warning and uses the existing flat dashboard, so it
cannot hide attention items.

Each group can select a different table with `columns`. The GitHub source
supplies `ci`, `ready`, `review`, and `stacked` for pull requests. `ready` is
`×` for a draft and `✓` otherwise. `stacked` is `✓` when the PR targets a
non-default branch, and `×` when it targets the default branch. A combined
group can use only keys that all of its sources share. Missing values render as
`—`. Without explicit `columns`, the dashboard derives the columns from the
items in the current list.

## What each bundled plugin surfaces

- **calendar**: events today/tomorrow on the configured calendars, weighted by
  how soon they start (declined/free/cancelled events excluded).
- **reminders**: open (incomplete) reminders on the configured lists, weighted
  by due date (overdue/due-today) or priority.
- **github**: PRs where your review is requested; PRs you (or anyone listed
  in `github.trackAuthors`) authored that need attention (failing checks,
  changes requested, a merge conflict, or a new review comment from a
  non-bot reviewer -- see `github.botReviewAllowlist` to opt specific bots
  back in); issues assigned to you; open issues in repos you own regardless
  of assignee. Draft PRs are shown with a `DRAFT:` status prefix rather than
  hidden. De-duplicated by repo+number if an item matches more than one of
  these. Also surfaces unread GitHub notifications (`mention`, `author`,
  `state_change`, `ci_activity`) via `gh api /notifications`, which catches
  items the search-based queries miss: direct mentions, comments on your PRs
  that aren't review comments, state changes on subscribed PRs, and CI
  failures on watched repos.
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

