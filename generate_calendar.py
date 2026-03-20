import datetime
import hashlib
import html
import json
import os
import re
import requests
import sys
from zoneinfo import ZoneInfo

ENDPOINT = "https://locosdewallstreet.com/wp-admin/admin-ajax.php"
LANG = "es"

# Mantener histórico desde enero 2026
START_YEAR = 2026
START_MONTH = 1

# Zona local de los horarios que entrega la web
LOCAL_TZ = ZoneInfo("Europe/Madrid")

# Archivo JSON para artículos detectados
ARTICLES_JSON = "articles.json"

# --- helpers de meses ---
def month_add(year: int, month: int, add: int):
    m = month - 1 + add
    return year + m // 12, (m % 12) + 1

def month_index(year: int, month: int) -> int:
    return year * 12 + (month - 1)

# --- UID estable para que Google no duplique ---
def stable_uid(date_str: str, hora: str, url: str, day_index: int) -> str:
    url = (url or "").strip()
    if url and url != "#":
        base = f"{url}|{date_str}|{hora}"
    else:
        base = f"{date_str}|{hora}|idx:{day_index}"
    return hashlib.sha1(base.encode("utf-8")).hexdigest() + "@lds-auto"

# --- parseo de hora ---
def parse_time_range(hora: str):
    hora = (hora or "").strip()
    if not hora:
        return None

    if "-" in hora:
        a, b = hora.split("-", 1)
        return a.strip(), b.strip()

    # "19:00" -> asumir 1 hora
    if re.match(r"^\d{1,2}:\d{2}$", hora):
        h, m = map(int, hora.split(":"))
        start = f"{h:02d}:{m:02d}"
        end_dt = datetime.datetime(2000, 1, 1, h, m) + datetime.timedelta(hours=1)
        end = f"{end_dt.hour:02d}:{end_dt.minute:02d}"
        return start, end

    return None

# --- fetch del mes (POST, como el front) ---
def fetch_month(y: int, m: int):
    payload = {
        "action": "fr_get_calendar_events",
        "mes": str(m),
        "ano": str(y),
        "lang": LANG,
        "_cb": str(int(datetime.datetime.now(datetime.UTC).timestamp())),
    }

    headers = {
        "User-Agent": "Mozilla/5.0",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Origin": "https://locosdewallstreet.com",
        "Referer": "https://locosdewallstreet.com/",
    }

    r = requests.post(ENDPOINT, data=payload, headers=headers, timeout=30)

    if r.status_code != 200:
        print(f"[WARN] HTTP {r.status_code} {y}-{m:02d} -> devolviendo vacío")
        return {}

    try:
        j = r.json()
    except Exception as e:
        print(f"[WARN] No es JSON {y}-{m:02d}: {e} -> devolviendo vacío")
        return {}

    eventos = j.get("data", {}).get("eventos", {})

    if isinstance(eventos, dict):
        return eventos

    if isinstance(eventos, list):
        out = {}
        for ev in eventos:
            if not isinstance(ev, dict):
                continue
            date_str = ev.get("fecha") or ev.get("date")
            if not date_str:
                continue
            out.setdefault(date_str, []).append(ev)
        return out

    return {}

# --- JSON artículos ---
def load_articles():
    if not os.path.exists(ARTICLES_JSON):
        return {"articles": []}

    try:
        with open(ARTICLES_JSON, "r", encoding="utf-8") as f:
            data = json.load(f)
            if not isinstance(data, dict) or "articles" not in data:
                return {"articles": []}
            if not isinstance(data["articles"], list):
                return {"articles": []}
            return data
    except Exception as e:
        print(f"[WARN] No se pudo leer {ARTICLES_JSON}: {e}")
        return {"articles": []}

def save_articles(data):
    with open(ARTICLES_JSON, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

def add_articles_if_missing(events):
    data = load_articles()
    existing_urls = {
        (article.get("url") or "").strip()
        for article in data["articles"]
        if (article.get("url") or "").strip()
    }

    added = 0

    for ev in events:
        url = (ev.get("url") or "").strip()
        if not url or url == "#":
            continue

        if url in existing_urls:
            continue

        data["articles"].append({
            "url": url,
            "title": ev.get("title", "").strip(),
            "date": ev.get("date", "").strip(),
            "hora": ev.get("hora", "").strip(),
            "tipo": ev.get("tipo", "").strip(),
            "date_detected": datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "processed": False,
            "audio_url": None,
            "audio_downloaded": False,
            "last_error": None
        })

        existing_urls.add(url)
        added += 1

    save_articles(data)
    print(f"Artículos nuevos añadidos al JSON: {added}")

# --- ICS escaping / folding ---
def ics_escape(s: str) -> str:
    s = s or ""
    return (s.replace("\\", "\\\\")
             .replace("\r\n", "\n")
             .replace("\r", "\n")
             .replace("\n", "\\n")
             .replace(",", "\\,")
             .replace(";", "\\;"))

def fold(line: str, limit: int = 75) -> str:
    if len(line) <= limit:
        return line
    out = []
    while len(line) > limit:
        out.append(line[:limit])
        line = " " + line[limit:]
    out.append(line)
    return "\r\n".join(out)

# --- rango: desde 2026-01 hasta (mes actual + 1) ---
now_local = datetime.datetime.now()
end_y, end_m = month_add(now_local.year, now_local.month, 1)

start_idx = month_index(START_YEAR, START_MONTH)
end_idx = month_index(end_y, end_m)

all_events = []
for idx in range(start_idx, end_idx + 1):
    y = idx // 12
    m = (idx % 12) + 1
    eventos = fetch_month(y, m)

    for date_str, evlist in eventos.items():
        for day_index, ev in enumerate(evlist):
            title = html.unescape(ev.get("titulo", "") or "").strip()
            url = (ev.get("url", "") or "").strip()
            tipo = html.unescape(ev.get("tipo", "") or "").strip()
            desc = html.unescape(ev.get("descripcion", "") or "").strip()
            hora = (ev.get("hora", "") or "").strip()

            all_events.append({
                "date": date_str,
                "title": title,
                "url": url,
                "tipo": tipo,
                "desc": desc,
                "hora": hora,
                "day_index": day_index,
            })

print(f"Eventos capturados: {len(all_events)}")

if len(all_events) == 0:
    print("[WARN] 0 eventos capturados. No se actualiza calendar.ics para evitar publicar un ICS vacío.")
    sys.exit(0)

# --- actualizar JSON de artículos ---
add_articles_if_missing(all_events)

dtstamp = datetime.datetime.now(datetime.UTC).strftime("%Y%m%dT%H%M%SZ")

lines = [
    "BEGIN:VCALENDAR",
    "VERSION:2.0",
    "PRODID:-//LDS Auto Feed//ES",
    "CALSCALE:GREGORIAN",
    "METHOD:PUBLISH",
]

for ev in sorted(all_events, key=lambda x: (x["date"], x["title"], x["hora"])):
    uid = stable_uid(ev["date"], ev["hora"], ev["url"], ev["day_index"])
    tr = parse_time_range(ev["hora"])

    lines.append("BEGIN:VEVENT")
    lines.append(fold(f"UID:{uid}"))
    lines.append(f"DTSTAMP:{dtstamp}")
    lines.append(fold("SUMMARY:" + ics_escape(ev["title"])))

    parts = []
    if ev["tipo"]:
        parts.append(f"Tipo: {ev['tipo']}")
    if ev["url"] and ev["url"] != "#":
        parts.append(f"URL: {ev['url']}")
    if ev["desc"]:
        parts.append(ev["desc"])
    if parts:
        lines.append(fold("DESCRIPTION:" + ics_escape("\n".join(parts))))

    y, m, d = map(int, ev["date"].split("-"))

    if tr:
        start, end = tr
        sh, sm = map(int, start.split(":"))
        eh, em = map(int, end.split(":"))

        dt_start_local = datetime.datetime(y, m, d, sh, sm, tzinfo=LOCAL_TZ)
        dt_end_local = datetime.datetime(y, m, d, eh, em, tzinfo=LOCAL_TZ)

        if dt_end_local <= dt_start_local:
            dt_end_local = dt_start_local + datetime.timedelta(minutes=30)

        dt_start_utc = dt_start_local.astimezone(datetime.UTC)
        dt_end_utc = dt_end_local.astimezone(datetime.UTC)

        lines.append(f"DTSTART:{dt_start_utc.strftime('%Y%m%dT%H%M%SZ')}")
        lines.append(f"DTEND:{dt_end_utc.strftime('%Y%m%dT%H%M%SZ')}")
    else:
        dtstart = datetime.date(y, m, d).strftime("%Y%m%d")
        dtend = (datetime.date(y, m, d) + datetime.timedelta(days=1)).strftime("%Y%m%d")
        lines.append(f"DTSTART;VALUE=DATE:{dtstart}")
        lines.append(f"DTEND;VALUE=DATE:{dtend}")

    lines.append("END:VEVENT")

lines.append("END:VCALENDAR")

with open("calendar.ics", "w", encoding="utf-8", newline="") as f:
    f.write("\r\n".join(lines) + "\r\n")

print("Archivo generado: calendar.ics")
print(f"Archivo actualizado: {ARTICLES_JSON}")
