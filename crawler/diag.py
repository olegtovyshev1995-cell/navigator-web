#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Диагностика хостинга для краулера «Навигатор» (чистый Python, без установки пакетов).

Проверяет, готова ли площадка к запуску краулера:
  1. версия Python и платформа;
  2. доступ в интернет к МСП.РФ (главный вопрос — геоблок/егресс);
  3. страна выхода (если доступен ip-api).

Результат пишется в файл-лог рядом с сайтом, чтобы открыть его в браузере:
    <docs>/data/_diag.txt      (и https://ВАШ-САЙТ/data/_diag.txt)

Запуск (из планировщика или командной строки):
    python3 diag.py
"""
import sys, os, platform, ssl, json
from datetime import datetime
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

HERE = Path(__file__).resolve().parent
# лог кладём в docs/data/ (…/crawler лежит в …/docs, значит ../data)
OUT_DIR = HERE.parent / "data"
OUT_DIR.mkdir(parents=True, exist_ok=True)
LOG = OUT_DIR / "_diag.txt"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0 Safari/537.36")
MSP = "https://xn--l1agf.xn--p1ai/"   # мсп.рф

lines = []
def log(msg=""):
    print(msg)
    lines.append(str(msg))

def fetch(url, timeout=20):
    req = Request(url, headers={"User-Agent": UA, "Accept-Language": "ru-RU,ru;q=0.9"})
    ctx = ssl.create_default_context()
    with urlopen(req, timeout=timeout, context=ctx) as r:
        data = r.read()
        return r.status, data

log("=" * 60)
log("ДИАГНОСТИКА КРАУЛЕРА — " + datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
log("=" * 60)
log("Python:   " + sys.version.split()[0] + "  (" + sys.executable + ")")
log("Платформа: " + platform.platform())
log("Рабочая папка скрипта: " + str(HERE))
log("Лог пишется в: " + str(LOG))
log("")

# 1) Страна выхода (не критично — просто справка)
log("[1] Страна выхода:")
try:
    st, data = fetch("http://ip-api.com/json/?fields=query,country,proxy,hosting", timeout=12)
    d = json.loads(data.decode("utf-8", "replace"))
    log("    IP: %s | страна: %s | hosting=%s" % (d.get("query"), d.get("country"), d.get("hosting")))
    if d.get("country") and d.get("country") != "Russia":
        log("    !!! Выходим НЕ из России — МСП.РФ, скорее всего, будет закрыт.")
except Exception as e:
    log("    не удалось определить (%s: %s) — не страшно, это лишь справка" % (type(e).__name__, e))
log("")

# 2) ГЛАВНОЕ: доступ к МСП.РФ
log("[2] Доступ к МСП.РФ (%s):" % MSP)
ok = False
try:
    st, data = fetch(MSP, timeout=25)
    ok = True
    log("    HTTP %s, получено %d байт" % (st, len(data)))
    txt = data.decode("utf-8", "replace")
    has_next = "__NEXT_DATA__" in txt
    log("    __NEXT_DATA__ на странице: %s" % ("ДА — краулеру есть что парсить" if has_next else "нет"))
    log("    первые 160 символов: " + " ".join(txt[:160].split()))
except HTTPError as e:
    log("    HTTPError %s — сервер ответил, но кодом ошибки" % e.code)
except URLError as e:
    log("    URLError: %s  → соединение НЕ установилось (геоблок/егресс/файрвол)" % e.reason)
except Exception as e:
    log("    Ошибка %s: %s" % (type(e).__name__, e))
log("")

# Вердикт
log("[ИТОГ]")
if ok:
    log("    ✅ Интернет и МСП.РФ ДОСТУПНЫ — можно запускать полный краулер.")
else:
    log("    ❌ МСП.РФ недоступен с этого сервера.")
    log("       Возможные причины: сервер вне РФ, закрыт исходящий трафик,")
    log("       или файрвол хостинга. Нужен запуск с российского IP с открытым егрессом.")
log("")
log("Готово. Открой этот файл в браузере: https://ВАШ-САЙТ/data/_diag.txt")

LOG.write_text("\n".join(lines) + "\n", encoding="utf-8")
print("\n[diag] лог сохранён:", LOG)
