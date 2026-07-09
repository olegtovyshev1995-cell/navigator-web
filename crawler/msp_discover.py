# -*- coding: utf-8 -*-
"""
Разведчик API мер поддержки МСП.РФ.

ЗАПУСКАТЬ ТОЛЬКО С РОССИЙСКОГО IP (выключи VPN) — портал режет зарубежные адреса.

Что делает:
  1. Проверяет, из какой страны мы выходим.
  2. Тянет каталог мер поддержки МСП.РФ.
  3. Ищет реальные API-эндпоинты: блок __NEXT_DATA__, ссылки /api/,
     вызовы fetch/axios в JS, встроенный JSON.
  4. Пробует набор вероятных эндпоинтов и записывает, что ответило.
  5. Пишет отчёт в data/_discovery.txt и найденный JSON в data/_sample_*.json.

Запуск:
    python msp_discover.py
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import requests
from bs4 import BeautifulSoup

try:
    import lxml  # noqa: F401
    BS_PARSER = "lxml"
except ImportError:
    BS_PARSER = "html.parser"

HERE = Path(__file__).resolve().parent
OUT = HERE / "data"
OUT.mkdir(exist_ok=True)

H = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9",
    "Accept": "text/html,application/json,*/*",
}

MSP = "https://xn--l1agf.xn--p1ai"  # мсп.рф в punycode

# Страницы, где заведомо есть каталог мер
PAGES = [
    MSP + "/",
    MSP + "/services/support/",
    MSP + "/services/",
    MSP + "/market/",
]

# Вероятные API-эндпоинты (проверим существование)
CANDIDATE_APIS = [
    MSP + "/api/support-measures/",
    MSP + "/api/v1/support-measures",
    MSP + "/api/services/support/",
    MSP + "/_next/data/",
    "https://api." + "xn--l1agf.xn--p1ai" + "/support-measures",
]

report: list[str] = []


def log(msg: str):
    print(msg)
    report.append(msg)


def check_country():
    try:
        r = requests.get("http://ip-api.com/json/?fields=query,country,proxy,hosting",
                         timeout=10)
        d = r.json()
        log(f"IP: {d.get('query')} | страна: {d.get('country')} | "
            f"proxy={d.get('proxy')} hosting={d.get('hosting')}")
        if d.get("country") != "Russia":
            log("!!! ВНИМАНИЕ: выходим НЕ из России — портал, скорее всего, будет закрыт.")
            log("!!! Выключи VPN и запусти снова.")
        return d.get("country")
    except Exception as e:  # noqa: BLE001
        log(f"Не удалось определить страну: {e}")
        return None


def scan_page(url: str):
    log("\n" + "=" * 70)
    log(f"Страница: {url}")
    try:
        r = requests.get(url, headers=H, timeout=20)
        log(f"  статус {r.status_code}, размер {len(r.text)} симв, тип {r.headers.get('Content-Type')}")
    except Exception as e:  # noqa: BLE001
        log(f"  НЕ ОТКРЫЛОСЬ: {e.__class__.__name__} — {e}")
        return

    if r.status_code >= 400:
        return

    soup = BeautifulSoup(r.text, BS_PARSER)

    # 1) __NEXT_DATA__ (Next.js кладёт сюда данные страницы)
    nd = soup.find("script", id="__NEXT_DATA__")
    if nd and nd.string:
        try:
            data = json.loads(nd.string)
            fname = OUT / ("_next_" + re.sub(r"\W+", "_", url)[-40:] + ".json")
            fname.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            log(f"  НАЙДЕН __NEXT_DATA__ -> сохранено в {fname.name} ({len(nd.string)} симв)")
            # ищем apiUrl / buildId внутри
            for key in ("buildId", "apiUrl", "assetPrefix"):
                if key in nd.string:
                    m = re.search(rf'"{key}"\s*:\s*"([^"]+)"', nd.string)
                    if m:
                        log(f"    {key} = {m.group(1)}")
        except json.JSONDecodeError:
            log("  __NEXT_DATA__ есть, но не распарсился как JSON")

    # 2) ссылки и вызовы на /api/
    api_hits = set(re.findall(r'["\'](/api/[^"\'?\s]+)', r.text))
    api_hits |= set(re.findall(r'https?://[a-z0-9.\-]*api[a-z0-9.\-]*/[^"\'?\s]+', r.text, re.I))
    if api_hits:
        log(f"  Найдены ссылки на API ({len(api_hits)}):")
        for h in sorted(api_hits)[:20]:
            log(f"    {h}")

    # 3) слова-маркеры каталога
    for marker in ("support-measures", "supportMeasures", "меры поддержки", "measures"):
        if marker.lower() in r.text.lower():
            log(f"  маркер каталога встречается: '{marker}'")


def try_apis():
    log("\n" + "=" * 70)
    log("Пробуем кандидатов-эндпоинтов:")
    for url in CANDIDATE_APIS:
        try:
            r = requests.get(url, headers=H, timeout=15)
            ct = r.headers.get("Content-Type", "")
            note = ""
            if "json" in ct:
                note = " <-- JSON!"
                fname = OUT / ("_api_" + re.sub(r"\W+", "_", url)[-30:] + ".json")
                fname.write_text(r.text[:200000], encoding="utf-8")
                note += f" сохранено в {fname.name}"
            log(f"  {r.status_code}  {ct[:30]:30}  {url}{note}")
        except Exception as e:  # noqa: BLE001
            log(f"  FAIL  {e.__class__.__name__:20}  {url}")


if __name__ == "__main__":
    country = check_country()
    for p in PAGES:
        scan_page(p)
    try_apis()

    (OUT / "_discovery.txt").write_text("\n".join(report), encoding="utf-8")
    log("\nОтчёт сохранён в data/_discovery.txt")
