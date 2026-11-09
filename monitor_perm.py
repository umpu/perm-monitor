#!/usr/bin/env python3
"""
Мониторинг новостроек Пермского края на наш.дом.рф.

Файлы (рядом со скриптом):
  seen_ids.json     — технический список уже увиденных objId (для сравнения)
  seen_groups.json  — известные новостройки (ключи групп: застройщик + название + адрес)
  found_objects.txt — человекочитаемый лог: дата обнаружения + метка + данные объекта
  tg_state.json     — позиция чтения обновлений Telegram (offset)

Уведомления в Telegram включаются переменными окружения TG_TOKEN и TG_CHAT_ID.
Бот приватный: карточки уходят только в TG_CHAT_ID, незнакомым — ответ «бот приватный».
На /start владелец получает приветствие с примером карточки.
Только стандартная библиотека — внешние пакеты не нужны.
"""
import html, http.cookiejar, json, os, re, sys, time
import urllib.error
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
SEEN_FILE   = os.path.join(BASE_DIR, "seen_ids.json")
GROUPS_FILE = os.path.join(BASE_DIR, "seen_groups.json")   # известные новостройки (ключи групп)
FOUND_FILE = os.path.join(BASE_DIR, "found_objects.txt")
TG_STATE_FILE = os.path.join(BASE_DIR, "tg_state.json")   # offset чтения обновлений Telegram
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

def object_url(obj_id):
    """Карточка объекта в каталоге (проверено в браузере 2026-11-10)."""
    return f"https://наш.дом.рф/сервисы/каталог-новостроек/объект/{obj_id}"

def quarter(d):
    """'2029-03-31' → 'I кв. 2029'."""
    try:
        y, m = d.split("-")[0], int(d.split("-")[1])
        return f"{('I', 'II', 'III', 'IV')[(m - 1) // 3]} кв. {y}"
    except Exception:
        return d or "?"

def norm_name(s):
    """«Новый Парковый. Этап 2» и «… Этап 3» — один проект: срезаем номер этапа/очереди/корпуса."""
    s = (s or "").strip().strip('"').lower()
    s = re.sub(r"\s*[.,—-]?\s*(этап|очередь|корпус|литер|секция|дом)\s*№?\s*[0-9ivx]+.*$", "", s)
    return s.strip(" .—-")

def clean_name(raw):
    """Название без внешних кавычек; парные ASCII-кавычки внутри — «…», одиночная убирается."""
    name = (raw or "").strip()
    while len(name) >= 2 and name[0] in "\"'«»„“" and name[-1] in "\"'«»„“":
        name = name[1:-1].strip()
    out, opened = [], False
    for ch in name:
        if ch == '"':
            out.append("»" if opened else "«")
            opened = not opened
        else:
            out.append(ch)
    if opened:  # непарная кавычка в данных — не оставляем висящую
        for i in range(len(out) - 1, -1, -1):
            if out[i] == "«":
                del out[i]
                break
    return "".join(out).strip()

def group_key(o):
    """Новостройка = проект застройщика: название без номера этапа/корпуса,
    иначе — адрес. Так новый этап уже известного проекта не считается новостью."""
    dev = (o.get("developer") or {}).get("devId")
    name = norm_name(o.get("objCommercNm"))
    if name:
        return ("n", dev, name)
    return ("a", dev, o.get("objAddr") or "")

def group_info(objs):
    o = objs[0]
    dev = (o.get("developer") or {}).get("shortName") or "?"
    name = clean_name(o.get("objCommercNm")) or "объект без названия"
    addr = o.get("objAddr") or "?"
    dates = sorted(d for d in {x.get("objReady100PercDt") or "" for x in objs} if d)
    when = quarter(dates[0]) if len(dates) < 2 else f"{quarter(dates[0])} — {quarter(dates[-1])}"
    ids = ", ".join(str(x["objId"]) for x in sorted(objs, key=lambda x: x["objId"]))
    n = len(objs)
    newest = max(x["objId"] for x in objs)
    return name, addr, dev, when, ids, n, newest

def group_line(objs):
    name, addr, dev, when, ids, n, newest = group_info(objs)
    corpus = "корпус" if n == 1 else f"корпусов {n}"
    return f"«{name}» | {addr} | {dev} | {corpus} | ID {ids} | {object_url(newest)}"

def esc(s):
    return html.escape(str(s), quote=False)

def card_text(objs):
    """Карточка для Telegram (parse_mode=HTML): название ЖК жирным, текст экранирован."""
    name, addr, dev, when, ids, n, newest = group_info(objs)
    lines = [f"<b>{esc(name)}</b>", esc(addr), esc(dev)]
    if n > 1:
        lines += [f"корпусов {n}", f"ID: {ids}"]
    lines += ["", esc(object_url(newest))]
    return "\n".join(lines)

def tg_api(method, params=None, timeout=15):
    """Вызов Telegram Bot API; None — если токен не задан."""
    if not TG_TOKEN:
        return None
    data = urllib.parse.urlencode(params or {}).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{TG_TOKEN}/{method}", data=data)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read())
        except Exception:
            return {"ok": False, "description": str(e)}
    except Exception as e:
        return {"ok": False, "description": str(e)}

def send_telegram(text, chat_id=None):
    """Сообщение в Telegram. Фото больше не шлём: parse_mode=HTML, название ЖК жирным."""
    chat_id = chat_id or TG_CHAT_ID
    if not TG_TOKEN or not chat_id:
        return  # уведомления не настроены — тихо пропускаем
    resp = tg_api("sendMessage", {"chat_id": chat_id, "text": text[:4000],
                                  "parse_mode": "HTML",
                                  "disable_web_page_preview": "true"})
    if resp and resp.get("ok"):
        print("Telegram: message_id", resp.get("result", {}).get("message_id"))
    else:
        print("Telegram error:", (resp or {}).get("description"))

def load_tg_state():
    try:
        with open(TG_STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def save_tg_state(st):
    try:
        with open(TG_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(st, f)
    except Exception as e:
        print("tg_state не сохранён:", e)

def latest_object():
    """Последняя новостройка в списке: максимальный objId среди свежих записей."""
    try:
        first, _ = fetch_page(0)
        perm = [o for o in first if o.get("rpdRegionCd") == REGION_CODE]
        return max(perm, key=lambda x: x["objId"]) if perm else None
    except Exception as e:
        print("Пример для приветствия недоступен:", e)
        return None

def greeting_text():
    text = ("Привет! Это приватный бот мониторинга новостроек Пермского края.\n"
            "Когда на наш.дом.рф появляется новая новостройка, я присылаю карточку.")
    o = latest_object()
    if o:
        text += ("\n\nНиже — пример карточки. Сейчас это последняя новостройка в списке:\n\n"
                 + card_text([o]))
    return text

def handle_updates():
    """Приём обновлений: /start — приветствие владельцу, всем остальным — «бот приватный»."""
    if not TG_TOKEN:
        return
    st = load_tg_state()
    resp = tg_api("getUpdates", {"offset": st.get("offset", 0), "timeout": 0,
                                 "allowed_updates": '["message"]'})
    if not resp:
        return
    if not resp.get("ok"):
        print("Telegram getUpdates:", resp.get("description"))
        return
    told = set()
    for upd in resp.get("result", []):
        st["offset"] = max(st.get("offset", 0), upd["update_id"] + 1)
        msg = upd.get("message") or {}
        chat_id = str((msg.get("chat") or {}).get("id") or "")
        text = (msg.get("text") or "").strip()
        if not chat_id:
            continue
        if TG_CHAT_ID and chat_id != TG_CHAT_ID:
            if chat_id not in told:
                told.add(chat_id)
                send_telegram("Этот бот приватный.", chat_id=chat_id)
            continue
        cmd = text.split()[0].split("@")[0] if text else ""
        if cmd == "/start":
            send_telegram(greeting_text(), chat_id=chat_id)
    save_tg_state(st)

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

def load_seen_groups():
    """None — файла ещё нет (первый прогон): всё считается известным, ничего не шлём."""
    if not os.path.exists(GROUPS_FILE):
        return None
    try:
        return {tuple(k) for k in json.load(open(GROUPS_FILE, encoding="utf-8"))}
    except Exception:
        return None

def save_seen_groups(keys):
    with open(GROUPS_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(k for k in keys), f, ensure_ascii=False)

def main():
    if "--test-send" in sys.argv:
        perm = [o for o in scan() if o.get("rpdRegionCd") == REGION_CODE]
        o = max(perm, key=lambda x: x["objId"])
        print(f"тест: последняя новостройка ID {o['objId']}: {o.get('objAddr')}")
        send_telegram("Тест из CI — так будет выглядеть карточка:\n\n" + card_text([o]))
        print("тест отправлен")
        return

    if "--test-greeting" in sys.argv:
        send_telegram(greeting_text())
        print("приветствие отправлено")
        return

    handle_updates()

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

    # Новый корпус в уже известной новостройке — не новость: молча добавляем в seen.
    known = load_seen_groups()
    first_groups = known is None
    if first_groups:
        known = set()
    records = sorted((g for k, g in groups.items() if k not in known),
                     key=lambda g: max((o.get("objPublDt") or "") for o in g),
                     reverse=True)
    quiet = len(groups) - len(records)

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
                send_telegram(card_text(g))
        else:
            tail = "" if quick else f" Всего в Пермском крае: {len(perm)}"
            print(f"[{now}] Новых объектов нет.{tail}")
        if quiet:
            print(f"[{now}] Новых корпусов в известных новостройках: {quiet} — не показываю")

    seen |= current_ids
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(seen), f, ensure_ascii=False)
    known |= {group_key(o) for o in perm}
    save_seen_groups(known)

if __name__ == "__main__":
    main()
