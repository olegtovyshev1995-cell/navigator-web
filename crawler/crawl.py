# -*- coding: utf-8 -*-
"""
Краулер мер поддержки для проекта «Навигатор» (гибрид: МСП.РФ + сайты организаций).

ЧТО ДЕЛАЕТ (пайплайн):
  1. Загружает каталог регионов data/regions_catalog.json (page-код <-> название).
     Каталог — ЕДИНЫЙ источник списка регионов (МСП-first): по нему идёт обход.
  2. Загружает data/regions.json (843 org-ссылки) как ДОПОЛНИТЕЛЬНЫЙ источник и
     привязывает организации к page-коду региона (по имени). Файл необязателен.
  3. Для каждого региона собирает меры из двух источников:
       - МСП.РФ (основной, структурировано)          -> msp_measures_for_region()
       - сайты организаций из regions.json (доп.)     -> org_measures_for_region()
  4. Извлекает меры со страниц:
       - Claude (Haiku), если задан ANTHROPIC_API_KEY -> точные поля + классификация статуса;
       - иначе эвристика (заголовки/списки, статус = все).
  5. Собирает итог в схему САЙТА и пишет ../data/measures.json:
       { "<page-код>": { "self":[...], "ip":[...], "ooo":[...] } }
     где мера = {kind,cat,level,title,amount(число ₽),note,who,deadline,docs[],law,source}.
  6. Дедуп + валидация + отчёт (мер на регион, регионы без данных).

ГЕОБЛОК: МСП.РФ и госсайты режут зарубежные IP — запускать с РОССИЙСКОГО IP
(VPN off локально ИЛИ на RU-VPS). См. msp_discover.py для поиска API МСП.РФ.

ЗАПУСК:
    pip install -r requirements.txt
    python crawl.py --selftest                 # offline: собрать measures из фикстуры (проверка сборки)
    python crawl.py --sample 3 --source org    # 3 региона, только org-сайты
    python crawl.py --region "Москва"          # один регион (оба источника)
    python crawl.py                            # все регионы (долго!)
Опции: --no-llm (без Claude), --delay СЕК, --source {msp,org,both}, --out ПУТЬ.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter

try:
    from urllib3.util.retry import Retry
except ImportError:  # pragma: no cover
    from requests.packages.urllib3.util.retry import Retry

# ---------------------------------------------------------------------------
# Пути / константы
# ---------------------------------------------------------------------------
HERE = Path(__file__).resolve().parent
CATALOG_FILE = HERE / "data" / "regions_catalog.json"   # page-код <-> название (83 региона)
REGIONS_FILE = HERE / "data" / "regions.json"           # org-ссылки (843) для доп. источника
OUTPUT_FILE = HERE.parent / "data" / "measures.json"    # его читает сайт

MSP = "https://xn--l1agf.xn--p1ai"                       # мсп.рф в punycode
# Эндпоинт каталога мер МСП.РФ. Заполнить после msp_discover.py (RU-IP).
MSP_API = os.environ.get("MSP_API", "")                 # напр. https://.../api/support-measures
MSP_CATALOG_PAGES = [MSP + "/services/support/", MSP + "/services/"]

STATUSES = ("self", "ip", "ooo")                        # самозанятый / ИП / ООО

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s  %(levelname)-7s  %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("crawl")

DEFAULT_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"),
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
}

SUPPORT_KEYWORDS = ["поддержк", "субсид", "грант", "льгот", "компенса", "возмещ", "займ",
                    "микрозайм", "гарант", "поручительств", "субвенц", "преференц", "лизинг",
                    "налог", "финансир", "мера", "меры"]
KW_RE = re.compile("|".join(SUPPORT_KEYWORDS), re.IGNORECASE)

PRIORITY_CATEGORIES = ["Мой бизнес", "Министерство экономики", "Инвестиционный портал",
                       "Фонд микрофинансирования", "Гарантийный фонд"]

# Классификация статуса и вида по ключевым словам (эвристика/подстраховка LLM)
KIND_SAVE_RE = re.compile("налог|льгот|ставк|вычет|каникул|освобожд|взнос", re.IGNORECASE)
STATUS_HINTS = {
    "self": re.compile("самозанят|нпд|налог на профессиональн", re.IGNORECASE),
    "ip":   re.compile(r"\bип\b|индивидуальн\w* предпринимател|псн|усн|патент", re.IGNORECASE),
    "ooo":  re.compile(r"\bооо\b|юридическ\w* лиц|организац|компан|налог на прибыль", re.IGNORECASE),
}


# ---------------------------------------------------------------------------
# Модель меры (поля соответствуют схеме сайта measures.json)
# ---------------------------------------------------------------------------
@dataclass
class Measure:
    title: str
    kind: str = "get"                 # get | save
    cat: str = ""                     # Субсидия | Грант | Микрозайм | Налоговая льгота | ...
    level: str = "Региональная"       # Федеральная | Региональная | Муниципальная
    amount: int = 0                   # рубли (число); 0 если суммы нет
    note: str = ""
    who: str = ""
    deadline: str = ""
    docs: list = field(default_factory=list)
    law: str = ""
    source: str = ""                  # URL первоисточника
    statuses: list = field(default_factory=lambda: list(STATUSES))  # к каким статусам применима

    def site_dict(self) -> dict:
        """Представление меры в схеме сайта (без служебного statuses)."""
        return {"kind": self.kind, "cat": self.cat, "level": self.level, "title": self.title,
                "amount": int(self.amount or 0), "note": self.note, "who": self.who,
                "deadline": self.deadline, "docs": list(self.docs or []),
                "law": self.law, "source": self.source}


# ---------------------------------------------------------------------------
# Каталог регионов: название -> page-код
# ---------------------------------------------------------------------------
def load_catalog() -> list:
    return json.loads(CATALOG_FILE.read_text(encoding="utf-8"))


def _norm_region(name: str) -> str:
    """Нормализация ИМЕНИ РЕГИОНА для матчинга (срезает тип: область/край/республика…)."""
    s = (name or "").lower()
    s = re.sub(r"[ёе]", "е", s)
    s = re.sub(r"\b(область|обл\.?|край|республика|респ\.?|автономн\w*|округ|город|г\.)\b", " ", s)
    s = re.sub(r"[^a-zа-я0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _norm_title(title: str) -> str:
    """Нормализация ЗАГОЛОВКА МЕРЫ для дедупа (без среза значимых слов)."""
    s = (title or "").lower()
    s = re.sub(r"[ёе]", "е", s)
    s = re.sub(r"[^a-zа-я0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def build_region_index(catalog: list) -> dict:
    idx = {}
    for c in catalog:
        idx[_norm_region(c["name"])] = c["page"]
    return idx


# Алиасы: в каталоге сайта эти регионы записаны сокращённо (ХМАО, ЯНАО, Сев. Осетия),
# а в regions.json — полными именами. Фрагмент (нормализованный) -> page-код.
REGION_ALIASES = {
    "северная осетия": "SE",
    "ханты мансийск": "KHM",
    "югра": "KHM",
    "ямало ненецк": "YAN",
}


def resolve_page(region_name: str, idx: dict) -> str | None:
    n = _norm_region(region_name)
    if n in idx:
        return idx[n]
    for frag, page in REGION_ALIASES.items():
        if frag and frag in n:
            return page
    for key, page in idx.items():          # частичное совпадение по первому слову
        if n and (n in key or key in n):
            return page
    return None


def build_org_index(regions: list, idx: dict) -> dict:
    """regions.json -> {page-код: region_obj}. Регионы без page-кода (Крым и др.) пропускаются."""
    org = {}
    for r in regions:
        page = resolve_page(r.get("region") or "", idx)
        if page:
            org.setdefault(page, r)   # первый матч выигрывает
    return org


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
def build_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(DEFAULT_HEADERS)
    retry = Retry(total=2, backoff_factor=0.6, status_forcelist=(429, 500, 502, 503, 504),
                  allowed_methods=frozenset(["GET", "HEAD"]), raise_on_status=False)
    ad = HTTPAdapter(max_retries=retry)
    s.mount("http://", ad); s.mount("https://", ad)
    return s


_robots: dict = {}


def robots_allowed(session, url: str) -> bool:
    p = urlparse(url); base = f"{p.scheme}://{p.netloc}"
    if base not in _robots:
        try:
            r = session.get(base + "/robots.txt", timeout=10)
            if r.status_code >= 400:
                _robots[base] = None
            else:
                rp = RobotFileParser(); rp.parse(r.text.splitlines()); _robots[base] = rp
        except requests.RequestException:
            _robots[base] = None
    rp = _robots[base]
    return True if rp is None else rp.can_fetch(session.headers.get("User-Agent", "*"), url)


def fetch(session, url: str, timeout: int = 15):
    if not robots_allowed(session, url):
        log.info("    robots.txt запрещает: %s", url); return None
    try:
        r = session.get(url, timeout=timeout)
        r.raise_for_status()
        if r.encoding is None or r.encoding.lower() == "iso-8859-1":
            r.encoding = r.apparent_encoding or "utf-8"
        if "html" not in r.headers.get("Content-Type", "") and "<html" not in r.text[:500].lower():
            return None
        return BeautifulSoup(r.text, "lxml")
    except requests.RequestException as e:
        log.info("    не открылось (%s): %s", e.__class__.__name__, url); return None


def clean_text(soup: BeautifulSoup) -> str:
    for tag in soup(["script", "style", "nav", "header", "footer", "noscript"]):
        tag.decompose()
    lines = [l for l in soup.get_text("\n", strip=True).splitlines() if l.strip()]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Извлечение мер: Claude (site-схема + статусы) или эвристика
# ---------------------------------------------------------------------------
def _amount_to_int(val) -> int:
    if isinstance(val, (int, float)):
        return int(val)
    digits = re.sub(r"[^\d]", "", str(val or ""))
    return int(digits) if digits else 0


def _clean_statuses(vals) -> list:
    out = [v for v in (vals or []) if v in STATUSES]
    return out or list(STATUSES)


def extract_with_llm(page_text: str, url: str) -> list:
    """Точное извлечение мер в схеме сайта + классификация статуса (Claude Haiku)."""
    import anthropic
    client = anthropic.Anthropic()
    prompt = (
        "Ниже текст страницы о мерах господдержки бизнеса. Извлеки КОНКРЕТНЫЕ меры "
        "(субсидии, гранты, займы, льготы, компенсации, гарантии). Ничего не выдумывай. "
        "Для каждой меры верни поля:\n"
        "- title: название меры\n"
        "- kind: 'get' если это деньги (субсидия/грант/займ/компенсация) или 'save' если налоговая льгота/ставка\n"
        "- cat: категория (Субсидия, Грант, Микрозайм, Налоговая льгота, Льготный кредит, Гарантия, Компенсация)\n"
        "- level: Федеральная / Региональная / Муниципальная\n"
        "- amount: максимальная сумма в рублях ЧИСЛОМ (0 если не указана)\n"
        "- who: кому подходит (кратко)\n"
        "- deadline: срок приёма заявок (если есть)\n"
        "- law: нормативный акт/программа (если есть)\n"
        "- statuses: массив из значений среди ['self','ip','ooo'] — кому применима "
        "(self=самозанятый, ip=ИП, ooo=ООО); если применима всем — верни все три.\n\n"
        f"ИСТОЧНИК: {url}\nТЕКСТ:\n{page_text[:12000]}"
    )
    tool = {"name": "save_measures", "description": "Сохранить меры",
            "input_schema": {"type": "object", "properties": {"measures": {"type": "array", "items": {
                "type": "object", "properties": {
                    "title": {"type": "string"}, "kind": {"type": "string"},
                    "cat": {"type": "string"}, "level": {"type": "string"},
                    "amount": {"type": "number"}, "who": {"type": "string"},
                    "deadline": {"type": "string"}, "law": {"type": "string"},
                    "statuses": {"type": "array", "items": {"type": "string"}}},
                "required": ["title"]}}}, "required": ["measures"]}}
    resp = client.messages.create(model="claude-haiku-4-5-20251001", max_tokens=3000,
                                  tools=[tool], tool_choice={"type": "tool", "name": "save_measures"},
                                  messages=[{"role": "user", "content": prompt}])
    out = []
    for block in resp.content:
        if block.type == "tool_use":
            for m in block.input.get("measures", []):
                title = (m.get("title") or "").strip()
                if not title:
                    continue
                kind = m.get("kind") if m.get("kind") in ("get", "save") else \
                    ("save" if KIND_SAVE_RE.search(title) else "get")
                out.append(Measure(
                    title=title, kind=kind, cat=(m.get("cat") or "").strip(),
                    level=(m.get("level") or "Региональная").strip(),
                    amount=_amount_to_int(m.get("amount")), who=(m.get("who") or "").strip(),
                    deadline=(m.get("deadline") or "").strip(), law=(m.get("law") or "").strip(),
                    source=url, statuses=_clean_statuses(m.get("statuses"))))
    return out


def extract_heuristic(soup: BeautifulSoup, url: str) -> list:
    """Фолбэк без API: заголовки/пункты со словами-маркерами. Статус — по подсказкам."""
    out, seen = [], set()
    for tag in soup.find_all(["h2", "h3", "h4", "li"]):
        text = tag.get_text(" ", strip=True)
        if not text or len(text) < 12 or len(text) > 200 or not KW_RE.search(text):
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        st = [s for s, rx in STATUS_HINTS.items() if rx.search(text)]
        out.append(Measure(title=text, kind=("save" if KIND_SAVE_RE.search(text) else "get"),
                           source=url, statuses=st or list(STATUSES)))
        if len(out) >= 25:
            break
    return out


def extract_from_soup(soup, url, use_llm) -> list:
    if use_llm:
        try:
            return extract_with_llm(clean_text(soup), url)
        except Exception as e:  # noqa: BLE001
            log.warning("    LLM ошибка (%s) -> эвристика: %s", e.__class__.__name__, url)
    return extract_heuristic(soup, url)


# ---------------------------------------------------------------------------
# Источник A: МСП.РФ
# ---------------------------------------------------------------------------
# Поля, по которым распознаём «меру» внутри произвольного JSON (__NEXT_DATA__/API).
_TITLE_KEYS = ("title", "name", "measureName", "shortName", "header")
_AMOUNT_KEYS = ("amount", "sum", "maxSum", "maxAmount", "supportAmount", "value")
_CAT_KEYS = ("category", "type", "measureType", "supportType", "kind")
_LEVEL_KEYS = ("level", "budgetLevel", "scope")
_MEASURE_HINT_RE = re.compile("поддержк|субсид|грант|займ|льгот|компенса|гарант|мера", re.IGNORECASE)


def _first_key(d: dict, keys) -> str:
    for k in keys:
        v = d.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
        if isinstance(v, (int, float)):
            return str(v)
    return ""


def _looks_like_measure(d: dict) -> bool:
    if not isinstance(d, dict):
        return False
    title = _first_key(d, _TITLE_KEYS)
    if not title or len(title) < 6:
        return False
    # признак «меры»: есть сумма/категория ИЛИ маркер в заголовке
    return bool(_first_key(d, _AMOUNT_KEYS) or _first_key(d, _CAT_KEYS)
                or _MEASURE_HINT_RE.search(title))


def parse_next_data(data, url: str, region_name: str = "") -> list:
    """
    Рекурсивно ищет в JSON (__NEXT_DATA__ или ответе API) массивы объектов-мер и
    достаёт из них поля. Схема МСП.РФ неизвестна заранее (портал под геоблоком),
    поэтому распознаём меру эвристически по набору ключей. Точную схему подтвердит
    msp_discover.py с российского IP — тогда сюда можно добавить прямой маппинг.
    """
    out: list = []
    seen: set = set()
    rn = _norm_region(region_name) if region_name else ""

    def walk(node):
        if isinstance(node, list):
            # массив, где большинство элементов похожи на меры — берём его
            measures = [x for x in node if _looks_like_measure(x)]
            if len(measures) >= 2:
                for m in measures:
                    title = _first_key(m, _TITLE_KEYS)
                    key = _norm_title(title)
                    if not key or key in seen:
                        continue
                    seen.add(key)
                    amount = _amount_to_int(_first_key(m, _AMOUNT_KEYS))
                    out.append(Measure(
                        title=title,
                        kind=("save" if KIND_SAVE_RE.search(title) else "get"),
                        cat=_first_key(m, _CAT_KEYS),
                        level=(_first_key(m, _LEVEL_KEYS) or "Федеральная"),
                        amount=amount, source=url, statuses=list(STATUSES)))
            for x in node:
                walk(x)
        elif isinstance(node, dict):
            for v in node.values():
                walk(v)

    walk(data)
    # если знаем регион — оставляем меры, где он упомянут (когда такое поле есть);
    # но т.к. точная привязка неизвестна, фильтр мягкий: не режем, если совпадений нет.
    if rn:
        pass
    return out


def msp_measures_for_region(session, region_name: str, use_llm: bool, delay: float) -> list:
    """
    Меры с МСП.РФ по региону. Приоритет:
      1) MSP_API (JSON), если задан после msp_discover;
      2) __NEXT_DATA__ каталожных страниц (Next.js кладёт данные туда);
      3) HTML-извлечение (LLM/эвристика).
    Требует RU-IP.
    """
    measures: list = []

    # 1) Прямой API (заполнить MSP_API после msp_discover)
    if MSP_API:
        try:
            r = session.get(MSP_API, params={"region": region_name}, timeout=20)
            if "json" in r.headers.get("Content-Type", ""):
                measures += parse_next_data(r.json(), MSP_API, region_name)
                log.info("    МСП.РФ API: %d мер", len(measures))
                if measures:
                    return measures
        except Exception as e:  # noqa: BLE001
            log.warning("    МСП.РФ API ошибка: %s", e)

    # 2) + 3) Каталожные страницы: сначала __NEXT_DATA__, потом HTML
    for page in MSP_CATALOG_PAGES:
        soup = fetch(session, page)
        if soup is None:
            continue
        nd = soup.find("script", id="__NEXT_DATA__")
        if nd and nd.string:
            try:
                measures += parse_next_data(json.loads(nd.string), page, region_name)
            except json.JSONDecodeError:
                pass
        if not measures:
            measures += extract_from_soup(soup, page, use_llm)
        time.sleep(delay)
    return measures


# ---------------------------------------------------------------------------
# Источник B: сайты организаций из regions.json
# ---------------------------------------------------------------------------
def discover_measure_pages(session, home_url, max_pages=4) -> list:
    soup = fetch(session, home_url)
    if soup is None:
        return []
    host = urlparse(home_url).netloc
    scored, seen = [], set()
    for a in soup.find_all("a", href=True):
        href = urljoin(home_url, a["href"].split("#")[0])
        if href in seen or urlparse(href).netloc != host:
            continue
        seen.add(href)
        anchor = a.get_text(" ", strip=True)
        score = len(KW_RE.findall(anchor)) * 2 + len(KW_RE.findall(href))
        if score:
            scored.append((score, href))
    scored.sort(reverse=True)
    return [home_url] + [u for _, u in scored[:max_pages]]


def org_measures_for_region(session, region_obj, use_llm, delay) -> list:
    entries = region_obj.get("entries", [])

    def prio(e):
        try:
            return PRIORITY_CATEGORIES.index(e.get("category", ""))
        except ValueError:
            return len(PRIORITY_CATEGORIES)

    measures = []
    for entry in sorted(entries, key=prio)[:3]:      # топ-3 источника на регион
        home = entry["url"].split("?")[0]
        for page_url in discover_measure_pages(session, home):
            soup = fetch(session, page_url)
            if soup is None:
                continue
            for m in extract_from_soup(soup, page_url, use_llm):
                m.source = m.source or urlparse(home).netloc
                measures.append(m)
            time.sleep(delay)
    return measures


# ---------------------------------------------------------------------------
# Сборка measures.json (схема сайта) + дедуп + валидация
# ---------------------------------------------------------------------------
def assemble(raw_by_page: dict) -> dict:
    """{page:[Measure]} -> {page:{self:[dict],ip:[dict],ooo:[dict]}} с дедупом по title."""
    out = {}
    for page, measures in raw_by_page.items():
        buckets = {s: [] for s in STATUSES}
        seen = {s: set() for s in STATUSES}
        for m in measures:
            title = (m.title or "").strip()
            if not title:
                continue
            key = _norm_title(title)          # ключ дедупа — нормализованный заголовок меры
            for st in _clean_statuses(m.statuses):
                if key in seen[st]:
                    continue
                seen[st].add(key)
                buckets[st].append(m.site_dict())
        if any(buckets[s] for s in STATUSES):
            out[page] = buckets
    return out


def validate(measures_json: dict, catalog: list) -> list:
    report = []
    total = 0
    for page, st in measures_json.items():
        n = sum(len(st[s]) for s in STATUSES)
        total += n
        no_amount = sum(1 for s in STATUSES for m in st[s] if not m.get("amount"))
        if n == 0:
            report.append(f"  [пусто] {page}")
        elif no_amount:
            report.append(f"  [{page}] мер: {n}, без суммы: {no_amount}")
    covered = set(measures_json.keys())
    missing = [c["page"] for c in catalog if c["page"] not in covered]
    report.append(f"ИТОГО: регионов с данными {len(covered)}/{len(catalog)}, мер всего {total}")
    if missing:
        report.append(f"Без данных ({len(missing)}): {', '.join(missing[:40])}"
                      + (" …" if len(missing) > 40 else ""))
    return report


# ---------------------------------------------------------------------------
# selftest: собрать measures.json из фикстуры (без сети) — проверка сборки
# ---------------------------------------------------------------------------
def selftest_fixture() -> dict:
    return {"MSK": [
        Measure(title="Социальный контракт на своё дело", kind="get", cat="Субсидия",
                level="Федеральная", amount=350000, deadline="до 30.09.2026",
                law="ПП РФ № 2394", source="https://mos.ru/", statuses=["self", "ip"]),
        Measure(title="Налоговые каникулы 0% (УСН/ПСН)", kind="save", cat="Налоговая льгота",
                level="Региональная", amount=90000, source="https://mos.ru/", statuses=["ip"]),
        Measure(title="Социальный контракт на своё дело", kind="get", cat="Субсидия",  # дубль
                level="Федеральная", amount=350000, source="https://x/", statuses=["self"]),
    ]}


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main(argv=None):
    ap = argparse.ArgumentParser(description="Краулер мер поддержки (гибрид)")
    ap.add_argument("--sample", type=int, help="первые N регионов")
    ap.add_argument("--region", help="один регион по имени (подстрока)")
    ap.add_argument("--delay", type=float, default=1.0)
    ap.add_argument("--no-llm", action="store_true", help="только эвристика")
    ap.add_argument("--source", choices=["msp", "org", "both"], default="both")
    ap.add_argument("--selftest", action="store_true", help="offline: собрать из фикстуры")
    ap.add_argument("--out", default=str(OUTPUT_FILE))
    args = ap.parse_args(argv)

    catalog = load_catalog()
    ridx = build_region_index(catalog)
    out_path = Path(args.out)

    if args.selftest:
        raw = selftest_fixture()
        mj = assemble(raw)
        sel = Path(HERE / "data" / "_selftest_measures.json")
        sel.write_text(json.dumps(mj, ensure_ascii=False, indent=2), encoding="utf-8")
        log.info("SELFTEST -> %s", sel)
        for line in validate(mj, catalog):
            log.info(line)
        # проверим ключевые свойства
        assert "MSK" in mj and len(mj["MSK"]["self"]) == 1 and len(mj["MSK"]["ip"]) == 2, "assemble broken"
        log.info("SELFTEST OK: дедуп и раскладка по статусам работают")
        return 0

    # Список регионов — из КАТАЛОГА (МСП-first, полное покрытие).
    work = list(catalog)
    if args.region:
        q = args.region.lower()
        work = [c for c in work if q in (c.get("name") or "").lower()
                or q in (c.get("abbr") or "").lower()]
    if args.sample:
        work = work[: args.sample]

    # org-источник (необязателен): page-код -> region_obj из regions.json.
    org_index = {}
    if args.source in ("org", "both") and REGIONS_FILE.exists():
        try:
            regions = json.loads(REGIONS_FILE.read_text(encoding="utf-8"))
            org_index = build_org_index(regions, ridx)
        except Exception as e:  # noqa: BLE001
            log.warning("regions.json не прочитан (%s) — org-источник отключён", e)

    use_llm = (not args.no_llm) and bool(os.environ.get("ANTHROPIC_API_KEY"))
    log.info("Регионов: %d | источник: %s | извлечение: %s | org-сайтов: %d",
             len(work), args.source, "Claude" if use_llm else "эвристика", len(org_index))

    session = build_session()
    raw_by_page: dict = {}
    for c in work:
        page = c["page"]
        rname = c.get("name") or page
        log.info("Регион: %s -> %s", rname, page)
        measures = []
        try:
            if args.source in ("msp", "both"):
                measures += msp_measures_for_region(session, rname, use_llm, args.delay)
            if args.source in ("org", "both") and page in org_index:
                measures += org_measures_for_region(session, org_index[page], use_llm, args.delay)
        except Exception as e:  # noqa: BLE001
            log.error("  регион упал: %s", e)
        raw_by_page.setdefault(page, []).extend(measures)
        log.info("  собрано (сырое): %d", len(measures))

    mj = assemble(raw_by_page)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(mj, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("Готово -> %s", out_path)
    for line in validate(mj, catalog):
        log.info(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
