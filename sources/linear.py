"""linear plugin -- your open Linear issues, via the Linear GraphQL API.

Config (config["linear"]):
    {"apiToken": "lin_api_..."}   # falls back to LINEAR_API_TOKEN/LINEAR_TOKEN env vars

Scope: issues assigned to you, in an active (non-completed/canceled/
duplicate) state, AND in your current cycle -- past-cycle and
not-yet-scheduled backlog items are deliberately excluded so this stays
a short "what's live right now" list rather than your whole backlog.
"""
import json
import os
import urllib.error
import urllib.request

from _util import resolve_configured_actions, run_cmd, run_configured_action, slugify

GRAPHQL_URL = "https://api.linear.app/graphql"



def _get_token(config):
    token = config.get("linear", {}).get("apiToken")
    if token:
        return token
    return os.environ.get("LINEAR_API_TOKEN") or os.environ.get("LINEAR_TOKEN")


def _query(token, query, variables=None):
    req = urllib.request.Request(
        GRAPHQL_URL,
        data=json.dumps({"query": query, "variables": variables or {}}).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": token},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"Linear query failed: {e}")
        return None


def _blocking_issue(issue):
    identifier = issue.get("identifier", "")
    for relation in issue.get("relations", {}).get("nodes", []):
        if (
            relation.get("type") == "blocks"
            and relation.get("relatedIssue", {}).get("identifier") == identifier
        ):
            return relation.get("issue") or {}
    return {}

def fetch(config):
    token = _get_token(config)
    if not token:
        return []

    query = """
query {
  viewer {
    assignedIssues(filter: { state: { type: { nin: ["completed", "canceled", "duplicate"] } }, cycle: { isActive: { eq: true } } }, first: 250) {
      nodes {
        id
        identifier
        title
        url
        state {
          name
        }
        project {
          name
        }
        relations(first: 20) {
          nodes {
            type
            issue { identifier title }
            relatedIssue { identifier }
          }
        }
      }
    }
  }
}
"""
    res_data = _query(token, query)
    if not res_data or "errors" in res_data:
        return []

    def extract(data):
        if isinstance(data, list):
            for item in data:
                yield from extract(item)
        elif isinstance(data, dict):
            if "identifier" in data and "title" in data:
                yield data
            else:
                for val in data.values():
                    yield from extract(val)

    raw = list(extract(res_data.get("data", {})))

    items = []
    for l in raw:
        title = l.get("title", "Untitled").strip().replace("\t", " ").replace("|", "/")
        identifier = l.get("identifier", "unknown").replace("\t", " ").replace("|", "/")
        state_raw = l.get("state", {}).get("name", "Unknown") if isinstance(l.get("state"), dict) else "Unknown"
        state = state_raw.replace("\t", " ").replace("|", "/")
        project_raw = l.get("project", {}).get("name", "") if isinstance(l.get("project"), dict) else ""
        project = project_raw.replace("\t", " ").replace("|", "/")
        db_id = l.get("id", "")
        url = l.get("url", "")

        blocker = _blocking_issue(l)
        blocker_identifier = blocker.get("identifier", "")
        blocker_title = blocker.get("title", "")
        weight = 90 if blocker_identifier else 80 if state.lower() == "in progress" else 65
        status = f"BLOCKED BY {blocker_identifier}" if blocker_identifier else state.upper()

        record = {
            "url": url,
            "identifier": identifier,
            "identifier_lower": identifier.lower(),
            "id": identifier,
            "slug": slugify(title),
            "db_id": db_id,
            "title": title,
            "context": project,
            "status": status,
            "blocking_issue": blocker_identifier,
            "blocking_issue_title": blocker_title,
        }

        actions = [
            {"key": "o", "label": "open", "primary": True, "payload": {"kind": "open", "url": url}},
            {"key": "c", "label": "comment", "payload": {"kind": "comment", "db_id": db_id, "token": None}},
            {"key": "t", "label": "transition", "payload": {"kind": "transition", "db_id": db_id, "token": None}},
        ]
        configured_actions = config.get("linear", {}).get("actions", [])
        actions.extend(resolve_configured_actions(configured_actions, record))

        items.append({
            "status": status,
            "context": project,
            "title": title,
            "details": f"Blocked by {blocker_identifier}: {blocker_title}" if blocker_identifier else "",
            "indicators": {
                "state": (
                    f"Blocked ❌" if blocker_identifier or "block" in state.lower()
                    else f"{state.title()} ⏳"
                ),
            },
            "weight": weight,
            "id": identifier,
            "kind": "issue",
            "absorb_note": f"Linear {identifier}: {state.upper()}",
            "identity_key": f"linear:{identifier}",
            # Linear's workflow state is the authoritative signal for
            # "where is this work" -- when a PR title/body cross-links to
            # this issue and the two get merged into one dashboard row,
            # this outranks the PR's own status (which describes review/CI
            # mechanics, not progress) so the merged row still shows it.
            "status_priority": 10,
            "actions": actions,
        })

    # Stash the token on every comment/transition payload -- act() is a
    # separate call (possibly a separate process) with no other way back
    # to config, and these two actions need to make their own follow-up
    # GraphQL calls.
    for item in items:
        for a in item["actions"]:
            if a["payload"].get("kind") in ("comment", "transition"):
                a["payload"]["token"] = token
    return items


def _linear_comment(db_id, token):
    if not db_id:
        print("No issue database ID available to comment.")
        return
    body = input("\nEnter comment body: ").strip()
    if not body:
        print("Empty comment. Canceled.")
        return
    query = """
    mutation($issueId: String!, $body: String!) {
      commentCreate(input: { issueId: $issueId, body: $body }) {
        success
      }
    }
    """
    res = _query(token, query, {"issueId": db_id, "body": body})
    if res and not res.get("errors"):
        print("Comment created successfully!")
    else:
        print("Failed to create comment.")


def _linear_transition(db_id, token):
    if not db_id:
        print("No issue database ID available to transition.")
        return
    # Workflow states are per-team, so listing every team's states mixed
    # together offers duplicate/foreign names (e.g. multiple "Backlog"s)
    # and silently fails issueUpdate if one from the wrong team gets
    # picked. Resolve the issue's own team first.
    res_team = _query(token, """
    query($id: String!) {
      issue(id: $id) {
        team {
          id
        }
      }
    }
    """, {"id": db_id})
    team_id = None
    if res_team and not res_team.get("errors"):
        team_id = res_team.get("data", {}).get("issue", {}).get("team", {}).get("id")
    if not team_id:
        print("Could not resolve the issue's team.")
        return

    res_states = _query(token, """
    query($teamId: ID!) {
      workflowStates(filter: { team: { id: { eq: $teamId } } }) {
        nodes {
          id
          name
        }
      }
    }
    """, {"teamId": team_id})
    if not res_states or res_states.get("errors"):
        print("Could not retrieve states.")
        return
    states = res_states.get("data", {}).get("workflowStates", {}).get("nodes", [])
    if not states:
        print("No workflow states found.")
        return

    print("\nAvailable States:")
    for idx, s in enumerate(states, 1):
        print(f"{idx}) {s['name']}")

    try:
        choice = input("\nSelect state to transition to [1-%d]: " % len(states)).strip()
        if choice.isdigit() and 1 <= int(choice) <= len(states):
            target_state_id = states[int(choice) - 1]["id"]
            target_state_name = states[int(choice) - 1]["name"]
            res_update = _query(token, """
            mutation($id: String!, $stateId: String!) {
              issueUpdate(id: $id, input: { stateId: $stateId }) {
                success
              }
            }
            """, {"id": db_id, "stateId": target_state_id})
            if res_update and not res_update.get("errors"):
                print(f"Issue transitioned successfully to '{target_state_name}'!")
            else:
                print("Failed to transition issue.")
        else:
            print("Invalid choice.")
    except (KeyboardInterrupt, EOFError):
        print("\nCanceled.")


def act(key, payload):
    if "command" in payload:
        return run_configured_action(payload)
    kind = payload.get("kind")
    if kind == "open":
        run_cmd(["open", payload["url"]]) if payload.get("url") else print("No URL.")
    elif kind == "comment":
        _linear_comment(payload.get("db_id"), payload.get("token"))
    elif kind == "transition":
        _linear_transition(payload.get("db_id"), payload.get("token"))
