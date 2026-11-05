"""Poll an IUST Exchange inbox and forward new emails to Telegram."""

import html
import json
import logging
import os
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv
from exchangelib import DELEGATE, Account, Build, Configuration, Credentials, Version

log = logging.getLogger("iust_mail")
STATE_FILE = Path(os.environ.get("STATE_FILE", "state.json"))
PREVIEW_CHARS = 1500


def connect() -> Account:
    creds = Credentials(os.environ["EWS_USERNAME"], os.environ["EWS_PASSWORD"])
    cfg = Configuration(
        service_endpoint=os.environ["EWS_ENDPOINT"],
        credentials=creds,
        version=Version(build=Build(15, 0)),
    )
    return Account(os.environ["EWS_EMAIL"], config=cfg, autodiscover=False, access_type=DELEGATE)


def load_seen() -> set[str] | None:
    if not STATE_FILE.exists():
        return None
    return set(json.loads(STATE_FILE.read_text()))


def save_seen(seen: set[str]) -> None:
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(sorted(seen)))
    tmp.replace(STATE_FILE)


def format_message(m) -> str:
    sender = m.sender.name or m.sender.email_address if m.sender else "unknown"
    addr = m.sender.email_address if m.sender else ""
    body = (m.text_body or "").strip()
    if len(body) > PREVIEW_CHARS:
        body = body[:PREVIEW_CHARS] + "…"
    e = html.escape
    return (
        f"📧 <b>{e(m.subject or '(no subject)')}</b>\n"
        f"<b>From:</b> {e(str(sender))} &lt;{e(addr)}&gt;\n"
        f"<b>Date:</b> {m.datetime_received:%Y-%m-%d %H:%M} UTC\n\n"
        f"{e(body)}"
    )[:4096]


def send_telegram(client: httpx.Client, text: str) -> None:
    url = f"https://api.telegram.org/bot{os.environ['TELEGRAM_BOT_TOKEN']}/sendMessage"
    r = client.post(url, json={
        "chat_id": os.environ["TELEGRAM_CHAT_ID"],
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    })
    r.raise_for_status()


def check_once(account: Account, client: httpx.Client, seen: set[str] | None) -> set[str]:
    items = list(
        account.inbox.all()
        .order_by("-datetime_received")
        .only("id", "subject", "sender", "datetime_received", "text_body")[:50]
    )
    if seen is None:  # first run: remember existing mail, don't spam it
        log.info("First run: marking %d existing emails as seen", len(items))
        return {m.id for m in items}
    for m in reversed(items):
        if m.id not in seen:
            log.info("New email: %s", m.subject)
            send_telegram(client, format_message(m))
            seen.add(m.id)
    return seen


def main() -> None:
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", force=True)
    interval = int(os.environ.get("POLL_SECONDS", "60"))
    proxy = os.environ.get("TELEGRAM_PROXY") or None
    seen = load_seen()
    log.info("Started; polling every %ds", interval)
    account = None
    with httpx.Client(proxy=proxy, timeout=30) as client:
        while True:
            try:
                account = account or connect()
                seen = check_once(account, client, seen)
                save_seen(seen)
            except Exception:
                log.exception("Check failed; will retry")
                account = None
            time.sleep(interval)
