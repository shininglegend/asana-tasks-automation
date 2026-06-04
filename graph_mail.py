"""
Thin Microsoft Graph client for one shared mailbox, using app-only (client
credentials) auth. Scoped to a single mailbox in production via an Exchange
Application Access Policy (see README), so the broad Mail.ReadWrite permission
can only ever touch tasks@company.com.
"""

import logging

import msal
import requests

GRAPH = "https://graph.microsoft.com/v1.0"
_SCOPE = ["https://graph.microsoft.com/.default"]


class GraphMail:
    def __init__(
        self, tenant_id: str, client_id: str, client_secret: str, mailbox: str
    ):
        self.mailbox = mailbox
        self._app = msal.ConfidentialClientApplication(
            client_id,
            authority=f"https://login.microsoftonline.com/{tenant_id}",
            client_credential=client_secret,
        )
        self.session = requests.Session()
        self._processed_folder_id = None

    # --- auth ---
    def _token(self) -> str:
        # MSAL caches the app token in-memory and refreshes only when near expiry.
        result = self._app.acquire_token_for_client(scopes=_SCOPE)
        if "access_token" not in result:
            raise RuntimeError(
                f"Graph token request failed: {result.get('error')}: "
                f"{result.get('error_description')}"
            )
        return result["access_token"]

    def _headers(self, extra: dict | None = None) -> dict:
        headers = {"Authorization": f"Bearer {self._token()}"}
        if extra:
            headers.update(extra)
        return headers

    # --- reads ---
    def list_unread(self, top: int) -> list[dict]:
        """Unread messages in the mailbox Inbox, with body returned as plain text
        (matches how Asana's native email-in renders the description)."""
        url = f"{GRAPH}/users/{self.mailbox}/mailFolders/Inbox/messages"
        params = {
            "$filter": "isRead eq false",
            "$top": str(top),
            "$select": "id,subject,from,hasAttachments,body,receivedDateTime,conversationId",
        }
        headers = self._headers({"Prefer": 'outlook.body-content-type="text"'})
        resp = self.session.get(url, headers=headers, params=params, timeout=30)
        resp.raise_for_status()
        return resp.json().get("value", [])

    def list_attachments(self, msg_id: str) -> list[dict]:
        url = f"{GRAPH}/users/{self.mailbox}/messages/{msg_id}/attachments"
        resp = self.session.get(url, headers=self._headers(), timeout=30)
        resp.raise_for_status()
        return resp.json().get("value", [])

    def attachment_value(self, msg_id: str, att_id: str) -> bytes:
        """Raw bytes for an attachment. Used as a fallback when contentBytes is
        not inlined in the attachment object (large files)."""
        url = f"{GRAPH}/users/{self.mailbox}/messages/{msg_id}/attachments/{att_id}/$value"
        resp = self.session.get(url, headers=self._headers(), timeout=120)
        resp.raise_for_status()
        return resp.content

    # --- writes ---
    def processed_folder_id(self, name: str) -> str:
        if self._processed_folder_id:
            return self._processed_folder_id
        url = f"{GRAPH}/users/{self.mailbox}/mailFolders"
        resp = self.session.get(
            url,
            headers=self._headers(),
            params={"$filter": f"displayName eq '{name}'"},
            timeout=30,
        )
        resp.raise_for_status()
        existing = resp.json().get("value", [])
        if existing:
            self._processed_folder_id = existing[0]["id"]
        else:
            resp = self.session.post(
                url,
                headers=self._headers({"Content-Type": "application/json"}),
                json={"displayName": name},
                timeout=30,
            )
            resp.raise_for_status()
            self._processed_folder_id = resp.json()["id"]
            logging.info("Created mailbox folder '%s'", name)
        return self._processed_folder_id

    def move_message(self, msg_id: str, folder_id: str) -> None:
        url = f"{GRAPH}/users/{self.mailbox}/messages/{msg_id}/move"
        resp = self.session.post(
            url,
            headers=self._headers({"Content-Type": "application/json"}),
            json={"destinationId": folder_id},
            timeout=30,
        )
        resp.raise_for_status()

    def reply_to_message(self, msg_id: str, comment: str) -> None:
        """Send an email reply only to the sender of the original email."""
        url = f"{GRAPH}/users/{self.mailbox}/messages/{msg_id}/reply"
        resp = self.session.post(
            url,
            headers=self._headers({"Content-Type": "application/json"}),
            json={"comment": comment},
            timeout=30,
        )
        resp.raise_for_status()
