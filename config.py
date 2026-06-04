"""
Configuration, loaded from Azure Function App settings (environment variables).

Secrets (TENANT_ID, CLIENT_ID, CLIENT_SECRET, ASANA_PAT) have no defaults and
MUST be set as app settings. The Asana GIDs are pre-filled with your workspace's
real values but can still be overridden by an app setting if you ever move things.
"""

import os


def _required(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Missing required app setting: {name}")
    return value


# --- Microsoft 365 / Entra (secrets — set these as app settings) ---
TENANT_ID = _required("TENANT_ID")
CLIENT_ID = _required("CLIENT_ID")
CLIENT_SECRET = _required("CLIENT_SECRET")
MAILBOX = _required("MAILBOX")

# --- Asana (PAT is a secret; GIDs are the real values) ---
ASANA_PAT = _required("ASANA_PAT")
ASANA_WORKSPACE_GID = _required("ASANA_WORKSPACE_GID")  # Asana workspace ID
ASANA_PROJECT_GID = _required("ASANA_PROJECT_GID")  # Email Inbox Project
ASANA_FALLBACK_ASSIGNEE_GID = os.environ.get(
    "ASANA_FALLBACK_ASSIGNEE_GID"
)  # Who to assign to if forwarder isn't in senders list

# "Source" custom field, set to its "Email" enum option on every created task.
# Fill these in (see BUILD_README / Asana custom field settings for the GIDs).
ASANA_SOURCE_FIELD_GID = os.environ.get("ASANA_SOURCE_FIELD_GID", "")
ASANA_SOURCE_EMAIL_OPTION_GID = os.environ.get("ASANA_SOURCE_EMAIL_OPTION_GID", "")

# Number of days from creation to set the task due date.
ASANA_DUE_IN_DAYS = int(os.environ.get("ASANA_DUE_IN_DAYS", "7"))

# --- Azure OpenAI (optional task summarization) ---
# If endpoint/key/deployment are all set, each task gets an LLM-written summary
# at the top, with the full email thread kept below as context. If any are
# missing, summarization is skipped and the raw body is used (no error).
AZURE_OPENAI_ENDPOINT = os.environ.get("AZURE_OPENAI_ENDPOINT", "")
AZURE_OPENAI_API_KEY = os.environ.get("AZURE_OPENAI_API_KEY", "")
AZURE_OPENAI_DEPLOYMENT = os.environ.get("AZURE_OPENAI_DEPLOYMENT", "")
# o-series reasoning models (o4-mini, o3-mini, ...) need a newer API version than
# the chat models. 2024-12-01-preview or later supports them + structured outputs.
AZURE_OPENAI_API_VERSION = os.environ.get("AZURE_OPENAI_API_VERSION", "2024-10-21")

# Set to true when AZURE_OPENAI_DEPLOYMENT points at an o-series reasoning model.
# Reasoning models reject a custom temperature, require max_completion_tokens, and
# spend tokens on hidden reasoning — so we budget more tokens and skip temperature.
AZURE_OPENAI_REASONING = os.environ.get("AZURE_OPENAI_REASONING", "").lower() in (
    "1",
    "true",
    "yes",
)
# Reasoning effort for o-series models: "low", "medium", or "high".
AZURE_OPENAI_REASONING_EFFORT = os.environ.get(
    "AZURE_OPENAI_REASONING_EFFORT", "medium"
)

# --- Behavior knobs ---
# Images smaller than this many bytes are treated as signature/inline clutter and dropped.
# Social icons are typically 1-8 KB; a real screenshot is usually >100 KB. Tune if needed.
MIN_IMAGE_BYTES = int(os.environ.get("MIN_IMAGE_BYTES", "30720"))  # 30 KB

# Optional escape hatch: comma-separated substrings; any attachment whose filename
# contains one of these is always dropped. Empty by default (you asked not to rely on names).
NAME_BLOCKLIST = [
    s.strip().lower()
    for s in os.environ.get("NAME_BLOCKLIST", "").split(",")
    if s.strip()
]

# Mailbox folder that processed messages are moved into (created automatically if missing).
PROCESSED_FOLDER = os.environ.get("PROCESSED_FOLDER", "Processed")

# Safety cap on how many messages to handle per timer run.
MAX_MESSAGES_PER_RUN = int(os.environ.get("MAX_MESSAGES_PER_RUN", "25"))
