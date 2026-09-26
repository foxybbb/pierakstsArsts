"""Получение данных о свободных временах с eveselibaspunkts.lv.

Страница — Vue SPA, данные приходят из POST /{lang}/Booking/ListSpecialistCalendars.
Этот эндпоинт требует Laravel CSRF (cookie XSRF-TOKEN + заголовок X-XSRF-TOKEN),
поэтому сначала открываем саму страницу, берём cookie, затем делаем POST.
Если это не сработало (например, Cloudflare-челлендж), используем headless Chromium.
"""

from __future__ import annotations

import logging
from datetime import datetime
from urllib.parse import parse_qs, unquote, urlparse
from zoneinfo import ZoneInfo

import httpx

log = logging.getLogger(__name__)

RIGA = ZoneInfo("Europe/Riga")
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0 Safari/537.36"
)
API_PATH = "Booking/ListSpecialistCalendars"


class FetchError(Exception):
    pass


def _api_url_and_body(page_url: str) -> tuple[str, dict]:
    u = urlparse(page_url)
    q = {k: v[0] for k, v in parse_qs(u.query).items()}
    lang = u.path.strip("/").split("/")[0] or "lv"
    body = {
        "InstitutionCode": q.get("InstitutionCode"),
        "ServiceCode": q.get("ServiceCode"),
        "ServiceType": q.get("ServiceType"),
        "DateFrom": datetime.now(RIGA).strftime("%d.%m.%Y"),
    }
    for extra in ("SpecialistCode", "SpecialityCode"):
        if extra in q:
            body[extra] = q[extra]
    return f"{u.scheme}://{u.netloc}/{lang}/{API_PATH}", body


def fetch_http(page_url: str) -> dict:
    api_url, body = _api_url_and_body(page_url)
    headers = {"User-Agent": UA, "Accept-Language": "lv,en;q=0.8"}
    with httpx.Client(headers=headers, timeout=30, follow_redirects=True) as c:
        r = c.get(page_url)
        r.raise_for_status()
        token = c.cookies.get("XSRF-TOKEN")
        if not token:
            raise FetchError("XSRF-TOKEN cookie not set")
        r = c.post(
            api_url,
            json=body,
            headers={
                "Accept": "application/json",
                "X-Requested-With": "XMLHttpRequest",
                "X-XSRF-TOKEN": unquote(token),
                "Referer": page_url,
            },
        )
        if r.status_code != 200:
            raise FetchError(f"API HTTP {r.status_code}: {r.text[:200]}")
        return r.json()


def fetch_browser(page_url: str) -> dict:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page(user_agent=UA, locale="lv-LV")
            with page.expect_response(
                lambda resp: API_PATH in resp.url and resp.request.method == "POST",
                timeout=60_000,
            ) as info:
                page.goto(page_url, wait_until="domcontentloaded", timeout=60_000)
            resp = info.value
            if resp.status != 200:
                raise FetchError(f"browser API HTTP {resp.status}")
            return resp.json()
        finally:
            browser.close()


def fetch(page_url: str, mode: str = "auto") -> dict:
    if mode == "browser":
        return fetch_browser(page_url)
    try:
        return fetch_http(page_url)
    except Exception as e:
        if mode == "http":
            raise
        log.warning("HTTP fetch failed (%s), falling back to headless browser", e)
        return fetch_browser(page_url)


def specialists(data: dict) -> list[dict]:
    """Все врачи из ответа, в т.ч. без свободных времён."""
    out = []
    for key in ("selectedSpecialists", "otherSpecialists", "specialistsWithoutTimeSlots"):
        for s in data.get(key) or []:
            na = s.get("nextAvailability") or {}
            if not isinstance(na, dict):  # у врачей без времён приходит []
                na = {}
            out.append(
                {
                    "key": s.get("key") or s.get("specialistCode"),
                    "name": s.get("displayName", "?"),
                    "speciality": s.get("specialityDisplayName", ""),
                    "institution": s.get("institutionDisplayName", ""),
                    "paid": (na.get("PaidTime") or {}).get("dateFrom"),
                    "gov": (na.get("GovernmentPaidTime") or {}).get("dateFrom"),
                }
            )
    return out
