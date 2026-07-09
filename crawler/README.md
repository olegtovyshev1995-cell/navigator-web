# Краулер мер поддержки — «Навигатор»

Собирает реальные меры господдержки бизнеса по регионам РФ и пишет
`../data/measures.json` в схеме, которую читает сайт.

## Важно: геоблокировка

МСП.РФ (`xn--l1agf.xn--p1ai`) и региональные госсайты **режут зарубежные IP**.
Краулер обязан выходить с **российского IP**:

- локально — **выключить VPN** и запускать `python crawl.py …`;
- либо на **российском VPS** (cron/Docker) для полной автоматики.

GitHub Actions (US/EU) до данных не дотянется — автоматизацию делать только на RU-VPS.

## Установка

```bash
pip install -r requirements.txt
```

## Запуск

```bash
python crawl.py --selftest              # offline-проверка сборки (без сети)
python crawl.py --region "Москва"       # один регион (оба источника)
python crawl.py --sample 3 --source org # 3 региона, только org-сайты
python crawl.py                         # все регионы (долго!)
```

Опции: `--no-llm` (только эвристика, без Claude), `--delay СЕК`,
`--source {msp,org,both}`, `--out ПУТЬ`.

Если задан `ANTHROPIC_API_KEY` — извлечение через Claude Haiku (точные поля +
классификация статуса self/ip/ooo). Иначе — эвристика по заголовкам/спискам.

## Источники данных

1. **МСП.РФ** (основной, `msp_measures_for_region`) — единый агрегатор.
   Порядок: прямой API (`MSP_API`) → `__NEXT_DATA__` каталожных страниц → HTML.
2. **Сайты организаций** (доп., `org_measures_for_region`) — из `data/regions.json`
   (843 ссылки: «Мой бизнес», инвестпорталы, минэкономики, фонды и т.д.).

Список регионов берётся из `data/regions_catalog.json` (page-код ↔ название,
83 региона) — он же задаёт покрытие. `regions.json` привязывается к нему по имени.

## Найти API МСП.РФ (нужен RU-IP)

```bash
python msp_discover.py     # проверит страну, разберёт каталог, найдёт /api/, __NEXT_DATA__
```

Отчёт → `data/_discovery.txt`, образцы JSON → `data/_next_*.json` / `data/_api_*.json`.
Найденный эндпоинт положить в переменную окружения `MSP_API` — краулер начнёт брать
меры прямо из JSON. При необходимости уточнить маппинг полей в `parse_next_data()`.

## Данные

| Файл | Что это |
|------|---------|
| `data/regions_catalog.json` | 83 региона: page-код, geo, округ, столица |
| `data/regions.json` | 86 регионов, 843 org-ссылки `[{okrug, region, entries:[{category, org, url}]}]` |
| `data/_selftest_measures.json` | пример выхода `--selftest` |
| `../data/measures.json` | **результат** — его читает сайт |

Схема меры в `measures.json`:
`{kind: get|save, cat, level, title, amount(₽ числом), note, who, deadline, docs[], law, source}`,
разложена по статусам: `{ "<page>": { "self":[…], "ip":[…], "ooo":[…] } }`.
