"""Мониторинг свободных времён врачей на eveselibaspunkts.lv с уведомлениями в Telegram."""

from __future__ import annotations

import html
import json
import logging
import os
import threading
import time
import unicodedata
from collections import Counter
from datetime import datetime, timedelta
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

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
TG = f"https://api.telegram.org/bot{TOKEN}"

TYPES = {"gov": "valsts apmaksāts", "paid": "maksas"}

check_lock = threading.Lock()


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
    if not cfg.get("watches"):
        raise SystemExit("config: 'watches' is empty")
    return cfg


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
    "/check — проверить сейчас и показать состояние\n"
    "/status — последнее сохранённое состояние\n"
    "/history — когда обычно появляются изменения\n"
    "/schedule — расписание проверок"
)


def command_loop() -> None:
    offset = None
    while True:
        try:
            r = httpx.get(f"{TG}/getUpdates", params={"timeout": 50, "offset": offset}, timeout=60)
            for u in r.json().get("result", []):
                offset = u["update_id"] + 1
                msg = u.get("message") or {}
                chat = str((msg.get("chat") or {}).get("id", ""))
                text = (msg.get("text") or "").split("@")[0].strip()
                if chat != str(CHAT_ID) or not text.startswith("/"):
                    continue
                cfg = load_config()
                tz = ZoneInfo(cfg["timezone"])
                if text == "/check":
                    send("⏳ Проверяю…", chat)
                    send(check(cfg, reason="manual", force_report=True), chat)
                elif text == "/status":
                    st = load_state()
                    lines = [f"Последняя проверка: {st.get('_last_check', '—')}"]
                    for k, v in st.items():
                        if not k.startswith("_"):
                            lines.append(f"<b>{html.escape(v['name'])}</b>: valsts {fmt_dt(v['gov'], tz)} | maksas {fmt_dt(v['paid'], tz)}")
                    send("\n".join(lines), chat)
                elif text == "/history":
                    send(history_report(tz), chat)
                elif text == "/schedule":
                    iv = cfg["interval_minutes"]
                    extra = f"\nи каждые {iv} мин с {cfg['interval_hours'][0]}:00 до {cfg['interval_hours'][1]}:00" if iv else ""
                    send(f"Проверки ({cfg['timezone']}): {', '.join(map(str, cfg['check_times']))}{extra}\n"
                         f"Следующая: {next_run(cfg, datetime.now(tz)).strftime('%d.%m %H:%M')}", chat)
                else:
                    send(HELP, chat)
        except Exception as e:
            log.warning("getUpdates failed: %s", e)
            time.sleep(10)


def main() -> None:
    cfg = load_config()
    log.info("watches: %d, times: %s, interval: %s min", len(cfg["watches"]), cfg["check_times"], cfg["interval_minutes"])
    if os.getenv("ENABLE_COMMANDS", "1") == "1":
        threading.Thread(target=command_loop, daemon=True).start()
    if os.getenv("RUN_ON_START", "1") == "1":
        text = check(cfg, reason="startup")
        if text:
            send(text)
    scheduler_loop()


if __name__ == "__main__":
    main()
