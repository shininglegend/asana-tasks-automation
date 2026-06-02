# Asana Email Intake Automation

Forward an email to **tasks@yourcompany.com** and it becomes an Asana task — assigned to whoever forwarded it, with the thread in the description and real attachments kept (signature icons stripped).

```
team member ──forward──▶ tasks@company.com
                                   │
              Azure Function (timer, every 2 min)
                           reads via Microsoft Graph
                                   │
        subject  → task name        body → description
        From     → assignee         attachments → uploaded
                                   │
                                   ▼
             Asana "Email Inbox" project
             email moved to mailbox "Processed"
```

**Why not Asana's built-in email intake?** Forwarding rewrites the `From` header (so Asana can't attribute the task to the real sender), and Asana's ingester keeps every attachment with no filter. Owning the middle step fixes both.

---

## Prerequisites

| Tool                       | Version    | Install                                                                 |
| -------------------------- | ---------- | ----------------------------------------------------------------------- |
| Python                     | 3.11       | `brew install python@3.11`                                              |
| Azure Functions Core Tools | v4         | `brew tap azure/functions && brew install azure-functions-core-tools@4` |
| Azure CLI                  | any recent | `brew install azure-cli`                                                |

You'll also need:

- An **Azure subscription** in the same Entra tenant as your M365 (M365 alone isn't enough — billing is separate; create one at portal.azure.com → Subscriptions → Add → Pay-As-You-Go)
- An **Asana Personal Access Token** (Asana → My Settings → Apps → Developer apps → Personal access tokens)

---

## Setup

See [BUILD_README.md](BUILD_README.md) for step-by-step instructions covering:

1. Create the `tasks@` shared mailbox in M365
2. Register an Entra app and grant `Mail.ReadWrite`
3. Scope the app to only that mailbox (PowerShell, ~5 min)
4. Create an Asana PAT
5. Provision the Azure Function App and deploy
6. Test end-to-end

Or paste the **AI setup prompt** below into Claude, ChatGPT, or similar to get guided through it interactively.

---

## AI Setup Prompt

> Copy everything between the lines and paste it into your AI assistant.

---

I need to set up an Azure Function that polls a Microsoft 365 shared mailbox every 2 minutes and creates Asana tasks from new emails. Here is the project: https://github.com/ShiningLegend/ix-asana-tasks (or you might currently be running in the project: Check for `BUILD_README.md`)

Help me complete these steps in order, explaining each one and waiting for confirmation before moving on:

1. **Shared mailbox** — create `tasks@company.com` in Microsoft 365 admin center (Teams & groups → Shared mailboxes).

2. **Entra app registration** — register a new app (`asana-email-intake`), copy the Client ID and Tenant ID, create a client secret, add the `Mail.ReadWrite` **application** permission, and grant admin consent.

3. **Scope the app to one mailbox** — using Exchange Online PowerShell (`Install-Module ExchangeOnlineManagement`), create a mail-enabled security group containing only `tasks@`, then run `New-ApplicationAccessPolicy` to restrict the app to that group only. Verify with `Test-ApplicationAccessPolicy`.

4. **Asana PAT** — guide me to Asana → My Settings → Apps → Developer apps → Personal access tokens → Create new token.

5. **Local test** — copy `local.settings.json.example` to `local.settings.json`, fill in the four secrets (`TENANT_ID`, `CLIENT_ID`, `CLIENT_SECRET`, `ASANA_PAT`), install dependencies (`pip install -r requirements.txt`), run `func start`, and trigger the function manually with curl.

6. **Azure provisioning** — create a resource group, storage account, and Flex Consumption Function App, set the secrets as app settings, then deploy with `func azure functionapp publish asana-email-intake`.

Ask me for my domain, Asana workspace GID, and target project GID: I might need help getting them, so include one short sentence about where they are.

---

## Local development

**First:** start the Azure Storage emulator in a separate terminal:

```bash
azurite --silent --location ~/.azurite --debug ~/.azurite/debug.log
```

Then in your main terminal:

### Using uv (preferred)

```bash
cp local.settings.json.example local.settings.json
# fill in any information placeholders in local.settings.json
uv sync
uv run func start
```

### Using pip

```bash
cp local.settings.json.example local.settings.json  # fill in the 4 secrets
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
func start
```

Trigger immediately (timer fires after 2 min otherwise):

```bash
curl -X POST http://localhost:7071/admin/functions/email_to_asana \
  -H "Content-Type: application/json" -d '{}'
```

Forward a test email to `tasks@company.com` first, then trigger. A task should appear in **Email Inbox** within seconds.

---

## Deploy

```bash
func azure functionapp publish asana-email-intake
```

That's it for code changes. To update a config knob (e.g. `MIN_IMAGE_BYTES`) without redeploying:

```bash
az functionapp config appsettings set -n asana-email-intake -g rg-asana-intake \
  --settings MIN_IMAGE_BYTES="20480"
```

---

## Configuration

| Variable                        | Required | Default             | Description                                          |
| ------------------------------- | -------- | ------------------- | ---------------------------------------------------- |
| `TENANT_ID`                     | yes      | —                   | Entra directory (tenant) ID                          |
| `CLIENT_ID`                     | yes      | —                   | App registration client ID                           |
| `CLIENT_SECRET`                 | yes      | —                   | App registration client secret                       |
| `ASANA_PAT`                     | yes      | —                   | Asana personal access token                          |
| `MAILBOX`                       | no       | `tasks@company.com` | Shared mailbox to poll                               |
| `ASANA_WORKSPACE_GID`           | no       | -     | Asana workspace                                      |
| `ASANA_PROJECT_GID`             | no       | -     | Target Asana project                                 |
| `ASANA_FALLBACK_ASSIGNEE_GID`   | no       | -     | Assignee when sender has no Asana account            |
| `ASANA_DUE_IN_DAYS`             | no       | `7`                 | Days from creation to set due date                   |
| `ASANA_SOURCE_FIELD_GID`        | no       | —                   | GID of a "Source" custom field                       |
| `ASANA_SOURCE_EMAIL_OPTION_GID` | no       | —                   | Enum option GID for "Email" on that field            |
| `AZURE_OPENAI_ENDPOINT`         | no       | —                   | Azure OpenAI endpoint (enables LLM summaries)        |
| `AZURE_OPENAI_API_KEY`          | no       | —                   | Azure OpenAI key                                     |
| `AZURE_OPENAI_DEPLOYMENT`       | no       | —                   | Model deployment name                                |
| `AZURE_OPENAI_API_VERSION`      | no       | `2024-10-21`        | API version (`2024-12-01-preview` for o-series)      |
| `AZURE_OPENAI_REASONING`        | no       | `false`             | Set `true` for o-series reasoning models             |
| `AZURE_OPENAI_REASONING_EFFORT` | no       | `medium`            | `low` / `medium` / `high`                            |
| `MIN_IMAGE_BYTES`               | no       | `30720`             | Images below this size are dropped (signature icons) |
| `NAME_BLOCKLIST`                | no       | —                   | Comma-separated filename substrings to always drop   |
| `PROCESSED_FOLDER`              | no       | `Processed`         | Mailbox folder emails are moved to after processing  |
| `MAX_MESSAGES_PER_RUN`          | no       | `25`                | Safety cap on emails handled per timer invocation    |

### Optional: LLM summarization

If `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_API_KEY`, and `AZURE_OPENAI_DEPLOYMENT` are all set, each task gets an AI-written summary at the top with the full thread kept below. If any are missing, the raw body is used — no error.

---

## Attachment filtering

One knob controls what gets kept: **`MIN_IMAGE_BYTES`** (default 30 KB).

- Images **below** the threshold → dropped (social icons are ~1–8 KB)
- Images **at or above** → kept (real screenshots are usually >100 KB)
- Non-image files (PDF, docx, etc.) → always kept regardless of size
- `NAME_BLOCKLIST` → force-drop by filename substring (last resort; empty by default)

---

## Troubleshooting

| Symptom                                | Likely cause                                                                                       |
| -------------------------------------- | -------------------------------------------------------------------------------------------------- |
| `403 / ErrorAccessDenied` reading mail | Part 3 access policy not applied yet, wrong `CLIENT_ID`, or mailbox not in the group               |
| Token error in logs                    | Bad or expired `CLIENT_SECRET`, or wrong `TENANT_ID`                                               |
| Task not assigned to sender            | Forwarder's email doesn't exactly match an Asana user's email → falls back to Titus (by design)    |
| Nothing happens                        | Check that `AzureWebJobsStorage` is set and the function appears under **Functions** in the portal |
| Duplicate task                         | Crash between task creation and move-to-Processed — rare; next run will reprocess                  |

Logs: Azure portal → Function App → **Monitor** / Application Insights → Logs.

---

## Files

| File                          | Purpose                                               |
| ----------------------------- | ----------------------------------------------------- |
| `function_app.py`             | Timer trigger + orchestration                         |
| `graph_mail.py`               | Microsoft Graph mail client (read, attachments, move) |
| `asana_api.py`                | Asana task/attachment client, email→user matching     |
| `summarize.py`                | Azure OpenAI summarization (optional)                 |
| `config.py`                   | All settings (GIDs pre-filled; secrets from env)      |
| `host.json`                   | Azure Functions host config                           |
| `requirements.txt`            | Python dependencies                                   |
| `local.settings.json.example` | Template for local dev secrets                        |
| `BUILD_README.md`             | Detailed first-time setup walkthrough                 |
