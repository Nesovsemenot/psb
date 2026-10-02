"""
Строит отчёт из cache_events.json + маппинга URL→цель из mapping.xlsx.
Добавляет цель 609847539 «Переходы на ib.psbank» отдельной строкой.
"""
import json, subprocess, os, openpyxl
from collections import defaultdict
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment
from openpyxl.utils import get_column_letter

# =========== НАСТРОЙКИ ===========
TOKEN   = "y0__wgBEKaF9L0IGPCoSCCf1KzpGDDmpum6CHE9vmg-bcHFLjRukMLqUqzrcGsy"
COUNTER = "18882175"
DATE1, DATE2 = "2026-09-01", "2026-09-30"
FILTER_SMART = "ym:s:UTMSource=~'ai_'"
EXTRA_GOAL_ID = 609847539
EXTRA_GOAL_NAME = "Переходы на ib.psbank // Оформить заявку (с переходом на ib.psbank)"
MAPPING_FILE = "mapping.xlsx"
CACHE  = "cache_events.json"
OUTPUT = "psb_events_with_ai.xlsx"
# =================================

# ========= 1. Читаем маппинг URL → название цели =========
def load_mapping(path):
    wb = openpyxl.load_workbook(path, data_only=True)
    m = {}
    for sn in wb.sheetnames:
        ws = wb[sn]
        rows = list(ws.iter_rows(values_only=True))
        if not rows: continue
        header = [str(c or "").strip().lower() for c in rows[0]]
        try:
            i_url  = next(i for i,h in enumerate(header) if "url" in h and "страниц" in h)
            i_name = next(i for i,h in enumerate(header) if "название цели" in h)
        except StopIteration:
            continue  # лист без нужных колонок
        for r in rows[1:]:
            if not r or len(r) <= max(i_url, i_name): continue
            url  = r[i_url]
            name = r[i_name]
            if not url or not name: continue
            url  = str(url).strip().split("?")[0].rstrip("/")
            name = str(name).strip()
            if url.startswith("http"):
                m[url] = name
    return m

mapping = load_mapping(MAPPING_FILE)
print(f"Маппинг URL→цель: {len(mapping)} записей")

# ========= 2. Загружаем кэш событий =========
c = json.load(open(CACHE))
rows_all = c["rows_all"]
rows_ss  = c["rows_ss"]
meta     = c["meta"]

def row_key(r):
    dims = r["dimensions"]
    return (dims[2].get("name"), dims[3].get("name"))  # L2, L3

ss_by_key = {row_key(r): r for r in rows_ss}

# Дедупликация: одна строка на (L2, L3), берём максимум
dedup = defaultdict(lambda: {"events_all":0, "users_all":0, "events_ss":0, "users_ss":0})
for r in rows_all:
    k = row_key(r)
    g = dedup[k]
    g["events_all"] = max(g["events_all"], int(r["metrics"][0]))
    g["users_all"]  = max(g["users_all"],  int(r["metrics"][1]))
    ss = ss_by_key.get(k)
    if ss:
        g["events_ss"] = max(g["events_ss"], int(ss["metrics"][0]))
        g["users_ss"]  = max(g["users_ss"],  int(ss["metrics"][1]))

# Собираем финальный список строк
def l3_to_url(l3):
    if not l3: return ""
    s = str(l3).strip()
    if s.startswith("http"): return s.split("?")[0].rstrip("/")
    return f"https://www.psbank.ru/{s.lstrip('/')}".rstrip("/")

import re
output = []
skipped_no_mapping = 0
skipped_not_url    = 0
for (l2, l3), info in dedup.items():
    # Отфильтровываем "L3", которые не являются URL-путями:
    #   кириллица, пробелы, слова без слэшей — это названия кнопок/блоков,
    #   а не адрес страницы. По ним нет события отправки формы.
    s = str(l3 or "").strip()
    if not s or re.search(r'[а-яА-Я ]', s) or '/' not in s:
        skipped_not_url += 1
        continue
    url = l3_to_url(l3)
    goal_name = mapping.get(url, "")
    if not goal_name:
        skipped_no_mapping += 1
        continue
    output.append({
        "url":   url,
        "goal":  goal_name,
        "events_all": info["events_all"],
        "users_all":  info["users_all"],
        "events_ss":  info["events_ss"],
        "users_ss":   info["users_ss"],
    })

print(f"Строк MSB_prod с найденной целью: {len(output)}")
print(f"  пропущено (не URL — клики по кнопкам): {skipped_not_url}")
print(f"  пропущено (URL есть, но нет в маппинге): {skipped_no_mapping}")

# ========= 3. Запрос по цели 609847539 (ib.psbank) =========
def ym(filters=None, dimensions=None, metrics=None, limit=1000):
    params = {"ids":COUNTER, "date1":DATE1, "date2":DATE2,
              "metrics":metrics, "limit":str(limit), "accuracy":"full"}
    if dimensions: params["dimensions"] = dimensions
    if filters:    params["filters"] = filters
    cmd = ["curl","-s","-G","https://api-metrika.yandex.net/stat/v1/data",
           "-H",f"Authorization: OAuth {TOKEN}","--max-time","30"]
    for k,v in params.items():
        cmd += ["--data-urlencode", f"{k}={v}"]
    r = subprocess.run(cmd, capture_output=True, text=True)
    return json.loads(r.stdout) if r.stdout else {}

print("\nЗапрос по цели 609847539 (ib.psbank, разбивка по endURL — страница ухода)...")
# endURL = последняя страница psbank.ru в визите, с которой пользователь ушёл на ib.psbank
m_r = f"ym:s:goal{EXTRA_GOAL_ID}reaches,ym:s:goal{EXTRA_GOAL_ID}users"
d_all = ym(dimensions="ym:s:endURL", metrics=m_r, limit=1000)
d_ss  = ym(dimensions="ym:s:endURL", metrics=m_r, filters=FILTER_SMART, limit=1000)

def strip_q(u):
    if not u: return ""
    return str(u).split("?")[0].rstrip("/")

ib_all = defaultdict(lambda: {"ev":0, "us":0})
ib_ss  = defaultdict(lambda: {"ev":0, "us":0})
for r in d_all.get("data", []):
    u = strip_q(r["dimensions"][0].get("name"))
    ev, us = int(r["metrics"][0]), int(r["metrics"][1])
    if ev > 0:
        ib_all[u]["ev"] += ev
        ib_all[u]["us"] += us
for r in d_ss.get("data", []):
    u = strip_q(r["dimensions"][0].get("name"))
    ev, us = int(r["metrics"][0]), int(r["metrics"][1])
    if ev > 0:
        ib_ss[u]["ev"] += ev
        ib_ss[u]["us"] += us

for u, info in ib_all.items():
    ss = ib_ss.get(u, {"ev":0, "us":0})
    output.append({
        "url":   u,
        "goal":  EXTRA_GOAL_NAME,
        "events_all": info["ev"],
        "users_all":  info["us"],
        "events_ss":  ss["ev"],
        "users_ss":   ss["us"],
    })

print(f"Добавлено строк по цели 609847539: {len(ib_all)}")
print(f"  достижений всего:  {sum(x['ev'] for x in ib_all.values())}")
print(f"  посетителей всего: {sum(x['us'] for x in ib_all.values())}")
print(f"  достижений из ИИ:  {sum(x['ev'] for x in ib_ss.values())}")
print(f"  посетителей из ИИ: {sum(x['us'] for x in ib_ss.values())}")

output.sort(key=lambda x: -x["events_all"])

# =============== XLSX ===============
NAVY = "0B1F5C"; ORANGE = "F26522"; GREEN = "2E9E5F"
FILL_H = PatternFill("solid", fgColor=NAVY)
FH  = Font(name="Arial", size=11, bold=True, color="FFFFFF")
FB  = Font(name="Arial", size=10)
FBB = Font(name="Arial", size=10, bold=True)
FBO = Font(name="Arial", size=10, bold=True, color=ORANGE)
FBG = Font(name="Arial", size=10, bold=True, color=GREEN)
ALIGN_HC = Alignment(horizontal="center", vertical="center", wrap_text=True)
ALIGN_LT = Alignment(wrap_text=True, vertical="top")

wb = Workbook()
ws = wb.active
ws.title = "События"

ws.cell(row=1, column=1, value=f"События конверсий · Яндекс Метрика {meta['counter']}").font = Font(size=12, bold=True, color=NAVY)
ws.cell(row=2, column=1, value=f"Период: {meta['date1']} — {meta['date2']}  ·  Атрибуция: LastSign (кросс-девайс)").font = Font(size=10, italic=True, color="666666")

headers = ["№", "URL страницы", "Название цели",
           "События ВСЕГО", "Посетители ВСЕГО",
           "События из ИИ-поиска", "Посетители из ИИ-поиска",
           "Доля ИИ, %"]
for i, h in enumerate(headers, 1):
    cl = ws.cell(row=4, column=i, value=h)
    cl.font = FH; cl.fill = FILL_H; cl.alignment = ALIGN_HC
ws.row_dimensions[4].height = 36

for i, r in enumerate(output, 1):
    row = 4 + i
    ws.cell(row=row, column=1, value=i).font = FB
    uc = ws.cell(row=row, column=2, value=r["url"]); uc.font = FBB; uc.alignment = ALIGN_LT
    nc = ws.cell(row=row, column=3, value=r["goal"] or "—"); nc.font = FB if not r["goal"] else FBB
    nc.alignment = ALIGN_LT
    ws.cell(row=row, column=4, value=r["events_all"]).font = FBB
    ws.cell(row=row, column=5, value=r["users_all"]).font = FB
    ev_ss = r["events_ss"]
    sc = ws.cell(row=row, column=6, value=ev_ss)
    sc.font = FBG if ev_ss > 0 else FB
    ws.cell(row=row, column=7, value=r["users_ss"]).font = FB
    share = ev_ss / r["events_all"] if r["events_all"] else 0
    pc = ws.cell(row=row, column=8, value=share)
    pc.number_format = "0.00%"
    pc.font = FBO if share > 0 else FB

tot_all = sum(r["events_all"] for r in output)
tot_ss  = sum(r["events_ss"] for r in output)
# Итоговую строку не рисуем:
#  — посетителей корректно сложить нельзя (один посетитель = несколько URL)
#  — долю ИИ по среднему тоже некорректно
# Итоги по событиям печатаются в лог для сверки, но в файле их нет.

widths = [4, 55, 42, 12, 14, 14, 16, 10]
for i, w in enumerate(widths, 1):
    ws.column_dimensions[get_column_letter(i)].width = w
ws.freeze_panes = "A5"
ws.auto_filter.ref = f"A4:H{4+len(output)}"
# высота строк в основной таблице — чтобы длинные названия целей не обрезались
for i in range(5, 5+len(output)):
    ws.row_dimensions[i].height = 28

# ========= Легенда =========
ws3 = wb.create_sheet("Легенда")
rows_legend = [
    ("Параметр",             "Значение"),
    ("Счётчик",               meta['counter']),
    ("Период",                f"{meta['date1']} — {meta['date2']}"),
    ("Источник",              "Яндекс Метрика API, таблица event_params (preset=goal_params) + цель 609847539"),
    ("Фильтр событий",        "ym:ep:eventParamsLevel1=~'MSB_prod' (для событий MSB) + цель 609847539 (переходы ib.psbank)"),
    ("Фильтр «из ИИ-поиска»", "ym:s:UTMSource=~'ai_' (переходы с меткой ai_search)"),
    ("Маппинг целей",         "mapping.xlsx (Разметка - Апрель 2026)"),
    ("", ""),
    ("Колонка",               "Что означает"),
    ("URL страницы",          "Полный URL без query-параметров (без ?utm_source и т.д.)"),
    ("Название цели",         "Название из листа «Разметка». Прочерк — URL отсутствует в маппинге"),
    ("События ВСЕГО",         "Количество срабатываний JS-события или достижений цели 609847539"),
    ("Посетители ВСЕГО",      "Уникальные посетители (для цели 609847539 — 0: было запрошено только число достижений)"),
    ("События из ИИ-поиска",  "Те же события, но от визитов с меткой utm_source=ai_search"),
    ("Посетители из ИИ-поиска","Уникальные посетители из ai_search-визитов"),
    ("Доля ИИ, %",            "События из ИИ-поиска / События ВСЕГО"),
]
for i, (a, b) in enumerate(rows_legend, 1):
    ca = ws3.cell(row=i, column=1, value=a)
    cb = ws3.cell(row=i, column=2, value=b)
    is_header = i == 1 or i == 9
    if is_header:
        ca.font = FH; cb.font = FH
        ca.fill = FILL_H; cb.fill = FILL_H
    else:
        ca.font = FBB; cb.font = FB
        cb.alignment = Alignment(wrap_text=True, vertical="top")
ws3.column_dimensions['A'].width = 24
ws3.column_dimensions['B'].width = 90

wb.save(OUTPUT)
print(f"\nSaved: {OUTPUT}")
print(f"  строк: {len(output)}")
print(f"  события всего: {tot_all}")
print(f"  события из ИИ: {tot_ss}")
print(f"  доля ИИ: {tot_ss/tot_all*100:.3f}%" if tot_all else "")
