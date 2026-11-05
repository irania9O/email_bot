"""IUST assistant bot: university email, Samad food reservation and LMS, for many users."""

import json
import logging
import os
import threading
from pathlib import Path

from dotenv import load_dotenv

log = logging.getLogger("iust_mail")


def bootstrap_owner() -> None:
    """One-time import of the original single-user setup (.env accounts + old state files)
    into the database for TELEGRAM_CHAT_ID, so the existing user keeps working unchanged."""
    from . import store

    owner = os.environ.get("TELEGRAM_CHAT_ID")
    if not owner or store.get("bootstrapped"):
        return
    owner = int(owner)
    env = os.environ.get
    if env("EWS_USERNAME") and env("EWS_PASSWORD") and env("EWS_EMAIL") and not store.get_account(owner, "email"):
        store.set_account(owner, "email", {"username": env("EWS_USERNAME"), "password": env("EWS_PASSWORD"),
                                           "email": env("EWS_EMAIL")})
    lms_user, lms_pass = env("LMS_USERNAME") or env("EWS_USERNAME"), env("LMS_PASSWORD") or env("EWS_PASSWORD")
    if lms_user and lms_pass and not store.get_account(owner, "lms"):
        store.set_account(owner, "lms", {"username": lms_user, "password": lms_pass})
    if env("SAMAD_USERNAME") and env("SAMAD_PASSWORD") and not store.get_account(owner, "samad"):
        store.set_account(owner, "samad", {"username": env("SAMAD_USERNAME"), "password": env("SAMAD_PASSWORD")})

    data = Path(os.environ.get("DATA_DIR", "/data"))
    try:  # emails already forwarded
        store.put(f"mail_seen:{owner}", json.loads((data / "state.json").read_text()))
    except (OSError, ValueError):
        pass
    try:  # last Samad reminder day
        last = json.loads((data / "samad_state.json").read_text()).get("last_reminder")
        if last:
            store.put(f"samad_reminder:{owner}", last)
    except (OSError, ValueError):
        pass
    try:  # short email keys used by buttons on old messages
        ids = json.loads((data / "mail_keys.json").read_text()).get("ids", {})
        with store._lock, store._connect() as conn:
            for k, v in ids.items():
                conn.execute("INSERT OR IGNORE INTO mail_keys (key, item_id) VALUES (?, ?)", (int(k), v))
    except (OSError, ValueError):
        pass
    for service in ("samad", "lms"):  # sessions saved by the single-user version
        old = store.get(f"{service}_session")
        if old:
            store.put(f"{service}_session:{owner}", old)
            store.delete(f"{service}_session")
    store.put("bootstrapped", True)
    log.info("Imported existing setup for chat %s", owner)


def main() -> None:
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", force=True)
    logging.getLogger("httpx").setLevel(logging.WARNING)  # its URLs contain the bot token

    from .bot import BotAPI, mail_loop, reminder_loop, updates_loop

    bootstrap_owner()
    bot = BotAPI()
    threading.Thread(target=updates_loop, args=(bot,), daemon=True, name="bot-updates").start()
    threading.Thread(target=reminder_loop, args=(bot,), daemon=True, name="samad-reminder").start()
    log.info("Bot started")
    mail_loop(bot)
