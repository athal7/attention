# attention

Prioritized multi-source triage dashboard. Pulls near-term calendar events,
open reminders, GitHub PRs/issues that need your attention, and open Linear
issues into one [fzf](https://github.com/junegunn/fzf)-driven list, sorted by
urgency, with per-item hotkeys to act on a row immediately.

## Install

```sh
brew install athal7/tap/attention
```

Requires [`fzf`](https://github.com/junegunn/fzf) (installed automatically as
a dependency). Everything else below is optional, per source.

## Usage

```
attention             # run the interactive dashboard (default)
attention list        # print the current prioritized list (fzf's raw input)
attention expect-keys # print the comma-joined set of hotkeys the dashboard binds
attention act K LINE  # dispatch a single hotkey press against one dashboard row
```

In the dashboard: `Enter` runs the row's primary action (open in browser for
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
`~/.config/attention/config.json`. Everything is optional — with no config
file at all, GitHub and Linear are enabled by default (each gated only by
their own auth availability below) and calendar/reminders are enabled but
contribute nothing until you list some.

```json
{
  "codeDir": "/Users/you/code",
  "sources": {
    "calendar":  { "enabled": true, "names": ["Work"] },
    "reminders": { "enabled": true, "lists": ["Personal", "Work"] },
    "github":    { "enabled": true },
    "linear":    { "enabled": true, "apiToken": "lin_api_..." }
  }
}
```

| Key | Meaning |
|---|---|
| `codeDir` | Parent directory of your local repo clones (default `~/code`). GitHub items resolve their local checkout by matching each subdirectory's own `git remote origin` against the item's repo — not by name, so a repo cloned under a shorthand directory name still resolves. Used for the session-dispatch (`⌥s`) working directory. |
| `sources.calendar.names` | Calendar names to pull near-term events from (via [`ical`](https://github.com/BRO3886/ical) on `PATH`). |
| `sources.reminders.lists` | Reminder list names to pull open items from (via [`remindctl`](https://github.com/steipete/remindctl) on `PATH`). |
| `sources.github` | Toggle GitHub fetching (via [`gh`](https://cli.github.com) on `PATH`, already authenticated). No further config — it's always your PRs/issues. |
| `sources.linear.apiToken` | Your [Linear personal API key](https://linear.app/settings/account/security). Falls back to the `LINEAR_API_TOKEN` or `LINEAR_TOKEN` environment variable if omitted — put it there instead if you'd rather not keep a secret in a config file. |

## What each source surfaces

- **Calendar**: events today/tomorrow on the configured calendars, weighted by
  how soon they start (declined/free/cancelled events excluded).
- **Reminders**: open (incomplete) reminders on the configured lists, weighted
  by due date (overdue/due-today) or priority.
- **GitHub**: PRs where your review is requested; PRs you authored that need
  attention (failing checks, changes requested, a merge conflict, or a new
  comment from someone else); issues assigned to you; open issues in repos you
  own regardless of assignee. De-duplicated by repo+number if an item matches
  more than one of these.
- **Linear**: your assigned issues not in a completed/canceled/duplicate
  state.

A GitHub item whose title contains a Linear issue identifier (e.g. `ABC-123`)
absorbs the matching Linear item instead of listing it twice. A reminder whose
title contains a calendar event's title absorbs into that event the same way.

## Dependencies

- [`fzf`](https://github.com/junegunn/fzf) — required
- [`gh`](https://cli.github.com) — GitHub source (authenticated)
- [`ical`](https://github.com/BRO3886/ical) — calendar source
- [`remindctl`](https://github.com/steipete/remindctl) — reminders source
- [`lumen`](https://github.com/jnsahaj/lumen) — `⌥l` review-diff hotkey
- `aoe-cmd` — `⌥s` session-dispatch hotkey (your own wrapper around
  [`aoe`](https://github.com/agent-of-empires/agent-of-empires), not a
  standalone published tool)
