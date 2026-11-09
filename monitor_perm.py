#!/usr/bin/env python3
"""
Мониторинг новостроек Пермского края на наш.дом.рф.

Файлы (рядом со скриптом):
  seen_ids.json     — технический список уже увиденных objId (для сравнения)
  found_objects.txt — человекочитаемый лог: дата обнаружения + метка + данные объекта

Уведомления в Telegram включаются переменными окружения TG_TOKEN и TG_CHAT_ID.
Только стандартная библиотека — внешние пакеты не нужны.
"""
import http.cookiejar, json, os, sys, time
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

def fmt_obj(o):
    dev  = (o.get("developer") or {}).get("shortName") or "?"
    name = o.get("commerNm") or ""
    addr = o.get("objAddr") or "?"
    if name:
        addr = f"{name}, {addr}"
    return (f"ID {o['objId']} | опубликован {o.get('objPublDt','?')} | "
            f"ввод {o.get('objReady100PercDt','?')} | {dev} | {addr}")

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

def main():
    try:
        first, total = fetch_page(0)
    except Exception as e:
        print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] Ошибка API: {e}")
        sys.exit(1)
    if not first:
        print("API вернул пустой список.")
        return

    all_objects = list(first)
    offsets = list(range(PAGE_SIZE, total, PAGE_SIZE))
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        for page in ex.map(fetch_page, offsets):
            all_objects.extend(page[0])

    perm = [o for o in all_objects if o.get("rpdRegionCd") == REGION_CODE]
    by_id = {o["objId"]: o for o in perm}
    current_ids = set(by_id)

    seen = set()
    if os.path.exists(SEEN_FILE):
        try:
            seen = set(json.load(open(SEEN_FILE, encoding="utf-8")))
        except Exception:
            seen = set()

    new_ids = current_ids - seen
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    baseline = (len(seen) == 0)

    records = sorted(
        (by_id[i] for i in new_ids),
        key=lambda o: (o.get("objPublDt") or "", o["objId"]),
        reverse=True,
    )

    if baseline:
        with open(FOUND_FILE, "w", encoding="utf-8") as f:
            f.write(f"# Найдено объектов в Пермском крае — базовая линия от {now}\n")
            f.write(f"# Всего: {len(records)}\n")
            f.write("# Формат: дата обнаружения | метка | объект\n")
            for o in records:
                f.write(f"{now} | базовая линия | {fmt_obj(o)}\n")
        print(f"Базовая линия записана: {len(records)} объектов → {FOUND_FILE}")
    else:
        if records:
            with open(FOUND_FILE, "a", encoding="utf-8") as f:
                for o in records:
                    f.write(f"{now} | НОВЫЙ | {fmt_obj(o)}\n")
            print(f"[{now}] Найдено новых объектов: {len(records)}")
            for o in records:
                print(f"  {fmt_obj(o)}")
            send_telegram("Новые новостройки (Пермский край):\n\n"
                          + "\n\n".join(fmt_obj(o) for o in records[:20]))
        else:
            print(f"[{now}] Новых объектов нет. Всего в Пермском крае: {len(perm)}")

    seen |= current_ids
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(seen), f, ensure_ascii=False)

if __name__ == "__main__":
    main()
