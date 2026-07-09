#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Разведка МСП.РФ, фаза 2: сайт оказался SPA (данные грузятся через API).
Ищем адреса API в HTML и в JS-бандлах. Чистый Python, совместим с 3.6.

Что делает:
  1. Качает главную страницу МСП.РФ (unverified SSL — баг .рф в Python 3.6).
  2. Сохраняет её в docs/data/_page.html (до 3 МБ) для ручного разбора.
  3. Находит <script src=...>, инлайновые JSON-теги, и адреса вида /api, /v1,
     graphql, *.json, абсолютные http(s) на api-хосты.
  4. Качает главные JS-бандлы и грепает в них базовый URL API / эндпоинты.
  5. Пробует несколько вероятных API-эндпоинтов, пишет их ответ.

Результат → docs/data/_diag.txt   (+ _page.html)
Запуск:   python3 diag.py
"""
import sys, ssl, json, re
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

HERE = Path(__file__).resolve().parent
OUT = HERE.parent / "data"
OUT.mkdir(parents=True, exist_ok=True)
LOG = OUT / "_diag.txt"
PAGE_DUMP = OUT / "_page.html"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0 Safari/537.36")
MSP = "https://xn--l1agf.xn--p1ai"

CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE

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
log("РАЗВЕДКА МСП.РФ (фаза 2, поиск API) — " + datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
log("=" * 64)
log("Python: " + sys.version.split()[0] + "  " + sys.executable)
log("")

# --- главная страница ---
home = MSP + "/"
log("Качаю главную: " + home)
try:
    st, ct, data = fetch(home)
except Exception as e:
    log("  ОШИБКА: %s: %s" % (type(e).__name__, e))
    LOG.write_text("\n".join(lines) + "\n", encoding="utf-8")
    sys.exit(0)
txt = data.decode("utf-8", "replace")
log("  HTTP %s, %s, %d байт" % (st, ct, len(data)))
PAGE_DUMP.write_text(txt[:3_000_000], encoding="utf-8")
log("  сохранено → _page.html (первые 3 МБ)")
log("")

# --- маркеры фреймворка / инлайн-состояние ---
log("[Маркеры состояния в HTML]")
for marker in ("__NEXT_DATA__", "__NUXT__", "__INITIAL_STATE__", "window.__",
               "application/json", "apiUrl", "apiHost", "API_URL", "baseURL",
               "graphql", "supportMeasures", "меры поддержки"):
    c = txt.count(marker)
    if c:
        log("  '%s': %d" % (marker, c))
log("")

# --- инлайновые json-теги ---
json_tags = re.findall(r'<script[^>]*type="application/json"[^>]*>', txt)
if json_tags:
    log("[<script type=application/json> теги: %d] примеры атрибутов:" % len(json_tags))
    for t in json_tags[:8]:
        log("  " + t[:120])
    log("")

# --- js-бандлы ---
srcs = re.findall(r'<script[^>]+src="([^"]+)"', txt)
srcs = [urljoin(home, s) for s in srcs]
log("[<script src> найдено: %d]" % len(srcs))
for s in srcs[:20]:
    log("  " + s)
log("")

# --- абсолютные и относительные адреса-кандидаты API в HTML ---
def find_apis(s):
    hits = set()
    hits |= set(re.findall(r'["\'](/(?:api|v1|v2|graphql)[^"\'?\s]*)', s))
    hits |= set(re.findall(r'https?://[a-z0-9.\-]*(?:api|back|gw|gateway)[a-z0-9.\-]*/[^"\'?\s]*', s, re.I))
    return hits

api_hits = find_apis(txt)
if api_hits:
    log("[Кандидаты API в HTML: %d]" % len(api_hits))
    for a in sorted(api_hits)[:25]:
        log("  " + a)
    log("")

# --- грепаем главные JS-бандлы на предмет базового URL API ---
def looks_main(u):
    b = u.lower()
    return any(k in b for k in ("main", "app", "index", "chunk", "runtime", "vendor", "_next", "bundle"))

bundles = [s for s in srcs if s.endswith(".js")]
bundles.sort(key=lambda u: (0 if looks_main(u) else 1))
log("[Грепаю JS-бандлы на api/endpoint]")
scanned = 0
for b in bundles:
    if scanned >= 4:
        break
    try:
        st, ct, bd = fetch(b, timeout=30)
    except Exception as e:
        log("  %s — не скачался (%s)" % (b.split('/')[-1], type(e).__name__)); continue
    js = bd.decode("utf-8", "replace")
    scanned += 1
    found = set()
    found |= set(re.findall(r'https?://[a-z0-9.\-]+/[a-z0-9/_\-]*(?:api|measures|support)[a-z0-9/_\-]*', js, re.I))
    found |= set(re.findall(r'["\'](/(?:api|v1|v2|graphql)[a-z0-9/_\-]*)', js, re.I))
    found |= set(re.findall(r'(?:baseURL|apiUrl|API_URL|apiHost|endpoint)\s*[:=]\s*["\']([^"\']+)', js))
    log("  %s (%d КБ): %d совпадений" % (b.split('/')[-1][:40], len(js) // 1024, len(found)))
    for f in sorted(found)[:15]:
        log("      " + f[:100])
log("")

# --- пробуем вероятные API-эндпоинты ---
CANDS = [
    MSP + "/api/measures",
    MSP + "/api/v1/measures",
    MSP + "/api/support-measures",
    MSP + "/api/catalog/measures",
    MSP + "/gateway/api/measures",
]
log("[Пробую вероятные API-эндпоинты]")
for u in CANDS:
    try:
        st, ct, bd = fetch(u, timeout=20)
        head = bd[:200].decode("utf-8", "replace").replace("\n", " ")
        mark = "  <-- JSON!" if "json" in ct.lower() else ""
        log("  %s  %s  %s%s" % (st, ct[:24], u, mark))
        if "json" in ct.lower():
            log("      " + head)
    except HTTPError as e:
        log("  %s (HTTP)  %s" % (e.code, u))
    except Exception as e:
        log("  FAIL %s  %s" % (type(e).__name__, u))

log("")
log("Готово. Пришли мне файл _page.html (https://ВАШ-САЙТ/data/_page.html)")
log("и скриншот этого лога — по ним построю парсер.")
LOG.write_text("\n".join(lines) + "\n", encoding="utf-8")
print("\n[diag] лог:", LOG)
