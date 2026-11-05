"""University email (Exchange EWS) access for one user."""

import html
import logging
import os

from exchangelib import DELEGATE, Account, Build, Configuration, Credentials, Version

log = logging.getLogger("iust_mail.mail")
EWS_ENDPOINT = os.environ.get("EWS_ENDPOINT", "https://email.iust.ac.ir/EWS/Exchange.asmx")
PREVIEW_CHARS = 1500


def connect(username: str, password: str, email: str) -> Account:
    cfg = Configuration(
        service_endpoint=EWS_ENDPOINT,
        credentials=Credentials(username, password),
        version=Version(build=Build(15, 0)),
    )
    return Account(email, config=cfg, autodiscover=False, access_type=DELEGATE)


def check_login(username: str, password: str, email: str) -> int:
    """Raises on bad credentials/address; returns the inbox size."""
    return connect(username, password, email).inbox.total_count


class Mailbox:
    def __init__(self, username: str, password: str, email: str) -> None:
        self.creds = (username, password, email)
        self._account: Account | None = None

    @property
    def account(self) -> Account:
        if self._account is None:
            self._account = connect(*self.creds)
        return self._account

    def reset(self) -> None:
        self._account = None

    def latest(self, limit: int = 50) -> list:
        return list(self.account.inbox.all().order_by("-datetime_received")
                    .only("id", "subject", "sender", "datetime_received", "text_body", "is_read")[:limit])

    def list(self, unread_only: bool = False, limit: int = 8) -> list:
        qs = self.account.inbox.all()
        if unread_only:
            qs = qs.filter(is_read=False)
        return list(qs.order_by("-datetime_received").only("id", "subject", "sender", "is_read")[:limit])

    def get(self, item_id: str | None):
        if not item_id:
            return None
        try:
            return self.account.inbox.get(id=item_id)
        except Exception as e:  # deleted/moved, or not in this user's mailbox
            if "NotFound" in type(e).__name__ or "DoesNotExist" in type(e).__name__:
                return None
            raise

    def set_read(self, item_id: str, read: bool) -> bool:
        m = self.get(item_id)
        if m is None:
            return False
        m.is_read = read
        m.save(update_fields=["is_read"])
        return True

    def trash(self, item_id: str) -> bool:
        m = self.get(item_id)
        if m is None:
            return False
        m.move_to_trash()
        return True


# ---------- formatting ----------

def parse_mode() -> str:
    """BOT_PARSE_MODE: HTML (default), Markdown, or none."""
    mode = os.environ.get("BOT_PARSE_MODE", "HTML").strip().lower()
    return {"html": "HTML", "markdown": "Markdown"}.get(mode, "")


def md_escape(text: str) -> str:
    for ch in "\\_*`[":
        text = text.replace(ch, "\\" + ch)
    return text


def format_message(m, mode: str = "") -> str:
    sender = (m.sender.name or m.sender.email_address) if m.sender else "unknown"
    addr = m.sender.email_address if m.sender else ""
    body = (m.text_body or "").strip()
    if len(body) > PREVIEW_CHARS:
        body = body[:PREVIEW_CHARS] + "…"
    subject = m.subject or "(no subject)"
    date = f"{m.datetime_received:%Y-%m-%d %H:%M} UTC"
    if mode == "HTML":
        e = html.escape
        text = (f"📧 <b>{e(subject)}</b>\n<b>From:</b> {e(str(sender))} &lt;{e(addr)}&gt;\n"
                f"<b>Date:</b> {date}\n\n{e(body)}")
    elif mode == "Markdown":
        e = md_escape
        text = f"📧 *{e(subject)}*\n*From:* {e(str(sender))} <{e(addr)}>\n*Date:* {date}\n\n{e(body)}"
    else:
        text = f"📧 {subject}\nFrom: {sender} <{addr}>\nDate: {date}\n\n{body}"
    return text[:4096]
