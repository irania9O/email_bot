# AGENTS.md

Guidance for AI coding agents working on this repository.

## What this is

A multi-user Bale/Telegram bot for Iran University of Science and Technology (IUST) students:

- **📧 Email**: university Exchange mailbox (EWS). New mail is forwarded per user; list, open, mark read/unread, delete.
- **🍽 Samad**: food reservation (`stu.iust.ac.ir`). Next week's menu with reserve/cancel buttons, this week's reservations, credit, and a Wednesday/Thursday reminder.
- **📚 LMS**: Moodle at `lms.iust.ac.ir`, logged in through ITS SSO (`its.iust.ac.ir`). Courses, deadlines, files, and Adobe Connect class join links and recordings.

Every chat links its own accounts from inside the bot. They're stored in SQLite.

User-facing text is **Persian**. Code, comments and commits are **English**.

## Layout

```
src/iust_mail/
  __init__.py   main(): bootstrap, then start the background threads (updates, reminder) and the mail loop
  bot.py        BotAPI, account setup conversation, views/handlers for each section, background loops
  store.py      SQLite: kv (sessions, per-user state), accounts, mail_keys
  mail.py       EWS client (exchangelib), Mailbox, format_message
  samad.py      Samad REST client (OAuth password grant), week/date helpers
  lms.py        Moodle client: ITS SSO login, AJAX web services, Adobe Connect page scraping
Dockerfile, docker-compose.yml, Makefile
```

## Key design points

- **Per-user clients.** Get them with `bot.client(chat_id, service)` (`"samad" | "email" | "lms"`). It raises `NeedAccount` when the user has no account, and the dispatcher turns that into the setup flow. Call `forget_client()` whenever credentials change.
- **Setup conversation.** Progress lives in kv under `setup:{chat_id}`. Credentials are verified with a real login before they're saved. Password messages are deleted from the chat.
- **One message per section.** Navigation uses `show()`, which edits the tapped message (`editMessageText`) and falls back to sending a new one. Every sub-view has a `↩️` back button.
  - Exception: next week's menu is sent as one message per day, because each day has its own reserve buttons.
- **callback_data is limited to 64 bytes.** Prefixes:
  - `a:` accounts
  - `f:` food menu, `r:`/`c:` reserve/cancel
  - `m:` mail; email ids are mapped to short numbers in `mail_keys`
  - `l:` LMS
  - Unknown data is ignored.
- **Sessions persist across restarts.** They're stored in kv as `samad_session:{chat_id}` and `lms_session:{chat_id}`, are re-validated on use, and trigger an automatic re-login when expired.
- **Background loops iterate over users.** `mail_loop` (seen ids in `mail_seen:{chat_id}`) and `reminder_loop` (`samad_reminder:{chat_id}`). A failure for one user must not stop the others.
- **`bootstrap_owner()`** imports the old single-user `.env` setup for `TELEGRAM_CHAT_ID`, once.

## External services: gotchas

- **Samad:** the API base for IUST is `https://stu.iust.ac.ir`.
  - `weekStartDate` must be formatted as `YYYY-MM-DD 00:00:00`.
  - Responses may start with a UTF-8 BOM.
  - Prices are in rials; `toman()` divides by 10.
- **LMS:** many Moodle web services are disabled. These work through `/lib/ajax/service.php`:
  - `core_course_get_enrolled_courses_by_timeline_classification`
  - `core_courseformat_get_state` (returns a JSON string; the item type is in `module`, not `modname`)
  - `core_calendar_get_action_events_by_course`
  - `core_calendar_get_action_events_by_timesort`

  Adobe Connect join and recording URLs are found by following `join.php` / `joinrecording.php` redirects; the resulting URL carries a personal `session=` token. Items with `module == "label"` have no `url`.
- **LMS sign-in emails:** every LMS login sends the user a "New sign in" email. Avoid needless logins in tests.
- **EWS:** the endpoint is `https://email.iust.ac.ir/EWS/Exchange.asmx`. Use `Version(Build(15, 0))` and `autodiscover=False`. Addresses can be on department subdomains (e.g. `@comp.iust.ac.ir`). It's only reachable from inside Iran.
- **Bale:** the API base is `https://tapi.bale.ai`, and it's Telegram-compatible. `editMessageText`, `editMessageReplyMarkup`, `deleteMessage`, inline and reply keyboards, and `url` buttons all work. Don't log request URLs: they contain the bot token (the httpx logger is set to WARNING).

## Build, run, deploy

- **Local run:** `uv run iust-mail` (needs `.env`; see `.env.example`).
- **Image:** `make build`. The Dockerfile installs dependencies from `uv.lock`, then runs the code from `src` via `PYTHONPATH`. There's deliberately no project build step, because DNS inside `docker build` is unreliable here.
  - **Adding a dependency** means the deps layer must download from PyPI during the build. Verify that `make build` actually succeeds.
  - **Never pipe build output into `tail` without checking the exit status.** A failed build once silently shipped an old image.
- **Deploy:** `make deploy` (`HOST=iran` by default). The server can't reach GitHub, PyPI or Docker Hub, so the image is `docker save | ssh docker load`. `make check` then compares a hash of the source files in the image and the running container; image IDs differ because the server uses the containerd store.
- **Server:** `/opt/email_bot` holds `docker-compose.yml`, `.env` and `data/` (`bot.db`, mode 600).
- **Other targets:** `make logs`, `make status`, `make restart`, `make ssh`.

## Testing

There's no test suite yet. Test views and handlers by calling them with a fake bot object that records `send`/`call`, and with `DB_FILE` pointed at a temp file. Real API calls are fine for read-only actions.

Don't make real reservations or cancellations, or mark, delete or change real mail, unless the user asks.

## Secrets

Never commit `.env`, `data/`, `state.json` or any password or token. Scan staged changes before committing. The repo is public.
