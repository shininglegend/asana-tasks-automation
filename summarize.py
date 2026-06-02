"""
Optional task summarization via Azure OpenAI.

Given a forwarded email's subject and body, asks a chat model to produce a short
task title and an actionable summary. The title becomes the Asana task name and
the summary leads the task description, with the full original thread kept
underneath as context (see function_app). The summary never has to be perfect —
if the call fails for any reason we return None and the caller falls back to the
raw subject/body. This is why every error here is swallowed: a flaky model call
must never cost us a task.
"""

import json
import logging
from dataclasses import dataclass

from openai import AzureOpenAI
from openai.types.shared_params import ResponseFormatJSONSchema

import config

_SYSTEM_PROMPT = (
    "You turn forwarded emails into concise, actionable Asana tasks for a team's "
    "board. Read the email (which may include forwarding headers and email "
    "signatures).\n"
    "\n"
    "IMPORTANT — work out the current request before writing anything. A forwarded "
    "thread contains several stacked messages, each starting with a 'From:'/'Sent:' "
    "/'Date:' header. Do this first:\n"
    "  1. Find every message's date/time header and identify the ONE message with "
    "the latest timestamp (it is normally at the top, but trust the timestamps, not "
    "the position).\n"
    "  2. The task is whatever that single latest message asks for — nothing else. "
    "It defines the scope: if it settles on one action, the task is that one action; "
    "if it lists several things, the task covers all of them.\n"
    "  3. Read the older messages below ONLY to resolve details the latest message "
    "points to (a link, amount, timestamp, name, or 'that'/'it' references). An "
    "older request that the latest message answered, replaced, or that already "
    "appears handled is NOT a task and must not become a subtask.\n"
    "  4. Ignore the subject line for scope — it reflects where the thread STARTED, "
    "not where it ended.\n"
    "\n"
    "Respond with a JSON object with these fields:\n"
    '  "title": a short, specific task name in imperative form — what needs to be '
    "done and about what, based on the most recent message. Max ~80 characters. "
    'Do NOT include email prefixes like "FW:" or "RE:".\n'
    '  "summary": 1-2 sentences stating the action/request, then any concrete '
    "details (amounts, dates, names, links) as short bullet lines.\n"
    '  "subtasks": a list of subtasks. Use this ONLY when the most recent message '
    "lists several distinct deliverables that each need to be done for the task to "
    "be complete (e.g. a numbered list of separate items). Most emails are a single "
    "task — return an empty list in that case. Each subtask is an object with:\n"
    '    "name": a short imperative subtask name.\n'
    '    "description": optional extra detail from the email for this subtask '
    "(e.g. a specific quote, link, amount, or who to contact). Use an empty string "
    "if the name already says everything.\n"
    "Never pad with generic steps like 'Read email' or 'Reply', and never invent "
    "work not stated in the email. At most 6 subtasks.\n"
    "\n"
    "Ignore signatures, social links, and boilerplate. Do not invent information. "
    "Plain text inside the fields (no markdown headings)."
)

# Don't ship an entire newsletter to the model; the request is always near the top.
_MAX_INPUT_CHARS = 12000

# Strict schema: the model is guaranteed to return exactly these two fields.
# Requires gpt-4o-mini (2024-07-18+), gpt-4o (2024-08-06+), or the gpt-4.1 family.
_RESPONSE_FORMAT: ResponseFormatJSONSchema = {
    "type": "json_schema",
    "json_schema": {
        "name": "task_summary",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "Short imperative task name, max ~80 chars, no FW:/RE: prefixes.",
                },
                "summary": {
                    "type": "string",
                    "description": "1-2 sentence action/request followed by all concrete detail bullets that the email supplies.",
                },
                "subtasks": {
                    "type": "array",
                    "description": "0-6 subtasks; empty unless the latest message calls for distinct steps.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {
                                "type": "string",
                                "description": "Short imperative subtask name.",
                            },
                            "description": {
                                "type": "string",
                                "description": "Extra detail from the email, or empty string if none.",
                            },
                        },
                        "required": ["name", "description"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["title", "summary", "subtasks"],
            "additionalProperties": False,
        },
    },
}

_client: AzureOpenAI | None = None


@dataclass
class Subtask:
    name: str
    description: str = ""


@dataclass
class TaskSummary:
    title: str
    summary: str
    subtasks: list[Subtask]


def _get_client() -> AzureOpenAI | None:
    """Lazily build a cached client, or None if Azure OpenAI isn't configured."""
    global _client
    if _client is not None:
        return _client
    if not (config.AZURE_OPENAI_ENDPOINT and config.AZURE_OPENAI_API_KEY):
        return None
    _client = AzureOpenAI(
        azure_endpoint=config.AZURE_OPENAI_ENDPOINT,
        api_key=config.AZURE_OPENAI_API_KEY,
        api_version=config.AZURE_OPENAI_API_VERSION,
    )
    return _client


def summarize_task(subject: str, body: str) -> TaskSummary | None:
    """Return a task title + summary, or None if summarization is unavailable/failed."""
    client = _get_client()
    if client is None or not config.AZURE_OPENAI_DEPLOYMENT:
        return None

    body = (body or "")[:_MAX_INPUT_CHARS]
    if not body.strip():
        return None

    user_content = f"Subject: {subject or '(no subject)'}\n\nBody:\n{body}"
    kwargs = {
        "model": config.AZURE_OPENAI_DEPLOYMENT,  # Azure deployment name
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        "response_format": _RESPONSE_FORMAT,
    }
    if config.AZURE_OPENAI_REASONING:
        # o-series: no custom temperature; budget extra tokens for hidden reasoning.
        kwargs["max_completion_tokens"] = 3000
        kwargs["reasoning_effort"] = config.AZURE_OPENAI_REASONING_EFFORT
    else:
        kwargs["temperature"] = 0.2
        kwargs["max_completion_tokens"] = 600
    try:
        resp = client.chat.completions.create(**kwargs)
        data = json.loads(resp.choices[0].message.content or "{}")
        title = (data.get("title") or "").strip()
        summary = (data.get("summary") or "").strip()
        subtasks = []
        for s in data.get("subtasks") or []:
            if not isinstance(s, dict):
                continue
            name = (s.get("name") or "").strip()
            if name:
                subtasks.append(Subtask(name=name, description=(s.get("description") or "").strip()))
        if not summary:
            return None
        return TaskSummary(title=title, summary=summary, subtasks=subtasks)
    except Exception:
        logging.exception("Task summarization failed; falling back to raw body")
        return None
