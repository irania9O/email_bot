"""Minimal client for the Samad (samad.app) food reservation API."""

import logging
import os
from datetime import date, datetime, timedelta, timezone

import httpx

from . import store

log = logging.getLogger("iust_mail.samad")

# Public client credentials of the Samad web/mobile app (samad-mobile:samad-mobile-secret).
APP_AUTH = "Basic c2FtYWQtbW9iaWxlOnNhbWFkLW1vYmlsZS1zZWNyZXQ="
TEHRAN = timezone(timedelta(hours=3, minutes=30))


def tehran_today() -> date:
    return datetime.now(TEHRAN).date()


def next_week_start(today: date | None = None) -> date:
    """The Saturday that starts next week (Iranian weeks start on Saturday)."""
    today = today or tehran_today()
    days = (5 - today.weekday()) % 7 or 7  # Monday=0 ... Saturday=5
    return today + timedelta(days=days)


def this_week_start(today: date | None = None) -> date:
    """The Saturday that started the current week."""
    today = today or tehran_today()
    return today - timedelta(days=(today.weekday() - 5) % 7)


def week_param(start: date) -> str:
    return f"{start:%Y-%m-%d} 00:00:00"


class Samad:
    def __init__(self, chat_id: int, username: str, password: str) -> None:
        self.base = os.environ.get("SAMAD_URL", "https://stu.iust.ac.ir").rstrip("/")
        self.chat_id = chat_id
        self.username = username
        self.password = password
        self.self_id = int(os.environ.get("SAMAD_SELF_ID", "1"))
        self.client = httpx.Client(timeout=40)
        self.session_key = f"samad_session:{chat_id}"
        saved = store.get(self.session_key) or {}
        # reuse the saved token only if it belongs to this username
        self.token: str | None = saved.get("token") if saved.get("username") == username else None

    def login(self) -> None:
        r = self.client.post(
            f"{self.base}/oauth/token",
            headers={"Authorization": APP_AUTH},
            data={
                "username": self.username,
                "password": self.password,
                "grant_type": "password",
                "scope": "read write",
            },
        )
        if r.status_code in (400, 401):
            raise SamadError("نام کاربری یا رمز سماد اشتباه است")
        r.raise_for_status()
        self.token = r.json()["access_token"]
        store.put(self.session_key, {"username": self.username, "token": self.token})
        log.info("Logged in to Samad")

    def _request(self, method: str, path: str, **kw) -> dict:
        if not self.token:
            self.login()
        for attempt in range(2):
            r = self.client.request(
                method, f"{self.base}{path}",
                headers={"Authorization": f"Bearer {self.token}"}, **kw,
            )
            if r.status_code == 401 and attempt == 0:
                self.login()
                continue
            break
        if r.status_code >= 400:
            raise SamadError(_error_message(r))
        return r.json()

    def week_reserves(self, start: date) -> dict:
        """weekDays (with reserves), mealTypes, remainCredit."""
        return self._request("GET", "/rest/reserves", params={"weekStartDate": week_param(start)})["payload"]

    def credit(self) -> int:
        """Remaining credit in rials."""
        return int(self._request("GET", "/rest/reserves", params={"weekStartDate": ""})["payload"]["remainCredit"])

    def week_programs(self, start: date) -> list[dict]:
        payload = self._request(
            "GET", "/rest/reservations/programs/v2",
            params={"selfId": self.self_id, "weekStartDate": week_param(start)},
        )["payload"]
        programs = []
        for item in payload.get("selfWeekPrograms") or []:
            programs.extend(item if isinstance(item, list) else [item])
        return programs

    def set_reserved(self, program_id: int, food_type_id: int, meal_type_id: int, selected: bool) -> dict:
        body = {
            "foodTypeId": food_type_id,
            "mealTypeId": meal_type_id,
            "selectedCount": 1 if selected else 0,
            "freeFoodSelected": False,
            "selected": selected,
        }
        return self._request("PUT", f"/rest/reserves/{program_id}/reserve", json=body)


class SamadError(Exception):
    pass


def _error_message(r: httpx.Response) -> str:
    try:
        d = r.json()
        return d.get("messageFa") or d.get("message") or d.get("error_description") or d.get("error") or r.text[:200]
    except ValueError:
        return f"HTTP {r.status_code}"


def reserved_program_ids(week: dict) -> dict[int, dict]:
    """programId -> reserve info, for selected reservations in a week report."""
    out = {}
    for day in week.get("weekDays", []):
        for meal in day.get("mealTypes") or []:
            res = meal.get("reserve")
            if res and res.get("selected"):
                out[res["programId"]] = res
    return out


def toman(rials: int | float) -> str:
    return f"{int(rials) // 10:,} تومان"
