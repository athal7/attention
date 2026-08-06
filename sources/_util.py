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
