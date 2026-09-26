"""Мониторинг свободных времён врачей на eveselibaspunkts.lv с уведомлениями в Telegram."""

from __future__ import annotations

import html
import json
import logging
import os
import threading
import time
import unicodedata
import uuid
from collections import Counter
from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlparse
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import yaml

from fetcher import fetch, specialists

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("monitor")
logging.getLogger("httpx").setLevel(logging.WARNING)  # иначе в логах виден токен бота

CONFIG_PATH = Path(os.getenv("CONFIG_PATH", "/config/config.yaml"))
DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
STATE_FILE = DATA_DIR / "state.json"
HISTORY_FILE = DATA_DIR / "history.jsonl"
WATCHES_FILE = DATA_DIR / "watches.json"

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
TG = f"https://api.telegram.org/bot{TOKEN}"

TYPES = {"gov": "valsts apmaksāts", "paid": "maksas"}

check_lock = threading.Lock()
watches_lock = threading.Lock()


# ---------- config / state ----------

def load_config() -> dict:
    cfg = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
    if os.getenv("CHECK_TIMES"):
        cfg["check_times"] = [t.strip() for t in os.environ["CHECK_TIMES"].split(",") if t.strip()]
    cfg.setdefault("timezone", "Europe/Riga")
    cfg.setdefault("check_times", ["09:05"])
    cfg.setdefault("interval_minutes", 0)
    cfg.setdefault("interval_hours", [7, 22])
    cfg.setdefault("fetch_mode", os.getenv("FETCH_MODE", "auto"))
    cfg.setdefault("notify_types", ["gov", "paid"])
    cfg.setdefault("notify_unchanged", False)
    cfg.setdefault("notify_lost", True)
    cfg.setdefault("notify_startup", True)
    cfg.setdefault("notify_errors", True)
    cfg.setdefault("weekly_report", {"day": "mon", "time": "09:00"})
    cfg["watches"] = load_watches(cfg.get("watches") or [])
    return cfg


def load_watches(seed: list[dict]) -> list[dict]:
    """Список врачей хранится в data/watches.json (им управляет бот).
    При первом запуске он заполняется из watches в config.yaml."""
    with watches_lock:
        try:
            return json.loads(WATCHES_FILE.read_text(encoding="utf-8"))
        except FileNotFoundError:
            save_watches(seed)
            return seed


def save_watches(watches: list[dict]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = WATCHES_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(watches, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(WATCHES_FILE)


def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STATE_FILE)


def append_history(record: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with HISTORY_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


# ---------- helpers ----------

def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s)
    return "".join(c for c in s if not unicodedata.combining(c)).lower()


def matches(name: str, filters: list[str]) -> bool:
    if not filters:
        return True
    n = norm(name)
    return any(all(tok in n for tok in norm(f).split()) for f in filters)


def fmt_dt(iso: str | None, tz: ZoneInfo) -> str:
    if not iso:
        return "—"
    dt = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(tz)
    return dt.strftime("%d.%m.%Y %H:%M")


def tg(method: str, **payload) -> dict:
    try:
        r = httpx.post(f"{TG}/{method}", json=payload, timeout=30)
        if r.status_code != 200:
            log.error("Telegram %s error %s: %s", method, r.status_code, r.text)
        return r.json()
    except (httpx.HTTPError, ValueError) as e:
        log.error("Telegram %s failed: %s", method, e)
        return {}


def send(text: str, chat_id: str = CHAT_ID) -> None:
    for chunk in [text[i:i + 4000] for i in range(0, len(text), 4000)] or [""]:
        try:
            r = httpx.post(
                f"{TG}/sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": chunk,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
                timeout=30,
            )
            if r.status_code != 200:
                log.error("Telegram error %s: %s", r.status_code, r.text)
        except httpx.HTTPError as e:
            log.error("Telegram send failed: %s", e)


# ---------- core check ----------

def check(cfg: dict, reason: str = "schedule", force_report: bool = False) -> str:
    """Проверяет все watch'и, обновляет state, возвращает текст отчёта ('' если нечего слать)."""
    tz = ZoneInfo(cfg["timezone"])
    with check_lock:
        state = load_state()
        first_run = not state
        now = datetime.now(tz)
        alerts, summary, errors = [], [], []

        for w in cfg["watches"]:
            url = w["url"]
            title = w.get("name") or url
            try:
                data = fetch(url, cfg["fetch_mode"])
            except Exception as e:
                log.exception("fetch failed for %s", title)
                errors.append(f"⚠️ <b>{html.escape(title)}</b>: {html.escape(str(e))[:300]}")
                continue

            docs = [d for d in specialists(data) if matches(d["name"], w.get("doctors") or [])]
            if w.get("doctors") and not docs:
                errors.append(f"⚠️ <b>{html.escape(title)}</b>: врач {w['doctors']} не найден на странице")

            for d in docs:
                skey = f"{url}|{d['key']}"
                old = state.get(skey)
                new = {"name": d["name"], "paid": d["paid"], "gov": d["gov"]}
                head = f"<b>{html.escape(d['name'])}</b> ({html.escape(d['speciality'])}, {html.escape(d['institution'])})"
                line = (
                    f"{head}\n  valsts: {fmt_dt(d['gov'], tz)} | maksas: {fmt_dt(d['paid'], tz)}"
                )
                summary.append(line)

                if old is not None:
                    changes = []
                    for t in ("gov", "paid"):
                        o, n = old.get(t), new[t]
                        if o == n:
                            continue
                        append_history({
                            "ts": now.isoformat(timespec="seconds"),
                            "doctor": d["name"], "type": t, "old": o, "new": n,
                        })
                        if t not in cfg["notify_types"]:
                            continue
                        if n and (not o or n < o):
                            what = "появилось время" if not o else "освободилось более раннее время"
                            changes.append(f"  🟢 {TYPES[t]}: {what}: <b>{fmt_dt(n, tz)}</b> (было {fmt_dt(o, tz)})")
                        elif o and not n and cfg["notify_lost"]:
                            changes.append(f"  🔴 {TYPES[t]}: свободных времён больше нет (было {fmt_dt(o, tz)})")
                    if changes:
                        alerts.append(f"{head}\n" + "\n".join(changes) + f"\n  <a href=\"{html.escape(url)}\">Записаться</a>")
                state[skey] = new

        state["_last_check"] = now.isoformat(timespec="seconds")
        save_state(state)

    stamp = now.strftime("%d.%m.%Y %H:%M")
    parts = []
    if alerts:
        parts.append(f"🔔 <b>Изменения в записи</b> ({stamp})\n\n" + "\n\n".join(alerts))
    if force_report or (first_run and cfg["notify_startup"]) or (cfg["notify_unchanged"] and not alerts):
        hdr = "🚀 Мониторинг запущен" if first_run else "📋 Текущее состояние"
        parts.append(f"{hdr} ({stamp})\n\n" + ("\n\n".join(summary) or "нет данных"))
    if errors and (force_report or cfg["notify_errors"]):
        parts.append("\n".join(errors))
    log.info("check (%s): %d doctors, %d alerts, %d errors", reason, len(summary), len(alerts), len(errors))
    return "\n\n".join(parts)


def history_report(tz: ZoneInfo) -> str:
    try:
        rows = [json.loads(x) for x in HISTORY_FILE.read_text(encoding="utf-8").splitlines() if x.strip()]
    except FileNotFoundError:
        return "История изменений пока пуста."
    if not rows:
        return "История изменений пока пуста."
    hours = Counter(datetime.fromisoformat(r["ts"]).astimezone(tz).hour for r in rows)
    wdays = Counter(datetime.fromisoformat(r["ts"]).astimezone(tz).strftime("%a") for r in rows)
    lines = [f"📈 <b>Когда меняется база</b> (всего {len(rows)} изменений)", "", "По часам (время обнаружения):"]
    for h in sorted(hours):
        lines.append(f"  {h:02d}:00 — {hours[h]} {'█' * min(hours[h], 30)}")
    lines.append("\nПо дням недели: " + ", ".join(f"{k} {v}" for k, v in wdays.most_common()))
    lines.append("\nПоследние:")
    for r in rows[-10:]:
        lines.append(
            f"  {r['ts'][:16].replace('T', ' ')} {html.escape(r['doctor'])} {TYPES[r['type']]}: "
            f"{fmt_dt(r['old'], tz)} → {fmt_dt(r['new'], tz)}"
        )
    return "\n".join(lines)


WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def weekly_report_text(cfg: dict) -> str:
    """Отчёт за 7 дней: появлялись ли записи у каждого отслеживаемого врача."""
    tz = ZoneInfo(cfg["timezone"])
    now = datetime.now(tz)
    since = now - timedelta(days=7)
    try:
        rows = [json.loads(x) for x in HISTORY_FILE.read_text(encoding="utf-8").splitlines() if x.strip()]
    except FileNotFoundError:
        rows = []
    appeared: dict[str, list[str]] = {}
    for r in rows:
        if datetime.fromisoformat(r["ts"]) < since or r["type"] not in cfg["notify_types"]:
            continue
        if r["new"] and (not r["old"] or r["new"] < r["old"]):
            appeared.setdefault(r["doctor"], []).append(
                f"{datetime.fromisoformat(r['ts']).astimezone(tz).strftime('%d.%m %H:%M')} — "
                f"{TYPES[r['type']]} {fmt_dt(r['new'], tz)}")
    state = load_state()
    lines = [f"🗓 <b>Недельный отчёт</b> ({since.strftime('%d.%m')}–{now.strftime('%d.%m.%Y')})"]
    for k, v in state.items():
        if k.startswith("_") or not is_watched(k, v["name"], cfg["watches"]):
            continue
        name = v["name"]
        lines.append("")
        if name in appeared:
            lines.append(f"🟢 <b>{html.escape(name)}</b>: записи появлялись {len(appeared[name])} раз")
            lines.extend(f"  • {x}" for x in appeared[name][-5:])
        else:
            lines.append(f"⚪️ <b>{html.escape(name)}</b>: новых записей не было")
        lines.append(f"  сейчас: valsts {fmt_dt(v['gov'], tz)} | maksas {fmt_dt(v['paid'], tz)}")
    if len(lines) == 1:
        lines.append("\nНет отслеживаемых врачей.")
    return "\n".join(lines)


def next_weekly(cfg: dict, now: datetime) -> datetime | None:
    wr = cfg.get("weekly_report")
    if not wr:
        return None
    wd = WEEKDAYS.index(str(wr.get("day", "mon")).lower()[:3])
    hh, mm = map(int, str(wr.get("time", "09:00")).split(":"))
    for d in range(8):
        day = (now + timedelta(days=d)).date()
        t = datetime(day.year, day.month, day.day, hh, mm, tzinfo=now.tzinfo)
        if day.weekday() == wd and t > now:
            return t
    return None


def weekly_loop() -> None:
    while True:
        cfg = load_config()
        tz = ZoneInfo(cfg["timezone"])
        nxt = next_weekly(cfg, datetime.now(tz))
        if nxt is None:
            time.sleep(3600)
            continue
        log.info("next weekly report at %s", nxt.strftime("%Y-%m-%d %H:%M %Z"))
        while (delay := (nxt - datetime.now(tz)).total_seconds()) > 0:
            time.sleep(min(delay, 60))
        try:
            cfg = load_config()
            check(cfg, reason="weekly")  # свежие данные перед отчётом (алерты уйдут обычным путём)
            send(weekly_report_text(cfg))
        except Exception:
            log.exception("weekly report failed")


# ---------- scheduling ----------

def next_run(cfg: dict, now: datetime) -> datetime:
    tz = now.tzinfo
    candidates = []
    for day in (0, 1):
        base = (now + timedelta(days=day)).date()
        for t in cfg["check_times"]:
            hh, mm = map(int, str(t).split(":"))
            candidates.append(datetime(base.year, base.month, base.day, hh, mm, tzinfo=tz))
        step = int(cfg["interval_minutes"] or 0)
        if step > 0:
            h0, h1 = cfg["interval_hours"]
            m = h0 * 60
            while m <= h1 * 60:
                candidates.append(datetime(base.year, base.month, base.day, m // 60, m % 60, tzinfo=tz))
                m += step
    return min(c for c in candidates if c > now)


def scheduler_loop() -> None:
    while True:
        cfg = load_config()  # перечитываем, чтобы правки конфига применялись без рестарта
        tz = ZoneInfo(cfg["timezone"])
        nxt = next_run(cfg, datetime.now(tz))
        log.info("next check at %s", nxt.strftime("%Y-%m-%d %H:%M %Z"))
        while (delay := (nxt - datetime.now(tz)).total_seconds()) > 0:
            time.sleep(min(delay, 60))
        try:
            text = check(cfg)
            if text:
                send(text)
        except Exception:
            log.exception("check crashed")


# ---------- telegram commands ----------

HELP = (
    "/add &lt;ссылка&gt; [имя врача] — добавить врача (без имени — выбрать кнопкой)\n"
    "/list — список отслеживаемых врачей\n"
    "/remove &lt;N&gt; — убрать врача по номеру из /list\n"
    "/check — проверить сейчас и показать состояние\n"
    "/status — последнее сохранённое состояние\n"
    "/history — когда обычно появляются изменения\n"
    "/week — недельный отчёт сейчас\n"
    "/schedule — расписание проверок"
)

# ожидающие выбора врача после /add без имени: id -> {url, title, names}
pending: dict[str, dict] = {}


def is_watched(state_key: str, name: str, watches: list[dict]) -> bool:
    url = state_key.split("|", 1)[0]
    return any(w["url"] == url and matches(name, w.get("doctors") or []) for w in watches)


def watch_entries(watches: list[dict]) -> list[tuple[int, str | None]]:
    """Плоский список (индекс watch, имя врача | None = все врачи) для нумерации в /list."""
    out = []
    for i, w in enumerate(watches):
        docs = w.get("doctors") or []
        out.extend((i, d) for d in docs) if docs else out.append((i, None))
    return out


def list_text(watches: list[dict]) -> str:
    if not watches:
        return "Список пуст. Добавьте врача: /add &lt;ссылка&gt;"
    lines = ["👩‍⚕️ <b>Отслеживаются:</b>"]
    for n, (i, d) in enumerate(watch_entries(watches), 1):
        w = watches[i]
        lines.append(f"{n}. <b>{html.escape(d or 'все врачи')}</b> — "
                     f"<a href=\"{html.escape(w['url'])}\">{html.escape(w.get('name') or 'ссылка')}</a>")
    return "\n".join(lines)


def valid_url(url: str) -> bool:
    u = urlparse(url)
    return (u.scheme == "https" and (u.hostname or "").endswith("eveselibaspunkts.lv")
            and "InstitutionCode" in parse_qs(u.query))


def add_doctor(url: str, title: str, doctor: str | None) -> str:
    """Добавляет врача (None = все врачи на странице). Возвращает текст ответа."""
    with watches_lock:
        try:
            watches = json.loads(WATCHES_FILE.read_text(encoding="utf-8"))
        except FileNotFoundError:
            watches = []
        w = next((w for w in watches if w["url"] == url), None)
        if w is None:
            watches.append({"name": title, "url": url, "doctors": [doctor] if doctor else []})
        elif not w.get("doctors"):
            return f"Уже отслеживаются все врачи этой страницы ({html.escape(w.get('name') or url)})."
        elif doctor is None:
            w["doctors"] = []
        elif any(norm(d) == norm(doctor) for d in w["doctors"]):
            return f"{html.escape(doctor)} уже в списке."
        else:
            w["doctors"].append(doctor)
        save_watches(watches)
    # сразу проверяем, чтобы сохранить текущее состояние как точку отсчёта
    cfg = load_config()
    report = check(cfg, reason="add")
    if report:
        send(report)
    return f"✅ Добавлено: <b>{html.escape(doctor or 'все врачи')}</b> — {html.escape(title)}\n\n" + status_for(cfg, url, doctor)


def status_for(cfg: dict, url: str, doctor: str | None) -> str:
    tz = ZoneInfo(cfg["timezone"])
    lines = []
    for k, v in load_state().items():
        if k.startswith(url + "|") and matches(v["name"], [doctor] if doctor else []):
            lines.append(f"{html.escape(v['name'])}: valsts {fmt_dt(v['gov'], tz)} | maksas {fmt_dt(v['paid'], tz)}")
    return "\n".join(lines)


def remove_entry(n: int) -> str:
    with watches_lock:
        watches = json.loads(WATCHES_FILE.read_text(encoding="utf-8"))
        entries = watch_entries(watches)
        if not 1 <= n <= len(entries):
            return f"Нет номера {n}. Смотрите /list"
        i, d = entries[n - 1]
        w = watches[i]
        if d is None or len(w["doctors"]) == 1:
            watches.pop(i)
        else:
            w["doctors"].remove(d)
        save_watches(watches)
    with check_lock:  # чистим сохранённое состояние, чтобы /status не показывал удалённого врача
        state = load_state()
        for k in [k for k, v in state.items()
                  if not k.startswith("_") and not is_watched(k, v["name"], watches)]:
            del state[k]
        save_state(state)
    return f"🗑 Убрано: <b>{html.escape(d or 'все врачи')}</b> — {html.escape(w.get('name') or w['url'])}"


def cmd_add(args: str, chat: str) -> None:
    parts = args.split(maxsplit=1)
    if not parts:
        send("Использование: /add &lt;ссылка на страницу записи&gt; [имя врача]\n"
             "Ссылка — страница «Izvēlieties pieraksta laiku» на eveselibaspunkts.lv", chat)
        return
    url, name = parts[0], (parts[1].strip() if len(parts) > 1 else "")
    if not valid_url(url):
        send("Нужна ссылка вида https://eveselibaspunkts.lv/lv/Booking/AvailableTimeSlots?InstitutionCode=…", chat)
        return
    send("⏳ Загружаю страницу…", chat)
    try:
        data = fetch(url, load_config()["fetch_mode"])
    except Exception as e:
        send(f"⚠️ Не удалось загрузить страницу: {html.escape(str(e))[:300]}", chat)
        return
    docs = specialists(data)
    svc = (data.get("selectedService") or {}).get("displayName") or ""
    inst = (data.get("selectedInstitution") or {}).get("displayName") or ""
    title = " — ".join(x for x in (svc, inst) if x) or url
    if not docs:
        send("На странице не найдено ни одного врача.", chat)
        return
    if name:
        found = [d["name"] for d in docs if matches(d["name"], [name])]
        if len(found) == 1:
            send(add_doctor(url, title, found[0]), chat)
            return
        if not found:
            send(f"Врач «{html.escape(name)}» не найден на странице. Выберите из списка:", chat)
        docs = [d for d in docs if d["name"] in found] or docs
    names = list(dict.fromkeys(d["name"] for d in docs))[:40]
    pid = uuid.uuid4().hex[:8]
    pending[pid] = {"url": url, "title": title, "names": names}
    buttons = [[{"text": n, "callback_data": f"add:{pid}:{i}"}] for i, n in enumerate(names)]
    buttons.append([{"text": "Все врачи на странице", "callback_data": f"add:{pid}:*"}])
    tg("sendMessage", chat_id=chat, text=f"<b>{html.escape(title)}</b>\nКого отслеживать?",
       parse_mode="HTML", reply_markup={"inline_keyboard": buttons})


def handle_callback(cq: dict) -> None:
    msg = cq.get("message") or {}
    chat = str((msg.get("chat") or {}).get("id", ""))
    tg("answerCallbackQuery", callback_query_id=cq["id"])
    if chat != str(CHAT_ID):
        return
    _, pid, idx = (cq.get("data") or "::").split(":", 2)
    p = pending.pop(pid, None)
    if p is None:
        tg("editMessageText", chat_id=chat, message_id=msg.get("message_id"),
           text="Выбор устарел, повторите /add")
        return
    doctor = None if idx == "*" else p["names"][int(idx)]
    tg("editMessageText", chat_id=chat, message_id=msg.get("message_id"),
       text=f"Выбрано: {doctor or 'все врачи'}")
    send(add_doctor(p["url"], p["title"], doctor), chat)


def handle_message(msg: dict) -> None:
    chat = str((msg.get("chat") or {}).get("id", ""))
    text = (msg.get("text") or "").strip()
    if chat != str(CHAT_ID) or not text.startswith("/"):
        return
    parts = text.split(maxsplit=1)
    cmd = parts[0].split("@")[0].lower()
    args = parts[1] if len(parts) > 1 else ""
    cfg = load_config()
    tz = ZoneInfo(cfg["timezone"])
    if cmd == "/add":
        cmd_add(args, chat)
    elif cmd == "/list":
        send(list_text(cfg["watches"]), chat)
    elif cmd == "/remove":
        if args.strip().isdigit():
            send(remove_entry(int(args.strip())), chat)
        else:
            send("Использование: /remove &lt;N&gt;\n\n" + list_text(cfg["watches"]), chat)
    elif cmd == "/check":
        send("⏳ Проверяю…", chat)
        send(check(cfg, reason="manual", force_report=True), chat)
    elif cmd == "/status":
        st = load_state()
        lines = [f"Последняя проверка: {st.get('_last_check', '—')}"]
        for k, v in st.items():
            if not k.startswith("_") and is_watched(k, v["name"], cfg["watches"]):
                lines.append(f"<b>{html.escape(v['name'])}</b>: valsts {fmt_dt(v['gov'], tz)} | maksas {fmt_dt(v['paid'], tz)}")
        send("\n".join(lines), chat)
    elif cmd == "/week":
        send(weekly_report_text(cfg), chat)
    elif cmd == "/history":
        send(history_report(tz), chat)
    elif cmd == "/schedule":
        iv = cfg["interval_minutes"]
        extra = f"\nи каждые {iv} мин с {cfg['interval_hours'][0]}:00 до {cfg['interval_hours'][1]}:00" if iv else ""
        send(f"Проверки ({cfg['timezone']}): {', '.join(map(str, cfg['check_times']))}{extra}\n"
             f"Следующая: {next_run(cfg, datetime.now(tz)).strftime('%d.%m %H:%M')}", chat)
    else:
        send(HELP, chat)


def command_loop() -> None:
    tg("setMyCommands", commands=[
        {"command": "add", "description": "Добавить врача: /add <ссылка> [имя]"},
        {"command": "list", "description": "Список врачей"},
        {"command": "remove", "description": "Убрать врача: /remove <N>"},
        {"command": "check", "description": "Проверить сейчас"},
        {"command": "status", "description": "Последнее состояние"},
        {"command": "history", "description": "Когда меняется база"},
        {"command": "week", "description": "Недельный отчёт"},
        {"command": "schedule", "description": "Расписание проверок"},
    ])
    offset = None
    while True:
        try:
            r = httpx.get(f"{TG}/getUpdates", params={"timeout": 50, "offset": offset}, timeout=60)
            for u in r.json().get("result", []):
                offset = u["update_id"] + 1
                try:
                    if "callback_query" in u:
                        handle_callback(u["callback_query"])
                    elif "message" in u:
                        handle_message(u["message"])
                except Exception:
                    log.exception("command failed")
        except Exception as e:
            log.warning("getUpdates failed: %s", e)
            time.sleep(10)


HEARTBEAT = Path("/tmp/heartbeat")


def main() -> None:
    cfg = load_config()
    log.info("watches: %d, times: %s, interval: %s min", len(cfg["watches"]), cfg["check_times"], cfg["interval_minutes"])
    if os.getenv("RUN_ON_START", "1") == "1":
        try:
            text = check(cfg, reason="startup")
            if text:
                send(text)
        except Exception:
            log.exception("startup check failed")

    loops = [scheduler_loop, weekly_loop]
    if os.getenv("ENABLE_COMMANDS", "1") == "1":
        loops.append(command_loop)
    threads = [threading.Thread(target=f, name=f.__name__, daemon=True) for f in loops]
    for t in threads:
        t.start()

    # watchdog: если какой-то поток умер — выходим, Docker (restart: always) перезапустит контейнер
    while True:
        dead = [t.name for t in threads if not t.is_alive()]
        if dead:
            log.critical("threads died: %s — exiting for restart", dead)
            os._exit(1)
        HEARTBEAT.touch()
        time.sleep(30)

if __name__ == "__main__":
    main()
