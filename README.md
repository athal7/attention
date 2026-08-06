# attention

Prioritized multi-source triage dashboard. Pulls near-term calendar events,
open reminders, GitHub PRs/issues that need your attention, and open Linear
issues into one [fzf](https://github.com/junegunn/fzf)-driven list, sorted by
urgency, with per-item hotkeys to act on a row immediately.

The core has no source-specific logic at all -- everything above is a
plugin (see [PLUGINS.md](PLUGINS.md)). Enable, disable, or reorder them
freely, or write your own and point `attention` at it.

## Install

```sh
brew install athal7/tap/attention
```

Requires [`fzf`](https://github.com/junegunn/fzf) (installed automatically as
a dependency). Everything else below is optional, per plugin.

## Usage

```
attention             # run the interactive dashboard (default)
attention list        # print the current prioritized list (fzf's raw input)
attention expect-keys # print the comma-joined set of hotkeys the dashboard binds
attention act K LINE  # dispatch a single hotkey press against one dashboard row
```

In the dashboard: `Enter` runs a row's primary action (open in browser for
GitHub/Linear items); every other hotkey acts immediately on the highlighted
row without leaving the list. `Esc` quits.

| Row type | Hotkeys |
|---|---|
| Calendar event | `⌥y` yank to clipboard; `⌥x` (+ digits for extra linked reminders) complete a reminder matched to this event by title |
| Reminder | `⌥x` complete |
| GitHub PR/issue | `⌥o` open · `⌥s` dispatch a work session (background) · `⌥l` review diff in [`lumen`](https://github.com/jnsahaj/lumen) · `⌥a` approve · `⌥m` merge (squash + delete branch, confirms first) · `⌥c` comment · `⌥g` add label. If the item cross-links to a Linear issue (title contains its identifier): `O`/`C`/`T` open/comment/transition the *linked Linear issue* instead |
| Linear issue | `⌥o` open · `⌥s` dispatch a work session (background) · `⌥c` comment · `⌥t` transition (lists the issue's own team's workflow states) |

Session dispatch (`⌥s`) shells out to an `aoe-cmd` binary if present on
`PATH` (a thin personal wrapper around
[`aoe`](https://github.com/agent-of-empires/agent-of-empires) that creates a
session, optionally in a new git worktree, and sends it a message); the
session/worktree-branch name is a slug derived from the item's title, not
its number.

## Configuration

Config is JSON at `$XDG_CONFIG_HOME/attention/config.json`, falling back to
`~/.config/attention/config.json`.

```json
{
  "codeDir": "/Users/you/code",
  "plugins": ["calendar", "reminders", "github", "linear"],
  "calendar":  { "names": ["Work"] },
  "reminders": { "lists": ["Personal", "Work"] },
  "github":    {},
  "linear":    { "apiToken": "lin_api_..." }
}
```

| Key | Meaning |
|---|---|
| `plugins` | Which plugins to run, in order. A bare name (`"github"`) loads a bundled plugin; anything containing `/` or ending in `.py` is a path to your own -- see [PLUGINS.md](PLUGINS.md). Missing entirely = the four bundled plugins. |
| `codeDir` | Parent directory of your local repo clones (default `~/code`). Used by the `github` plugin, which resolves each item's local checkout by matching each subdirectory's own `git remote origin` against the item's repo -- not by name, so a repo cloned under a shorthand directory name still resolves. |
| `calendar.names` | Calendar names to pull near-term events from (via [`ical`](https://github.com/BRO3886/ical) on `PATH`). Missing/empty = the plugin contributes nothing. |
| `reminders.lists` | Reminder list names to pull open items from (via [`remindctl`](https://github.com/steipete/remindctl) on `PATH`). Missing/empty = the plugin contributes nothing. |
| `github` | No config needed -- it's always your PRs/issues, via [`gh`](https://cli.github.com) on `PATH`, already authenticated. |
| `linear.apiToken` | Your [Linear personal API key](https://linear.app/settings/account/security). Falls back to the `LINEAR_API_TOKEN` or `LINEAR_TOKEN` environment variable if omitted -- put it there instead if you'd rather not keep a secret in a config file. Missing entirely = the plugin contributes nothing (no error). |

## What each bundled plugin surfaces

- **calendar**: events today/tomorrow on the configured calendars, weighted by
  how soon they start (declined/free/cancelled events excluded).
- **reminders**: open (incomplete) reminders on the configured lists, weighted
  by due date (overdue/due-today) or priority.
- **github**: PRs where your review is requested; PRs you authored that need
  attention (failing checks, changes requested, a merge conflict, or a new
  comment from someone else); issues assigned to you; open issues in repos you
  own regardless of assignee. De-duplicated by repo+number if an item matches
  more than one of these.
- **linear**: your assigned issues not in a completed/canceled/duplicate
  state.

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

- [`fzf`](https://github.com/junegunn/fzf) -- required
- [`gh`](https://cli.github.com) -- `github` plugin (authenticated)
- [`ical`](https://github.com/BRO3886/ical) -- `calendar` plugin
- [`remindctl`](https://github.com/steipete/remindctl) -- `reminders` plugin
- [`lumen`](https://github.com/jnsahaj/lumen) -- `⌥l` review-diff hotkey (github)
- `aoe-cmd` -- `⌥s` session-dispatch hotkey (github, linear; your own wrapper
  around [`aoe`](https://github.com/agent-of-empires/agent-of-empires), not a
  standalone published tool)
