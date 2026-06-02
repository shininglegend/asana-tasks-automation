# Email → Asana intake (Azure Function)

Forward an email to **tasks@company.com** and it becomes an Asana task in the **Email Inbox** project, assigned to whoever forwarded it, with the full thread in the description and the signature icons stripped out — but real attachments kept.

## How it works

```
team member ──forward──▶ tasks@company.com (M365 shared mailbox)
                                   │
        Azure Function (timer, every 2 min, reads via Microsoft Graph)
                                   │
        • From address   → assignee (matched to Asana user; else you + a banner)
        • subject + body → Azure OpenAI (optional) → title + summary + subtasks
        • attachments    → kept, except images smaller than 30 KB (icons)
                                   │
                                   ▼
        Asana task in "Email Inbox"  →  subtasks created  →  attachments uploaded
                                   │
                                   ▼
                   message moved to mailbox "Processed" folder
```

**Summarization (optional):** if Azure OpenAI credentials are configured, each email gets an AI-written title, a 1–2 sentence summary, and subtasks for distinct action items — all prepended to the description with the full thread kept below. If the credentials are missing or the call fails, the raw subject/body is used instead.

Why a script and not Asana's built-in `x@mail.asana.com`: forwarding rewrites the `From` line (so Asana can't attribute the task to the real sender), and Asana's ingester takes **every** attachment with no filter. Owning the middle step fixes both.

---

## Part 1 — Create the shared mailbox

Microsoft 365 admin center → **Teams & groups → Shared mailboxes → Add a shared mailbox** (or Exchange admin center → Recipients → Mailboxes → Add a shared mailbox).

- Name: `Tasks`, address: `tasks@company.com`.
- Shared mailboxes need **no license** and can receive external mail by default.
- You don't need to sign into it; the Function reads it via Graph.

## Part 2 — Register the app (identity for Graph)

Entra admin center → **App registrations → New registration**.

1. Name it e.g. `asana-email-intake`. Single tenant. No redirect URI. Register.
2. Copy the **Application (client) ID** → `CLIENT_ID`, and **Directory (tenant) ID** → `TENANT_ID`.
3. **Certificates & secrets → New client secret** → copy the _Value_ → `CLIENT_SECRET`.
   (Note its expiry; set a calendar reminder to rotate. See "Hardening" to drop the secret entirely.)
4. **API permissions → Add a permission → Microsoft Graph → Application permissions →** add **`Mail.ReadWrite`** (needed to read messages/attachments _and_ move them to Processed).
5. Click **Grant admin consent**. The permission should show a green check.

> `Mail.ReadWrite` as an application permission grants access to **all** mailboxes until you scope it in Part 3. Do not skip Part 3.

## Part 3 — Scope the app to ONLY this mailbox

This is the supported way to constrain an app-only Graph mail permission. You create a mail-enabled security group containing just `tasks@`, then bind the app to it.

```powershell
# One-time: install + connect
Install-Module ExchangeOnlineManagement -Scope CurrentUser
Connect-ExchangeOnline -UserPrincipalName you@company.com

# A mail-enabled security group whose only member is the intake mailbox
New-DistributionGroup -Name "AAP-AsanaEmailIntake" `
  -Type Security `
  -PrimarySmtpAddress "aap-asana-intake@company.com"
Add-DistributionGroupMember -Identity "AAP-AsanaEmailIntake" `
  -Member "tasks@company.com"

# Bind the app (use the CLIENT_ID from Part 2) to that group
New-ApplicationAccessPolicy `
  -AppId "<CLIENT_ID>" `
  -PolicyScopeGroupId "aap-asana-intake@company.com" `
  -AccessRight RestrictAccess `
  -Description "Limit asana-email-intake to the tasks mailbox"

# Verify: should say Granted for the intake mailbox, Denied for any other
Test-ApplicationAccessPolicy -AppId "<CLIENT_ID>" -Identity "tasks@company.com"
Test-ApplicationAccessPolicy -AppId "<CLIENT_ID>" -Identity "you@company.com"
```

Policy changes can take a few minutes to propagate.

## Part 4 — Asana token

Asana → profile → **My Settings → Apps → Developer apps → Personal access tokens → Create new token**. Copy it → `ASANA_PAT`. (A PAT acts as you; the task creator will show as you, but the **assignee** is set correctly to the forwarder by the code.)

---

## Part 4.5 — Azure OpenAI (optional, for LLM summaries)

Skip this part if you want the raw email subject/body as the task name/description. Skip if you want to add it later — you just set three app settings, no code change.

If you already have an Azure OpenAI resource:

1. Azure portal → your OpenAI resource → **Keys and Endpoint** → copy the endpoint URL and either key.
2. **Model deployments** → deploy `gpt-4o-mini` (recommended: fast, cheap, supports structured output) or another chat model. Copy the deployment name.
3. Set these settings (locally in `local.settings.json`, or as Function App settings in Azure):

   ```
   AZURE_OPENAI_ENDPOINT   = https://<your-resource>.openai.azure.com/
   AZURE_OPENAI_API_KEY    = <key>
   AZURE_OPENAI_DEPLOYMENT = <deployment-name>
   AZURE_OPENAI_API_VERSION = 2024-10-21   # or 2024-12-01-preview for o-series
   ```

4. If your deployment is an **o-series reasoning model** (o4-mini, o3-mini, etc.):
   - Set `AZURE_OPENAI_REASONING = true`
   - Set `AZURE_OPENAI_REASONING_EFFORT = medium` (or `low` / `high`)
   - Use `AZURE_OPENAI_API_VERSION = 2024-12-01-preview` (earlier versions don't support o-series structured output)

> **Model requirements for structured output:** `gpt-4o-mini` (2024-07-18+), `gpt-4o` (2024-08-06+), or the `gpt-4.1` family. Structured output is what guarantees the model returns valid JSON — without it the parsing can silently fail and fall back to the raw body, which is safe but defeats the feature.

---

## Part 5 — Where it runs, and how to deploy

The Function runs **in Azure**, not on your Mac — a timer that fires every 2 minutes needs an always-on host. Your Mac (with **Azure CLI** + **Functions Core Tools v4** + **Python 3.11**) is only the machine you deploy _from_.

That means you need an **Azure subscription** in the same tenant as your M365. Having M365 does **not** by itself give you one (they share the Entra directory, but billing is separate).
Check:

```bash
az login
az account show      # empty/error? Create one at portal.azure.com → Subscriptions → Add →
                     # Pay-As-You-Go (free to create; this workload is cents–a few $/month)
```

### Option A — First light locally (recommended before provisioning anything)

You can validate the entire pipeline against your **real** mailbox and Asana from your Mac, with no Azure subscription, because the code just calls cloud APIs. Finish Parts 1–4 first.

```bash
cp local.settings.json.example local.settings.json   # fill in the 4 secrets
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
func start
```

The timer won't fire for 2 minutes — trigger it immediately from a second terminal:

```bash
curl -X POST http://localhost:7071/admin/functions/email_to_asana \
  -H "Content-Type: application/json" -d '{}'
```

Forward a test email to `tasks@` first, then trigger. A real task should appear in Email Inbox. Tip: during testing, forward from **your own** account so every task assigns to you and you don't ping teammates; bring the team in once it's clean.

### Option B — Deploy to Azure for the always-on schedule (Flex Consumption)

Flex Consumption is the current recommended serverless plan (classic Consumption is on a retirement path, Sept 30 2028). It's Linux-only, which is what Python requires anyway.

```bash
az group create -n rg-asana-intake -l eastus
az storage account create -n stasanaintake$RANDOM -g rg-asana-intake -l eastus --sku Standard_LRS
# copy the exact storage name it printed, use it below
az functionapp create -n asana-email-intake -g rg-asana-intake \
  --flexconsumption-location eastus --runtime python --runtime-version 3.11 \
  --storage-account <storage-name-from-above>

# Required secrets. GIDs already default in config.py.
az functionapp config appsettings set -n asana-email-intake -g rg-asana-intake --settings \
  TENANT_ID="<tenant-guid>" CLIENT_ID="<client-id>" CLIENT_SECRET="<client-secret>" \
  ASANA_PAT="<asana-pat>" MAILBOX="tasks@company.com"

# Optional: Azure OpenAI summarization (skip if you didn't complete Part 4.5)
az functionapp config appsettings set -n asana-email-intake -g rg-asana-intake --settings \
  AZURE_OPENAI_ENDPOINT="https://<your-resource>.openai.azure.com/" \
  AZURE_OPENAI_API_KEY="<key>" \
  AZURE_OPENAI_DEPLOYMENT="<deployment-name>" \
  AZURE_OPENAI_API_VERSION="2024-10-21"

func azure functionapp publish asana-email-intake
```

**Iterate live:** edit code → `func azure functionapp publish asana-email-intake` (~1 min).
Change a knob like `MIN_IMAGE_BYTES` → `az functionapp config appsettings set ...` (no redeploy).

> Want $0 instead of a few dollars/month? The classic Linux Consumption plan has a free monthly execution grant that covers this easily. Swap the create command's `--flexconsumption-location eastus` for `--consumption-plan-location eastus --os-type Linux --functions-version 4`. Fine for the next couple of years, but plan to migrate before the 2028 retirement.

## Part 6 — Test

1. From your own mailbox, forward any email (one with your signature icons) to
   `tasks@company.com`.
2. Within ~2 minutes a task appears in **Email Inbox**, assigned to you:
   - **Without OpenAI:** task name = email subject, description = raw body.
   - **With OpenAI:** task name = AI-generated title, description starts with a 1–2 sentence summary + any subtasks, then a divider and the full thread below. Real attachments are kept; signature icons (< 30 KB images) are dropped.
3. Forward from a teammate's account → task assigned to _them_.
4. Forward from an address with no Asana user → task assigned to you with a banner
   naming the sender.
5. Forward a multi-item email (a numbered list of separate asks) → with OpenAI enabled, each item should appear as a subtask under the main task.

Watch it run: Azure portal → the Function App → **Monitor** / Application Insights → Logs.

---

## Tuning the attachment filter

One knob does the work: **`MIN_IMAGE_BYTES`** (default `30720` = 30 KB).

- Images **below** the threshold are dropped (social icons are ~1–8 KB; `image001*` blips are tiny).
- Images **at/above** it are kept (a real screenshot is usually >100 KB).
- **Non-image** files (PDF, docx, etc.) are _always_ kept regardless of size.
- Filenames are deliberately ignored, since a real file and an icon can both start with `img-`.

If a legitimately small image ever gets dropped, lower the threshold. If a large icon ever sneaks through, raise it. As a last-resort override, `NAME_BLOCKLIST` (comma-separated substrings) force-drops by filename; it's empty by default.

Change either via an app setting and restart — no code edit:

```bash
az functionapp config appsettings set -n asana-email-intake -g rg-asana-intake \
  --settings MIN_IMAGE_BYTES="20480"
```

## Troubleshooting

| Symptom                                  | Likely cause                                                                                                                 |
| ---------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------- |
| `403` / `ErrorAccessDenied` reading mail | Part 3 policy not applied yet, wrong `CLIENT_ID`, or mailbox not in the group                                                |
| Token error in logs                      | Bad `CLIENT_SECRET` (expired?) or `TENANT_ID`                                                                                |
| Task created, not assigned to sender     | Forwarder's email doesn't match an Asana user's email exactly → falls back to you (by design)                                |
| Nothing happens                          | Timer needs `AzureWebJobsStorage`; confirm the Function App has a storage account and the function shows under **Functions** |
| Duplicate task                           | The message was processed but the move-to-Processed step failed before completing (rare). See note below.                    |

## Notes & limitations

- **Idempotency:** a message is removed from the Inbox (moved to `Processed`) only after its task is created, and the Inbox query only sees unread Inbox mail — so normal runs never double-process. The one gap: if the function crashes _between_ creating the task and moving the message, the next run remakes the task. At your volume that's unlikely; if you want exactly-once, the clean upgrade is to record processed message IDs in Azure Table Storage and skip known IDs.
- **Schedule:** every 2 minutes, set in the `@app.timer_trigger` NCRONTAB (`0 */2 * * * *`)
  in `function_app.py`. Change the cron and republish to adjust latency vs. invocation count.
- **Cost:** on Flex Consumption this runs in the cents-to-a-few-dollars/month range at your volume (~21.6k short executions/month + a near-empty storage account). The classic Linux Consumption plan's free monthly grant covers it at ~$0 if you'd rather (see Part 5 note).

## Hardening (optional, when you're ready)

- **Drop the client secret:** enable a **system-assigned managed identity** on the Function
  App, grant it the `Mail.ReadWrite` Graph **app role**, and use `DefaultAzureCredential`
  instead of MSAL+secret. Keep the Part 3 access policy. No secret to rotate.
- **Vault the remaining secrets:** store `ASANA_PAT` in Azure Key Vault and reference it from
  app settings as `@Microsoft.KeyVault(SecretUri=...)`.
- **Certificate instead of secret** for the app registration if you stay on MSAL.

## Files

- `function_app.py` — timer trigger + orchestration
- `graph_mail.py` — Microsoft Graph mail client (read, attachments, move)
- `asana_api.py` — Asana task + attachment + subtask client, email→user matching
- `summarize.py` — Azure OpenAI summarization (optional; returns `None` gracefully if unconfigured or failed)
- `config.py` — settings (your GIDs pre-filled; secrets from app settings)
- `host.json`, `requirements.txt`, `local.settings.json.example` — Functions scaffolding
