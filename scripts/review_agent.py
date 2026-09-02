"""
AI Review Agent — v1

Gathers a PR's diff, sends it to Claude with project context and a
structured prompt, and posts the findings as a PR comment.

This is a starting point, not a finished product. It's deliberately
conservative: it flags issues for a human to decide on, it never
auto-approves or auto-merges, and it logs what it did.

Env vars required (set as GitHub Actions secrets / job env):
  ANTHROPIC_API_KEY  - your Claude API key
  GITHUB_TOKEN       - provided automatically by GitHub Actions
  PR_NUMBER          - pull request number
  REPO               - "owner/repo"
  BASE_SHA           - base commit SHA
  HEAD_SHA           - head commit SHA
"""

import json
import os
import subprocess
import sys

import requests
from anthropic import Anthropic

# ---- Config -----------------------------------------------------------

MAX_DIFF_CHARS = 60_000          # keep the diff well within context budget
MODEL = "claude-sonnet-4-6"      # swap for whichever model you want to use
CONTRIBUTING_FILES = ["CONTRIBUTING.md", "CONTRIBUTING", "docs/CONTRIBUTING.md"]

SYSTEM_PROMPT = """You are a careful, senior code reviewer acting as an \
automated first-pass reviewer on a pull request. You do NOT have merge \
authority — your job is to surface issues for a human maintainer, not to \
approve or reject anything.

Review the diff for:
1. Correctness — logic errors, edge cases, likely bugs
2. Test coverage — is new/changed behavior covered by tests?
3. Style/consistency — does it match the conventions visible in the diff \
   and any CONTRIBUTING guidance provided?
4. Security — injection risks, unsafe dependency use, secrets in code, \
   unsafe file/network operations
5. Clarity — anything ambiguous enough that a human should double check \
   intent against the linked issue or spec

Respond with ONLY valid JSON, no markdown fences, no preamble, matching \
this schema:

{
  "summary": "1-3 sentence overall summary",
  "findings": [
    {
      "severity": "blocker" | "warning" | "suggestion" | "nit",
      "file": "path/to/file or null if general",
      "comment": "specific, actionable description of the issue"
    }
  ],
  "needs_human_attention": true | false
}

Keep findings specific and actionable. Do not invent issues that aren't \
supported by the diff. If the PR looks solid, say so plainly and return \
an empty findings list.
"""

# ---- Helpers ------------------------------------------------------------


def run(cmd: list[str]) -> str:
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return result.stdout


def get_diff(base_sha: str, head_sha: str) -> str:
    diff = run(["git", "diff", f"{base_sha}...{head_sha}"])
    if len(diff) > MAX_DIFF_CHARS:
        diff = diff[:MAX_DIFF_CHARS] + "\n\n[... diff truncated for length ...]"
    return diff


def get_contributing_guidance() -> str:
    for path in CONTRIBUTING_FILES:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                return f.read()[:4000]
    return "(no CONTRIBUTING file found in repo)"


def call_claude(diff: str, guidance: str) -> dict:
    client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    user_content = f"""Project contribution guidance:
---
{guidance}
---

Pull request diff:
---
{diff}
---

Review this PR per the instructions."""

    response = client.messages.create(
        model=MODEL,
        max_tokens=2000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_content}],
    )

    text = "".join(block.text for block in response.content if block.type == "text")

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {
            "summary": "Agent response could not be parsed as JSON.",
            "findings": [
                {"severity": "warning", "file": None, "comment": text[:1500]}
            ],
            "needs_human_attention": True,
        }


def format_comment(review: dict) -> str:
    severity_emoji = {
        "blocker": "🛑",
        "warning": "⚠️",
        "suggestion": "💡",
        "nit": "✏️",
    }

    lines = ["## 🤖 AI Review Agent — first-pass review", "", review.get("summary", "")]

    findings = review.get("findings", [])
    if not findings:
        lines.append("\nNo issues flagged.")
    else:
        lines.append("\n### Findings")
        for f in findings:
            icon = severity_emoji.get(f.get("severity", "nit"), "•")
            loc = f" (`{f['file']}`)" if f.get("file") else ""
            lines.append(f"- {icon} **{f.get('severity', 'nit')}**{loc}: {f.get('comment', '')}")

    lines.append(
        "\n---\n_This is an automated first-pass review. It does not "
        "approve, block, or merge this PR — a maintainer will follow up._"
    )
    return "\n".join(lines)


def post_comment(repo: str, pr_number: str, token: str, body: str) -> None:
    url = f"https://api.github.com/repos/{repo}/issues/{pr_number}/comments"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
    }
    resp = requests.post(url, headers=headers, json={"body": body}, timeout=30)
    resp.raise_for_status()


# ---- Main -----------------------------------------------------------------


def main() -> None:
    base_sha = os.environ["BASE_SHA"]
    head_sha = os.environ["HEAD_SHA"]
    repo = os.environ["REPO"]
    pr_number = os.environ["PR_NUMBER"]
    token = os.environ["GITHUB_TOKEN"]

    diff = get_diff(base_sha, head_sha)
    if not diff.strip():
        print("No diff content found — skipping review.")
        return

    guidance = get_contributing_guidance()
    review = call_claude(diff, guidance)

    print("---- Review agent output ----")
    print(json.dumps(review, indent=2))
    print("------------------------------")

    comment = format_comment(review)
    post_comment(repo, pr_number, token, comment)
    print("Posted review comment to PR.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"Review agent encountered an error: {exc}", file=sys.stderr)
        sys.exit(0)
