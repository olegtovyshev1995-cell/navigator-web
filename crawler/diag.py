#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Разведка МСП.РФ с российского IP (чистый Python, совместим с 3.6, без установки пакетов).

Задачи:
  1. Подтвердить Python/страну/доступ.
  2. Обойти несколько каталожных страниц МСП.РФ (с ОТКЛЮЧЁННОЙ проверкой сертификата —
     у домена .рф/punycode баг проверки имени в Python 3.6).
  3. Для каждой: статус, размер, наличие __NEXT_DATA__, buildId, ссылки на /api/.
  4. Найти в __NEXT_DATA__ объекты, похожие на меры поддержки, и выписать примеры.
  5. Сохранить сырой __NEXT_DATA__ (до 1 МБ) в docs/data/_next.json для разбора.

Результат → <docs>/data/_diag.txt  (открыть: https://ВАШ-САЙТ/data/_diag.txt)
Запуск:   python3 diag.py
"""
import sys, ssl, json, re
from datetime import datetime
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

HERE = Path(__file__).resolve().parent
OUT_DIR = HERE.parent / "data"           # …/docs/data
OUT_DIR.mkdir(parents=True, exist_ok=True)
LOG = OUT_DIR / "_diag.txt"
NEXT_DUMP = OUT_DIR / "_next.json"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0 Safari/537.36")
MSP = "https://xn--l1agf.xn--p1ai"       # мсп.рф в punycode

# Кандидаты каталога мер (какой-то из них должен отдать список мер)
PAGES = [
    MSP + "/",
    MSP + "/services/",
    MSP + "/services/podderzhka/",
    MSP + "/services/support/",
    MSP + "/market/",
    MSP + "/podderzhka/",
]

# Ключи, по которым распознаём «меру» в произвольном JSON
TITLE_KEYS = ("title", "name", "measureName", "shortName", "header", "caption")
AMOUNT_KEYS = ("amount", "sum", "maxSum", "maxAmount", "supportAmount", "value", "size")
CAT_KEYS = ("category", "type", "measureType", "supportType", "kind", "form")
HINT = re.compile("поддержк|субсид|грант|займ|льгот|компенса|гарант|мера|кредит|лизинг", re.I)

lines = []
def log(m=""):
    print(m); lines.append(str(m))

# SSL без проверки имени/цепочки — для .рф домена в Python 3.6 (данные публичные)
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE

def fetch(url, timeout=25):
    req = Request(url, headers={"User-Agent": UA,
                                "Accept": "text/html,application/json,*/*",
                                "Accept-Language": "ru-RU,ru;q=0.9"})
    with urlopen(req, timeout=timeout, context=CTX) as r:
        return getattr(r, "status", r.getcode()), r.read()

def first_key(d, keys):
    for k in keys:
        v = d.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
        if isinstance(v, (int, float)):
            return str(v)
    return ""

def looks_like_measure(d):
    if not isinstance(d, dict):
        return False
    t = first_key(d, TITLE_KEYS)
    if not t or len(t) < 6:
        return False
    return bool(first_key(d, AMOUNT_KEYS) or first_key(d, CAT_KEYS) or HINT.search(t))

def scan_measures(node, out, seen):
    if isinstance(node, list):
        for x in node:
            if looks_like_measure(x):
                t = first_key(x, TITLE_KEYS)
                if t and t not in seen:
                    seen.add(t)
                    out.append((t, first_key(x, AMOUNT_KEYS), first_key(x, CAT_KEYS),
                                sorted(x.keys())[:12]))
            scan_measures(x, out, seen)
    elif isinstance(node, dict):
        for v in node.values():
            scan_measures(v, out, seen)

log("=" * 64)
log("РАЗВЕДКА МСП.РФ — " + datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
log("=" * 64)
log("Python: " + sys.version.split()[0] + "   " + sys.executable)

# страна
try:
    st, data = fetch("http://ip-api.com/json/?fields=query,country,hosting", 12)
    d = json.loads(data.decode("utf-8", "replace"))
    log("IP: %s | страна: %s | hosting=%s" % (d.get("query"), d.get("country"), d.get("hosting")))
except Exception as e:
    log("страна: не определена (%s)" % e)
log("")

best_next = None
for url in PAGES:
    log("-" * 64)
    log("Страница: " + url)
    try:
        st, data = fetch(url)
    except HTTPError as e:
        log("  HTTP %s (ошибка)" % e.code); continue
    except URLError as e:
        log("  URLError: %s" % e.reason); continue
    except Exception as e:
        log("  %s: %s" % (type(e).__name__, e)); continue
    txt = data.decode("utf-8", "replace")
    log("  HTTP %s, %d байт" % (st, len(data)))
    # __NEXT_DATA__
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', txt, re.S)
    if m:
        raw = m.group(1)
        log("  __NEXT_DATA__: ДА (%d символов)" % len(raw))
        bid = re.search(r'"buildId"\s*:\s*"([^"]+)"', raw)
        if bid: log("    buildId = " + bid.group(1))
        try:
            nd = json.loads(raw)
            out, seen = [], set()
            scan_measures(nd, out, seen)
            log("    похожих на меры объектов: %d" % len(out))
            for t, amt, cat, keys in out[:8]:
                log("      • %s | сумма=%s | кат=%s | поля=%s" % (t[:60], amt, cat, keys))
            if best_next is None and out:
                best_next = (url, raw)
        except Exception as e:
            log("    __NEXT_DATA__ не распарсился: %s" % e)
    else:
        log("  __NEXT_DATA__: нет")
    # /api/
    apis = sorted(set(re.findall(r'["\'](/api/[^"\'?\s]+)', txt)))[:12]
    if apis:
        log("  ссылки /api/:")
        for a in apis:
            log("    " + a)

log("")
if best_next:
    raw = best_next[1][:1_000_000]
    NEXT_DUMP.write_text(raw, encoding="utf-8")
    log("Сырой __NEXT_DATA__ (%s) сохранён → %s" % (best_next[0], NEXT_DUMP.name))
    log("Открой и пришли: https://ВАШ-САЙТ/data/_next.json")
else:
    log("Каталог мер в __NEXT_DATA__ не найден — пришли этот лог, подберём другие URL.")

LOG.write_text("\n".join(lines) + "\n", encoding="utf-8")
print("\n[diag] лог:", LOG)
