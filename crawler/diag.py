#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Разведка МСП.РФ, фаза 3: качаем страницу карты региональной поддержки
(/services/reg-support-map/) и смежные, ищем, откуда берутся меры по регионам.
Сайт на 1С-Битрикс (server-rendered + AJAX). Чистый Python, совместим с 3.6.

Результат → docs/data/_diag.txt  (+ сохранённые страницы _regmap.html, _support.html)
Запуск:   python3 diag.py
"""
import sys, ssl, re
from datetime import datetime
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

HERE = Path(__file__).resolve().parent
OUT = HERE.parent / "data"
OUT.mkdir(parents=True, exist_ok=True)
LOG = OUT / "_diag.txt"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0 Safari/537.36")
MSP = "https://xn--l1agf.xn--p1ai"

CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE

# (URL, имя файла для сохранения)
TARGETS = [
    (MSP + "/services/reg-support-map/", "_regmap.html"),
    (MSP + "/services/support/", "_support.html"),
    (MSP + "/services/microloan/promo/", "_microloan.html"),
]

lines = []
def log(m=""):
    print(m); lines.append(str(m))

def fetch(url, timeout=30):
    req = Request(url, headers={"User-Agent": UA,
                                "Accept": "text/html,application/json,*/*",
                                "Accept-Language": "ru-RU,ru;q=0.9",
                                "Referer": MSP + "/"})
    with urlopen(req, timeout=timeout, context=CTX) as r:
        ct = r.headers.get("Content-Type", "")
        return getattr(r, "status", r.getcode()), ct, r.read()

log("=" * 64)
log("РАЗВЕДКА МСП.РФ (фаза 3, карта регионов) — " + datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
log("=" * 64)
log("Python: " + sys.version.split()[0])
log("")

MK = ("субсид", "грант", "займ", "микрозайм", "льгот", "компенса", "поручительств",
      "мера", "меры", "регион", "рублей", "₽")

for url, fname in TARGETS:
    log("-" * 64)
    log("Страница: " + url)
    try:
        st, ct, data = fetch(url)
    except HTTPError as e:
        log("  HTTP %s (ошибка)" % e.code); continue
    except URLError as e:
        log("  URLError: %s" % e.reason); continue
    except Exception as e:
        log("  %s: %s" % (type(e).__name__, e)); continue
    txt = data.decode("utf-8", "replace")
    (OUT / fname).write_text(txt[:3_000_000], encoding="utf-8")
    log("  HTTP %s, %s, %d байт → сохранено %s (до 3 МБ)" % (st, ct[:24], len(data), fname))

    # частоты маркеров мер
    freq = ["%s=%d" % (m, txt.lower().count(m)) for m in MK if txt.lower().count(m)]
    log("  маркеры: " + ", ".join(freq))

    # AJAX / Битрикс механизмы
    ajax = set()
    ajax |= set(re.findall(r'(/bitrix/services/main/ajax\.php[^"\'\s]*)', txt))
    ajax |= set(re.findall(r'["\'](/ajax/[a-z0-9/_.-]+)', txt, re.I))
    ajax |= set(re.findall(r'(bitrix/services/[a-z0-9/_.]+ajax\.php)', txt))
    for k, v in re.findall(r'[?&](action|componentName|mode)=([a-z0-9_.:%-]+)', txt, re.I):
        ajax.add(k + "=" + v)
    if ajax:
        log("  AJAX-подсказки:")
        for a in sorted(ajax)[:15]:
            log("    " + a[:120])

    # data-* с region
    dregion = sorted(set(re.findall(r'(data-[a-z-]*region[a-z-]*="[^"]{0,40}")', txt, re.I)))
    if dregion:
        log("  data-region атрибуты:")
        for d in dregion[:10]:
            log("    " + d)

    # ссылки внутри страницы, ведущие в этот же раздел (детали мер)
    sub = sorted(set(re.findall(r'href="(/services/reg-support-map/[a-z0-9/_-]+)"', txt)))
    if sub:
        log("  вложенные ссылки:")
        for s in sub[:15]:
            log("    " + s)

    # инлайн JSON-объекты, где встречается 'регион' или 'мер'
    if re.search(r'\{[^{}]{0,200}(регион|мера|субсид)[^{}]{0,200}\}', txt):
        log("  ! в HTML есть инлайновые объекты со словами регион/мера — данные, вероятно, в разметке")

log("")
log("Готово. Пришли файл _regmap.html (https://ВАШ-САЙТ/data/_regmap.html) и скрин лога.")
LOG.write_text("\n".join(lines) + "\n", encoding="utf-8")
print("[diag] лог:", LOG)
