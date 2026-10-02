"""
Выгружает JS-события MSB_prod из Яндекс Метрики через API.
Делает два запроса:
  1. все события MSB_prod за период
  2. те же события, но с фильтром «переходы из умного поиска» (utm_source~ai_)
Сохраняет в cache_events.json. При падении — продолжает с места.

Запуск:  python3 collect_events.py
"""
import subprocess, json, os, time

# =========== НАСТРОЙКИ ============
TOKEN   = "y0__wgBEKaF9L0IGPCoSCCf1KzpGDDmpum6CHE9vmg-bcHFLjRukMLqUqzrcGsy"
COUNTER = "18882175"
DATE1   = "2026-09-01"
DATE2   = "2026-09-30"
FILTER_PRODUCT  = "ym:ep:eventParamsLevel1=~'MSB_prod'"
FILTER_SMART    = "ym:ep:eventParamsLevel1=~'MSB_prod' AND ym:s:UTMSource=~'ai_'"
CACHE = "cache_events.json"
# ==================================

def ym_stat(filters, limit=500, offset=1):
    params = {
        "ids": COUNTER, "date1": DATE1, "date2": DATE2,
        "preset": "goal_params",
        "sort": "-ym:ep:eventsNumber",
        "limit": str(limit), "offset": str(offset),
        "accuracy": "full",
        "filters": filters,
    }
    cmd = ["curl","-s","-G","https://api-metrika.yandex.net/stat/v1/data",
           "-H",f"Authorization: OAuth {TOKEN}","--max-time","30"]
    for k, v in params.items():
        cmd += ["--data-urlencode", f"{k}={v}"]
    r = subprocess.run(cmd, capture_output=True, text=True)
    return json.loads(r.stdout) if r.stdout else {}

def fetch_all(label, filters):
    print(f"[{label}] filters={filters}")
    rows, offset = [], 1
    while True:
        d = ym_stat(filters, limit=500, offset=offset)
        if "errors" in d:
            print(f"  ERR: {d['errors']}")
            break
        page = d.get("data", [])
        if not page: break
        rows.extend(page)
        total = d.get("total_rows", 0)
        print(f"  offset={offset:>4} загружено {len(page)}, всего {total}")
        if len(rows) >= total: break
        offset += 500
        time.sleep(0.3)
    return rows

cache = json.load(open(CACHE)) if os.path.exists(CACHE) else {}

if "rows_all" not in cache:
    cache["rows_all"] = fetch_all("ALL", FILTER_PRODUCT)
    json.dump(cache, open(CACHE, "w"), ensure_ascii=False)

if "rows_ss" not in cache:
    cache["rows_ss"] = fetch_all("FROM_AI_SEARCH", FILTER_SMART)
    json.dump(cache, open(CACHE, "w"), ensure_ascii=False)

cache["meta"] = {"date1": DATE1, "date2": DATE2, "counter": COUNTER,
                 "filter_product": FILTER_PRODUCT, "filter_smart": FILTER_SMART}
json.dump(cache, open(CACHE, "w"), ensure_ascii=False)

print(f"\nГотово. {CACHE}")
print(f"  события всего:         {sum(r['metrics'][0] for r in cache['rows_all']):.0f}")
print(f"  события из ИИ-поиска:  {sum(r['metrics'][0] for r in cache['rows_ss']):.0f}")
