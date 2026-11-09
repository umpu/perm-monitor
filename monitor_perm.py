#!/usr/bin/env python3
"""
Мониторинг новостроек Пермского края на наш.дом.рф.

Файлы (рядом со скриптом):
  seen_ids.json     — технический список уже увиденных objId (для сравнения)
  found_objects.txt — человекочитаемый лог: дата обнаружения + метка + данные объекта

Уведомления в Telegram включаются переменными окружения TG_TOKEN и TG_CHAT_ID.
Только стандартная библиотека — внешние пакеты не нужны.
"""
import http.cookiejar, json, os, sys, time, uuid
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

# Портал на первом запросе отдаёт 307 с антибот-cookie spid и ждёт повтор с ней;
# общий CookieJar обязателен, иначе редирект зацикливается.
COOKIES = http.cookiejar.CookieJar()
OPENER = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(COOKIES))

TG_TOKEN   = os.environ.get("TG_TOKEN", "")
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
API_URL = "https://xn--80az8a.xn--d1aqf.xn--p1ai/%D1%81%D0%B5%D1%80%D0%B2%D0%B8%D1%81%D1%8B/api/kn/object"
REGION_CODE = 59
SEEN_FILE  = os.path.join(BASE_DIR, "seen_ids.json")
FOUND_FILE = os.path.join(BASE_DIR, "found_objects.txt")
PAGE_SIZE = 50
MAX_WORKERS = 10
QUICK_PAGES = 3   # быстрый режим: только первые 50–150 самых свежих записей

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://xn--80az8a.xn--d1aqf.xn--p1ai/",
    "Origin":  "https://xn--80az8a.xn--d1aqf.xn--p1ai",
}

def http_get(url, params=None, timeout=30):
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers=HEADERS)
    with OPENER.open(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))

def fetch_page(offset):
    params = {"offset": offset, "limit": PAGE_SIZE, "objStatus": 0,
              "sortField": "objPublDt", "sortType": "desc"}
    for attempt in range(3):
        try:
            d = http_get(API_URL, params).get("data", {})
            return d.get("list", []), d.get("total", 0)
        except Exception:
            if attempt == 2:
                raise
            time.sleep(2 * (attempt + 1))

SITE = "https://наш.дом.рф/новостройки/строящиеся/пермский-край/"

def quarter(d):
    """'2029-03-31' → 'I кв. 2029'."""
    try:
        y, m = d.split("-")[0], int(d.split("-")[1])
        return f"{('I', 'II', 'III', 'IV')[(m - 1) // 3]} кв. {y}"
    except Exception:
        return d or "?"

def group_key(o):
    """Корпуса одной новостройки: один застройщик, одно название, один адрес."""
    dev = (o.get("developer") or {}).get("devId")
    return (dev, (o.get("objCommercNm") or "").strip(), o.get("objAddr") or "")

def group_info(objs):
    o = objs[0]
    dev = (o.get("developer") or {}).get("shortName") or "?"
    name = (o.get("objCommercNm") or "").strip().strip('"') or "объект без названия"
    addr = o.get("objAddr") or "?"
    dates = sorted(d for d in {x.get("objReady100PercDt") or "" for x in objs} if d)
    when = quarter(dates[0]) if len(dates) < 2 else f"{quarter(dates[0])} — {quarter(dates[-1])}"
    ids = ", ".join(str(x["objId"]) for x in sorted(objs, key=lambda x: x["objId"]))
    n = len(objs)
    return name, addr, dev, when, ids, n

def group_line(objs):
    name, addr, dev, when, ids, n = group_info(objs)
    corpus = "корпус" if n == 1 else f"корпусов {n}"
    return f"«{name}» | {addr} | {dev} | ввод {when} | {corpus} | ID {ids}"

def card_text(objs):
    name, addr, dev, when, ids, n = group_info(objs)
    corpus = "корпус" if n == 1 else f"корпусов {n}"
    return (f"🏗 Новое в Пермском крае\n\n"
            f"«{name}»\n{addr}\n{dev}\n\n"
            f"Ввод: {when} ({corpus})\nID: {ids}\n\n{SITE}")

def send_telegram(text):
    if not TG_TOKEN or not TG_CHAT_ID:
        return  # уведомления не настроены — тихо пропускаем
    try:
        body = urllib.parse.urlencode({"chat_id": TG_CHAT_ID, "text": text[:4000],
                                       "disable_web_page_preview": "true"}).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", data=body)
        with urllib.request.urlopen(req, timeout=15):
            pass
    except Exception as e:
        print("Telegram error:", e)

def send_telegram_photo(photo, caption):
    """Карточка с фото здания. Ссылку на файл Telegram не принимает (сервер отдаёт
    картинку с чужим content-type), поэтому скачиваем сами и грузим multipart."""
    if not TG_TOKEN or not TG_CHAT_ID:
        return
    if not photo:
        send_telegram(caption)
        return
    try:
        with OPENER.open(urllib.request.Request(photo, headers=HEADERS), timeout=25) as r:
            img = r.read()
        if len(img) > 4_500_000:
            raise ValueError(f"фото {len(img)} байт — больше лимита sendPhoto")
        boundary = "----permmonitor" + uuid.uuid4().hex
        parts = []
        for name, value in (("chat_id", TG_CHAT_ID), ("caption", caption[:1000])):
            parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n".encode())
        parts.append((f"--{boundary}\r\nContent-Disposition: form-data; name=\"photo\"; "
                      f"filename=\"photo.jpg\"\r\nContent-Type: image/jpeg\r\n\r\n").encode()
                     + img + b"\r\n")
        parts.append(f"--{boundary}--\r\n".encode())
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendPhoto", data=b"".join(parts),
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        with urllib.request.urlopen(req, timeout=30) as r:
            resp = json.loads(r.read())
        if not resp.get("ok"):
            print("Telegram photo error:", resp)
    except Exception as e:
        print("Telegram photo error:", e)
        send_telegram(caption)

def scan(max_pages=None):
    first, total = fetch_page(0)
    if not first:
        return []
    pages = (total + PAGE_SIZE - 1) // PAGE_SIZE
    if max_pages:
        pages = min(pages, max_pages)
    all_objects = list(first)
    offsets = list(range(PAGE_SIZE, pages * PAGE_SIZE, PAGE_SIZE))
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        for page in ex.map(fetch_page, offsets):
            all_objects.extend(page[0])
    return all_objects

def load_seen():
    if not os.path.exists(SEEN_FILE):
        return set()
    try:
        return set(json.load(open(SEEN_FILE, encoding="utf-8")))
    except Exception:
        return set()

def main():
    quick = "--quick" in sys.argv
    try:
        all_objects = scan(QUICK_PAGES if quick else None)
    except Exception as e:
        print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] Ошибка API: {e}")
        sys.exit(1)
    if not all_objects:
        print("API вернул пустой список.")
        return

    seen = load_seen()
    perm = [o for o in all_objects if o.get("rpdRegionCd") == REGION_CODE]
    new_ids = {o["objId"] for o in perm} - seen

    # Новое появилось в свежих записях — тогда берём полный список,
    # чтобы занести в журнал всё и не потерять объект, вставший не по порядку дат.
    if quick and new_ids:
        print("Новое в свежих записях — полный проход по API")
        all_objects = scan()
        perm = [o for o in all_objects if o.get("rpdRegionCd") == REGION_CODE]
        new_ids = {o["objId"] for o in perm} - seen

    by_id = {o["objId"]: o for o in perm}
    current_ids = set(by_id)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    baseline = (len(seen) == 0)

    # Корпуса одной новостройки — одна запись: группируем по застройщику, названию и адресу.
    groups = {}
    for i in new_ids:
        o = by_id[i]
        groups.setdefault(group_key(o), []).append(o)
    records = sorted(groups.values(),
                     key=lambda g: max((o.get("objPublDt") or "") for o in g),
                     reverse=True)

    if baseline:
        with open(FOUND_FILE, "w", encoding="utf-8") as f:
            f.write(f"# Базовая линия от {now}: объектов {len(current_ids)}, новостроек {len(records)}\n")
            f.write("# Формат: дата обнаружения | метка | новостройка | адрес | застройщик | ввод | корпусов | ID\n")
            for g in records:
                f.write(f"{now} | базовая линия | {group_line(g)}\n")
        print(f"Базовая линия записана: {len(records)} новостроек ({len(current_ids)} объектов) → {FOUND_FILE}")
    else:
        if records:
            with open(FOUND_FILE, "a", encoding="utf-8") as f:
                for g in records:
                    f.write(f"{now} | НОВЫЙ | {group_line(g)}\n")
            print(f"[{now}] Новых новостроек: {len(records)} (объектов {len(new_ids)})")
            for g in records:
                print(f"  {group_line(g)}")
            for g in records[:10]:
                send_telegram_photo(g[0].get("hobjRenderPhotoUrl"), card_text(g))
        else:
            tail = "" if quick else f" Всего в Пермском крае: {len(perm)}"
            print(f"[{now}] Новых объектов нет.{tail}")

    seen |= current_ids
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(seen), f, ensure_ascii=False)

if __name__ == "__main__":
    main()
