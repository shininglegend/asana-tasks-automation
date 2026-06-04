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
    conversation_id = msg.get("conversationId")

    # 1. Filter out automated and out-of-office emails
    subject_lower = subject.lower()
    is_auto = (
        subject_lower.startswith("automatic reply:")
        or subject_lower.startswith("out of office:")
        or subject_lower.startswith("autoreply:")
        or subject_lower.startswith("read:")
        or "out of office" in subject_lower
        or "auto-reply" in subject_lower
        or "delivery status notification" in subject_lower
        or "undeliverable:" in subject_lower
        or sender_email.lower() == "postmaster"
    )
    if is_auto:
        logging.info(
            "Ignoring automated message %s (subject=%r, sender=%s)",
            msg_id,
            subject,
            sender_email,
        )
        graph.move_message(msg_id, processed_folder_id)
        return

    # 2. Check for existing thread using conversationId
    matching_task_gids = []
    if conversation_id:
        try:
            recent_tasks = asana.list_project_tasks(config.ASANA_PROJECT_GID, limit=100)
            footer_str = f"[Outlook-Conversation-ID: {conversation_id}]"
            for t in recent_tasks:
                task_notes = t.get("notes") or ""
                if footer_str in task_notes:
                    matching_task_gids.append(t["gid"])
            if matching_task_gids:
                logging.info(
                    "Found %d existing Asana task(s) matching conversation ID %s: %s",
                    len(matching_task_gids),
                    conversation_id,
                    matching_task_gids,
                )
        except Exception:
            logging.exception("Failed to look up existing tasks in Asana project")

    # 3. If thread exists, append reply as a comment & upload attachments
    if matching_task_gids:
        # Append email reply as a comment
        comment_body = f"✉️ New reply from {sender_name} <{sender_email}>:\n\n{body}"
        for task_gid in matching_task_gids:
            try:
                asana.create_comment(task_gid, comment_body)
                logging.info("Appended comment to existing task %s", task_gid)
            except Exception:
                logging.exception("Failed to append comment to task %s", task_gid)

        # Upload attachments to all matching tasks
        if msg.get("hasAttachments"):
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
                for task_gid in matching_task_gids:
                    try:
                        asana.upload_attachment(
                            task_gid, att.get("name"), data, att.get("contentType")
                        )
                        logging.info("Attached %r to existing task %s", att.get("name"), task_gid)
                    except Exception:
                        logging.exception(
                            "Failed to upload attachment %r to existing task %s",
                            att.get("name"),
                            task_gid,
                        )

        # Move to processed and return (no auto-reply for comments to prevent loops)
        graph.move_message(msg_id, processed_folder_id)
        logging.info("Moved message %s to '%s' (appended to existing task(s))", msg_id, config.PROCESSED_FOLDER)
        return

    # 4. Otherwise, proceed to create new task(s)
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
            if conversation_id:
                notes += f"\n\n[Outlook-Conversation-ID: {conversation_id}]"

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
        if conversation_id:
            notes += f"\n\n[Outlook-Conversation-ID: {conversation_id}]"

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

    # 5. Programmatically reply back to the forwarder with the task link(s)
    if created_task_gids:
        links = [f"https://app.asana.com/0/0/{gid}" for gid in created_task_gids]
        links_text = "\n".join(links)
        recipient_name = sender_name.split()[0] if sender_name else "there"
        if recipient_name.lower() == "unknown":
            recipient_name = "there"

        reply_comment = (
            f"Hi {recipient_name},\n\n"
            f"I have successfully created Asana task(s) for your request:\n"
            f"{links_text}\n\n"
            f"Best regards,\n"
            f"Tasks Automation"
        )
        try:
            graph.reply_to_message(msg_id, reply_comment)
            logging.info("Sent reply to sender %s for message %s", sender_email, msg_id)
        except Exception:
            logging.exception("Failed to send reply to sender %s", sender_email)

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
