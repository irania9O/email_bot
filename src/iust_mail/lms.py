"""IUST LMS (Moodle) client that logs in through ITS single sign-on."""

import logging
import os
import re
import threading

import httpx

from . import store

log = logging.getLogger("iust_mail.lms")
BASE = "https://lms.iust.ac.ir"


class LMSError(Exception):
    pass


class LMS:
    def __init__(self, chat_id: int, username: str, password: str) -> None:
        self.username = username
        self.password = password
        self.session_key = f"lms_session:{chat_id}"
        self.client: httpx.Client | None = None
        self.sesskey: str | None = None
        self.lock = threading.Lock()
        self._restore()

    def _new_client(self) -> httpx.Client:
        return httpx.Client(follow_redirects=True, timeout=40, headers={"User-Agent": "Mozilla/5.0"})

    def _restore(self) -> None:
        """Reuse the session saved in the database (it is re-validated on first use)."""
        saved = store.get(self.session_key) or {}
        if saved.get("username") != self.username or not saved.get("sesskey"):
            return
        self.client = self._new_client()
        for ck in saved.get("cookies", []):
            self.client.cookies.set(ck["name"], ck["value"], domain=ck["domain"], path=ck["path"])
        self.sesskey = saved["sesskey"]
        log.info("Restored LMS session from database")

    def _save(self) -> None:
        cookies = [{"name": c.name, "value": c.value, "domain": c.domain, "path": c.path}
                   for c in self.client.cookies.jar]
        store.put(self.session_key, {"username": self.username, "sesskey": self.sesskey, "cookies": cookies})

    def login(self) -> None:
        c = self._new_client()
        r = c.get(f"{BASE}/login/index.php")
        m = re.search(r'href="(https://lms\.iust\.ac\.ir/auth/oauth2/login\.php[^"]+)"', r.text)
        if not m:
            raise LMSError("ITS login button not found on LMS login page")
        r = c.get(m.group(1).replace("&amp;", "&"))
        parts = r.text.split('id="oauth2_server_authenticate_form"', 1)
        if len(parts) < 2:
            raise LMSError("ITS login form not found")
        form = parts[1].split("</form>", 1)[0]
        fbid = re.search(r'name="form_build_id" value="([^"]+)"', form).group(1)
        r = c.post(str(r.url), data={
            "name": self.username, "pass": self.password, "form_build_id": fbid,
            "form_id": "oauth2_server_authenticate_form", "op": "ورود",
        })
        sk = re.search(r'"sesskey":"([^"]+)"', r.text)
        if "lms.iust.ac.ir" not in str(r.url) or not sk:
            raise LMSError("ورود به LMS ناموفق بود (نام کاربری یا رمز ITS را بررسی کنید)")
        if self.client:
            self.client.close()
        self.client, self.sesskey = c, sk.group(1)
        self._save()
        log.info("Logged in to LMS")

    def call(self, method: str, **args):
        """Call a Moodle AJAX web service function, logging in again if the session expired."""
        with self.lock:
            for attempt in range(2):
                if not self.sesskey:
                    self.login()
                r = self.client.post(f"{BASE}/lib/ajax/service.php?sesskey={self.sesskey}",
                                     json=[{"index": 0, "methodname": method, "args": args}])
                try:
                    data = r.json()
                except ValueError:  # got an HTML login page: session expired
                    if attempt == 0:
                        self.sesskey = None
                        continue
                    raise LMSError("پاسخ نامعتبر از LMS")
                res = data[0] if isinstance(data, list) and data else data
                if isinstance(res, dict) and res.get("error"):
                    exc = res.get("exception") or {}
                    if attempt == 0 and exc.get("errorcode") in ("servicerequireslogin", "invalidsesskey", "requireloginerror"):
                        self.sesskey = None
                        continue
                    raise LMSError(exc.get("message") or str(res)[:200])
                return res["data"]

    def courses(self) -> list[dict]:
        data = self.call("core_course_get_enrolled_courses_by_timeline_classification",
                         classification="inprogress", limit=0, offset=0, sort="fullname")
        return data["courses"]


    def course_state(self, course_id: int) -> dict:
        import json
        return json.loads(self.call("core_courseformat_get_state", courseid=course_id))

    def course_events(self, course_id: int) -> list[dict]:
        return self.call("core_calendar_get_action_events_by_course", courseid=course_id)["events"]


    def _page(self, url: str) -> httpx.Response:
        """GET an LMS page as the logged-in user, logging in again if redirected to login."""
        with self.lock:
            if not self.sesskey:
                self.login()
            r = self.client.get(url)
            if "/login/" in str(r.url) or "its.iust.ac.ir" in str(r.url):
                self.login()
                r = self.client.get(url)
            return r

    def adobeconnect(self, cmid: int) -> dict:
        """Online class page: title, schedule, recordings (newest first)."""
        import html as htmlmod
        page = self._page(f"{BASE}/mod/adobeconnect/view.php?id={cmid}").text
        main = htmlmod.unescape(page.split('role="main"', 1)[-1])
        title = re.search(r"<h4[^>]*>(.*?)</h4>", main, re.S)
        text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", main))
        sched = re.search(r"زمان تشکیل جلسه\s*:?\s*(.+?)\s*(?:استاد محترم|ورود به جلسه|$)", text)
        recordings = []
        for row in re.findall(r"<tr[^>]*>(.*?)</tr>", main, re.S):
            rec = re.search(r'joinrecording\.php\?[^"]*recording=(\d+)', row)
            if not rec:
                continue
            cells = [re.sub(r"<[^>]+>|\s+", " ", c).strip() for c in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)]
            dl = re.search(r'href="(/mod/adobearchive/get\.php[^"]+)"', row)
            recordings.append({
                "id": rec.group(1),
                "title": cells[1] if len(cells) > 1 else "",
                "start": cells[2] if len(cells) > 2 else "",
                "duration": cells[4] if len(cells) > 4 else "",
                "download": f"{BASE}{dl.group(1)}" if dl else None,
            })
        return {
            "title": re.sub(r"<[^>]+>", "", title.group(1)).strip() if title else "",
            "schedule": sched.group(1).strip() if sched else "",
            "recordings": recordings[::-1],
        }

    def adobeconnect_join_url(self, cmid: int) -> str:
        """Fresh Adobe Connect URL (with a personal session) for the live class."""
        r = self._page(f"{BASE}/mod/adobeconnect/join.php?id={cmid}&sesskey={self.sesskey}&groupid=0")
        if "connect." not in str(r.url):
            raise LMSError("لینک کلاس پیدا نشد")
        return str(r.url)

    def adobeconnect_recording_url(self, cmid: int, rec_id: str) -> str:
        r = self._page(f"{BASE}/mod/adobeconnect/joinrecording.php?id={cmid}&recording={rec_id}"
                       f"&groupid=0&sesskey={self.sesskey}")
        if "connect." not in str(r.url):
            raise LMSError("لینک ضبط جلسه پیدا نشد")
        return str(r.url)


# Activity types grouped into the three sections shown in the bot.
ASSIGN_MODS = {"assign", "quiz", "workshop", "lesson", "choice", "feedback"}
LIVE_MODS = {"adobeconnect", "bigbluebuttonbn", "skyroom", "zoom", "webinar"}


def category_of(modname: str) -> str:
    if modname in ASSIGN_MODS:
        return "a"
    if modname in LIVE_MODS:
        return "v"
    return "f"  # resources, folders, pages, links, forums...


def course_url(course_id: int) -> str:
    return f"{BASE}/course/view.php?id={course_id}"
