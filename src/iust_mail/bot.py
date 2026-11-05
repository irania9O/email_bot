"""Multi-user Bale/Telegram bot: Samad food reservation, university email and LMS.

Each chat stores its own accounts (SQLite). Background jobs forward new email and send
Samad reminders for every user that has the matching account.
"""

import datetime as dt
import logging
import os
import threading
import time
from datetime import date, datetime

import httpx

from . import store
from .samad import (
    TEHRAN, Samad, SamadError, next_week_start, reserved_program_ids, tehran_today, this_week_start, toman,
)

log = logging.getLogger("iust_mail.bot")


class BotAPI:
    def __init__(self) -> None:
        base = os.environ.get("BOT_API_BASE", "https://api.telegram.org").rstrip("/")
        self.url = f"{base}/bot{os.environ['TELEGRAM_BOT_TOKEN']}"
        proxy = os.environ.get("TELEGRAM_PROXY") or None
        self.client = httpx.Client(proxy=proxy, timeout=60)
        self.lock = threading.Lock()  # httpx.Client is shared by the bot and background threads

    def call(self, method: str, **payload) -> dict:
        with self.lock:
            r = self.client.post(f"{self.url}/{method}", json=payload)
        data = r.json()
        if not data.get("ok"):
            raise RuntimeError(f"{method} failed: {data.get('description')}")
        return data["result"]

    def send(self, chat_id: int, text: str, keyboard: list | None = None,
             reply_keyboard: list | None = None, parse_mode: str = "") -> dict:
        payload = {"chat_id": chat_id, "text": text[:4096]}
        if parse_mode:
            payload["parse_mode"] = parse_mode
        if keyboard:
            payload["reply_markup"] = {"inline_keyboard": keyboard}
        elif reply_keyboard:
            payload["reply_markup"] = {"keyboard": reply_keyboard, "resize_keyboard": True}
        return self.call("sendMessage", **payload)

    def try_call(self, method: str, **payload) -> None:
        try:
            self.call(method, **payload)
        except Exception as e:  # not every Bale build supports every method
            log.warning("%s: %s", method, e)


# =====================================================================
# Accounts: per-user clients and the setup conversation
# =====================================================================

SERVICES = {
    "samad": {"title": "🍽 سماد", "fields": ["username", "password"]},
    "email": {"title": "📧 ایمیل", "fields": ["username", "password", "email"]},
    "lms": {"title": "📚 LMS", "fields": ["username", "password"]},
}
PROMPTS = {
    ("samad", "username"): "نام کاربری سماد را بفرستید (معمولاً شماره‌ی دانشجویی).",
    ("email", "username"): "نام کاربری ایمیل دانشگاه را بفرستید (مثلاً kamali_sa).",
    ("lms", "username"): "نام کاربری ITS را بفرستید (همان نام کاربری ایمیل دانشگاه).",
    ("email", "email"): "آدرس ایمیل دانشگاهی را بفرستید (مثلاً name@comp.iust.ac.ir).",
}
PASSWORD_PROMPT = "رمز عبور را بفرستید.\n🔒 پیام رمز بلافاصله از چت پاک می‌شود و فقط روی سرور ربات ذخیره می‌شود."


class NeedAccount(Exception):
    def __init__(self, service: str) -> None:
        self.service = service


_clients: dict[tuple[int, str], object] = {}
_clients_lock = threading.Lock()


def client(chat_id: int, service: str):
    """The cached Samad / Mailbox / LMS client for a user; raises NeedAccount if not set up."""
    key = (chat_id, service)
    with _clients_lock:
        if key in _clients:
            return _clients[key]
        acct = store.get_account(chat_id, service)
        if not acct:
            raise NeedAccount(service)
        if service == "samad":
            obj = Samad(chat_id, acct["username"], acct["password"])
        elif service == "email":
            from .mail import Mailbox
            obj = Mailbox(acct["username"], acct["password"], acct["email"])
        else:
            from .lms import LMS
            obj = LMS(chat_id, acct["username"], acct["password"])
        _clients[key] = obj
        return obj


def forget_client(chat_id: int, service: str) -> None:
    with _clients_lock:
        _clients.pop((chat_id, service), None)


def verify(chat_id: int, service: str, data: dict) -> None:
    """Try logging in with the new credentials; raises with a readable message on failure."""
    if service == "samad":
        Samad(chat_id, data["username"], data["password"]).login()
    elif service == "email":
        from .mail import check_login
        try:
            check_login(data["username"], data["password"], data["email"])
        except Exception as e:
            name = type(e).__name__
            if "NonExistentMailbox" in name:
                raise RuntimeError("آدرس ایمیل پیدا نشد؛ آدرس را بررسی کنید.") from e
            if "Unauthorized" in name or "401" in str(e):
                raise RuntimeError("نام کاربری یا رمز ایمیل اشتباه است.") from e
            raise
    else:
        from .lms import LMS
        LMS(chat_id, data["username"], data["password"]).login()


def start_setup(bot: BotAPI, chat_id: int, service: str, intro: str = "") -> None:
    store.put(f"setup:{chat_id}", {"service": service, "step": 0, "data": {}})
    rows = []
    email = store.get_account(chat_id, "email")
    if service == "lms" and email:
        rows.append([{"text": f"♻️ همان حساب ایمیل ({email['username']})", "callback_data": "a:copy:lms"}])
    rows.append([{"text": "✖️ انصراف", "callback_data": "a:cancel:0"}])
    title = SERVICES[service]["title"]
    bot.send(chat_id, f"{intro}🔐 تنظیم حساب {title}\n\n{PROMPTS[(service, 'username')]}", rows)


def handle_setup_message(bot: BotAPI, chat_id: int, msg: dict, state: dict) -> None:
    service, step, data = state["service"], state["step"], state["data"]
    fields = SERVICES[service]["fields"]
    field = fields[step]
    value = (msg.get("text") or "").strip()
    if field == "password":  # don't leave the password in the chat history
        bot.try_call("deleteMessage", chat_id=chat_id, message_id=msg["message_id"])
    if not value:
        bot.send(chat_id, "مقدار خالی است؛ دوباره بفرستید.")
        return
    data[field] = value
    if step + 1 < len(fields):
        store.put(f"setup:{chat_id}", {"service": service, "step": step + 1, "data": data})
        nxt = fields[step + 1]
        prompt = PASSWORD_PROMPT if nxt == "password" else PROMPTS[(service, nxt)]
        bot.send(chat_id, prompt, [[{"text": "✖️ انصراف", "callback_data": "a:cancel:0"}]])
        return
    finish_setup(bot, chat_id, service, data)


def finish_setup(bot: BotAPI, chat_id: int, service: str, data: dict) -> None:
    title = SERVICES[service]["title"]
    bot.send(chat_id, "⏳ در حال بررسی ورود…")
    try:
        verify(chat_id, service, data)
    except Exception as e:
        log.info("setup failed for %s/%s: %s", chat_id, service, e)
        store.delete(f"setup:{chat_id}")
        bot.send(chat_id, f"❌ ورود به {title} ناموفق بود: {e}",
                 [[{"text": "🔁 دوباره", "callback_data": f"a:set:{service}"}]])
        return
    store.delete(f"setup:{chat_id}")
    store.set_account(chat_id, service, data)
    forget_client(chat_id, service)
    if service == "email":
        store.delete(f"mail_seen:{chat_id}")  # start forwarding from now on
    bot.send(chat_id, f"✅ حساب {title} ذخیره شد.")
    bot.send(chat_id, *root_view(chat_id, service))


def accounts_view(chat_id: int) -> tuple[str, list]:
    lines = ["⚙️ حساب‌های شما", ""]
    rows = []
    for service, info in SERVICES.items():
        acct = store.get_account(chat_id, service)
        lines.append(f"{info['title']}: " + (f"✅ {acct['username']}" if acct else "❌ تنظیم نشده"))
        row = [{"text": f"✏️ {info['title']}", "callback_data": f"a:set:{service}"}]
        if acct:
            row.append({"text": "🗑 حذف", "callback_data": f"a:del:{service}"})
        rows.append(row)
    lines += ["", "رمزها فقط روی سرور ربات ذخیره می‌شوند. با «حذف» پاک می‌شوند."]
    return "\n".join(lines), rows


def handle_account_callback(bot: BotAPI, chat_id: int, cq: dict) -> None:
    _, action, service = cq["data"].split(":", 2)
    bot.try_call("answerCallbackQuery", callback_query_id=cq["id"])
    if action == "cancel":
        store.delete(f"setup:{chat_id}")
        show(bot, chat_id, cq, "لغو شد.", [])
    elif action == "set":
        start_setup(bot, chat_id, service)
    elif action == "copy":  # LMS with the same ITS account as email
        email = store.get_account(chat_id, "email")
        store.delete(f"setup:{chat_id}")
        if email:
            finish_setup(bot, chat_id, "lms", {"username": email["username"], "password": email["password"]})
    elif action == "del":
        title = SERVICES[service]["title"]
        show(bot, chat_id, cq, f"حساب {title} حذف شود؟",
             [[{"text": "🗑 بله، حذف کن", "callback_data": f"a:DEL:{service}"},
               {"text": "↩️ نه", "callback_data": "a:view:0"}]])
    elif action == "DEL":
        store.delete_account(chat_id, service)
        forget_client(chat_id, service)
        show(bot, chat_id, cq, *accounts_view(chat_id))
    elif action == "view":
        show(bot, chat_id, cq, *accounts_view(chat_id))


# =====================================================================
# Navigation helpers
# =====================================================================

FOOD_BTN = "🍽 سماد"
MAIL_BTN = "📧 ایمیل"
LMS_BTN = "📚 LMS"
ACCOUNTS_BTN = "⚙️ حساب‌ها"
MAIN_KEYBOARD = [[{"text": FOOD_BTN}, {"text": MAIL_BTN}], [{"text": LMS_BTN}, {"text": ACCOUNTS_BTN}]]


def back(data: str, text: str = "↩️ برگشت") -> list:
    return [{"text": text, "callback_data": data}]


def show(bot: BotAPI, chat_id: int, cq: dict | None, text: str, rows: list) -> None:
    """Edit the tapped message in place (one message per section); send a new one otherwise."""
    msg = (cq or {}).get("message")
    if msg:
        try:
            bot.call("editMessageText", chat_id=chat_id, message_id=msg["message_id"],
                     text=text[:4096], reply_markup={"inline_keyboard": rows})
            return
        except Exception as e:
            if "not modified" in str(e).lower():
                return
            log.warning("edit failed, sending new message: %s", e)
    bot.send(chat_id, text, rows)


def has_button(cq: dict, data: str) -> bool:
    kb = ((cq.get("message") or {}).get("reply_markup") or {}).get("inline_keyboard") or []
    return any(b.get("callback_data") == data for row in kb for b in row)


def root_view(chat_id: int, service: str) -> tuple[str, list]:
    if service == "samad":
        return food_root()
    if service == "email":
        return mail_root()
    return courses_view(chat_id)


# =====================================================================
# Samad
# =====================================================================

FOOD_MENU = [[{"text": "🗓 رزروهای این هفته", "callback_data": "f:week"}],
             [{"text": "📋 منوی هفته‌ی بعد", "callback_data": "f:menu"}],
             [{"text": "💰 موجودی", "callback_data": "f:credit"}]]


def food_root() -> tuple[str, list]:
    return FOOD_BTN, FOOD_MENU


def day_messages(samad: Samad, start: date) -> list[tuple[str, list]]:
    """One (text, inline keyboard) per day of the week that has food on the menu."""
    programs = samad.week_programs(start)
    week = samad.week_reserves(start)
    reserved = reserved_program_ids(week)
    days = {d["date"]: d for d in week.get("weekDays", [])}
    by_date: dict[str, list[dict]] = {}
    for p in programs:
        by_date.setdefault(p["date"], []).append(p)
    out = []
    for d in sorted(by_date):
        info = days.get(d, {})
        text = f"🍽 {info.get('dayTranslated', d)} {info.get('dateJStr', '')}"
        rows = []
        for p in sorted(by_date[d], key=lambda p: p["mealTypeId"]):
            res = reserved.get(p["programId"])
            closed = p.get("reserveRuleViolated")
            for ft in p.get("programFoodTypes") or [p]:
                food = ft.get("foodNames") or ft.get("foodName") or p.get("foodName", "?")
                ft_id = ft.get("foodTypeId", p["foodTypeId"])
                is_mine = bool(res and res.get("foodTypeId") == ft_id)
                mark = "✅ " if is_mine else ("🔒 " if closed else "")
                label = f"{mark}{p['mealTypeName']}: {food} — {toman(ft.get('price', p['price']))}"
                action = "c" if is_mine else "r"
                rows.append([{"text": label[:64],
                              "callback_data": f"{action}:{p['programId']}:{ft_id}:{p['mealTypeId']}:{start:%Y%m%d}"}])
        if rows:
            out.append((text, rows))
    return out


def week_view(samad: Samad, start: date | None = None) -> tuple[str, list]:
    start = start or this_week_start()
    week = samad.week_reserves(start)
    today = tehran_today().isoformat()
    lines = [f"🗓 رزروهای هفته (از {start:%Y-%m-%d})", ""]
    for day in week.get("weekDays", []):
        for meal in day.get("mealTypes") or []:
            res = meal.get("reserve")
            if not (res and res.get("selected")):
                continue
            if res.get("consumed"):
                status = "✔️ تحویل گرفته"
            elif day["date"] < today:
                status = "⚪️ گذشته"
            elif day["date"] == today:
                status = "👈 امروز"
            else:
                status = "⏳"
            lines.append(f"{status} {day.get('dayTranslated', '')} {day.get('dateJStr', '')} — "
                         f"{meal.get('name', '')}: {res.get('foodNames', '?')} ({res.get('selfName', '')})")
    if len(lines) == 2:
        lines.append("این هفته رزروی ندارید.")
    lines += ["", f"💰 موجودی: {toman(week.get('remainCredit', 0))}"]
    return "\n".join(lines), [back("f:root")]


def send_menu(bot: BotAPI, chat_id: int, samad: Samad, start: date | None = None) -> None:
    start = start or next_week_start()
    credit = samad.credit()
    msgs = day_messages(samad, start)
    if not msgs:
        bot.send(chat_id, f"منوی هفته‌ی {start:%Y-%m-%d} هنوز منتشر نشده.\n💰 موجودی: {toman(credit)}")
        return
    bot.send(chat_id, f"📋 منوی هفته‌ی بعد (از {start:%Y-%m-%d})\n💰 موجودی: {toman(credit)}\n\n"
                      "روی هر غذا بزنید تا رزرو شود. ✅ = رزرو شده (بزنید تا لغو شود)، 🔒 = مهلت گذشته.")
    for text, kb in msgs:
        bot.send(chat_id, text, kb)


def handle_food_callback(bot: BotAPI, chat_id: int, cq: dict) -> None:
    data = cq["data"]
    samad = client(chat_id, "samad")
    if data.startswith("f:"):
        bot.try_call("answerCallbackQuery", callback_query_id=cq["id"])
        if data == "f:root":
            show(bot, chat_id, cq, *food_root())
        elif data == "f:menu":
            send_menu(bot, chat_id, samad)  # one message per day, each with its own reserve buttons
        elif data == "f:week":
            show(bot, chat_id, cq, *week_view(samad))
        elif data == "f:credit":
            show(bot, chat_id, cq, f"💰 موجودی: {toman(samad.credit())}", [back("f:root")])
        return
    # reserve / cancel: "r|c:programId:foodTypeId:mealTypeId:weekStart"
    action, program_id, ft_id, meal_id, ws = data.split(":")
    try:
        samad.set_reserved(int(program_id), int(ft_id), int(meal_id), selected=(action == "r"))
        note = "✅ رزرو شد" if action == "r" else "❌ رزرو لغو شد"
        ok = True
    except SamadError as e:
        note, ok = f"⚠️ {e}", False
    bot.try_call("answerCallbackQuery", callback_query_id=cq["id"], text=note[:200])
    msg = cq.get("message") or {}
    if ok and msg:  # redraw this day's buttons so ✅ moves
        start = datetime.strptime(ws, "%Y%m%d").date()
        for _, kb in day_messages(samad, start):
            if any(b["callback_data"].split(":")[1] == program_id for row in kb for b in row):
                bot.try_call("editMessageReplyMarkup", chat_id=chat_id, message_id=msg["message_id"],
                             reply_markup={"inline_keyboard": kb})
                break
    bot.send(chat_id, f"{note}\n💰 موجودی: {toman(samad.credit())}")


# =====================================================================
# Email
# =====================================================================

MAIL_MENU = [[{"text": "📥 آخرین ایمیل‌ها", "callback_data": "m:list"}],
             [{"text": "🔵 خوانده‌نشده‌ها", "callback_data": "m:unread"}]]


def mail_root() -> tuple[str, list]:
    return MAIL_BTN, MAIL_MENU


def mail_buttons(key: str, is_read: bool) -> list:
    toggle = ({"text": "🔵 خوانده‌نشده کن", "callback_data": f"m:u:{key}"} if is_read
              else {"text": "✅ خوانده شد", "callback_data": f"m:r:{key}"})
    return [[toggle, {"text": "🗑 حذف", "callback_data": f"m:d:{key}"}]]


def mail_list_view(chat_id: int, unread_only: bool) -> tuple[str, list]:
    items = client(chat_id, "email").list(unread_only)
    rows = []
    for m in items:
        sender = (m.sender.name or m.sender.email_address) if m.sender else "?"
        dot = "🔵 " if not m.is_read else ""
        rows.append([{"text": f"{dot}{m.subject or '(بدون موضوع)'} — {sender}"[:64],
                      "callback_data": f"m:o:{store.mail_key_for(m.id)}"}])
    if not items:
        title = "ایمیل خوانده‌نشده‌ای ندارید ✅" if unread_only else "صندوق ورودی خالی است."
    else:
        title = "📥 ایمیل‌های خوانده‌نشده:" if unread_only else "📥 آخرین ایمیل‌ها:"
    rows.append(back("m:root"))
    return title, rows


def handle_mail_callback(bot: BotAPI, chat_id: int, cq: dict) -> None:
    from .mail import format_message
    data = cq["data"]
    box = client(chat_id, "email")
    if data in ("m:root", "m:list", "m:unread"):
        bot.try_call("answerCallbackQuery", callback_query_id=cq["id"])
        view = mail_root() if data == "m:root" else mail_list_view(chat_id, data == "m:unread")
        show(bot, chat_id, cq, *view)
        return
    _, action, key = data.split(":", 2)
    item_id = store.mail_item_id(key)
    in_menu = has_button(cq, "m:list") or action == "o"  # opened from the list -> keep a back button
    extra = [back("m:list", "↩️ برگشت به فهرست")] if in_menu else []
    msg_id = (cq.get("message") or {}).get("message_id")

    def set_buttons(kb):
        if msg_id:
            bot.try_call("editMessageReplyMarkup", chat_id=chat_id, message_id=msg_id,
                         reply_markup={"inline_keyboard": kb})

    note, missing = "", "این ایمیل پیدا نشد (شاید حذف شده)."
    try:
        if action == "o":
            m = box.get(item_id)
            if m is None:
                note = missing
            else:
                show(bot, chat_id, cq, format_message(m), mail_buttons(key, m.is_read) + extra)
        elif action in ("r", "u"):
            read = action == "r"
            if box.set_read(item_id, read):
                set_buttons(mail_buttons(key, read) + extra)
                note = "✅ خوانده شد" if read else "🔵 خوانده‌نشده شد"
            else:
                note = missing
        elif action == "d":
            set_buttons([[{"text": "🗑 بله، حذف کن", "callback_data": f"m:D:{key}"},
                          {"text": "↩️ نه", "callback_data": f"m:n:{key}"}]] + extra)
            note = "مطمئنید؟"
        elif action == "n":
            m = box.get(item_id)
            set_buttons(mail_buttons(key, bool(m and m.is_read)) + extra)
            note = "لغو شد"
        elif action == "D":
            if box.trash(item_id):
                if extra:
                    show(bot, chat_id, cq, *mail_list_view(chat_id, False))
                else:
                    set_buttons([[{"text": "🗑 به سطل زباله منتقل شد", "callback_data": "m:x:0"}]])
                note = "🗑 حذف شد"
            else:
                note = missing
    except Exception as e:
        box.reset()
        log.exception("mail action failed")
        note = f"⚠️ خطا: {e}"
    bot.try_call("answerCallbackQuery", callback_query_id=cq["id"], text=note[:200])
    if note.startswith(("⚠️", "این ایمیل")):
        bot.send(chat_id, note)


# =====================================================================
# LMS
# =====================================================================

CATEGORY_TITLES = {"a": "📝 تکالیف و ددلاین‌ها", "f": "📂 فایل‌ها و محتوا", "v": "🎥 کلاس آنلاین"}
MOD_ICONS = {"assign": "📝", "quiz": "❓", "resource": "📄", "folder": "📁", "url": "🔗", "page": "📃",
             "forum": "💬", "adobeconnect": "🎥", "bigbluebuttonbn": "🎥", "skyroom": "🎥"}
PAGE_SIZE = 10
_course_names: dict[tuple[int, int], str] = {}


def course_name(chat_id: int, cid: int) -> str:
    if (chat_id, cid) not in _course_names:
        try:
            courses_view(chat_id)
        except Exception:
            pass
    return _course_names.get((chat_id, cid), f"درس {cid}")


def courses_view(chat_id: int) -> tuple[str, list]:
    courses = client(chat_id, "lms").courses()
    for c in courses:
        _course_names[(chat_id, c["id"])] = c["fullname"]
    if not courses:
        return "📚 این ترم درسی در LMS ندارید.", []
    rows = [[{"text": c["fullname"][:64], "callback_data": f"l:c:{c['id']}"}] for c in courses]
    return f"📚 کلاس‌های شما ({len(courses)} درس):", rows


def course_view(chat_id: int, cid: int) -> tuple[str, list]:
    from .lms import course_url
    rows = [[{"text": CATEGORY_TITLES[k], "callback_data": f"l:{k}:{cid}"}] for k in ("a", "f", "v")]
    rows.append([{"text": "🌐 باز کردن درس در مرورگر", "url": course_url(cid)}])
    rows.append(back("l:list:0", "↩️ برگشت به کلاس‌ها"))
    return f"📘 {course_name(chat_id, cid)}", rows


def category_view(chat_id: int, cid: int, cat: str, page: int = 0) -> tuple[str, list]:
    from .lms import category_of
    lms = client(chat_id, "lms")
    state = lms.course_state(cid)
    sections = {s["id"]: s["title"] for s in state.get("section", [])}
    items = [c for c in state.get("cm", [])
             if c.get("uservisible", True) and c.get("url")  # labels are page text, not links
             and category_of(c.get("module", "")) == cat]
    lines = [f"{CATEGORY_TITLES[cat]} — {course_name(chat_id, cid)}", ""]
    if cat == "a":
        events = lms.course_events(cid)
        now = dt.datetime.now(TEHRAN)
        for e in sorted(events, key=lambda e: e["timesort"]):
            t = dt.datetime.fromtimestamp(e["timesort"], TEHRAN)
            left = t - now
            left_s = f"{left.days} روز و {left.seconds // 3600} ساعت" if left.total_seconds() > 0 else "گذشته"
            lines.append(f"⏰ {e['name'].replace(' موعدش رسیده', '')}\n    {t:%Y-%m-%d %H:%M} (مانده: {left_s})")
        if not events:
            lines.append("ددلاین نزدیکی ندارید ✅")
    elif not items:
        lines.append("موردی در این بخش نیست.")
    pages = max(1, -(-len(items) // PAGE_SIZE))
    page = min(max(page, 0), pages - 1)
    if pages > 1:
        lines.append(f"\nصفحه {page + 1} از {pages} ({len(items)} مورد)")
    rows = []
    for c in items[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]:
        if c.get("module") == "adobeconnect":  # join link + recordings instead of the LMS page
            rows.append([{"text": f"🎥 {c['name']}"[:64], "callback_data": f"l:j:{c['id']}:{cid}"}])
            continue
        icon = MOD_ICONS.get(c.get("module"), "▫️")
        sec = sections.get(c.get("sectionid"), "")
        label = f"{icon} {c['name']}" + (f" ({sec})" if sec and cat == "f" else "")
        rows.append([{"text": label[:64], "url": c["url"]}])
    if pages > 1:
        nav = []
        if page > 0:
            nav.append({"text": "➡️ قبلی", "callback_data": f"l:{cat}:{cid}:{page - 1}"})
        if page < pages - 1:
            nav.append({"text": "بعدی ⬅️", "callback_data": f"l:{cat}:{cid}:{page + 1}"})
        rows.append(nav)
    rows.append(back(f"l:c:{cid}"))
    return "\n".join(lines).strip(), rows


def adobeconnect_view(chat_id: int, cmid: int, cid: str) -> tuple[str, list]:
    info = client(chat_id, "lms").adobeconnect(cmid)
    lines = [f"🎥 {info['title']}"]
    if info["schedule"]:
        lines.append(f"🕒 {info['schedule']}")
    rows = [[{"text": "🔴 ورود به کلاس آنلاین", "callback_data": f"l:J:{cmid}:{cid}"}]]
    recs = info["recordings"][:5]
    if recs:
        lines += ["", "📼 جلسات ضبط‌شده (جدیدترین اول):"]
        for n, r in enumerate(recs, 1):
            lines.append(f"{n}. {r['start']} — مدت {r['duration']}")
            row = [{"text": f"▶️ تماشای جلسه {n}", "callback_data": f"l:w:{cmid}:{cid}:{r['id']}"}]
            if r["download"]:
                row.append({"text": f"⬇️ دانلود MP4 جلسه {n}", "url": r["download"]})
            rows.append(row)
        if any(r["download"] for r in recs):
            lines.append("\n⬇️ دانلود فقط در مرورگری کار می‌کند که در LMS وارد شده باشید.")
    else:
        lines += ["", "هنوز جلسه‌ی ضبط‌شده‌ای نیست."]
    rows.append(back(f"l:v:{cid}"))
    return "\n".join(lines), rows


def link_view(title: str, url: str, back_data: str) -> tuple[str, list]:
    return (f"{title}\n(این لینک شخصی است و بعد از مدتی منقضی می‌شود)",
            [[{"text": title, "url": url}], back(back_data)])


def handle_lms_callback(bot: BotAPI, chat_id: int, cq: dict) -> None:
    bot.try_call("answerCallbackQuery", callback_query_id=cq["id"])
    parts = cq["data"].split(":")
    action = parts[1]
    lms = client(chat_id, "lms")
    if action == "list":
        view = courses_view(chat_id)
    elif action == "c":
        view = course_view(chat_id, int(parts[2]))
    elif action in CATEGORY_TITLES:
        view = category_view(chat_id, int(parts[2]), action, int(parts[3]) if len(parts) > 3 else 0)
    elif action == "j":  # l:j:cmid:cid
        view = adobeconnect_view(chat_id, int(parts[2]), parts[3] if len(parts) > 3 else "0")
    elif action == "J":  # l:J:cmid:cid
        cmid, cid = parts[2], parts[3] if len(parts) > 3 else "0"
        view = link_view("🔴 ورود به کلاس آنلاین", lms.adobeconnect_join_url(int(cmid)), f"l:j:{cmid}:{cid}")
    elif action == "w":  # l:w:cmid:cid:recid
        cmid, cid, rec = parts[2], parts[3], parts[4]
        view = link_view("▶️ تماشای جلسه‌ی ضبط‌شده",
                         lms.adobeconnect_recording_url(int(cmid), rec), f"l:j:{cmid}:{cid}")
    else:
        return
    show(bot, chat_id, cq, *view)


# =====================================================================
# Dispatch
# =====================================================================

def allowed(chat_id: int) -> bool:
    ids = os.environ.get("ALLOWED_CHAT_IDS", "").strip()
    return not ids or str(chat_id) in {x.strip() for x in ids.split(",")}


def handle_callback(bot: BotAPI, cq: dict) -> None:
    chat_id = (cq.get("message") or {}).get("chat", {}).get("id") or cq["from"]["id"]
    if not allowed(chat_id):
        return
    data = cq.get("data", "")
    try:
        if data.startswith("a:"):
            handle_account_callback(bot, chat_id, cq)
        elif data.startswith("l:"):
            handle_lms_callback(bot, chat_id, cq)
        elif data.startswith("m:"):
            handle_mail_callback(bot, chat_id, cq)
        elif data.startswith("f:") or (data[:2] in ("r:", "c:") and data.count(":") == 4):
            handle_food_callback(bot, chat_id, cq)
        else:
            bot.try_call("answerCallbackQuery", callback_query_id=cq["id"])  # unknown/old button
    except NeedAccount as e:
        bot.try_call("answerCallbackQuery", callback_query_id=cq["id"])
        start_setup(bot, chat_id, e.service, "اول باید حسابتان را وصل کنید.\n")
    except Exception as e:
        log.exception("callback %s failed", data)
        bot.try_call("answerCallbackQuery", callback_query_id=cq["id"])
        bot.send(chat_id, f"⚠️ خطا: {e}")


def handle_message(bot: BotAPI, msg: dict) -> None:
    chat = msg.get("chat", {})
    chat_id = chat.get("id")
    if chat.get("type", "private") != "private" or not allowed(chat_id):
        return
    text = (msg.get("text") or "").strip()
    command = text.split("@")[0]
    state = store.get(f"setup:{chat_id}")
    if state and not command.startswith("/") and command not in (FOOD_BTN, MAIL_BTN, LMS_BTN, ACCOUNTS_BTN):
        handle_setup_message(bot, chat_id, msg, state)
        return
    if state:  # user moved on to something else: drop the half-finished setup
        store.delete(f"setup:{chat_id}")
    try:
        if command == "/start":
            bot.send(chat_id, "سلام! 👋\nیکی از بخش‌ها را انتخاب کنید. بار اول نام کاربری و رمز همان بخش "
                              "از شما پرسیده می‌شود.\nبا «⚙️ حساب‌ها» می‌توانید حساب‌ها را تغییر دهید یا حذف کنید.",
                     reply_keyboard=MAIN_KEYBOARD)
        elif command == FOOD_BTN:
            client(chat_id, "samad")
            bot.send(chat_id, *food_root())
        elif command == MAIL_BTN:
            client(chat_id, "email")
            bot.send(chat_id, *mail_root())
        elif command in (LMS_BTN, "/lms"):
            bot.send(chat_id, *courses_view(chat_id))
        elif command in (ACCOUNTS_BTN, "/accounts"):
            bot.send(chat_id, *accounts_view(chat_id))
        elif command == "/menu":
            send_menu(bot, chat_id, client(chat_id, "samad"))
        elif command == "/credit":
            bot.send(chat_id, f"💰 موجودی: {toman(client(chat_id, 'samad').credit())}")
        elif command == "/mail":
            bot.send(chat_id, *mail_list_view(chat_id, False))
    except NeedAccount as e:
        start_setup(bot, chat_id, e.service)
    except Exception as e:
        log.exception("message handling failed")
        bot.send(chat_id, f"⚠️ خطا: {e}")


def updates_loop(bot: BotAPI) -> None:
    offset = None
    while True:
        try:
            payload = {"timeout": 30}
            if offset is not None:
                payload["offset"] = offset
            for upd in bot.call("getUpdates", **payload):
                offset = upd["update_id"] + 1
                try:
                    if "callback_query" in upd:
                        handle_callback(bot, upd["callback_query"])
                    elif "message" in upd:
                        handle_message(bot, upd["message"])
                except Exception:
                    log.exception("update handling failed")
        except Exception:
            log.exception("getUpdates failed; retrying")
            time.sleep(10)


# =====================================================================
# Background jobs (per user)
# =====================================================================

def mail_loop(bot: BotAPI) -> None:
    """Forward new email to every user that has an email account."""
    from .mail import format_message, parse_mode
    interval = int(os.environ.get("POLL_SECONDS", "60"))
    while True:
        for chat_id, _ in store.users_with("email"):
            try:
                box = client(chat_id, "email")
                items = box.latest(30)
                seen = store.get(f"mail_seen:{chat_id}")
                if seen is None:  # first check for this user: don't forward old mail
                    store.put(f"mail_seen:{chat_id}", [m.id for m in items])
                    continue
                seen_set = set(seen)
                for m in reversed(items):
                    if m.id in seen_set:
                        continue
                    log.info("New email for %s: %s", chat_id, m.subject)
                    bot.send(chat_id, format_message(m, parse_mode()),
                             mail_buttons(store.mail_key_for(m.id), m.is_read), parse_mode=parse_mode())
                    seen.append(m.id)
                    seen_set.add(m.id)
                store.put(f"mail_seen:{chat_id}", seen[-500:])
            except NeedAccount:
                pass
            except Exception:
                log.exception("mail check failed for %s", chat_id)
                forget_client(chat_id, "email")
        time.sleep(interval)


def reminder_loop(bot: BotAPI) -> None:
    """On reminder days, remind every Samad user who has not reserved for next week."""
    hour = int(os.environ.get("SAMAD_REMIND_HOUR", "10"))
    # Python weekday numbers: Mon=0 Tue=1 Wed=2 Thu=3 Fri=4 Sat=5 Sun=6. Default: Wednesday, Thursday.
    days = {int(d) for d in os.environ.get("SAMAD_REMIND_DAYS", "2,3").split(",") if d.strip()}
    while True:
        now = datetime.now(TEHRAN)
        today = tehran_today().isoformat()
        if now.weekday() in days and now.hour >= hour:
            for chat_id, _ in store.users_with("samad"):
                key = f"samad_reminder:{chat_id}"
                if store.get(key) == today:
                    continue
                try:
                    remind(bot, chat_id, client(chat_id, "samad"))
                    store.put(key, today)
                except Exception:
                    log.exception("reminder failed for %s", chat_id)
        time.sleep(300)


def remind(bot: BotAPI, chat_id: int, samad: Samad) -> None:
    start = next_week_start()
    if not [p for p in samad.week_programs(start) if not p.get("reserveRuleViolated")]:
        return
    if reserved_program_ids(samad.week_reserves(start)):
        return
    bot.send(chat_id, "⏰ یادآوری: هنوز برای هفته‌ی بعد غذا رزرو نکرده‌اید!")
    send_menu(bot, chat_id, samad, start)
