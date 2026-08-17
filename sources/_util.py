"""Shared helpers for the bundled plugins in this directory. Not part of
the plugin interface itself -- a third-party plugin living elsewhere is
free to import this too (core puts every plugin's own directory on
sys.path before loading it, and this file sits next to the bundled
plugins), but nothing requires it.
"""
import os
import re
import subprocess
import tempfile


def slugify(text, max_len=50):
    """Filesystem/git-branch/tmux-safe slug derived from a title, e.g.
    "Fix the login bug!" -> "fix-the-login-bug". Trimmed at a hyphen
    boundary so it never cuts mid-word.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    if len(slug) > max_len:
        slug = slug[:max_len].rsplit("-", 1)[0]
    return slug or "session"


def run_cmd(cmd):
    try:
        print(f"Running: {' '.join(cmd)}")
        subprocess.run(cmd, check=True)
    except Exception as e:
        print(f"Command failed: {e}")


def dispatch_background(cmd):
    """Launch a fire-and-forget subprocess without blocking the caller --
    e.g. a work-session dispatch can take up to ~30s (worktree creation
    plus a readiness poll) before it even sends anything, and the
    dashboard has no reason to sit frozen for that. Detached from this
    process's stdio/session so it survives past this call returning;
    output goes to a per-dispatch log file since the terminal may already
    be showing the next dashboard render by the time it produces any.
    """
    log_fd, log_path = tempfile.mkstemp(prefix="attention-dispatch-", suffix=".log")
    with os.fdopen(log_fd, "w") as log_file:
        try:
            subprocess.Popen(
                cmd, stdout=log_file, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL, start_new_session=True,
            )
            print(f"Dispatched in background (log: {log_path}): {' '.join(cmd)}")
        except Exception as e:
            print(f"Failed to dispatch: {e}")


def copy_to_clipboard(text):
    try:
        p = subprocess.Popen(["pbcopy"], stdin=subprocess.PIPE)
        p.communicate(input=text.encode("utf-8"))
        print("Copied to clipboard!")
    except Exception as e:
        print(f"Failed to copy: {e}")


_PLACEHOLDER = re.compile(r"\{([\w.]+)\}")

_INPUT_PLACEHOLDER = "input"


def _lookup(record, path):
    val = record
    for part in path.split("."):
        if not isinstance(val, dict):
            return None
        val = val.get(part)
    return val


def resolve_template(template, record):
    if not isinstance(template, str):
        return template

    def _sub(m):
        name = m.group(1)
        if name == _INPUT_PLACEHOLDER or name.startswith(_INPUT_PLACEHOLDER + "."):
            return m.group(0)
        val = _lookup(record, name)
        return "" if val is None else str(val)

    return _PLACEHOLDER.sub(_sub, template)


def _resolve_input_spec(raw, label, record):

    if raw is None:
        return None
    if isinstance(raw, str):
        prompt, choices, default = raw, None, None
    elif isinstance(raw, dict):
        prompt = raw.get("prompt")
        choices = raw.get("choices")
        default = raw.get("default")
    else:
        return None

    prompt = resolve_template(prompt if prompt else (label or "Input"), record)
    if isinstance(choices, list):
        choices = [str(resolve_template(c, record)) for c in choices]
    else:
        choices = None
    if default is not None:
        default = resolve_template(default, record)

    spec = {"prompt": prompt}
    if choices:
        spec["choices"] = choices
    if default is not None:
        spec["default"] = default
    return spec


def _resolve_input_specs(action, label, record):

    raw_inputs = action.get("inputs")
    if isinstance(raw_inputs, list):
        specs = []
        for raw in raw_inputs:
            if not isinstance(raw, dict):
                continue
            name = raw.get("name", "")
            if not name:
                continue
            spec = _resolve_input_spec(raw, label, record)
            if spec is not None:
                spec["name"] = name
                specs.append(spec)
        return specs
    spec = _resolve_input_spec(action.get("input"), label, record)
    if spec is None:
        return []
    spec["name"] = ""
    return [spec]


def resolve_configured_actions(configured_actions, record):
    if not isinstance(configured_actions, list):
        return []
    actions = []
    for a in configured_actions:
        if not isinstance(a, dict):
            continue
        key = a.get("key", "")
        if not key:
            continue
        cmd_template = a.get("command", [])
        if isinstance(cmd_template, list):
            cmd = [resolve_template(tok, record) for tok in cmd_template]
        else:
            cmd = []
        payload = {
            "command": cmd,
            "background": a.get("background", False),
        }
        input_specs = _resolve_input_specs(a, a.get("label", ""), record)
        if input_specs:
            payload["inputs"] = input_specs
        actions.append({
            "key": key,
            "label": a.get("label", ""),
            "primary": a.get("primary", False),
            "payload": payload,
        })
    return actions


def prompt_for_input(spec):

    prompt = spec.get("prompt") or "Input"
    choices = spec.get("choices")
    default = spec.get("default")

    if choices:
        print()
        for i, c in enumerate(choices, 1):
            print(f"{i}) {c}")
        default_idx = 0
        if default in choices:
            default_idx = choices.index(default) + 1
        prompt_suffix = f" [1-{len(choices)}]"
        if default_idx:
            prompt_suffix += f" (Enter={default_idx})"
        try:
            raw = input(f"{prompt}{prompt_suffix}: ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\nCanceled.")
            return None
        if raw == "" and default_idx:
            return choices[default_idx - 1]
        if raw.isdigit() and 1 <= int(raw) <= len(choices):
            return choices[int(raw) - 1]
        print("Invalid choice.")
        return None

    if default is not None:
        prompt = f"{prompt} [{default}]"
    try:
        raw = input(f"{prompt}: ").strip()
    except (KeyboardInterrupt, EOFError):
        print("\nCanceled.")
        return None
    if raw:
        return raw
    if default is not None:
        return default
    print("Canceled.")
    return None


def run_configured_action(payload):
    command = payload.get("command")
    if not command:
        print("No command configured.")
        return
    specs = payload.get("inputs")
    if specs:
        values = {}
        for spec in specs:
            name = spec.get("name", "")
            value = prompt_for_input(spec)
            if value is None:
                return
            values[name] = value

        def _fill(tok):
            if not isinstance(tok, str):
                return tok
            for name, value in values.items():
                placeholder = "{input}" if not name else f"{{input.{name}}}"
                tok = tok.replace(placeholder, value)
            return tok

        command = [_fill(tok) for tok in command]
    if payload.get("background"):
        dispatch_background(command)
    else:
        run_cmd(command)
