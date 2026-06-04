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
import time
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
    "  2. The request is whatever that single latest message asks for — nothing else. "
    "It defines the scope.\n"
    "  3. Read the older messages below ONLY to resolve details the latest message "
    "points to (a link, amount, timestamp, name, or 'that'/'it' references).\n"
    "  4. Ignore the subject line for scope — it reflects where the thread STARTED, "
    "not where it ended.\n"
    "\n"
    "TASK SPLITTING RULES:\n"
    "  - Determine if the latest request represents meeting notes, action points, or similar documents "
    "that explicitly outline multiple tasks/deliverables assigned to MULTIPLE different people.\n"
    "  - If (and ONLY if) this condition is met, return one task object for each person (e.g. 'Jim's action points', "
    "'Nick's action points') with their specific action items listed in that task's subtasks array. Try to find the "
    "corresponding person's email address in the email headers or body and set it in the 'assignee_email' field.\n"
    "  - For all other emails (default), return EXACTLY ONE task in the tasks list representing the overall request, "
    "with subtasks for any specific sub-steps required if applicable. In this case, leave 'assignee_email' empty.\n"
    "\n"
    "Respond with a JSON object with a single 'tasks' field containing a list of task objects, each with these fields:\n"
    '  "title": a short, specific task name in imperative form. Max ~80 characters. Do NOT include email prefixes like "FW:" or "RE:".\n'
    '  "summary": 1-2 sentences stating the action/request, then any concrete details (amounts, dates, names, links) as short bullet lines.\n'
    '  "assignee_email": the email address of the person who should do this task, if clear (e.g., matching a name from meeting notes headers like Jim -> jim@innerexcellence.com, or Nick -> nick@innerexcellence.com). Use an empty string if not found, unclear, or if this is the default single task case.\n'
    '  "subtasks": a list of subtasks. Use this when the request or the person\'s action points list several distinct deliverables that each need to be done. Each subtask is an object with:\n'
    '    "name": a short imperative subtask name.\n'
    '    "description": optional extra detail from the email for this subtask. Use an empty string if the name says everything.\n'
    "Never pad with generic steps like 'Read email' or 'Reply', and never invent work not stated in the email. At most 6 subtasks per task.\n"
    "\n"
    "Ignore signatures, social links, and boilerplate. Do not invent information. Plain text inside the fields (no markdown headings)."
)

# Don't ship an entire newsletter to the model; the request is always near the top.
_MAX_INPUT_CHARS = 12000

# Strict schema: the model is guaranteed to return exactly these fields.
# Requires gpt-4o-mini (2024-07-18+), gpt-4o (2024-08-06+), or the gpt-4.1 family.
_RESPONSE_FORMAT: ResponseFormatJSONSchema = {
    "type": "json_schema",
    "json_schema": {
        "name": "tasks_list",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "tasks": {
                    "type": "array",
                    "description": "List of tasks identified from the email. Must satisfy task splitting rules.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "title": {
                                "type": "string",
                                "description": "Short imperative task name, max ~80 chars, no FW:/RE: prefixes.",
                            },
                            "summary": {
                                "type": "string",
                                "description": "1-2 sentence action/request followed by all concrete detail bullets.",
                            },
                            "assignee_email": {
                                "type": "string",
                                "description": "The email address of the person this task is for, inferred from email headers/body (e.g. 'nick@innerexcellence.com'). Empty string if not found/unclear.",
                            },
                            "subtasks": {
                                "type": "array",
                                "description": "0-6 subtasks; empty unless this task itself calls for distinct steps.",
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
                        "required": ["title", "summary", "assignee_email", "subtasks"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["tasks"],
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
    assignee_email: str = ""
    subtasks: list[Subtask]  = None


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


def summarize_task(subject: str, body: str) -> list[TaskSummary] | None:
    """Return a list of task summaries, or None if summarization is unavailable/failed."""
    client = _get_client()
    if client is None or not config.AZURE_OPENAI_DEPLOYMENT:
        return None
    if body and len(body) > _MAX_INPUT_CHARS:
        logging.info(f"Stripping this from the request:\n{body[_MAX_INPUT_CHARS:]}")

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
    for attempt in range(2):
        try:
            resp = client.chat.completions.create(**kwargs)
            choice = resp.choices[0]
            logging.debug("summarize_task finish_reason=%s refusal=%s", choice.finish_reason, choice.message.refusal)
            raw_content = choice.message.content or "{}"
            logging.debug("summarize_task raw response: %s", raw_content)
            data = json.loads(raw_content)
            tasks_data = data.get("tasks") or []
            if not tasks_data:
                logging.debug("summarize_task: no tasks list in parsed data %r", data)
                if attempt == 0:
                    logging.warning("summarize_task: empty result, retrying in 60s (attempt %d)", attempt + 1)
                    time.sleep(60)
                    continue
                return None

            summaries = []
            for t in tasks_data:
                if not isinstance(t, dict):
                    continue
                title = (t.get("title") or "").strip()
                summary_text = (t.get("summary") or "").strip()
                assignee_email = (t.get("assignee_email") or "").strip()

                subtasks = []
                for s in t.get("subtasks") or []:
                    if not isinstance(s, dict):
                        continue
                    name = (s.get("name") or "").strip()
                    if name:
                        subtasks.append(Subtask(name=name, description=(s.get("description") or "").strip()))

                if title and summary_text:
                    summaries.append(TaskSummary(
                        title=title,
                        summary=summary_text,
                        assignee_email=assignee_email,
                        subtasks=subtasks
                    ))

            if not summaries:
                logging.debug("summarize_task: parsed list of tasks was empty")
                if attempt == 0:
                    logging.warning("summarize_task: empty result, retrying in 60s (attempt %d)", attempt + 1)
                    time.sleep(60)
                    continue
                return None
            return summaries
        except Exception:
            logging.exception("Task summarization failed; falling back to raw body")
            return None
    return None
