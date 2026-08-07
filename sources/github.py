"""github plugin -- PRs needing your review or attention, and issues
assigned to you or open in repos you own, via the `gh` CLI
(https://cli.github.com, already authenticated).

Config (config["github"]): none required. codeDir (top-level, shared
with other repo-resolving plugins) is used to resolve each item's local
checkout.
"""
import json
import os
import re
import subprocess
from pathlib import Path

from _util import dispatch_background, run_cmd, slugify


def _gh_json(args):
    """Run `gh <args...>` and parse its stdout as JSON, returning [] on
    any failure (non-zero exit, timeout, malformed JSON).
    """
    try:
        res = subprocess.run(["gh"] + args, capture_output=True, text=True, timeout=30)
        if res.returncode != 0:
            return []
        return json.loads(res.stdout or "[]")
    except Exception:
        return []


def _get_gh_login():
    try:
        res = subprocess.run(["gh", "api", "user", "--jq", ".login"], capture_output=True, text=True)
        if res.returncode == 0:
            return res.stdout.strip()
    except Exception:
        pass
    return ""


def _fetch_pr_attention(author):
    """Open PRs `author` authored that need attention right now: failing
    checks, changes requested, a merge conflict, or a comment from
    someone else. `author` is a `gh search prs --author=` value ("@me"
    for yourself, or any other GitHub username to also track a
    teammate's PRs, see config["github"]["trackAuthors"]). `gh search
    prs` (used to find the candidates) exposes none of that state --
    its `--json` fields are limited to search-index metadata -- so
    this follows up with one `gh pr view` per candidate (repo is
    already known from the search hit, so this is a targeted lookup,
    not a repo-wide scan) to pull the actionable fields.
    """
    prs = _gh_json([
        "search", "prs", f"--author={author}", "--state=open", "--limit", "50",
        "--json", "number,title,repository,url,isDraft,createdAt",
    ])
    if not prs:
        return []

    me = _get_gh_login()
    expected_author = me if author == "@me" else author
    flagged = []
    for p in prs:
        repo = p.get("repository", {}).get("nameWithOwner", "")
        number = p.get("number")
        if not repo or number is None:
            continue
        detail = _gh_json([
            "pr", "view", str(number), "-R", repo,
            "--json", "mergeable,reviewDecision,statusCheckRollup,comments,isDraft",
        ])
        if not isinstance(detail, dict):
            continue

        reasons = []
        if detail.get("reviewDecision") == "CHANGES_REQUESTED":
            reasons.append("Changes Requested")
        if detail.get("mergeable") == "CONFLICTING":
            reasons.append("Merge Conflict")
        checks = detail.get("statusCheckRollup") or []
        if any(c.get("conclusion") in ("FAILURE", "ERROR") for c in checks):
            reasons.append("Checks Failing")
        comments = detail.get("comments") or []
        if expected_author and any(
            (c.get("author", {}).get("login") or "").casefold() != expected_author.casefold()
            for c in comments
        ):
            reasons.append("New Comments")

        if not reasons:
            continue
        p["attention_reasons"] = reasons
        flagged.append(p)
    return flagged


def _fetch_my_repo_issues():
    # Open issues in repos I own, regardless of assignee -- distinct from
    # the assignee=@me query below, which only catches issues explicitly
    # assigned to me and misses everything else in my own repos.
    return _gh_json([
        "search", "issues", "--owner=@me", "--state=open", "--limit", "50",
        "--json", "number,title,repository,url,createdAt",
    ])


def _build_repo_dir_index(code_dir):
    """Map "owner/repo" (lowercased) -> the actual local directory name
    under code_dir, derived from each subdirectory's own `git remote
    origin`. Intentionally not a maintained/committed mapping -- a repo
    cloned under a shorthand directory name still resolves, with nothing
    to keep in sync.
    """
    index = {}
    try:
        entries = list(os.scandir(code_dir))
    except OSError:
        return index
    # Strip any inherited GIT_DIR/GIT_WORK_TREE/etc: if the caller's
    # environment has one set (e.g. running inside another git hook),
    # `-C entry.path` is silently overridden and every git call below
    # would operate on that unrelated repo instead of entry.path.
    git_env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    for entry in entries:
        if not entry.is_dir():
            continue
        try:
            res = subprocess.run(
                ["git", "-C", entry.path, "remote", "get-url", "origin"],
                capture_output=True, text=True, timeout=2, env=git_env,
            )
            if res.returncode != 0:
                continue
            m = re.search(r"[:/]([^/:]+/[^/]+?)(?:\.git)?$", res.stdout.strip())
            if m:
                index[m.group(1).lower()] = entry.name
        except Exception:
            continue
    return index


def _fetch_raw(config):
    # `gh pr list`/`gh issue list` are repo-scoped: they resolve a single
    # target repo from the cwd's git remote (or an explicit -R) and only
    # filter *within* that repo, so --search never made them search across
    # all of @me's repos. `gh search prs`/`gh search issues` are the actual
    # global cross-repo search subcommands.
    review_prs = _gh_json([
        "search", "prs", "--review-requested=@me", "--state=open", "--limit", "50",
        "--json", "number,title,repository,url,isDraft,createdAt",
    ])
    for p in review_prs:
        p["type"] = "review_request"

    assigned_issues = _gh_json([
        "search", "issues", "--assignee=@me", "--state=open", "--limit", "50",
        "--json", "number,title,repository,url,createdAt",
    ])
    for i in assigned_issues:
        i["type"] = "assigned_issue"

    authored_prs = _fetch_pr_attention("@me")
    for p in authored_prs:
        p["type"] = "authored_attention"

    tracked_prs = []
    for author in config.get("github", {}).get("trackAuthors", []):
        prs = _fetch_pr_attention(author)
        for p in prs:
            p["type"] = "tracked_attention"
            p["tracked_author"] = author
        tracked_prs.extend(prs)

    repo_issues = _fetch_my_repo_issues()
    for i in repo_issues:
        i["type"] = "repo_issue"

    # De-dupe: the same PR/issue can surface from more than one query (a PR
    # I authored could also be review-requested; an issue assigned to me in
    # my own repo matches both the assignee and owner queries). Issue and PR
    # numbers share one counter per repo, so (repo, number) alone uniquely
    # identifies the item. Concatenation order decides which variant wins:
    # review-request/authored-attention carry more specific detail than a
    # plain assigned/repo listing, so they're listed first.
    seen = set()
    combined = []
    for item in review_prs + authored_prs + tracked_prs + assigned_issues + repo_issues:
        key = (item.get("repository", {}).get("nameWithOwner", ""), item.get("number"))
        if key in seen:
            continue
        seen.add(key)
        combined.append(item)
    return combined


def get_repo_from_url(url):
    # e.g., https://github.com/athal7/kb/pull/40 -> athal7/kb
    m = re.search(r"github\.com/([^/]+/[^/]+)", url)
    return m.group(1) if m else ""


def fetch(config):
    raw = _fetch_raw(config)
    if not raw:
        return []

    code_dir = config.get("codeDir", str(Path.home() / "code"))
    # Match each repo's actual `git remote origin` against local
    # directories under code_dir, so a repo cloned under a shorthand name
    # still resolves. One-time scan, only when there's something to
    # resolve.
    repo_dir_index = _build_repo_dir_index(code_dir)

    items = []
    for g in raw:
        title = g.get("title", "Untitled").strip().replace("\t", " ").replace("|", "/")
        repo_name = g.get("repository", {}).get("nameWithOwner", "unknown").replace("\t", " ").replace("|", "/")
        number = str(g.get("number"))
        url = g.get("url", "")
        is_draft = g.get("isDraft", False)

        gtype = g.get("type", "")
        details = ""
        if gtype == "review_request":
            weight, status = 90, "REVIEW REQUESTED"
        elif gtype == "authored_attention":
            weight, status = 88, "NEEDS ATTENTION"
            details = ", ".join(g.get("attention_reasons", []))
        elif gtype == "tracked_attention":
            weight, status = 85, f"{g.get('tracked_author', '').upper()}: NEEDS ATTENTION"
            details = ", ".join(g.get("attention_reasons", []))
        elif gtype == "assigned_issue":
            weight, status = 75, "ASSIGNED"
        else:
            # repo_issue: boost so they rank higher
            weight, status = 70, "OPEN"

        if is_draft:
            status = f"DRAFT: {status}"

        dir_name = repo_dir_index.get(repo_name.lower(), repo_name.split("/")[-1])
        repo_path = os.path.join(code_dir, dir_name)
        slug = slugify(title)

        items.append({
            "status": status,
            "context": repo_name,
            "title": title,
            "details": details,
            "weight": weight,
            "id": number,
            "created_at": g.get("createdAt", ""),
            "absorb_note": f"{status}: {title}",
            "actions": [
                {"key": "alt-o", "label": "open", "primary": True, "payload": {"kind": "open", "url": url}},
                {"key": "alt-s", "label": "session", "payload": {
                    "kind": "session", "repo_path": repo_path, "slug": slug, "item_id": number,
                }},
                {"key": "alt-l", "label": "lumen", "payload": {"kind": "lumen", "url": url}},
                {"key": "alt-a", "label": "approve", "payload": {"kind": "approve", "id": number, "url": url}},
                {"key": "alt-m", "label": "merge", "payload": {"kind": "merge", "id": number, "url": url}},
                {"key": "alt-c", "label": "comment", "payload": {"kind": "comment", "id": number, "url": url}},
                {"key": "alt-g", "label": "label", "payload": {"kind": "label", "id": number, "url": url}},
            ],
        })
    return items


def _confirm_and_merge(item_id, url):
    """Merge confirmation gate, run after fzf has already exited: a
    plain input() y/n prompt on the bare terminal rather than a second
    fzf process.
    """
    if not url:
        print("No URL.")
        return
    repo = get_repo_from_url(url)
    try:
        choice = input(f"\nMerge {repo}#{item_id} (squash + delete branch)? [y/N]: ").strip().lower()
    except (KeyboardInterrupt, EOFError):
        print("\nCanceled.")
        return
    if choice == "y":
        run_cmd(["gh", "pr", "merge", "--squash", "--delete-branch", item_id, "--repo", repo])
    else:
        print("Canceled.")


def act(key, payload):
    kind = payload.get("kind")
    if kind == "open":
        run_cmd(["open", payload["url"]]) if payload.get("url") else print("No URL.")
    elif kind == "session":
        repo_path = payload.get("repo_path")
        if not repo_path:
            print("No local repo path mapped.")
            return
        slug, item_id = payload["slug"], payload["item_id"]
        dispatch_background(["aoe-cmd", "-d", repo_path, "-n", slug, "-b", "-w", slug, f"Work on issue {item_id} in this repo"])
    elif kind == "lumen":
        run_cmd(["lumen", "diff", "--pr", payload["url"]]) if payload.get("url") else print("No URL.")
    elif kind == "approve":
        url = payload.get("url")
        if not url:
            print("No URL.")
            return
        run_cmd(["gh", "pr", "review", "--approve", payload["id"], "--repo", get_repo_from_url(url)])
    elif kind == "merge":
        _confirm_and_merge(payload["id"], payload.get("url"))
    elif kind == "comment":
        url = payload.get("url")
        if not url:
            print("No URL.")
            return
        body = input("\nEnter comment body: ").strip()
        run_cmd(["gh", "issue", "comment", payload["id"], "-R", get_repo_from_url(url), "-b", body])
    elif kind == "label":
        url = payload.get("url")
        if not url:
            print("No URL.")
            return
        label = input("\nEnter label name: ").strip()
        run_cmd(["gh", "issue", "edit", payload["id"], "-R", get_repo_from_url(url), "--add-label", label])
