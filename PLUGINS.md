# Writing a plugin

`attention`'s core has no source-specific logic at all -- it loads
whichever plugins `config["plugins"]` names, calls each one's `fetch()`,
merges/prioritizes/renders what comes back, and routes hotkey presses to
whichever plugin's action produced the pressed key. The five bundled
sources (`sources/calendar.py`, `sources/reminders.py`,
`sources/github.py`, `sources/linear.py`, `sources/generic.py`) are
ordinary plugins written against this same interface -- read one of
them for a working example. `generic.py` is itself a config-only
provider engine; see "Config-only providers" below before writing a new
`.py` file for a source that's just "run a command, map its output".

## Interface

A plugin is a Python module (a single `.py` file) exposing two functions:

```python
def fetch(config: dict) -> list[dict]:
    """Return the items this plugin currently sees. `config` is the
    full attention config -- look up your own section by whatever name
    you're registered under in config["plugins"].
    """

def act(key: str, payload) -> None:
    """Run the action for `key`. Always the exact key you declared in
    fetch() -- if this action got absorbed into another item and its key
    was renamed to dodge a collision (see below), you still receive your
    original key here, never the renamed one; core tracks that mapping
    for you. `payload` is exactly whatever value you attached to that
    action's "payload" field in fetch() -- round-tripped unchanged
    (JSON-serialized in between), yours to shape however you like. Print
    whatever the user should see; raise to report failure (printed as
    "Action failed: ...", not a crash). Return `False` to signal the
    action did not complete (e.g. the user canceled a prompt or the
    command failed) -- core then skips the "wip": true auto-mark below;
    any other return value (including None) counts as completed.
    """
```

### Item shape

Enforced at the plugin boundary: right after a plugin's `fetch()`
returns, each item (and its actions) is validated, raising
`ValueError(f"plugin '<name>' returned a malformed item: <detail>")`
naming the plugin and the exact problem. That error is caught by core,
not propagated -- one malformed item discards that whole plugin's
contribution for this render (not just the bad item) and prints the
error to stderr; every other plugin still renders normally. `act()`'s
payload is validated the same way before dispatch (uncaught there, so
a malformed payload surfaces as this row's "Action failed: ..."
instead). Required fields default to nothing (must be present);
optional fields are filled in with the defaults shown below if
omitted.

```python
{
    "status":  "REVIEW REQUESTED",   # str, required -- normalized, all-caps by convention
    "context": "myorg/myrepo",       # str, required -- where this came from
    "title":   "Fix the login bug",  # str, required
    "details": "",                   # str, required -- may be empty
    "weight":  90,                   # int, required -- sort key, descending (bool rejected)
    "id":      "42",                 # str, optional (default "") -- enables cross-link merging, see below
    "absorb_note": "...",            # str, optional (default "") -- text used if THIS item gets merged into another
    "identity_key": "github:myorg/myrepo#42", # str, optional (default "") -- provider-qualified association target
    "association_keys": [],                 # list[str], optional (default []) -- identity keys this item absorbs
    "status_priority": 0,            # int, optional (default 0) -- on merge, higher wins the visible status, see below
    "indicators": {"ci": "✓"},       # dict[str, str], optional (default {}) -- shared dashboard table cells
    "kind": "issue",                 # str, optional (default "") -- dashboard type-group selector
    "actions": [                     # list, optional (default [])
        {
            "key": "o",              # str, required -- one ASCII letter or digit; dashboard reserves lowercase q, j, and k
            "label": "open",         # str, required -- short verb, shown in the footer hint
            "primary": True,         # bool, optional (default False) -- at most one per item, what plain Enter runs
            "wip": True,             # true marks, "clear" unmarks after completion; optional (default False)
            "payload": {...},        # dict, optional (default {}) -- anything JSON-serializable; yours, passed back to act()
        },
        ...
    ],
}
```

`indicators` holds compact values for table columns. Keys are shared column
identifiers such as `ci`, `ready`, `review`, `merge`, and `stacked`. Values are
visible strings, usually `✓`, `×`, `…`, or `—`. A dashboard group selects its
columns by key. An item without a selected key shows `—`.

### Indicator tables

Use the same indicator key only when it has the same meaning in every source.
For example, `ready` means that the item is ready for the next user action.
Do not use `ready` only to show that a source returned data.

```python
{
    "status": "BLOCKED",
    "context": "Payments",
    "title": "Investigate checkout timeout",
    "details": "Customer impact",
    "weight": 90,
    "id": "INC-42",
    "indicators": {
        "severity": "P1",
        "owner": "ME",
        "ready": "×",
    },
}
```

Configure a group table with the indicator keys that it should display:

```json
{
  "name": "Incidents",
  "match": { "plugins": ["incidents"] },
  "columns": ["severity", "owner", "ready"]
}
```

The dashboard displays an upper-case label for each key. A combined group
should select only shared keys. Missing keys display `—`.

Every bundled source assigns a `kind`. The default dashboard groups pull
requests, issues, reminders, and events by this value. Custom sources should
set a stable kind such as `incident` or `deployment`. Group rules can match
`kinds` in addition to `plugins`, `contexts`, `contextPrefixes`, and `statuses`.

Actions run first when the highlighted item supports that key.
`j` and `k` move through the list, `q` quits, `Enter` runs the primary action,
and any other printable character is added to the filter query.

### Cross-link merging

Core runs three generic, source-agnostic passes over every fetched item,
regardless of which plugin produced it:

1. **Declared association**: an item that lists an `association_keys`
   value absorbs the item whose `identity_key` equals one of those keys. This pass runs
   first; the declaring item is the host.
2. **ID-shaped title match**: if item A's title contains a token shaped
   like `[A-Z]+-\d+` (e.g. `ABC-123`) and some other item B's `id`
   (case-insensitive) equals that token, B is absorbed into A.
3. **Title-substring match**: if item A's title (≥4 chars, not
   "untitled") is a substring of item B's title, B is absorbed into A.

"Absorbed" means: whichever of A/B has the higher `status_priority`
(ties keep A's) keeps its `status` on the merged row; the other's
`status`/`details` collapse into a note appended to the winner's
`details` (B's `absorb_note`, if it lost, or a generic status/title
fallback). A's `weight` becomes `max(A.weight, B.weight) + 5`; and B's
`actions` get appended to A's, each renamed only if its key would
otherwise collide with one A already has (remapped to the lowest unused bare
digit)
and labeled `"<label> (linked)"`. B itself is dropped from the rendered
list. `status_priority` defaults to 0 for every plugin, so unless a
plugin opts in, the pre-existing behavior (the declaring/host item A
always keeps its own status) is unchanged.

## Registering

`config["plugins"]` is an ordered list of plugin identifiers:

- A bare name (e.g. `"github"`) resolves to a bundled
  `sources/<name>.py` next to the `attention` script.
- Anything containing `/` or ending in `.py` is treated as a direct
  filesystem path to your own module -- nothing to register or install;
  just point at the file.

```json
{ "plugins": ["calendar", "reminders", "github", "linear", "/Users/you/attention-plugins/jira.py"] }
```

Each plugin owns its own config however it likes -- by convention, put
your settings under a top-level key matching your plugin's name (e.g.
`config["jira"]`), since that's what the bundled plugins do, but nothing
in core enforces this.

Bundled plugins (e.g., `github`, `linear`, `calendar`, `reminders`) accept
an `actions` array in their config section to attach non-inherent custom actions
(shortcuts and command templates) to fetched items:

```json
{
  "github": {
    "actions": [
      {
        "key": "s",
        "label": "session",
        "background": true,
        "command": ["my-session", "-d", "{repo_path}", "--agent", "{input.tool}", "{input.command}"],
        "inputs": [
          { "name": "tool", "prompt": "Agent harness", "choices": ["opencode", "omp"], "default": "opencode" },
          { "name": "command", "prompt": "Session message", "default": "Work on issue {id} in this repo" }
        ]
      },
      {
        "key": "l",
        "label": "lumen",
        "command": ["lumen", "diff", "{url}"]
      },
      {
        "key": "p",
        "label": "priority",
        "command": ["gh", "issue", "edit", "{id}", "-R", "{repo}", "--add-label", "priority:{input}"],
        "input": { "prompt": "Priority", "choices": ["p0", "p1", "p2"] }
      }
    ]
  }
}
```

An action may declare `"input"` (a single prompt) or `"inputs"` (a list
of named prompts) to collect values before its command runs. The typed
(or chosen) values replace the reserved placeholders in the command
tokens: `{input}` for a single prompt, `{input.<name>}` for each named
entry in `"inputs"`. A plain string is a text prompt; a dict supports
`"prompt"` (defaults to the action's `"label"`), `"choices"` (a list ->
a numbered pick-one prompt instead of free text), and `"default"`
(text-mode fallback shown in the prompt and used when the user answers
with nothing; in choice mode it's the pre-selected option, picked by
pressing Enter). Prompt, choice, and default strings are themselves
`{field}` templates resolved against the item, so a default like
`"Work on issue {id} in this repo"` carries item context. Text input
reads one line; an empty answer (or Ctrl-C/EOF, or an out-of-range
choice) cancels the action, unless a `"default"` is set, in which case
answering with nothing runs the default. `{input}` and `{input.<name>}`
are reserved -- they always mean prompted values, so a record field
literally named `input` can't be referenced by name.

An action may set `"wip": true` to mark the item work in progress, or
`"wip": "clear"` to unmark it. A custom configured action like "start a
session" can flag an item, while a "stop" or "done" action can clear it.
The mark or unmark lands only when the action completes -- a canceled
prompt or a failed command leaves the current state unchanged. Both
operations are idempotent. There is no separate manual mark action; a
marked item shows a `WORK IN PROGRESS` banner in its details while it
remains in the feed.


## Config-only providers (no Python required)

For a source that's just "run a command, map its JSON output onto the
item shape" -- no cross-repo resolution, no multi-query de-duping, no
bespoke logic -- you don't need a `.py` file at all. Add `"generic"` to
`config["plugins"]` and define as many named providers as you like
under `config["generic"]`:

```json
{
  "plugins": ["github", "generic"],
  "generic": {
    "jules-prs": {
      "command": ["gh", "search", "prs", "--author=google-labs-jules[bot]",
                   "--state=open", "--json", "number,title,repository,url"],
      "status": "NEEDS REVIEW",
      "context": "{repository.nameWithOwner}",
      "title": "{title}",
      "id": "{number}",
      "indicators": {"ready": "{ready_symbol}", "owner": "{owner}"},
      "kind": "pull_request",
      "weight": 85,
      "actions": [
        {"key": "o", "label": "open", "primary": true, "command": ["open", "{url}"]}
      ]
    }
  }
}
```

Every text field (`status`/`context`/`title`/`details`/`id`, each
`indicators` value, and each action's `command` token) is a template: plain
text is used as-is, and `{dotted.path}` substitutes that field from the record
(missing paths become `""`). `weight` is a plain int, or a `{path}`
template parsed as one (falling back to `50` if that fails). An action
may set `"background": true` to dispatch via `dispatch_background`
(fire-and-forget, e.g. starting a long-running session) instead of the
default `run_cmd` (blocks, prints failures). Provider actions support
the same optional `"input"` or `"inputs"` declarations as bundled
plugins: text or pick-one prompts fill `{input}` or
`{input.<name>}` placeholders in the command.

An action may also set `"wip": true` to mark the item work in progress
when it runs, exactly as bundled-plugin actions do.

Each named provider is fetched and mapped independently -- one with a
missing/failing `command` or non-JSON-array output contributes nothing,
without affecting the others.