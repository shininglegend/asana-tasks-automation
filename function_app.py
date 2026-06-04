"""
Azure Function (Python v2 model). Every 2 minutes:
  1. Read unread mail in tasks@company.com
  2. Create an Asana task in the Email Inbox project
  3. Assign it to the forwarder (or fall back to you, with a banner)
  4. Attach real files, drop signature icons (small images)
  5. Move the message to the Processed folder so it's never handled twice
"""

import base64
import logging

import azure.functions as func

import config
from asana_api import AsanaClient
from graph_mail import GraphMail
from summarize import summarize_task

app = func.FunctionApp()


def should_keep(att: dict) -> bool:
    """Decide whether a Graph attachment is a real attachment worth keeping.

    Rule (matches the agreed design):
      - Only file attachments are considered; item/reference attachments are skipped.
      - Any filename substring in NAME_BLOCKLIST is dropped (off by default).
      - Images smaller than MIN_IMAGE_BYTES are dropped (signature icons, inline blips).
      - Everything else is kept (PDFs, docs, and full-size screenshots/photos).
    Filename is NOT used to keep an image, because a real file and an icon can
    share a prefix like 'img-'.
    """
    if att.get("@odata.type") != "#microsoft.graph.fileAttachment":
        return False
    name = (att.get("name") or "").lower()
    if any(bad in name for bad in config.NAME_BLOCKLIST):
        return False
    content_type = (att.get("contentType") or "").lower()
    size = att.get("size") or 0
    if content_type.startswith("image/") and size < config.MIN_IMAGE_BYTES:
        return False
    return True


def process_one(
    graph: GraphMail, asana: AsanaClient, msg: dict, processed_folder_id: str
) -> None:
    msg_id = msg["id"]
    subject = (msg.get("subject") or "").strip() or "(no subject)"

    from_field = msg.get("from") or {}
    sender = from_field.get("emailAddress") or {}
    sender_email = sender.get("address", "")
    sender_name = sender.get("name") or sender_email or "unknown sender"

    body = (msg.get("body") or {}).get("content", "") or ""

    # Lead with an LLM summary when available, creating multiple tasks if the email
    # contains separate action items for different people. summarize_task returns None
    # if Azure OpenAI is unconfigured or the call fails, in which case we fall back to a single task.
    summaries = summarize_task(subject, body)
    created_task_gids = []

    custom_fields = None
    if config.ASANA_SOURCE_FIELD_GID and config.ASANA_SOURCE_EMAIL_OPTION_GID:
        # "Source" is a multi-select (multi_enum) field, so the value is a list.
        custom_fields = {
            config.ASANA_SOURCE_FIELD_GID: [config.ASANA_SOURCE_EMAIL_OPTION_GID]
        }

    if summaries:
        for summary in summaries:
            task_name = summary.title or subject
            notes = f"{summary.summary}\n\n{'\u2500' * 12} Full email thread {'\u2500' * 12}\n\n{body}"

            # Resolve assignee: prioritize the model's inferred assignee, fall back to sender
            assignee = None
            if summary.assignee_email:
                assignee = asana.resolve_assignee(summary.assignee_email)
            if not assignee:
                assignee = asana.resolve_assignee(sender_email)
            if not assignee:
                # No Asana user matches: assign to fallback owner and prep a banner
                assignee = config.ASANA_FALLBACK_ASSIGNEE_GID
                banner = (
                    f"\u26a0 Forwarded by {sender_name} <{sender_email}> \u2014 "
                    f"no matching Asana user, so this was assigned to the fallback owner.\n"
                    f"{'-' * 48}\n\n"
                )
                notes = banner + notes

            try:
                task_gid = asana.create_task(
                    task_name,
                    notes,
                    assignee,
                    config.ASANA_PROJECT_GID,
                    due_in_days=config.ASANA_DUE_IN_DAYS,
                    custom_fields=custom_fields,
                )
                logging.info(
                    "Created Asana task %s from message %s (subject=%r)", task_gid, msg_id, subject
                )
                created_task_gids.append(task_gid)

                # Create subtasks under this task
                if summary.subtasks:
                    for subtask in summary.subtasks:
                        try:
                            sub_gid = asana.create_subtask(
                                task_gid, subtask.name, subtask.description, assignee
                            )
                            logging.info(
                                "Created subtask %s (%r) under task %s",
                                sub_gid,
                                subtask.name,
                                task_gid,
                            )
                        except Exception:
                            logging.exception(
                                "Failed to create subtask %r under task %s", subtask.name, task_gid
                            )
            except Exception:
                logging.exception(
                    "Failed to create task %r from message %s", task_name, msg_id
                )

    # Fallback: if no tasks were successfully created via the summaries, create one from the raw email
    if not created_task_gids:
        notes = body
        assignee = asana.resolve_assignee(sender_email)
        if not assignee:
            assignee = config.ASANA_FALLBACK_ASSIGNEE_GID
            banner = (
                f"\u26a0 Forwarded by {sender_name} <{sender_email}> \u2014 "
                f"no matching Asana user, so this was assigned to the fallback owner.\n"
                f"{'-' * 48}\n\n"
            )
            notes = banner + notes

        try:
            task_gid = asana.create_task(
                subject,
                notes,
                assignee,
                config.ASANA_PROJECT_GID,
                due_in_days=config.ASANA_DUE_IN_DAYS,
                custom_fields=custom_fields,
            )
            logging.info(
                "Created fallback Asana task %s from message %s (subject=%r)", task_gid, msg_id, subject
            )
            created_task_gids.append(task_gid)
        except Exception:
            logging.exception("Failed to create fallback task for message %s", msg_id)

    # Upload attachments to all successfully created tasks
    if msg.get("hasAttachments") and created_task_gids:
        for att in graph.list_attachments(msg_id):
            if not should_keep(att):
                logging.info(
                    "Skipping attachment %r (type=%s size=%s inline=%s)",
                    att.get("name"),
                    att.get("contentType"),
                    att.get("size"),
                    att.get("isInline"),
                )
                continue
            content_b64 = att.get("contentBytes")
            data = (
                base64.b64decode(content_b64)
                if content_b64
                else graph.attachment_value(msg_id, att["id"])
            )
            for task_gid in created_task_gids:
                try:
                    asana.upload_attachment(
                        task_gid, att.get("name"), data, att.get("contentType")
                    )
                    logging.info("Attached %r to task %s", att.get("name"), task_gid)
                except Exception:
                    logging.exception(
                        "Failed to upload attachment %r to task %s",
                        att.get("name"),
                        task_gid,
                    )

    # Done last: once moved out of Inbox the message will not be picked up again.
    graph.move_message(msg_id, processed_folder_id)
    logging.info("Moved message %s to '%s'", msg_id, config.PROCESSED_FOLDER)


@app.timer_trigger(
    schedule="0 */2 * * * *", arg_name="timer", run_on_startup=False, use_monitor=True
)
def email_to_asana(timer: func.TimerRequest) -> None:
    if timer.past_due:
        logging.warning("Timer is past due; running anyway")

    graph = GraphMail(
        config.TENANT_ID, config.CLIENT_ID, config.CLIENT_SECRET, config.MAILBOX
    )
    asana = AsanaClient(config.ASANA_PAT, config.ASANA_WORKSPACE_GID)

    try:
        messages = graph.list_unread(config.MAX_MESSAGES_PER_RUN)
    except Exception:
        logging.exception("Failed to list mailbox messages")
        return

    if not messages:
        logging.info("No new messages")
        return

    processed_folder_id = graph.processed_folder_id(config.PROCESSED_FOLDER)

    for msg in messages:
        try:
            process_one(graph, asana, msg, processed_folder_id)
        except Exception:
            # Leave this message unread so it retries next run; keep processing the rest.
            logging.exception(
                "Failed to process message %s; left in Inbox for retry", msg.get("id")
            )
