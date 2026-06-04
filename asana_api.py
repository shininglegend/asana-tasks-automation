"""
Minimal Asana REST client (https://app.asana.com/api/1.0) for creating intake
tasks and uploading attachments. Uses a Personal Access Token.
"""

import datetime
import logging

import requests

ASANA_BASE = "https://app.asana.com/api/1.0"

# Asana limits: task name is generous; notes (description) ~65k chars.
_MAX_NAME = 1024
_MAX_NOTES = 65000


class AsanaClient:
    def __init__(self, pat: str, workspace_gid: str):
        self.workspace = workspace_gid
        self.session = requests.Session()
        self.session.headers.update({"Authorization": f"Bearer {pat}"})
        self._user_map: dict[str, str] | None = None

    def user_map(self) -> dict[str, str]:
        """email (lowercased) -> user GID, for the workspace. Cached per run."""
        if self._user_map is not None:
            return self._user_map
        mapping: dict[str, str] = {}
        url = f"{ASANA_BASE}/users"
        params = {"workspace": self.workspace, "opt_fields": "email,name", "limit": 100}
        while url:
            resp = self.session.get(url, params=params, timeout=30)
            resp.raise_for_status()
            body = resp.json()
            for user in body.get("data", []):
                email = (user.get("email") or "").strip().lower()
                if email:
                    mapping[email] = user["gid"]
            nxt = body.get("next_page")
            if nxt and nxt.get("uri"):
                url, params = nxt["uri"], None  # next_page uri already encodes params
            else:
                url = None
        self._user_map = mapping
        logging.info("Loaded %d Asana users for assignee matching", len(mapping))
        return mapping

    def resolve_assignee(self, email: str | None) -> str | None:
        if not email:
            return None
        return self.user_map().get(email.strip().lower())

    def create_task(
        self,
        name: str,
        notes: str,
        assignee_gid: str | None,
        project_gid: str,
        due_in_days: int = 7,
        custom_fields: dict[str, str | list[str]] | None = None,
    ) -> str:
        due_on = (
            datetime.date.today() + datetime.timedelta(days=due_in_days)
        ).isoformat()
        data = {
            "data": {
                "name": (name or "(no subject)")[:_MAX_NAME],
                "notes": (notes or "")[:_MAX_NOTES],
                "projects": [project_gid],
                "due_on": due_on,
            }
        }
        if assignee_gid:
            data["data"]["assignee"] = assignee_gid
        if custom_fields:
            data["data"]["custom_fields"] = custom_fields
        resp = self.session.post(f"{ASANA_BASE}/tasks", json=data, timeout=30)
        resp.raise_for_status()
        return resp.json()["data"]["gid"]

    def create_subtask(
        self,
        parent_gid: str,
        name: str,
        notes: str = "",
        assignee_gid: str | None = None,
    ) -> str:
        data = {"data": {"name": (name or "(untitled)")[:_MAX_NAME]}}
        if notes:
            data["data"]["notes"] = notes[:_MAX_NOTES]
        if assignee_gid:
            data["data"]["assignee"] = assignee_gid
        resp = self.session.post(
            f"{ASANA_BASE}/tasks/{parent_gid}/subtasks", json=data, timeout=30
        )
        resp.raise_for_status()
        return resp.json()["data"]["gid"]

    def upload_attachment(
        self, task_gid: str, filename: str, content: bytes, content_type: str | None
    ) -> str:
        # requests sets the multipart Content-Type/boundary automatically when `files` is used.
        files = {
            "file": (
                filename or "attachment",
                content,
                content_type or "application/octet-stream",
            )
        }
        resp = self.session.post(
            f"{ASANA_BASE}/attachments",
            data={"parent": task_gid},
            files=files,
            timeout=120,
        )
        resp.raise_for_status()
        return resp.json()["data"]["gid"]

    def get_task(self, task_gid: str) -> dict:
        url = f"{ASANA_BASE}/tasks/{task_gid}"
        params = {"opt_fields": "name,notes,assignee,projects"}
        resp = self.session.get(url, params=params, timeout=30)
        resp.raise_for_status()
        return resp.json()["data"]

    def get_subtasks(self, task_gid: str) -> list[dict]:
        url = f"{ASANA_BASE}/tasks/{task_gid}/subtasks"
        resp = self.session.get(url, timeout=30)
        resp.raise_for_status()
        return resp.json().get("data") or []

    def delete_task(self, task_gid: str) -> None:
        url = f"{ASANA_BASE}/tasks/{task_gid}"
        resp = self.session.delete(url, timeout=30)
        resp.raise_for_status()

    def update_task(
        self,
        task_gid: str,
        name: str,
        notes: str,
        assignee_gid: str | None = None,
    ) -> None:
        url = f"{ASANA_BASE}/tasks/{task_gid}"
        data: dict[str, dict[str, str | None]] = {
            "data": {
                "name": name[:_MAX_NAME],
                "notes": notes[:_MAX_NOTES],
            }
        }
        if assignee_gid:
            data["data"]["assignee"] = assignee_gid
        else:
            data["data"]["assignee"] = None
        resp = self.session.put(url, json=data, timeout=30)
        resp.raise_for_status()

    def list_project_tasks(self, project_gid: str, limit: int = 100) -> list[dict]:
        """List tasks in a project, returning their name, notes, and gid."""
        url = f"{ASANA_BASE}/projects/{project_gid}/tasks"
        params = {"opt_fields": "name,notes", "limit": min(limit, 100)}
        tasks: list[dict] = []
        while url and len(tasks) < limit:
            resp = self.session.get(url, params=params, timeout=30)
            resp.raise_for_status()
            body = resp.json()
            tasks.extend(body.get("data") or [])
            nxt = body.get("next_page")
            if nxt and nxt.get("uri"):
                url, params = nxt["uri"], None
            else:
                url = None
        return tasks[:limit]

    def create_comment(self, task_gid: str, text: str) -> str:
        """Add a comment (story) to a task."""
        url = f"{ASANA_BASE}/tasks/{task_gid}/stories"
        data = {
            "data": {
                "text": text[:_MAX_NOTES]
            }
        }
        resp = self.session.post(url, json=data, timeout=30)
        resp.raise_for_status()
        return resp.json()["data"]["gid"]
