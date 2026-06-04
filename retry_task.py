#!/usr/bin/env python3
"""CLI utility to re-run AI task summarization on an existing Asana task.

Fetches the task from Asana, extracts the original email thread from its notes,
calls Azure OpenAI, clears old subtasks, and updates the task. If a meeting summary
is split into multiple tasks, the first task is updated in-place and subsequent ones
are created as new parent tasks.

Usage:
  python retry_task.py <task_gid>
"""

import sys
import json
import os
import argparse
import logging

# Set up logging
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("task_gid", help="The GID of the Asana task to retry.")
    args = parser.parse_args()

    # Load local settings into environment variables
    settings_file = "local.settings.json"
    if not os.path.exists(settings_file):
        print(f"Error: {settings_file} not found in current directory.", file=sys.stderr)
        sys.exit(1)

    with open(settings_file) as f:
        try:
            for k, v in json.load(f)["Values"].items():
                os.environ.setdefault(k, v)
        except Exception as e:
            print(f"Error reading {settings_file}: {e}", file=sys.stderr)
            sys.exit(1)

    # Import modules after env is set up
    from asana_api import AsanaClient
    from summarize import summarize_task
    import config

    asana = AsanaClient(config.ASANA_PAT, config.ASANA_WORKSPACE_GID)

    print(f"Fetching Asana task {args.task_gid}...")
    try:
        task = asana.get_task(args.task_gid)
    except Exception as e:
        print(f"Error fetching task from Asana: {e}", file=sys.stderr)
        sys.exit(1)

    notes = task.get("notes") or ""
    name = task.get("name") or ""
    projects = task.get("projects") or []
    project_gid = projects[0]["gid"] if projects else config.ASANA_PROJECT_GID

    # Strip any starting fallback assignee warning banner
    if notes.startswith("\u26a0 Forwarded by") or notes.startswith("⚠ Forwarded by"):
        banner_parts = notes.split("-" * 48, 1)
        if len(banner_parts) > 1:
            notes = banner_parts[1].strip()

    # Extract the original email thread (everything after the separator banner)
    email_body = notes
    thread_separator = "Full email thread"
    if thread_separator in notes:
        parts = notes.split(thread_separator, 1)
        # Strip off any remaining separator line characters (like \u2500 dashes)
        email_body = parts[1].strip(" \n\r\t\u2500")

    print("Re-summarizing email thread...")
    summaries = summarize_task(name, email_body)
    if not summaries:
        print("Error: Summarization failed to return any tasks. The AI response was empty or invalid.", file=sys.stderr)
        sys.exit(1)

    print(f"Successfully generated {len(summaries)} task(s) from email.")

    # Fetch and delete existing subtasks under the parent task to clear previous runs
    print("Clearing old subtasks of the parent task...")
    try:
        old_subtasks = asana.get_subtasks(args.task_gid)
        for sub in old_subtasks:
            asana.delete_task(sub["gid"])
            print(f"  Deleted old subtask: {sub.get('name')}")
    except Exception as e:
        print(f"Warning: failed to clear old subtasks: {e}", file=sys.stderr)

    # Process and apply summaries
    for i, summary in enumerate(summaries):
        task_name = summary.title or name
        task_notes = f"{summary.summary}\n\n{'\u2500' * 12} Full email thread {'\u2500' * 12}\n\n{email_body}"

        # Resolve assignee
        assignee = None
        if summary.assignee_email:
            assignee = asana.resolve_assignee(summary.assignee_email)
        if not assignee:
            # Fall back to original task's assignee (if any)
            assignee = task.get("assignee", {}).get("gid") if task.get("assignee") else None
        if not assignee:
            assignee = config.ASANA_FALLBACK_ASSIGNEE_GID
            # Prepend warning banner for fallback assignee
            banner = (
                f"\u26a0 Assigned to fallback owner because assignee matches were not found.\n"
                f"{'-' * 48}\n\n"
            )
            task_notes = banner + task_notes

        if i == 0:
            # Reuse/update the existing parent task for the first summary
            print(f"Updating parent task {args.task_gid} to: {task_name}")
            asana.update_task(args.task_gid, task_name, task_notes, assignee)
            current_task_gid = args.task_gid
        else:
            # Duplicate parent task (create a brand new task) for subsequent summaries
            custom_fields = None
            if config.ASANA_SOURCE_FIELD_GID and config.ASANA_SOURCE_EMAIL_OPTION_GID:
                custom_fields = {
                    config.ASANA_SOURCE_FIELD_GID: [config.ASANA_SOURCE_EMAIL_OPTION_GID]
                }
            print(f"Creating sister task: {task_name}")
            current_task_gid = asana.create_task(
                task_name,
                task_notes,
                assignee,
                project_gid,
                due_in_days=config.ASANA_DUE_IN_DAYS,
                custom_fields=custom_fields
            )
            print(f"  Created new task {current_task_gid}")

        # Create subtasks under this parent task
        if summary.subtasks:
            print(f"  Creating {len(summary.subtasks)} subtask(s)...")
            for subtask in summary.subtasks:
                sub_gid = asana.create_subtask(current_task_gid, subtask.name, subtask.description, assignee)
                print(f"    Created subtask {sub_gid}: {subtask.name}")

    print("Done! Tasks updated/created successfully.")

if __name__ == "__main__":
    main()
