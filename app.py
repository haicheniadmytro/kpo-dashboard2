import html
import re
import logging
import os
import subprocess
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import gspread
import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import plotly.io as pio
import streamlit as st
from google.oauth2.service_account import Credentials


# ============================================================
# 0. Build info
# ============================================================
def _get_build_info():
    app_dir = os.path.dirname(os.path.abspath(__file__))
    app_file = os.path.abspath(__file__)
    try:
        dt = datetime.fromtimestamp(os.path.getmtime(app_file))
        build_date = dt.strftime("%Y-%m-%d"); build_time = dt.strftime("%H:%M")
    except Exception:
        build_date = "—"; build_time = "—"
    build_number = "?"; commit_hash = None; commit_subject = None
    try:
        res = subprocess.run(["git", "rev-list", "--count", "HEAD"], cwd=app_dir, capture_output=True, text=True, timeout=2)
        if res.returncode == 0 and res.stdout.strip(): build_number = res.stdout.strip()
        res = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=app_dir, capture_output=True, text=True, timeout=2)
        if res.returncode == 0 and res.stdout.strip(): commit_hash = res.stdout.strip()
        res = subprocess.run(["git", "log", "-1", "--pretty=%s"], cwd=app_dir, capture_output=True, text=True, timeout=2)
        if res.returncode == 0 and res.stdout.strip(): commit_subject = res.stdout.strip()
    except Exception:
        pass
    return build_number, build_date, build_time, commit_hash, commit_subject


(APP_BUILD_NUMBER, APP_BUILD_DATE, APP_BUILD_TIME, APP_COMMIT_HASH, APP_COMMIT_SUBJECT) = _get_build_info()


# ============================================================
# 0b. Історія останніх 10 змін
# ============================================================
RECENT_CHANGES = [
    "**v1.23** — Overview: покращено читабельність міток і заголовків блоків (світліший колір, трохи більший шрифт, більші іконки «?»).",
    "**v1.22** — «Динаміка» → «Порівняння по роках (YoY)»: пунктирна лінія аналогічного періоду торік, коли увімкнено «Показати аналогічний період торік».",
    "**v1.21** — Кнопка «Скинути всі фільтри» тепер повертає поля «Мін. кількість за день» та «% погоджень» до 0 / 0–100 (раніше значення поверталися після зміни місяця).",
    "**v1.20** — Виправлено регресії v1.19: (1) в режимі «Тотал» повернено «Найактивніша операція» і «Коефіцієнт погоджень по операціях» — `period_ops` тепер на повному наборі операцій з урахуванням day-фільтрів (раніше був порожній, бо `filtered` у режимі «Тотал» містить лише рядки «Тотал»). (2) Прибрано чекбокс «Показати дельту до попереднього дня» та відповідну лінію/другу вісь. (3) Прибрано Drill-down теплових карт (selectbox + таблиця) в обох вкладках — «Навантаження» і «Операції».",
    "**v1.19** — Виправлено 5 багів v1.18: квартал (усі 3 місяці), слайдери→поля, drill-down подвоєння, day-фільтри на period_ops, розділено «даних немає» vs «фільтри відсіяли».",
    "**v1.18** — Фільтри й аналіз: діапазон % погоджень і мін. кількості, «Швидкі періоди», торішній період, дельти DoD, drill-down теплових карт.",
    "**v1.17** — «Порівняння періодів»: спільний графік «Порівняльна динаміка по днях».",
    "**v1.16** — «Порівняння періодів»: 2–6 місяців одночасно.",
    "**v1.15** — «Порівняння періодів»: обрізання до тієї ж дати.",
]

MAX_RECENT_CHANGES = 10
if len(RECENT_CHANGES) > MAX_RECENT_CHANGES:
    RECENT_CHANGES = RECENT_CHANGES[:MAX_RECENT_CHANGES]

LAST_UPDATE_NOTE = RECENT_CHANGES[0]


# ============================================================
# 1. Конфігурація
# ============================================================
st.set_page_config(page_title="KPO Dashboard", page_icon="📊", layout="wide")

try:
    _st_ver = tuple(map(int, st.__version__.split(".")[:2]))
except Exception:
    _st_ver = (0, 0)
STREAMLIT_SUPPORTS_CLICK = _st_ver >= (1, 35)


# ============================================================
# 2. Тема
# ============================================================
KPO_BG = "#0b0f17"; KPO_CARD_BG = "#131824"; KPO_BORDER = "#1f2733"
KPO_TEXT = "#e6edf3"; KPO_TEXT_MUTED = "#8b98a9"
KPO_CYAN = "#00d9ff"; KPO_AMBER = "#ffb703"; KPO_GREEN = "#06d6a0"; KPO_RED = "#ef476f"
KPO_PURPLE = "#8338ec"; KPO_ORANGE = "#f4a261"; KPO_BLUE = "#4cc9f0"; KPO_PINK = "#ff6b9d"

KPO_COLORWAY = [KPO_CYAN, KPO_AMBER, KPO_GREEN, KPO_RED, KPO_PURPLE, KPO_ORANGE, KPO_BLUE, KPO_PINK]
KPO_HEAT_SCALE = [[0.0, "#0b0f17"], [0.5, "#0d5c73"], [1.0, KPO_CYAN]]

_kpo_dark_template = go.layout.Template(layout=go.Layout(
    paper_bgcolor=KPO_BG, plot_bgcolor=KPO_BG,
    font=dict(color=KPO_TEXT, family="Inter, sans-serif"),
    colorway=KPO_COLORWAY,
    xaxis=dict(gridcolor=KPO_BORDER, zerolinecolor=KPO_BORDER, linecolor=KPO_BORDER),
    yaxis=dict(gridcolor=KPO_BORDER, zerolinecolor=KPO_BORDER, linecolor=KPO_BORDER),
    legend=dict(bgcolor="rgba(0,0,0,0)"),
    hoverlabel=dict(bgcolor=KPO_CARD_BG, font_color=KPO_TEXT, bordercolor=KPO_BORDER),
    colorscale=dict(sequential=KPO_HEAT_SCALE),
))
pio.templates["kpo_dark"] = _kpo_dark_template
pio.templates.default = "kpo_dark"


# ============================================================
# 3. Константи
# ============================================================
SPREADSHEET_ID = "1STX1vgDAk3zVDshXdZmTgJJSvQNCN4WmmftOskwymYI"
KYIV_TZ = ZoneInfo("Europe/Kyiv")

MONTHS = {
    "Січень": 1, "Лютий": 2, "Березень": 3, "Квітень": 4, "Травень": 5, "Червень": 6,
    "Липень": 7, "Серпень": 8, "Вересень": 9, "Жовтень": 10, "Листопад": 11, "Грудень": 12,
}

OPERATIONS = [
    "Бонуси", "Призупинка", "Відновлення", "Відміна SF",
    "Переоформлення", "Закриття контракта", "Со-доступ", "Зміна дати активації", "Loyalty",
]

ALIASES = {"Зміна дати активації": "Зміна дати активації"}

WEEKDAY_UA = {"Monday": "Пн", "Tuesday": "Вт", "Wednesday": "Ср", "Thursday": "Чт",
              "Friday": "Пт", "Saturday": "Сб", "Sunday": "Нд"}
WEEKDAY_ORDER_UA = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Нд"]

APPROVAL_GOOD_THRESHOLD = 90
APPROVAL_WARN_THRESHOLD = 80
COLOR_GOOD = KPO_GREEN; COLOR_WARN = KPO_AMBER; COLOR_BAD = KPO_RED

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger(__name__)


# ============================================================
# 5. Часові
# ============================================================
def now_kyiv() -> pd.Timestamp:
    return pd.Timestamp.now(tz=KYIV_TZ).replace(tzinfo=None).normalize()


def now_kyiv_exact() -> pd.Timestamp:
    return pd.Timestamp.now(tz=KYIV_TZ).replace(tzinfo=None)


# ============================================================
# 6. Парсинг
# ============================================================
DAY_HEADER_RE = re.compile(r"^\s*(\d{1,2})\s*[./\-]\s*(\d{1,2})\s*(?:[./\-]\s*\d{2,4})?\s*$")

_CHAR_FIXES = {
    "\xa0": " ", "\u202f": " ", "\u2007": " ",
    "\u2010": "-", "\u2011": "-", "\u2012": "-", "\u2013": "-", "\u2014": "-", "\u2212": "-",
    "\u2019": "'", "\u02bc": "'", "`": "'",
}


def is_empty_cell(value):
    if value is None: return True
    if isinstance(value, bool): return False
    return str(value).strip() == ""


def normalize_operation(value):
    if not isinstance(value, str): return ""
    for bad, good in _CHAR_FIXES.items():
        value = value.replace(bad, good)
    return " ".join(value.strip().split())


def as_number(value):
    if is_empty_cell(value): return 0.0, True
    val_str = str(value)
    for bad, good in _CHAR_FIXES.items():
        val_str = val_str.replace(bad, good)
    val_str = val_str.replace(" ", "").replace("'", "")
    match = re.search(r"-?\d+(?:[.,]\d+)?", val_str)
    if not match:
        logger.warning(f"Не вдалося розпізнати число в '{val_str}'")
        return 0.0, False
    try:
        return float(match.group(0).replace(",", ".")), True
    except ValueError:
        logger.warning(f"Не вдалося перетворити '{val_str}' на число")
        return 0.0, False


def parse_month_header(value, sheet_year):
    if not isinstance(value, str): return None
    value = normalize_operation(value)
    match = re.match(
        r"^(Січень|Лютий|Березень|Квітень|Травень|Червень|"
        r"Липень|Серпень|Вересень|Жовтень|Листопад|Грудень)\s+(\d{2,4})$",
        value, flags=re.IGNORECASE,
    )
    if not match: return None
    year_part = match.group(2)
    year = int(year_part) if len(year_part) == 4 else 2000 + int(year_part)
    return MONTHS[match.group(1).capitalize()], year


def _find_month_blocks(values, sheet_year):
    blocks = []
    for idx, row in enumerate(values):
        parsed = parse_month_header(row[0] if row else "", sheet_year)
        if parsed: blocks.append([idx, parsed[0], parsed[1], None])
    for i, block in enumerate(blocks):
        block[3] = blocks[i + 1][0] if i + 1 < len(blocks) else len(values)
    return [tuple(b) for b in blocks]


def _find_total_row(values, start, end):
    for r in range(start, end):
        row = values[r]
        if row and normalize_operation(row[0]).lower() == "тотал": return r, 0
    for r in range(start, end):
        row = values[r]
        for c in range(1, len(row)):
            if normalize_operation(row[c]).lower() == "тотал": return r, c
    return None, None


def _detect_day_columns(values, start, end, month):
    best = None
    for r in range(start, min(start + 8, end)):
        found = []
        for c, cell in enumerate(values[r]):
            match = DAY_HEADER_RE.match(str(cell))
            if not match: continue
            day, mon = int(match.group(1)), int(match.group(2))
            if mon == month and 1 <= day <= 31: found.append((c, day))
        if found and (best is None or len(found) > len(best[1])): best = (r, sorted(found))
    return best if best else (None, [])


def _looks_like_operation_label(label):
    return bool(re.search(r"[A-Za-zА-Яа-яІіЇїЄєҐґ]", label))


def _build_day_spans(day_cols):
    if not day_cols: return []
    gaps = [day_cols[i + 1][0] - day_cols[i][0] for i in range(len(day_cols) - 1)]
    default_span = int(np.median(gaps)) if gaps else 1
    default_span = max(1, min(default_span, 2))
    spans = []
    for i, (col, day) in enumerate(day_cols):
        span = day_cols[i + 1][0] - col if i + 1 < len(day_cols) else default_span
        spans.append((day, col, max(1, min(span, 2))))
    return spans


def _discover_year_sheets(spreadsheet):
    all_worksheets = spreadsheet.worksheets()
    year_ws = {}
    for ws in all_worksheets:
        title = ws.title.strip()
        if re.fullmatch(r"\d{2}", title): year_ws[title] = ws
    ordered_titles = sorted(year_ws.keys(), key=int)
    ordered_worksheets = [year_ws[t] for t in ordered_titles]
    all_titles = [ws.title for ws in all_worksheets]
    return ordered_titles, ordered_worksheets, all_titles


# ============================================================
# 7. Google Sheets
# ============================================================
def get_client():
    scopes = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
    credentials = Credentials.from_service_account_info(dict(st.secrets["gcp_service_account"]), scopes=scopes)
    return gspread.authorize(credentials)


@st.cache_data(ttl=300, show_spinner="Завантаження даних з Google Таблиці…")
def load_data():
    client = get_client()
    spreadsheet = client.open_by_key(SPREADSHEET_ID)
    records = []; warnings = []; unknown_ops = {}; sheet_month_totals = {}; debug_info = []
    _today_for_load = now_kyiv(); _current_ym = (_today_for_load.year, _today_for_load.month)
    sheet_names, worksheets, all_titles = _discover_year_sheets(spreadsheet)
    if not sheet_names:
        raise ValueError(f"Не знайдено жодного аркуша з назвою-роком. Наявні: {', '.join(all_titles)}.")
    other_titles = [t for t in all_titles if t.strip() not in sheet_names]
    if other_titles:
        warnings.append(f"ℹ️ Аркуші без формату «рік» (пропущено): {', '.join(other_titles)}.")

    for sheet_name, worksheet in zip(sheet_names, worksheets):
        try:
            values = worksheet.get_all_values()
        except Exception as exc:
            warnings.append(f"⚠️ Не вдалося прочитати аркуш «{sheet_name}»: {exc}")
            continue
        if not values: continue
        sheet_year = 2000 + int(sheet_name)
        candidates_by_month = {}; month_warnings = {}

        for header_row, month, year, block_end in _find_month_blocks(values, sheet_year):
            month_key = f"{year}-{month:02d}"
            where_block = f"аркуш «{sheet_name}», {month:02d}.{year}"
            month_warnings.setdefault(month_key, [])
            total_row_idx, label_col = _find_total_row(values, header_row, block_end)
            if total_row_idx is None:
                month_warnings[month_key].append(f"{where_block}: не знайдено рядок «Тотал».")
                continue
            _, day_cols = _detect_day_columns(values, header_row, block_end, month)
            if not day_cols:
                month_warnings[month_key].append(f"{where_block}: не знайдено заголовків днів.")
                continue
            valid_rows = 0; matched_known = 0
            for r in range(total_row_idx + 1, block_end):
                row = values[r]
                label = normalize_operation(row[label_col] if label_col < len(row) else "")
                if not label: break
                if label.lower() == "тотал": continue
                if parse_month_header(label, sheet_year): break
                if not _looks_like_operation_label(label): break
                valid_rows += 1
                if ALIASES.get(label, label) in OPERATIONS: matched_known += 1
            score = (matched_known, len(day_cols), valid_rows)
            candidates_by_month.setdefault(month_key, []).append(
                (score, header_row, month, year, block_end, total_row_idx, label_col, day_cols)
            )

        successfully_loaded_months = set(); chosen_blocks = []
        for month_key, candidates in candidates_by_month.items():
            candidates.sort(key=lambda c: c[0], reverse=True)
            best = candidates[0]
            if best[0][2] == 0:
                ml = f"{best[2]:02d}.{best[3]}"
                month_warnings.setdefault(month_key, []).append(
                    f"аркуш «{sheet_name}», {ml}: під рядком «Тотал» не знайдено жодної операції.")
                continue
            successfully_loaded_months.add(month_key)
            if len(candidates) > 1:
                ml = f"{best[2]:02d}.{best[3]}"
                month_warnings.setdefault(month_key, []).append(
                    f"аркуш «{sheet_name}», {ml}: на аркуші {len(candidates)} блоків з цим місяцем — використано найповніший.")
            chosen_blocks.append(best[1:])

        for month_key, msgs in month_warnings.items():
            if not msgs: continue
            if month_key in successfully_loaded_months:
                for msg in msgs: warnings.append(f"ℹ️ {msg}")
            else:
                for msg in msgs: warnings.append(f"⚠️ {msg}")
                warnings.append(f"⚠️ Місяць {month_key}: жоден блок не завантажено.")

        for header_row, month, year, block_end, total_row_idx, label_col, day_cols in chosen_blocks:
            month_key = f"{year}-{month:02d}"
            month_label = f"{month:02d}.{year}"
            where = f"аркуш «{sheet_name}», {month_label}"
            block_ym = (year, month)
            day_spans = _build_day_spans(day_cols)
            first_day_col = day_spans[0][1]
            sum_cols = [c for c in range(label_col + 1, first_day_col)][:2]
            expected_days = pd.Period(month_key).days_in_month
            if len(day_spans) < expected_days:
                prefix = "ℹ️" if block_ym >= _current_ym else "⚠️"
                warnings.append(f"{prefix} {where}: знайдено {len(day_spans)} колонок днів із {expected_days}.")
            elif len(day_spans) > expected_days:
                warnings.append(f"ℹ️ {where}: знайдено {len(day_spans)} колонок днів, хоча в місяці {expected_days}.")
            total_row = values[total_row_idx]
            if len(sum_cols) == 2:
                t_true, _ = as_number(total_row[sum_cols[0]] if sum_cols[0] < len(total_row) else "")
                t_false, _ = as_number(total_row[sum_cols[1]] if sum_cols[1] < len(total_row) else "")
                sheet_month_totals[month_key] = (t_true, t_false)
            debug_info.append({
                "аркуш": sheet_name, "місяць": month_label,
                "рядок «Тотал» (Excel)": total_row_idx + 1, "колонка назви": label_col,
                "вміст рядка (перші 6)": " | ".join(str(x) for x in total_row[:6]),
            })
            invalid_days_warned = set(); rows_read = 0
            for r in range(total_row_idx + 1, block_end):
                row = values[r]
                label = normalize_operation(row[label_col] if label_col < len(row) else "")
                if not label: break
                if label.lower() == "тотал": continue
                if parse_month_header(label, sheet_year): break
                if not _looks_like_operation_label(label):
                    warnings.append(f"⚠️ {where}: рядок «{label}» не схожий на операцію — читання зупинено.")
                    break
                operation = ALIASES.get(label, label)
                if operation not in OPERATIONS:
                    unknown_ops.setdefault(operation, []).append(month_label)
                rows_read += 1
                for day, col, span in day_spans:
                    try:
                        date = pd.Timestamp(year=year, month=month, day=day)
                    except (ValueError, OverflowError):
                        if day not in invalid_days_warned:
                            invalid_days_warned.add(day)
                            warnings.append(f"ℹ️ {where}: невалідний день {day:02d}.{month:02d} — пропущено.")
                        continue
                    raw_true = row[col] if col < len(row) else ""
                    raw_false = row[col + 1] if (span >= 2 and col + 1 < len(row)) else ""
                    val_true, ok_true = as_number(raw_true)
                    val_false, ok_false = as_number(raw_false)
                    if not ok_true or not ok_false:
                        bad = raw_true if not ok_true else raw_false
                        warnings.append(f"⚠️ {where}, {operation}, {day:02d}.{month:02d}: «{bad}» → 0.")
                    records.append({
                        "date": date, "operation": operation,
                        "value": val_true + val_false, "sum_true": val_true, "sum_false": val_false,
                        "has_data": not (is_empty_cell(raw_true) and is_empty_cell(raw_false)),
                    })
            if rows_read == 0:
                warnings.append(f"⚠️ {where}: під «Тотал» не знайдено жодної операції.")

    if not records:
        raise ValueError("Не знайдено деталізованих даних у Google Таблиці.")

    df_raw = pd.DataFrame(records)
    df_raw["date"] = pd.to_datetime(df_raw["date"])
    df = df_raw.groupby(["date", "operation"], as_index=False).agg(
        value=("value", "sum"), sum_true=("sum_true", "sum"),
        sum_false=("sum_false", "sum"), has_data=("has_data", "any"))
    total = df.groupby("date", as_index=False).agg(
        value=("value", "sum"), sum_true=("sum_true", "sum"),
        sum_false=("sum_false", "sum"), has_data=("has_data", "any")).assign(operation="Тотал")
    df = pd.concat([df, total], ignore_index=True)
    df["year"] = df["date"].dt.year
    df["month"] = df["date"].dt.strftime("%Y-%m")
    df["month_name"] = df["date"].dt.strftime("%b %Y")
    df["weekday"] = df["date"].dt.day_name()
    df["is_weekend"] = df["date"].dt.weekday >= 5
    df = df.sort_values(["date", "operation"]).reset_index(drop=True)

    for op, months in unknown_ops.items():
        warnings.append(f"🆕 Операція «{op}» відсутня в OPERATIONS ({', '.join(sorted(set(months)))}).")

    computed = df[df["operation"] == "Тотал"].groupby("month")["value"].sum()
    for month_key, (t_true, t_false) in sheet_month_totals.items():
        sheet_sum = t_true + t_false
        calc_sum = float(computed.get(month_key, 0))
        if sheet_sum > 0 and abs(calc_sum - sheet_sum) > 0.5:
            warnings.append(f"❗ {month_key}: сума по днях = {calc_sum:,.0f}, «Тотал» = {sheet_sum:,.0f}.")
    return df, warnings, debug_info


# ============================================================
# 8. Хелпери
# ============================================================
def with_data(df):
    if "has_data" not in df.columns: return df
    return df[df["has_data"]]


def calc_busiest_weekday(df):
    df = with_data(df)
    if df.empty: return None, None
    daily = df.groupby("date")["value"].sum().reset_index()
    daily["weekday"] = daily["date"].dt.day_name()
    wd_avg = daily.groupby("weekday")["value"].mean()
    return wd_avg.idxmax(), wd_avg.max()


def calc_busiest_operation(df):
    if df.empty or df["operation"].nunique() == 0: return None, None
    ops = with_data(df[df["operation"] != "Тотал"])
    if ops.empty: return None, None
    total_by_op = ops.groupby("operation")["value"].sum()
    return total_by_op.idxmax(), total_by_op.max()


def calc_stability(df, daily_avg):
    daily = with_data(df).groupby("date")["value"].sum()
    if daily.empty: return 0, 0, "Немає даних"
    std = daily.std(); cv = (std / daily_avg * 100) if daily_avg > 0 else 0
    if cv < 15: interp = "🟢 Низька варіативність (≤15%)"
    elif cv < 30: interp = "🟡 Середня варіативність (15-30%)"
    else: interp = "🔴 Висока варіативність (>30%)"
    return std, cv, interp


def detect_anomalies(df, window=14, threshold=3.0):
    df = with_data(df)
    if df.empty: return pd.DataFrame()
    daily = df.groupby("date")["value"].sum().reset_index().sort_values("date")
    if len(daily) < window: return pd.DataFrame()
    daily["month_key"] = daily["date"].dt.to_period("M")
    out = []
    for _, group in daily.groupby("month_key"):
        if len(group) < 3: continue
        g = group.sort_values("date").copy()
        g["rolling_median"] = g["value"].rolling(window=window, min_periods=1, center=True).median()
        g["rolling_mad"] = g["value"].rolling(window=window, min_periods=1, center=True).apply(
            lambda x: np.median(np.abs(x - np.median(x))) if len(x) > 1 else 0)
        g["z_score"] = (g["value"] - g["rolling_median"]) / (g["rolling_mad"] * 1.4826).replace(0, 1)
        g["is_anomaly"] = abs(g["z_score"]) > threshold
        out.append(g)
    if not out: return pd.DataFrame()
    result = pd.concat(out, ignore_index=True)
    return result[result["value"] > 0]


def gaussian_kde_np(data, x_grid, bandwidth=None):
    data = np.asarray(data, dtype=float); data = data[np.isfinite(data)]
    n = len(data)
    if n == 0: return np.zeros_like(x_grid)
    std = np.std(data)
    if std == 0: bandwidth = 1.0
    elif bandwidth is None: bandwidth = 1.06 * std * n ** (-0.2)
    x_grid = np.asarray(x_grid)
    u = (x_grid[:, None] - data[None, :]) / bandwidth
    kernel = np.exp(-0.5 * u * u)
    return kernel.sum(axis=1) / (n * bandwidth * np.sqrt(2 * np.pi))


def analyze_density(group_names, dev_data):
    stats = {}
    for name, data in zip(group_names, dev_data):
        if data is None: continue
        data = np.asarray(data, dtype=float); data = data[np.isfinite(data)]
        if len(data) < 2: continue
        n = len(data); std_d = float(np.std(data, ddof=0)); median_d = float(np.median(data))
        p25, p75 = np.percentile(data, [25, 75]); iqr = float(p75 - p25)
        if n >= 3:
            try:
                skew_val = float(pd.Series(data).skew())
                if not np.isfinite(skew_val): skew_val = None
            except Exception: skew_val = None
        else: skew_val = None
        bins = max(2, min(12, n)); hist, edges = np.histogram(data, bins=bins)
        peak_idx = int(np.argmax(hist)); peak_center = float((edges[peak_idx] + edges[peak_idx + 1]) / 2)
        peak_count = int(hist[peak_idx]); peak_height_pct = float(peak_count / n * 100)
        if n < 5: width_key = "insufficient"; width_txt = f"σ = {std_d:.1f}%, але n = {n} — ненадійно"
        elif std_d < 15: width_key = "narrow"; width_txt = f"σ = {std_d:.1f}% — невеликий розкид"
        elif std_d < 30: width_key = "medium"; width_txt = f"σ = {std_d:.1f}% — помітний розкид"
        else: width_key = "wide"; width_txt = f"σ = {std_d:.1f}% — значний розкид"
        if skew_val is None: skew_txt = f"асиметрію оцінити неможливо (n = {n})"
        elif abs(skew_val) < 0.3: skew_txt = f"близький до симетричного (skew = {skew_val:+.2f})"
        elif skew_val > 0: skew_txt = f"зміщений вправо (skew = {skew_val:+.2f})"
        else: skew_txt = f"зміщений вліво (skew = {skew_val:+.2f})"
        if n < 3: reliability = "very_low"; reliability_txt = f"n = {n} — надто мало"
        elif n < 5: reliability = "low"; reliability_txt = f"n = {n} — дуже мала вибірка"
        elif n < 15: reliability = "low"; reliability_txt = f"n = {n} — мала вибірка"
        elif n < 20: reliability = "moderate"; reliability_txt = f"n = {n} — ще невелика"
        else: reliability = "good"; reliability_txt = f"n = {n} — достатньо"
        stats[name] = {
            "n": n, "std": std_d, "median": median_d, "q25": float(p25), "q75": float(p75), "iqr": iqr,
            "skew": skew_val, "peak": peak_center, "peak_pct": peak_height_pct, "peak_count": peak_count,
            "width_key": width_key, "width_txt": width_txt, "skew_txt": skew_txt,
            "reliability": reliability, "reliability_txt": reliability_txt,
            "min": float(data.min()), "max": float(data.max()),
        }
    return stats


def forecast_scenarios(df, current_month):
    if df.empty or current_month not in df["month"].values: return None, None
    current_period = pd.Period(current_month); today = now_kyiv()
    if today.year != current_period.year or today.month != current_period.month: return None, None
    month_data = with_data(df[df["month"] == current_month])
    if not month_data.empty: month_data = month_data[month_data["date"] <= today]
    if month_data.empty: return None, None
    total_days = current_period.days_in_month
    last_data_day = min(int(month_data["date"].max().day), today.day)
    remaining_days = max(0, total_days - last_data_day)
    fact_days = month_data[month_data["date"].dt.day <= last_data_day]
    if fact_days.empty: return None, None
    daily_sums = fact_days.groupby("date")["value"].sum()
    fact_sum = float(daily_sums.sum()); n_days = int(len(daily_sums))
    avg_daily = float(daily_sums.mean()) if n_days > 0 else 0.0
    if n_days >= 2:
        std_daily = float(daily_sums.std())
        if not np.isfinite(std_daily): std_daily = 0.0
        stat_min = fact_sum + max(0.0, avg_daily - 0.5 * std_daily) * remaining_days
        stat_max = fact_sum + (avg_daily + 0.5 * std_daily) * remaining_days
        range_available = True
    else:
        std_daily = 0.0; stat_min = None; stat_max = None; range_available = False
    stat_base = fact_sum + avg_daily * remaining_days
    stat_forecast = {
        "base": stat_base, "min": stat_min, "max": stat_max,
        "avg_daily": avg_daily, "std_daily": std_daily, "fact": fact_sum,
        "days_passed": last_data_day, "total_days": total_days,
        "remaining_days": remaining_days, "n_days": n_days, "range_available": range_available,
    }
    prev_period = current_period - 12; prev_period_str = str(prev_period); season_forecast = None
    if prev_period_str in df["month"].unique():
        prev_data = with_data(df[df["month"] == prev_period_str])
        prev_fact = prev_data[prev_data["date"].dt.day <= last_data_day]
        prev_remaining = prev_data[prev_data["date"].dt.day > last_data_day]
        if not prev_fact.empty and prev_fact["value"].sum() > 0 and prev_fact["value"].sum() >= 10:
            sf = fact_sum / prev_fact["value"].sum(); sf = max(0.3, min(3.0, sf))
            fr = prev_remaining["value"].sum() * sf
            season_forecast = {
                "base": fact_sum + fr, "min": fact_sum + fr * 0.9, "max": fact_sum + fr * 1.1,
                "seasonality_factor": sf, "fact": fact_sum,
                "days_passed": last_data_day, "total_days": total_days, "remaining_days": remaining_days,
                "prev_fact_sum": prev_fact["value"].sum(), "prev_remaining_sum": prev_remaining["value"].sum(),
                "forecast_remaining": fr, "has_prev_year": True, "prev_period": prev_period_str,
                "range_available": True,
            }
    return stat_forecast, season_forecast


def _style_line_traces(fig, font_size=10, font_color=None, show_text=True):
    for trace in fig.data:
        if getattr(trace, "type", None) != "scatter": continue
        try:
            if show_text:
                trace.textposition = "top center"
                trace.textfont = dict(size=font_size, color=font_color) if font_color else dict(size=font_size)
                trace.cliponaxis = False
            else:
                trace.text = None
        except (ValueError, TypeError) as exc:
            logger.warning(f"Не вдалося стилізувати text-мітки: {exc}")


# ============================================================
# 9. Основна
# ============================================================
st.title("📊 Dashboard погоджень КПО")
st.caption("Дані завантажуються напряму з Google Таблиці. Кеш оновлюється кожні 5 хвилин. Час — за Києвом.")

try:
    df, load_warnings, load_debug_info = load_data()
except Exception as exc:
    st.error("Не вдалося завантажити Google Таблицю.")
    st.code(str(exc))
    st.info("Перевір: 1) Google Sheets API, 2) доступ service account, 3) secrets у Streamlit.")
    st.stop()


def _is_critical_warning(w: str) -> bool:
    if w.startswith("ℹ️"): return False
    if w.startswith("⚠️") or w.startswith("❗"): return True
    return False


critical_warnings = [w for w in load_warnings if _is_critical_warning(w)]
informational_warnings = [w for w in load_warnings if not _is_critical_warning(w)]

if critical_warnings:
    st.error(f"⚠️ **Дані завантажено неповністю** — {len(critical_warnings)} критичних попереджень.")

with st.expander("🔧 Діагностика розпізнавання таблиці", expanded=False):
    if load_debug_info:
        st.dataframe(pd.DataFrame(load_debug_info), use_container_width=True, hide_index=True)
    else:
        st.info("Немає даних діагностики.")

if load_warnings:
    with st.expander(f"⚠️ Попередження ({len(load_warnings)})", expanded=bool(critical_warnings)):
        if critical_warnings:
            st.markdown("**🔴 Критичні:**")
            for w in critical_warnings: st.error(w)
        if informational_warnings:
            st.markdown("**ℹ️ Інформаційні:**")
            for w in informational_warnings: st.info(w)


# ============================================================
# 10. Сайдбар
# ============================================================
today = now_kyiv()


def _period_bounds(period_type, anchor, df_dates):
    if period_type == "day": return anchor, anchor, anchor.strftime("%d.%m.%Y")
    if period_type == "week":
        start = anchor - pd.Timedelta(days=anchor.weekday()); end = start + pd.Timedelta(days=6)
        return start, end, f"Тиждень {anchor.isocalendar().week} · {start.strftime('%d.%m')}–{end.strftime('%d.%m.%Y')}"
    if period_type == "decade":
        m_start = anchor.replace(day=1)
        if anchor.day <= 10: start, end = m_start, m_start.replace(day=10); label = f"01–10.{anchor.month:02d}.{anchor.year}"
        elif anchor.day <= 20: start, end = m_start.replace(day=11), m_start.replace(day=20); label = f"11–20.{anchor.month:02d}.{anchor.year}"
        else:
            start = m_start.replace(day=21); end = (m_start + pd.offsets.MonthEnd(0))
            label = f"21–{end.day:02d}.{anchor.month:02d}.{anchor.year}"
        return start, end, label
    if period_type == "month":
        start = anchor.replace(day=1); end = start + pd.offsets.MonthEnd(0)
        return start, end, start.strftime("%m.%Y")
    if period_type == "quarter":
        q = (anchor.month - 1) // 3 + 1; q_start_month = (q - 1) * 3 + 1
        start = pd.Timestamp(year=anchor.year, month=q_start_month, day=1)
        end = pd.Period(start, freq="Q").end_time.normalize()
        return start, end, f"Q{q} {anchor.year} ({start.strftime('%b')}–{end.strftime('%b')})"
    if period_type == "halfyear":
        h = 1 if anchor.month <= 6 else 2; start_month = 1 if h == 1 else 7
        start = pd.Timestamp(year=anchor.year, month=start_month, day=1)
        end = pd.Timestamp(year=anchor.year, month=start_month + 5, day=1) + pd.offsets.MonthEnd(0)
        return start, end, f"H{h} {anchor.year} ({start.strftime('%b')}–{end.strftime('%b')})"
    if period_type == "year":
        start = pd.Timestamp(year=anchor.year, month=1, day=1); end = pd.Timestamp(year=anchor.year, month=12, day=31)
        return start, end, str(anchor.year)
    return anchor, anchor, anchor.strftime("%d.%m.%Y")


def _anchor_for_shift(period_type, anchor, shift):
    if shift == 0: return anchor
    if period_type == "day": return anchor - pd.Timedelta(days=shift)
    if period_type == "week": return anchor - pd.Timedelta(weeks=shift)
    if period_type == "decade":
        for _ in range(shift):
            if anchor.day <= 10: anchor = anchor.replace(day=1) - pd.Timedelta(days=1)
            elif anchor.day <= 20: anchor = anchor.replace(day=10)
            else: anchor = anchor.replace(day=20)
        return anchor
    if period_type == "month": return (pd.Period(anchor, freq="M") - shift).end_time.normalize()
    if period_type == "quarter": return (pd.Period(anchor, freq="Q") - shift).start_time.normalize()
    if period_type == "halfyear":
        cur_h_start = _period_bounds("halfyear", anchor, None)[0]
        return cur_h_start - pd.DateOffset(months=6 * shift)
    if period_type == "year": return pd.Timestamp(year=anchor.year - shift, month=anchor.month, day=anchor.day)
    return anchor


def _list_available_periods(period_type, df_dates):
    if not df_dates: return []
    min_d = min(df_dates); max_d = max(df_dates); result = []; seen = set()
    if period_type == "day":
        for d in pd.date_range(min_d, max_d, freq="D"):
            if d <= today:
                lbl = d.strftime("%d.%m.%Y")
                if lbl not in seen: seen.add(lbl); result.append((d, lbl))
    elif period_type == "week":
        cur = min_d - pd.Timedelta(days=min_d.weekday())
        while cur <= max_d:
            s, e, lbl = _period_bounds("week", cur, None)
            if s <= today and lbl not in seen: seen.add(lbl); result.append((s, lbl))
            cur += pd.Timedelta(weeks=1)
    elif period_type == "decade":
        cur = min_d.replace(day=1)
        while cur <= max_d:
            for day_anchor in [cur.replace(day=1), cur.replace(day=11), cur.replace(day=21)]:
                s, e, lbl = _period_bounds("decade", day_anchor, None)
                if s <= today and lbl not in seen: seen.add(lbl); result.append((s, lbl))
            cur = (cur + pd.offsets.MonthEnd(0)) + pd.Timedelta(days=1)
    elif period_type == "month":
        cur = pd.Period(min_d, freq="M"); end_p = pd.Period(max_d, freq="M")
        while cur <= end_p:
            lbl = cur.strftime("%m.%Y")
            if cur.start_time.normalize() <= today and lbl not in seen: seen.add(lbl); result.append((cur.start_time.normalize(), lbl))
            cur += 1
    elif period_type == "quarter":
        cur = pd.Period(min_d, freq="Q"); end_p = pd.Period(max_d, freq="Q")
        while cur <= end_p:
            lbl = f"Q{cur.quarter} {cur.year} ({cur.start_time.strftime('%b')}–{cur.end_time.strftime('%b')})"
            if cur.start_time.normalize() <= today and lbl not in seen: seen.add(lbl); result.append((cur.start_time.normalize(), lbl))
            cur += 1
    elif period_type == "halfyear":
        y = min_d.year
        while y <= max_d.year:
            for h in [1, 2]:
                sm = 1 if h == 1 else 7
                s = pd.Timestamp(year=y, month=sm, day=1)
                e = pd.Timestamp(year=y, month=sm + 5, day=1) + pd.offsets.MonthEnd(0)
                lbl = f"H{h} {y} ({s.strftime('%b')}–{e.strftime('%b')})"
                if s <= today and lbl not in seen: seen.add(lbl); result.append((s, lbl))
            y += 1
    elif period_type == "year":
        for y in range(min_d.year, max_d.year + 1):
            lbl = str(y); s = pd.Timestamp(year=y, month=1, day=1)
            if s <= today and lbl not in seen: seen.add(lbl); result.append((s, lbl))
    result.sort(key=lambda x: x[0], reverse=True)
    return result


# ---- Callback скидання фільтрів по днях (виконується ДО перерендеру) ----
def _reset_day_filters():
    st.session_state["min_qty_input"] = 0
    st.session_state["pct_min_input"] = 0.0
    st.session_state["pct_max_input"] = 100.0


def _reset_all_filters():
    for k in list(st.session_state.keys()):
        if k.startswith("q_") or k == "period_mode":
            del st.session_state[k]
    _reset_day_filters()


st.sidebar.header("Фільтри")
period_mode = st.sidebar.radio("Період", options=["За місяцями", "Швидкі періоди"], index=0, key="period_mode")

custom_range = None; selected_years = []; selected_months = []
min_date = df["date"].min(); max_date = df["date"].max()
if "has_data" in df.columns and bool(df["has_data"].any()): last_data_date = df[df["has_data"]]["date"].max()
else: last_data_date = max_date
if last_data_date > today: last_data_date = today
max_selectable = last_data_date.date()

if period_mode == "За місяцями":
    years = sorted(df["year"].unique()); current_year = today.year
    default_years = [current_year] if current_year in years else [years[-1]] if years else []
    selected_years = st.sidebar.multiselect("Рік", options=years, default=default_years)
    available_months = df[df["year"].isin(selected_years)]["month"].drop_duplicates().sort_values().tolist()
    if len(selected_years) == 1 and selected_years[0] != current_year: default_months = available_months
    else:
        cms = today.strftime("%Y-%m")
        default_months = [cms] if cms in available_months else (available_months[-1:] if available_months else [])
    selected_months = st.sidebar.multiselect("Місяць", options=available_months, default=default_months,
                                              format_func=lambda x: pd.Period(x).strftime("%m.%Y"))
else:
    labels = {"day": "День", "week": "Тиждень", "decade": "Декада", "month": "Місяць",
              "quarter": "Квартал", "halfyear": "Півріччя", "year": "Рік", "custom": "Свій"}
    period_type = st.sidebar.radio("Тип", options=list(labels.keys()), format_func=lambda k: labels[k],
                                    horizontal=False, key="q_period_type")
    if period_type == "custom":
        default_start = max(min_date, last_data_date - pd.Timedelta(days=13))
        if default_start.date() > max_selectable: default_start = last_data_date
        dr = st.sidebar.date_input("Діапазон дат", value=(default_start.date(), last_data_date.date()),
                                    min_value=min_date.date(), max_value=max_selectable, key="q_custom_range")
        if isinstance(dr, tuple) and len(dr) == 2: custom_range = (pd.Timestamp(dr[0]), pd.Timestamp(dr[1]))
        else:
            single = dr[0] if isinstance(dr, tuple) else dr
            custom_range = (pd.Timestamp(single), pd.Timestamp(single))
            st.sidebar.info("Оберіть другу дату.")
        if custom_range[0] > custom_range[1]: custom_range = (custom_range[1], custom_range[0])
        selected_years = sorted({custom_range[0].year, custom_range[1].year})
        selected_months = sorted({d.strftime("%Y-%m") for d in pd.date_range(custom_range[0], custom_range[1], freq="D")})
    else:
        def _has_shift(shift):
            anchor = _anchor_for_shift(period_type, today, shift)
            s, e, _ = _period_bounds(period_type, anchor, None)
            return ((df["date"] >= s) & (df["date"] <= e)).any()
        shift_options = []; shift_labels = []
        if _has_shift(0): shift_options.append(0); shift_labels.append("Цей")
        if _has_shift(1): shift_options.append(1); shift_labels.append("Минулий")
        if _has_shift(2): shift_options.append(2); shift_labels.append("Позаминулий")
        shift_options.append("custom"); shift_labels.append("Конкретний")
        if not shift_options:
            st.sidebar.warning("Немає даних за жоден період цього типу."); st.stop()
        shift_choice = st.sidebar.radio("Прив'язка", options=shift_options,
                                         format_func=lambda k: shift_labels[shift_options.index(k)],
                                         index=0, key=f"q_shift_{period_type}")
        if shift_choice == "custom":
            avail = _list_available_periods(period_type, list(df["date"]))
            if not avail:
                st.sidebar.warning("Немає доступних періодів."); st.stop()
            avail_labels = [lbl for _, lbl in avail]
            picked = st.sidebar.selectbox("Конкретний період", options=avail_labels, index=0,
                                           key=f"q_pick_{period_type}")
            anchor = next(s for s, lbl in avail if lbl == picked)
        else:
            anchor = _anchor_for_shift(period_type, today, shift_choice)
        s, e, active_label = _period_bounds(period_type, anchor, None)
        e_eff = min(e, today)
        custom_range = (s, e_eff)
        selected_years = sorted({s.year, e_eff.year})
        selected_months = sorted({d.strftime("%Y-%m") for d in pd.date_range(s, e_eff, freq="D")})
        st.sidebar.caption(f"📅 {active_label} · {s.strftime('%d.%m.%Y')} – {e_eff.strftime('%d.%m.%Y')}")

if period_mode == "За місяцями":
    if selected_months:
        sm = sorted(selected_months)
        st.sidebar.caption(f"📅 {pd.Period(sm[0]).strftime('%m.%Y')}"
                           + (f" – {pd.Period(sm[-1]).strftime('%m.%Y')}" if len(sm) > 1 else ""))
elif custom_range is not None:
    st.sidebar.caption(f"📅 {custom_range[0].strftime('%d.%m.%Y')} – {custom_range[1].strftime('%d.%m.%Y')} "
                       f"({(custom_range[1] - custom_range[0]).days + 1} дн.)")

operation_mode = st.sidebar.radio("Режим показу", options=["Тотал", "Вибрані операції"], index=0)
_present_ops = set(df["operation"].unique()) - {"Тотал"}
all_ops = [op for op in OPERATIONS if op in _present_ops]
all_ops += sorted(_present_ops - set(all_ops))
if operation_mode == "Тотал": selected_operations = ["Тотал"]
else:
    default_ops = all_ops if all_ops else []
    selected_operations = st.sidebar.multiselect("Операції", options=all_ops, default=default_ops)
    if not selected_operations:
        st.sidebar.warning("Виберіть хоча б одну операцію.")
        selected_operations = ["Тотал"]

_prev_year_available = False
if period_mode == "За місяцями" and selected_months:
    for m in selected_months:
        prev_m = f"{pd.Period(m).year - 1}-{pd.Period(m).month:02d}"
        if (df["month"] == prev_m).any(): _prev_year_available = True; break
elif custom_range is not None:
    _prev_s = custom_range[0] - pd.DateOffset(years=1); _prev_e = custom_range[1] - pd.DateOffset(years=1)
    _prev_year_available = ((df["date"] >= _prev_s) & (df["date"] <= _prev_e)).any()

compare_prev_year = st.sidebar.checkbox("Показати аналогічний період торік", value=False,
                                          disabled=not _prev_year_available,
                                          help=None if _prev_year_available else "Дані за минулий рік недоступні")

st.sidebar.divider()
st.sidebar.subheader("Фільтри по днях")

_total_only = df[df["operation"] == "Тотал"]
if "has_data" in _total_only.columns and bool(_total_only["has_data"].any()):
    _max_daily_possible = int(max(1, _total_only[_total_only["has_data"]].groupby("date")["value"].sum().max()))
else: _max_daily_possible = 1000

min_qty = st.sidebar.number_input("Мін. кількість за день", min_value=0, max_value=_max_daily_possible,
                                    value=0, step=1, key="min_qty_input")
pct_col1, pct_col2 = st.sidebar.columns(2)
pct_min = pct_col1.number_input("% погоджень — мін", min_value=0.0, max_value=100.0,
                                  value=0.0, step=0.1, format="%.1f", key="pct_min_input")
pct_max = pct_col2.number_input("% погоджень — макс", min_value=0.0, max_value=100.0,
                                  value=100.0, step=0.1, format="%.1f", key="pct_max_input")
if pct_min > pct_max:
    pct_min, pct_max = pct_max, pct_min
    st.sidebar.warning("Мін і макс % поміняно місцями.")

st.sidebar.divider()
st.sidebar.subheader("Налаштування графіків")
smooth_enabled = st.sidebar.checkbox("Згладжування динаміки (ковзне середнє)", value=False)
smooth_window = 7
if smooth_enabled:
    smooth_window = st.sidebar.selectbox("Вікно згладжування (дні)", [3, 5, 7, 14], index=2)

st.sidebar.divider()
st.sidebar.button("🔄 Скинути всі фільтри", use_container_width=True, on_click=_reset_all_filters)


# ============================================================
# 11. Фільтрація
# ============================================================
if operation_mode == "Тотал": op_mask = df["operation"] == "Тотал"
else: op_mask = df["operation"].isin(selected_operations)

if period_mode == "За місяцями":
    filtered = df[df["year"].isin(selected_years) & df["month"].isin(selected_months) & op_mask].copy()
    _period_base_mask = df["year"].isin(selected_years) & df["month"].isin(selected_months)
else:
    if custom_range is None:
        st.warning("Не вдалось визначити період."); st.stop()
    filtered = df[(df["date"] >= custom_range[0]) & (df["date"] <= custom_range[1]) & op_mask].copy()
    _period_base_mask = (df["date"] >= custom_range[0]) & (df["date"] <= custom_range[1])

filtered = filtered[filtered["date"] <= today]
_period_base_mask &= df["date"] <= today

if filtered.empty:
    st.info("📭 **Даних ще немає** за вибраний період. Можливо, для поточного місяця ще не внесено жодного дня.")
    st.stop()


def _apply_day_filters(frame):
    frame = frame.copy()
    if frame.empty: return frame, 0, 0, set()
    daily_agg = frame.groupby("date", as_index=False).agg(
        value=("value", "sum"), sum_true=("sum_true", "sum"),
        sum_false=("sum_false", "sum"), has_data=("has_data", "any"))
    if "has_data" in daily_agg.columns: daily_agg = daily_agg[daily_agg["has_data"]]
    daily_agg["day_total"] = daily_agg["sum_true"] + daily_agg["sum_false"]
    daily_agg["day_pct"] = np.where(daily_agg["day_total"] > 0,
                                     daily_agg["sum_true"] / daily_agg["day_total"] * 100, 0.0)
    pass_qty = daily_agg["value"] >= min_qty
    pass_pct = (daily_agg["day_pct"] >= pct_min - 1e-9) & (daily_agg["day_pct"] <= pct_max + 1e-9)
    keep_dates = set(daily_agg.loc[pass_qty & pass_pct, "date"])
    return frame[frame["date"].isin(keep_dates)], len(keep_dates), len(daily_agg), keep_dates


filtered, _kept_days, _total_days, _kept_dates = _apply_day_filters(filtered)

if not filtered.empty:
    st.sidebar.caption(f"📊 Показано {_kept_days} з {_total_days} днів у динаміці")

if filtered.empty:
    st.warning("🔍 **Фільтри по днях відсіяли всі дні.** Спробуйте розширити «мін. кількість» / «% погоджень» "
               "або натиснути «Скинути всі фільтри».")
    st.stop()

filtered_stats = with_data(filtered)
if filtered_stats.empty:
    st.info("Дані ще не внесені для жодного дня — статистика недоступна, показано лише графіки.")


# ============================================================
# 12. Метрики
# ============================================================
daily_total = filtered_stats.groupby("date")["value"].sum()
total_value = daily_total.sum()
daily_avg = daily_total.mean() if not daily_total.empty else 0

weekday_mask = filtered_stats["is_weekend"] == False
weekend_mask = filtered_stats["is_weekend"] == True
daily_avg_weekday = filtered_stats[weekday_mask].groupby("date")["value"].sum().mean() if weekday_mask.any() else None
daily_avg_weekend = filtered_stats[weekend_mask].groupby("date")["value"].sum().mean() if weekend_mask.any() else None

peak = daily_total.max() if not daily_total.empty else 0
peak_date = daily_total.idxmax() if not daily_total.empty else None
min_val = daily_total.min() if not daily_total.empty else 0
peak_avg_ratio = peak / daily_avg if daily_avg > 0 else 0
busiest_weekday, busiest_weekday_val = calc_busiest_weekday(filtered)

# FIX #1+#3: period_ops з повного df (усі операції) + _kept_dates — а не з filtered (який у режимі «Тотал» містить лише рядки «Тотал»)
_period_ops_mask = _period_base_mask & df["date"].isin(_kept_dates) & (df["operation"] != "Тотал")
period_ops = with_data(df[_period_ops_mask])

if operation_mode == "Тотал": _busiest_source = period_ops
elif not period_ops.empty: _busiest_source = period_ops[period_ops["operation"].isin(selected_operations)]
else: _busiest_source = period_ops
busiest_op, busiest_op_val = calc_busiest_operation(_busiest_source)

std, cv, cv_interp = calc_stability(filtered, daily_avg)

if not filtered_stats.empty:
    sum_true_total = float(filtered_stats["sum_true"].sum())
    sum_false_total = float(filtered_stats["sum_false"].sum())
    total_ratio = sum_true_total + sum_false_total
    approval_rate_val = (sum_true_total / total_ratio * 100) if total_ratio > 0 else 0
    approval_rate_str = f"{approval_rate_val:.1f}%" if total_ratio > 0 else "—"
    approval_rate_available = total_ratio > 0
else:
    sum_true_total = sum_false_total = 0.0
    approval_rate_val = 0; approval_rate_str = "—"; approval_rate_available = False

approval_by_op = pd.DataFrame(columns=["operation", "sum_true", "sum_false", "total", "approval_rate"])
if not period_ops.empty:
    approval_by_op = period_ops.groupby("operation", as_index=False)[["sum_true", "sum_false"]].sum()
    approval_by_op["total"] = approval_by_op["sum_true"] + approval_by_op["sum_false"]
    approval_by_op = approval_by_op[approval_by_op["total"] > 0].copy()
    approval_by_op["approval_rate"] = (approval_by_op["sum_true"] / approval_by_op["total"] * 100).round(1)
    approval_by_op = approval_by_op.sort_values("approval_rate", ascending=False)


# ============================================================
# 12b. MoM/YoY
# ============================================================
comparison_parts = []
if period_mode == "За місяцями" and len(selected_months) == 1 and operation_mode == "Тотал":
    current_period = pd.Period(selected_months[0]); today_comp = now_kyiv(); prev_period = current_period - 1
    _full_cur = with_data(df[(df["month"] == str(current_period)) & (df["operation"] == "Тотал") & (df["date"] <= today_comp)])
    if not _full_cur.empty:
        last_data_day_period = min(int(_full_cur["date"].max().day), today_comp.day)
        if current_period.end_time <= today_comp:
            cur_sum = _full_cur["value"].sum()
            prev_sum = with_data(df[(df["month"] == str(prev_period)) & (df["operation"] == "Тотал")])["value"].sum()
            delta_prev = ((cur_sum - prev_sum) / prev_sum * 100) if prev_sum > 0 else None
        else:
            cur_sum = _full_cur[_full_cur["date"].dt.day <= last_data_day_period]["value"].sum()
            day_limit_prev = min(last_data_day_period, prev_period.days_in_month)
            prev_sum = with_data(df[(df["month"] == str(prev_period)) & (df["operation"] == "Тотал")
                                    & (df["date"].dt.day <= day_limit_prev)])["value"].sum()
            delta_prev = ((cur_sum - prev_sum) / prev_sum * 100) if prev_sum > 0 else None
        if delta_prev is not None: comparison_parts.append(f"Попер. міс: {delta_prev:+.1f}%")
        prev_year_period = pd.Period(year=current_period.year - 1, month=current_period.month, freq="M")
        has_prev_year = (df["month"] == str(prev_year_period)).any()
        if has_prev_year:
            if current_period.end_time <= today_comp:
                cur_sum = _full_cur["value"].sum()
                prev_year_sum = with_data(df[(df["month"] == str(prev_year_period)) & (df["operation"] == "Тотал")])["value"].sum()
                delta_year = ((cur_sum - prev_year_sum) / prev_year_sum * 100) if prev_year_sum > 0 else None
            else:
                cur_sum = _full_cur[_full_cur["date"].dt.day <= last_data_day_period]["value"].sum()
                day_limit_prev_year = min(last_data_day_period, prev_year_period.days_in_month)
                prev_year_sum = with_data(df[(df["month"] == str(prev_year_period)) & (df["operation"] == "Тотал")
                                              & (df["date"].dt.day <= day_limit_prev_year)])["value"].sum()
                delta_year = ((cur_sum - prev_year_sum) / prev_year_sum * 100) if prev_year_sum > 0 else None
            if delta_year is not None: comparison_parts.append(f"Мин. рік: {delta_year:+.1f}%")

comparison_text = "  ".join(comparison_parts) if comparison_parts else "—"


# ============================================================
# 12c. Частка дня у місяці
# ============================================================
def _add_pct_of_month(frame, monthly_lookup, value_col="value"):
    frame = frame.copy()
    frame["month_key"] = frame["date"].dt.strftime("%Y-%m")
    frame["month_total"] = [monthly_lookup.get((m, o), np.nan) for m, o in zip(frame["month_key"], frame["operation"])]
    frame["pct_of_month"] = np.where(frame["month_total"] > 0, frame[value_col] / frame["month_total"] * 100, np.nan)
    frame["label"] = frame["pct_of_month"].apply(lambda x: f"{x:.1f}%" if pd.notna(x) else "")
    return frame


_monthly_lookup_full = (df.assign(month_key=lambda d: d["date"].dt.strftime("%Y-%m"))
                         .groupby(["month_key", "operation"])["value"].sum().to_dict())


# ============================================================
# 13. Верстка
# ============================================================
def custom_metric(label, value, help_text=None, color=None, compact=False):
    safe_label = html.escape(str(label)); safe_value = html.escape(str(value))
    help_icon = ""
    if help_text: help_icon = f'<span class="help-icon" title="{html.escape(str(help_text))}">?</span>'
    words = safe_label.rsplit(' ', 1)
    if len(words) > 1: label_html = f'{words[0]} <span style="white-space: nowrap;">{words[1]}{help_icon}</span>'
    else: label_html = f'<span style="white-space: nowrap;">{words[0]}{help_icon}</span>'
    value_style = f' style="color:{html.escape(color)};"' if color else ""
    extra = " metric-compact" if compact else ""
    return f'<div class="metric-container{extra}"><div class="metric-label">{label_html}</div><div class="metric-value"{value_style}>{safe_value}</div></div>'


def metric_group(title, items):
    rows = ""
    for item in items:
        safe_label = html.escape(str(item["label"]))
        safe_value = str(item["value"]) if item.get("allow_html") else html.escape(str(item["value"]))
        help_icon = ""
        if item.get("help"): help_icon = f'<span class="help-icon" title="{html.escape(str(item["help"]))}">?</span>'
        words = safe_label.rsplit(' ', 1)
        if len(words) > 1: label_html = f'{words[0]} <span style="white-space: nowrap;">{words[1]}{help_icon}</span>'
        else: label_html = f'<span style="white-space: nowrap;">{words[0]}{help_icon}</span>'
        value_style = f' style="color:{html.escape(item["color"])};"' if item.get("color") else ""
        rows += (f'<div class="group-metric-row"><div class="group-metric-label">{label_html}</div>'
                 f'<div class="group-metric-value" title="{safe_value.replace("<br>", " ")}"{value_style}>{safe_value}</div></div>')
    return f'<div class="metric-group"><div class="metric-group-title">{html.escape(str(title))}</div>{rows}</div>'


def approval_rate_color(value):
    if value is None: return None
    if value >= APPROVAL_GOOD_THRESHOLD: return COLOR_GOOD
    if value >= APPROVAL_WARN_THRESHOLD: return COLOR_WARN
    return COLOR_BAD


def approval_rate_tier(value):
    if value >= APPROVAL_GOOD_THRESHOLD: return f"🟢 Високий (≥{APPROVAL_GOOD_THRESHOLD}%)"
    if value >= APPROVAL_WARN_THRESHOLD: return f"🟡 Середній ({APPROVAL_WARN_THRESHOLD}-{APPROVAL_GOOD_THRESHOLD}%)"
    return f"🔴 Низький (<{APPROVAL_WARN_THRESHOLD}%)"


APPROVAL_TIER_COLOR_MAP = {
    approval_rate_tier(100): COLOR_GOOD,
    approval_rate_tier(APPROVAL_WARN_THRESHOLD): COLOR_WARN,
    approval_rate_tier(0): COLOR_BAD,
}


def cv_color(value):
    if value is None or value <= 0: return None
    if value < 15: return COLOR_GOOD
    if value < 30: return COLOR_WARN
    return COLOR_BAD


st.markdown(f"""
<style>
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@500;600;700&display=swap');
    html, body, [class*="css"] {{ font-family: 'Inter', sans-serif; }}
    .stApp {{ background-color: {KPO_BG}; }}
    .metric-container {{
        background: {KPO_CARD_BG}; border: 1px solid {KPO_BORDER}; border-left: 3px solid {KPO_CYAN};
        border-radius: 8px; padding: 0.65rem 0.9rem; margin-bottom: 0.5rem;
        transition: border-left-color 0.15s ease; overflow: hidden; box-sizing: border-box;
    }}
    .metric-container:hover {{ border-left-color: {KPO_AMBER}; }}
    .metric-group {{
        background: {KPO_CARD_BG}; border: 1px solid {KPO_BORDER}; border-left: 3px solid {KPO_CYAN};
        border-radius: 8px; padding: 0.55rem 0.85rem; margin-bottom: 0.5rem;
        transition: border-left-color 0.15s ease; overflow: hidden; box-sizing: border-box;
    }}
    .metric-group:hover {{ border-left-color: {KPO_AMBER}; }}
    .metric-group-title {{
        font-size: 0.7rem !important; font-weight: 600; text-transform: uppercase;
        letter-spacing: 0.08em; color: #b4c2d3 !important;
        margin-bottom: 0.45rem; padding-bottom: 0.32rem; border-bottom: 1px solid {KPO_BORDER};
        white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
    }}
    .group-metric-row {{
        display: flex; justify-content: space-between; align-items: center;
        padding: 0.32rem 0; gap: 0.4rem; min-width: 0;
    }}
    .group-metric-row + .group-metric-row {{ border-top: 1px solid rgba(31, 39, 51, 0.55); }}
    .group-metric-label {{
        font-size: 0.8rem !important; font-weight: 500; color: #b8c3d1 !important;
        flex: 1 1 auto; min-width: 0; line-height: 1.3; letter-spacing: 0.01em;
        overflow-wrap: break-word; word-break: break-word;
    }}
    .group-metric-value {{
        font-family: 'JetBrains Mono', monospace; font-size: 0.9rem !important; font-weight: 700;
        color: {KPO_TEXT} !important; text-align: right; line-height: 1.2;
        flex: 0 0 auto; max-width: 62%; overflow: hidden; text-overflow: ellipsis;
    }}
    .metric-label {{
        font-size: 0.7rem !important; font-weight: 500; text-transform: uppercase;
        letter-spacing: 0.06em; margin-bottom: 0.3rem; color: {KPO_TEXT_MUTED} !important;
        overflow-wrap: break-word;
    }}
    .metric-value {{
        font-family: 'JetBrains Mono', monospace; font-size: 1.35rem !important; font-weight: 700;
        line-height: 1.2; color: {KPO_TEXT} !important; overflow: hidden; text-overflow: ellipsis;
    }}
    .help-icon {{
        display: inline-block; background: rgba(0, 217, 255, 0.15); border-radius: 50%;
        width: 16px; height: 16px; text-align: center; line-height: 16px; font-size: 0.7rem;
        color: {KPO_CYAN} !important; cursor: help; margin-left: 3px; flex-shrink: 0;
    }}
    .stTabs [data-baseweb="tab-list"] {{ gap: 4px; border-bottom: 1px solid {KPO_BORDER}; }}
    .stTabs [data-baseweb="tab"] {{
        background-color: transparent; border-radius: 6px 6px 0 0; color: {KPO_TEXT_MUTED};
        padding: 8px 18px; font-weight: 500;
    }}
    .stTabs [aria-selected="true"] {{
        background-color: {KPO_CARD_BG} !important; color: {KPO_CYAN} !important;
        border-bottom: 2px solid {KPO_CYAN} !important;
    }}
    section[data-testid="stSidebar"] {{ background-color: #0e131d; border-right: 1px solid {KPO_BORDER}; }}
    h1, h2, h3 {{ font-family: 'Inter', sans-serif; letter-spacing: -0.01em; }}
    hr {{ border-color: {KPO_BORDER} !important; }}
</style>
""", unsafe_allow_html=True)


def forecast_cards(title, forecast, help_base=None, help_min=None, help_max=None):
    if forecast is None: return
    c1, c2, c3 = st.columns(3)
    with c1: st.markdown(custom_metric(f"{title} (базовий)", f"{forecast['base']:,.0f}", help_base), unsafe_allow_html=True)
    with c2:
        if forecast.get("min") is not None:
            st.markdown(custom_metric(f"{title} (консервативний)", f"{forecast['min']:,.0f}", help_min), unsafe_allow_html=True)
        else: st.markdown(custom_metric(f"{title} (консервативний)", "—", help_min or "n < 2"), unsafe_allow_html=True)
    with c3:
        if forecast.get("max") is not None:
            st.markdown(custom_metric(f"{title} (оптимістичний)", f"{forecast['max']:,.0f}", help_max), unsafe_allow_html=True)
        else: st.markdown(custom_metric(f"{title} (оптимістичний)", "—", help_max or "n < 2"), unsafe_allow_html=True)


# ============================================================
# 14. Вкладки
# ============================================================
tab1, tab2, tab3, tab4, tab5 = st.tabs(
    ["📊 Overview", "📈 Динаміка", "🧩 Операції", "📅 Навантаження", "🆚 Порівняння періодів"]
)


# ============================================================
# TAB 1: OVERVIEW
# ============================================================
with tab1:
    total_weekday_val = float(filtered_stats[weekday_mask]["value"].sum()) if len(filtered_stats) else 0.0
    total_weekend_val = float(filtered_stats[weekend_mask]["value"].sum()) if len(filtered_stats) else 0.0

    filtered_wd = filtered[filtered["is_weekend"] == False]
    filtered_we = filtered[filtered["is_weekend"] == True]
    cv_wd = calc_stability(filtered_wd, daily_avg_weekday)[1] if daily_avg_weekday and daily_avg_weekday > 0 else 0.0
    cv_we = calc_stability(filtered_we, daily_avg_weekend)[1] if daily_avg_weekend and daily_avg_weekend > 0 else 0.0

    wd_stats = filtered_stats[filtered_stats["is_weekend"] == False]
    sum_true_wd = float(wd_stats["sum_true"].sum()) if not wd_stats.empty else 0.0
    sum_false_wd = float(wd_stats["sum_false"].sum()) if not wd_stats.empty else 0.0
    total_ratio_wd = sum_true_wd + sum_false_wd
    approval_rate_wd = (sum_true_wd / total_ratio_wd * 100) if total_ratio_wd > 0 else 0
    approval_rate_wd_str = f"{approval_rate_wd:.1f}%" if total_ratio_wd > 0 else "—"

    we_stats = filtered_stats[filtered_stats["is_weekend"] == True]
    sum_true_we = float(we_stats["sum_true"].sum()) if not we_stats.empty else 0.0
    sum_false_we = float(we_stats["sum_false"].sum()) if not we_stats.empty else 0.0
    total_ratio_we = sum_true_we + sum_false_we
    approval_rate_we = (sum_true_we / total_ratio_we * 100) if total_ratio_we > 0 else 0
    approval_rate_we_str = f"{approval_rate_we:.1f}%" if total_ratio_we > 0 else "—"

    col1, col2, col3, col4, col5 = st.columns([1, 1, 1, 1, 1.3])

    with col1:
        st.markdown(metric_group("Всього", [
            {"label": "Загальна кількість", "value": f"{total_value:,.0f}", "help": "Загальна кількість операцій"},
            {"label": "Будні", "value": f"{total_weekday_val:,.0f}", "help": "Сума за будні"},
            {"label": "Вихідні", "value": f"{total_weekend_val:,.0f}", "help": "Сума за вихідні"},
        ]), unsafe_allow_html=True)

    with col2:
        avg_weekday_str = f"{daily_avg_weekday:.0f}" if daily_avg_weekday is not None and not pd.isna(daily_avg_weekday) else "—"
        avg_weekend_str = f"{daily_avg_weekend:.0f}" if daily_avg_weekend is not None and not pd.isna(daily_avg_weekend) else "—"
        st.markdown(metric_group("Середнє", [
            {"label": "За день", "value": f"{daily_avg:.0f}", "help": "Сума / кількість днів з даними"},
            {"label": "Будні", "value": avg_weekday_str, "help": "Середнє у будні"},
            {"label": "Вихідні", "value": avg_weekend_str, "help": "Середнє у вихідні"},
        ]), unsafe_allow_html=True)

    with col3:
        st.markdown(metric_group("Стабільність (CV)", [
            {"label": "Загальна", "value": f"{cv:.1f}%" if cv > 0 else "—",
             "color": cv_color(cv if cv > 0 else None), "help": "🟢 <15% 🟡 15-30% 🔴 >30%"},
            {"label": "Будні", "value": f"{cv_wd:.1f}%" if cv_wd > 0 else "—", "color": cv_color(cv_wd if cv_wd > 0 else None)},
            {"label": "Вихідні", "value": f"{cv_we:.1f}%" if cv_we > 0 else "—", "color": cv_color(cv_we if cv_we > 0 else None)},
        ]), unsafe_allow_html=True)

    with col4:
        st.markdown(metric_group("Коефіцієнт погоджень", [
            {"label": "Загальний", "value": approval_rate_str,
             "color": approval_rate_color(approval_rate_val if approval_rate_available else None),
             "help": f"🟢 ≥{APPROVAL_GOOD_THRESHOLD}% 🟡 {APPROVAL_WARN_THRESHOLD}-{APPROVAL_GOOD_THRESHOLD}% 🔴 <{APPROVAL_WARN_THRESHOLD}%"},
            {"label": "Будні", "value": approval_rate_wd_str, "color": approval_rate_color(approval_rate_wd if total_ratio_wd > 0 else None)},
            {"label": "Вихідні", "value": approval_rate_we_str, "color": approval_rate_color(approval_rate_we if total_ratio_we > 0 else None)},
        ]), unsafe_allow_html=True)

    with col5:
        peak_display = f"{peak:,.0f}" if peak > 0 else "—"
        if peak_date is not None: peak_display += f" ({peak_date.strftime('%d.%m')})"
        if busiest_weekday:
            day_ua = WEEKDAY_UA.get(busiest_weekday, busiest_weekday)
            wd_val = f"{day_ua} ({busiest_weekday_val:.0f}/д)"; wd_help = "День тижня з найвищим середнім"
        else: wd_val, wd_help = "—", None
        if busiest_op:
            display_name = busiest_op if len(busiest_op) <= 12 else busiest_op[:10] + "…"
            op_val = f"{display_name} ({busiest_op_val:,.0f})"; op_help = f"{busiest_op} — {busiest_op_val:,.0f}"
        else: op_val, op_help = "—", None
        comp_val = comparison_text.replace("  ", "<br>") if comparison_text != "—" else "—"
        st.markdown(metric_group("Піки, лідери й порівняння", [
            {"label": "Пік за день", "value": peak_display, "help": "Найбільша кількість за день"},
            {"label": "Пік / середнє", "value": f"{peak_avg_ratio:.2f}×", "help": "У скільки разів пік > середнє"},
            {"label": "Найактивніший день", "value": wd_val, "help": wd_help},
            {"label": "Найактивніша операція", "value": op_val, "help": op_help},
            {"label": "Порівняння", "value": comp_val,
             "help": "Порівняння з попереднім місяцем та аналогічним місяцем минулого року (без фільтрів по днях)",
             "allow_html": True},
        ]), unsafe_allow_html=True)

    st.divider()
    st.subheader("💡 Інсайти")
    insights = []
    if approval_rate_available:
        if approval_rate_val >= APPROVAL_GOOD_THRESHOLD: insights.append(f"🟢 Коефіцієнт погоджень **{approval_rate_str}** — вище {APPROVAL_GOOD_THRESHOLD}%.")
        elif approval_rate_val >= APPROVAL_WARN_THRESHOLD: insights.append(f"🟡 Коефіцієнт погоджень **{approval_rate_str}** — у середній зоні.")
        else: insights.append(f"🔴 Коефіцієнт погоджень **{approval_rate_str}** — нижче {APPROVAL_WARN_THRESHOLD}%.")
    else: insights.append("ℹ️ Коефіцієнт погоджень недоступний (немає TRUE/FALSE).")
    if not approval_by_op.empty and len(approval_by_op) > 1:
        best_row = approval_by_op.iloc[0]; worst_row = approval_by_op.iloc[-1]
        if best_row["operation"] != worst_row["operation"]:
            insights.append(f"🧩 Найкращий % — **{best_row['operation']}** ({best_row['approval_rate']:.1f}%), найгірший — **{worst_row['operation']}** ({worst_row['approval_rate']:.1f}%).")
    if cv > 0: insights.append(f"{cv_interp} (CV = {cv:.1f}%).")
    if busiest_weekday:
        day_ua = WEEKDAY_UA.get(busiest_weekday, busiest_weekday)
        insights.append(f"📅 Найбільше навантаження — **{day_ua}** ({busiest_weekday_val:.0f} оп/день).")
    if busiest_op: insights.append(f"📈 Найактивніша операція — **{busiest_op}** ({busiest_op_val:,.0f}).")
    if peak_avg_ratio >= 2: insights.append(f"⚠️ Пік у **{peak_avg_ratio:.1f}×** перевищує середнє.")
    if period_mode == "За місяцями" and comparison_text != "—": insights.append(f"🔄 Порівняння: {comparison_text}.")
    for i in insights: st.markdown(f"- {i}")

    st.divider()
    if period_mode != "За місяцями": st.info("📊 Прогнози доступні лише в режимі 'За місяцями' з одним обраним місяцем.")
    elif len(selected_months) != 1: st.info("📊 Прогнози доступні для одного обраного місяця.")
    else:
        st.caption("ℹ️ Прогнози рахуються на повному періоді, без урахування фільтрів по днях.")
        forecast_target_options = ["Тотал"] + all_ops
        forecast_target = st.selectbox("Прогнозувати для:", options=forecast_target_options, index=0,
                                        help="Прогноз для Тоталу або окремої операції.")
        stat_forecast, season_forecast = forecast_scenarios(df[df["operation"] == forecast_target], selected_months[0])
        if stat_forecast and stat_forecast.get("n_days", 0) < 3:
            st.warning(f"⚠️ Прогноз на основі лише {stat_forecast['n_days']} днів — орієнтовно.")
        if stat_forecast or season_forecast:
            st.subheader(f"📊 Прогнози на поточний місяць — {forecast_target}")
            if stat_forecast:
                st.markdown("**📈 Статистичний прогноз**")
                forecast_cards("Стат.", stat_forecast, help_base="факт + середнє × залишок",
                                help_min="факт + (середнє − 0.5×σ) × залишок", help_max="факт + (середнє + 0.5×σ) × залишок")
            if season_forecast:
                st.markdown("**📅 Сезонний прогноз**")
                with st.expander("🔍 Деталі сезонного прогнозу"):
                    st.write(f"**Період:** {season_forecast['prev_period']}")
                    st.write(f"**Факт:** {season_forecast['fact']:,.0f}")
                    st.write(f"**Минулий рік:** {season_forecast['prev_fact_sum']:,.0f}")
                    st.write(f"**Коеф. сезонності:** {season_forecast['seasonality_factor']:.3f}")
                    st.write(f"**Загальний прогноз:** {season_forecast['base']:,.0f}")
                forecast_cards("Сезон.", season_forecast, help_base="факт + залишок × коеф.",
                                help_min="факт + 0.9 × прогноз", help_max="факт + 1.1 × прогноз")
        else: st.info("Прогноз недоступний: немає фактичних даних за поточний місяць.")

    st.divider()
    st.subheader("📈 Динаміка за період")
    daily = filtered.groupby("date", as_index=False).agg(
        value=("value", "sum"), sum_true=("sum_true", "sum"), sum_false=("sum_false", "sum"))
    daily = daily.sort_values("date").reset_index(drop=True)

    if operation_mode == "Тотал":
        monthly_lookup_agg = (df[df["operation"] == "Тотал"]
                              .assign(month_key=lambda d: d["date"].dt.strftime("%Y-%m"))
                              .groupby("month_key")["value"].sum().to_dict())
    else:
        monthly_lookup_agg = (df[df["operation"].isin(selected_operations)]
                              .assign(month_key=lambda d: d["date"].dt.strftime("%Y-%m"))
                              .groupby("month_key")["value"].sum().to_dict())

    daily["month_key"] = daily["date"].dt.strftime("%Y-%m")
    daily["month_total"] = daily["month_key"].map(monthly_lookup_agg).fillna(0)
    daily["pct_of_month"] = np.where(daily["month_total"] > 0, daily["value"] / daily["month_total"] * 100, np.nan)
    daily["pct_true_day"] = np.where(daily["value"] > 0, daily["sum_true"] / daily["value"] * 100, np.nan)
    daily["pct_false_day"] = np.where(daily["value"] > 0, daily["sum_false"] / daily["value"] * 100, np.nan)

    max_total = float(daily["value"].max()) if not daily.empty else 0
    y_max = max_total * 1.18 if max_total > 0 else 10
    label_threshold = max(2.0, max_total * 0.04)

    false_inside, true_inside = [], []
    for _, r in daily.iterrows():
        false_inside.append(f"<b>{r['sum_false']:.0f}</b><br>{r['pct_false_day']:.0f}%"
                            if r["sum_false"] >= label_threshold and pd.notna(r["pct_false_day"])
                            else (f"<b>{r['sum_false']:.0f}</b>" if r["sum_false"] >= label_threshold else ""))
        true_inside.append(f"<b>{r['sum_true']:.0f}</b><br>{r['pct_true_day']:.0f}%"
                           if r["sum_true"] >= label_threshold and pd.notna(r["pct_true_day"])
                           else (f"<b>{r['sum_true']:.0f}</b>" if r["sum_true"] >= label_threshold else ""))

    fig_overview = go.Figure()
    fig_overview.add_trace(go.Bar(x=daily["date"], y=daily["sum_false"], name="Відхилено (FALSE)",
        marker_color=KPO_RED, text=false_inside, textposition="inside", insidetextanchor="middle",
        textfont=dict(size=14, color="#ffffff", family="Inter, sans-serif"),
        hovertemplate="Відхилено: %{y:.0f}<br>Частка: %{customdata:.0f}%<extra></extra>",
        customdata=daily["pct_false_day"].fillna(0).values, cliponaxis=False))
    fig_overview.add_trace(go.Bar(x=daily["date"], y=daily["sum_true"], name="Погоджено (TRUE)",
        marker_color=KPO_GREEN, text=true_inside, textposition="inside", insidetextanchor="middle",
        textfont=dict(size=14, color="#ffffff", family="Inter, sans-serif"),
        hovertemplate="Погоджено: %{y:.0f}<br>Частка: %{customdata:.0f}%<extra></extra>",
        customdata=daily["pct_true_day"].fillna(0).values, cliponaxis=False))
    total_labels = [f"{r['value']:.0f}<br>{r['pct_of_month']:.1f}%" if pd.notna(r["pct_of_month"]) else f"{r['value']:.0f}"
                    for _, r in daily.iterrows()]
    fig_overview.add_trace(go.Scatter(x=daily["date"], y=daily["value"], mode="text", text=total_labels,
        textposition="top center", textfont=dict(size=12, color="#e6edf3", family="Inter, sans-serif"),
        showlegend=False, hoverinfo="skip"))

    small_annotations = []
    for _, r in daily.iterrows():
        if 0 < r["sum_false"] < label_threshold:
            txt = f"{r['sum_false']:.0f} ({r['pct_false_day']:.0f}%)" if pd.notna(r["pct_false_day"]) else f"{r['sum_false']:.0f}"
            small_annotations.append(dict(x=r["date"], y=r["sum_false"] / 2, text=txt, showarrow=False,
                xshift=-18, xanchor="right", font=dict(size=11, color=KPO_RED), bgcolor=KPO_BG,
                bordercolor=KPO_RED, borderwidth=1, borderpad=2))
        if 0 < r["sum_true"] < label_threshold:
            txt = f"{r['sum_true']:.0f} ({r['pct_true_day']:.0f}%)" if pd.notna(r["pct_true_day"]) else f"{r['sum_true']:.0f}"
            small_annotations.append(dict(x=r["date"], y=r["sum_false"] + r["sum_true"] / 2, text=txt, showarrow=False,
                xshift=18, xanchor="left", font=dict(size=11, color=KPO_GREEN), bgcolor=KPO_BG,
                bordercolor=KPO_GREEN, borderwidth=1, borderpad=2))

    fig_overview.update_layout(barmode="stack", height=420, margin=dict(l=10, r=10, t=30, b=10),
        bargap=0.35, legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        hovermode="x unified", annotations=small_annotations)
    fig_overview.update_xaxes(tickformat="%d.%m", title_text="Дата")
    fig_overview.update_yaxes(title_text="Кількість", range=[0, y_max])
    st.plotly_chart(fig_overview, use_container_width=True)


# ============================================================
# TAB 2: ДИНАМІКА
# ============================================================
with tab2:
    st.subheader("📈 Детальна динаміка")

    prev_year_range = None
    if compare_prev_year and _prev_year_available:
        if period_mode == "За місяцями" and selected_months:
            s_months = sorted(selected_months)
            ps = pd.Period(s_months[0]) - 12; pe = pd.Period(s_months[-1]) - 12
            prev_year_range = (ps.start_time.normalize(), pe.end_time.normalize())
        elif custom_range is not None:
            prev_year_range = (custom_range[0] - pd.DateOffset(years=1),
                                custom_range[1] - pd.DateOffset(years=1))

    if operation_mode == "Тотал":
        daily = filtered.groupby("date")["value"].sum().reset_index()
        daily["operation"] = "Тотал"
        daily = _add_pct_of_month(daily, _monthly_lookup_full)
        fig_daily_detailed = px.line(daily, x="date", y="value", markers=True, text="label",
                                      labels={"date": "Дата", "value": "Кількість"}, title="Щоденна динаміка")
        _style_line_traces(fig_daily_detailed, font_size=11, font_color=KPO_TEXT, show_text=True)
        fig_daily_detailed.update_xaxes(tickformat="%d.%m", title_text="Дата")

        if smooth_enabled:
            daily["value_smooth"] = daily["value"].rolling(window=smooth_window, min_periods=1, center=True).mean()
            fig_daily_detailed.add_scatter(x=daily["date"], y=daily["value_smooth"], mode="lines",
                name=f"Ковзне середнє ({smooth_window} дн.)", line=dict(color=KPO_AMBER, width=3))

        anomalies = detect_anomalies(filtered, window=14, threshold=3.0)
        if not anomalies.empty:
            anomaly_points = anomalies[anomalies["is_anomaly"]]
            if not anomaly_points.empty:
                fig_daily_detailed.add_scatter(x=anomaly_points["date"], y=anomaly_points["value"],
                    mode="markers", marker=dict(color=KPO_RED, size=10, symbol="x"), name="Аномалія")
    else:
        plot_df = _add_pct_of_month(filtered, _monthly_lookup_full)
        fig_daily_detailed = px.line(plot_df, x="date", y="value", color="operation", markers=True, text="label",
                                      labels={"date": "Дата", "value": "Кількість", "operation": "Операція"},
                                      title="Динаміка вибраних операцій")
        _style_line_traces(fig_daily_detailed, font_size=10, show_text=True)
        fig_daily_detailed.update_xaxes(tickformat="%d.%m", title_text="Дата")

    if operation_mode == "Тотал":
        _all_dates_in_range = pd.date_range(filtered["date"].min(), filtered["date"].max(), freq="D") if not filtered.empty else pd.DatetimeIndex([])
        _present_dates = set(filtered["date"])
        _empty_dates = [d for d in _all_dates_in_range if d not in _present_dates]
        if _empty_dates:
            fig_daily_detailed.add_scatter(x=_empty_dates, y=[0] * len(_empty_dates), mode="markers",
                marker=dict(color=KPO_TEXT_MUTED, size=10, symbol="circle-open", line=dict(width=2)),
                name="Порожній день (немає даних)", hovertemplate="%{x|%d.%m}: немає даних<extra></extra>")

    if prev_year_range is not None and operation_mode == "Тотал":
        prev_year_df = with_data(df[(df["operation"] == "Тотал")
                                     & (df["date"] >= prev_year_range[0])
                                     & (df["date"] <= prev_year_range[1])]).copy()
        if not prev_year_df.empty:
            prev_daily = prev_year_df.groupby("date")["value"].sum().reset_index().sort_values("date")
            prev_daily["date_shifted"] = prev_daily["date"] + pd.DateOffset(years=1)
            prev_label = (pd.Period(selected_months[0]).year - 1
                          if period_mode == "За місяцями" and selected_months else prev_year_range[0].year)
            fig_daily_detailed.add_scatter(x=prev_daily["date_shifted"], y=prev_daily["value"],
                mode="lines+markers", name=f"Торік ({prev_label})",
                line=dict(color=KPO_BLUE, width=2, dash="dash"), opacity=0.5,
                marker=dict(size=6, opacity=0.5))

    fig_daily_detailed.update_layout(height=440, hovermode="x unified", margin=dict(l=10, r=10, t=40, b=10))
    st.plotly_chart(fig_daily_detailed, use_container_width=True)

    if operation_mode == "Тотал":
        st.subheader("📊 Порівняння по роках (YoY)")
        yoy_data = with_data(df[df["operation"] == "Тотал"]).copy()
        yoy_data = yoy_data[yoy_data["year"].isin(selected_years if selected_years else sorted(df["year"].unique()))]
        yoy_monthly = yoy_data.groupby(["year", "month"])["value"].sum().reset_index()
        yoy_monthly["month_num"] = yoy_monthly["month"].apply(lambda x: pd.Period(x).month)
        yoy_monthly["month_label"] = yoy_monthly["month"].apply(lambda x: pd.Period(x).strftime("%b"))

        # Пунктир: весь минулий рік для найновішого року на графіку (лише коли увімкнено чекбокс)
        prev_yoy_monthly = None
        if compare_prev_year and not yoy_monthly.empty:
            latest_year = int(yoy_monthly["year"].max())
            prev_df = with_data(df[(df["operation"] == "Тотал") & (df["year"] == latest_year - 1)]).copy()
            if not prev_df.empty:
                prev_yoy_monthly = prev_df.groupby("month")["value"].sum().reset_index()
                prev_yoy_monthly["month_num"] = prev_yoy_monthly["month"].apply(lambda x: pd.Period(x).month)
                prev_yoy_monthly["month_label"] = prev_yoy_monthly["month"].apply(lambda x: pd.Period(x).strftime("%b"))
                prev_yoy_monthly["year"] = latest_year - 1

        # Порядок осі X: 12 місяців, щоб пунктир і суцільні лінії були на одній шкалі
        axis_frames = [yoy_monthly] + ([prev_yoy_monthly] if prev_yoy_monthly is not None else [])
        all_monthly = pd.concat(axis_frames, ignore_index=True).sort_values(["year", "month_num"])
        month_axis_order = all_monthly.drop_duplicates("month_num").sort_values("month_num")["month_label"].tolist()

        yoy_monthly = yoy_monthly.sort_values(["year", "month_num"])
        fig_yoy = px.line(yoy_monthly, x="month_label", y="value", color="year", markers=True,
                          labels={"month_label": "Місяць", "value": "Кількість", "year": "Рік"},
                          title="Порівняння місячних сум по роках",
                          category_orders={"month_label": month_axis_order})

        if prev_yoy_monthly is not None:
            prev_yoy_monthly = prev_yoy_monthly.sort_values("month_num")
            fig_yoy.add_scatter(x=prev_yoy_monthly["month_label"], y=prev_yoy_monthly["value"],
                mode="lines+markers", name=f"Торік ({latest_year - 1}), весь рік",
                line=dict(color=KPO_BLUE, width=2, dash="dash"), opacity=0.6,
                marker=dict(size=6, opacity=0.6))

        fig_yoy.update_layout(height=380, margin=dict(l=10, r=10, t=20, b=10))
        st.plotly_chart(fig_yoy, use_container_width=True)

    st.subheader("📈 Накопичувальна сума за період")
    if operation_mode == "Тотал":
        cumsum = filtered.groupby("date")["value"].sum().sort_index().cumsum().reset_index()
        cumsum.columns = ["date", "cumulative"]
        fig_cum = px.line(cumsum, x="date", y="cumulative", markers=True,
                          labels={"date": "Дата", "cumulative": "Накопичена кількість"},
                          title="Накопичувальна сума (Тотал)")
        if prev_year_range is not None:
            prev_cum_df = with_data(df[(df["operation"] == "Тотал")
                                         & (df["date"] >= prev_year_range[0])
                                         & (df["date"] <= prev_year_range[1])]).copy()
            if not prev_cum_df.empty:
                prev_cum = prev_cum_df.groupby("date")["value"].sum().sort_index().cumsum().reset_index()
                prev_cum.columns = ["date", "cumulative"]
                prev_cum["date_shifted"] = prev_cum["date"] + pd.DateOffset(years=1)
                prev_label = (pd.Period(selected_months[0]).year - 1
                              if period_mode == "За місяцями" and selected_months else prev_year_range[0].year)
                fig_cum.add_scatter(x=prev_cum["date_shifted"], y=prev_cum["cumulative"],
                    mode="lines+markers", name=f"Торік ({prev_label})",
                    line=dict(color=KPO_BLUE, width=2, dash="dash"), opacity=0.5,
                    marker=dict(size=6, opacity=0.5))
    else:
        cumsum_df = (filtered[filtered["operation"] != "Тотал"].sort_values("date")
                     .groupby(["operation", "date"], as_index=False)["value"].sum())
        cumsum_df["cumulative"] = cumsum_df.groupby("operation")["value"].cumsum()
        fig_cum = px.line(cumsum_df, x="date", y="cumulative", color="operation", markers=True,
                          labels={"date": "Дата", "cumulative": "Накопичена кількість", "operation": "Операція"},
                          title="Накопичувальна сума по операціях")

    fig_cum.update_xaxes(tickformat="%d.%m", title_text="Дата")
    fig_cum.update_layout(height=380, margin=dict(l=10, r=10, t=20, b=10))
    st.plotly_chart(fig_cum, use_container_width=True)

    st.subheader("🔍 Аномальні дні")
    anomalies = detect_anomalies(filtered, window=14, threshold=3.0)
    if not anomalies.empty:
        anomaly_points = anomalies[anomalies["is_anomaly"]]
        if not anomaly_points.empty:
            ap = anomaly_points.copy()
            ap["date_str"] = ap["date"].dt.strftime("%d.%m.%Y")
            ap["deviation"] = ((ap["value"] - ap["rolling_median"]) / ap["rolling_median"] * 100).round(1)
            ap["type"] = ap["deviation"].apply(lambda x: "🔴 Високий" if x > 10 else "🔵 Низький" if x < -10 else "🟡 Помірний")
            ap = ap.sort_values("date", ascending=False)
            st.dataframe(ap[["date_str", "value", "rolling_median", "deviation", "type", "z_score"]],
                column_config={"date_str": "Дата", "value": "Значення", "rolling_median": "Медіана (14 днів)",
                               "deviation": "Відхилення, %", "type": "Тип",
                               "z_score": st.column_config.NumberColumn("Z-score (MAD)", help="Кількість MAD від медіани")},
                use_container_width=True, hide_index=True)
        else: st.info("Аномальних днів не виявлено.")
    else: st.info("Недостатньо даних для виявлення аномалій.")


# ============================================================
# TAB 3: ОПЕРАЦІЇ
# ============================================================
with tab3:
    st.subheader("🧩 Аналіз операцій")
    st.subheader("✅ Коефіцієнт погоджень по операціях")
    if not approval_by_op.empty:
        total_true = float(filtered_stats["sum_true"].sum()) if not filtered_stats.empty else 0.0
        total_false = float(filtered_stats["sum_false"].sum()) if not filtered_stats.empty else 0.0
        total_ratio_ops = total_true + total_false
        total_rate = (total_true / total_ratio_ops * 100) if total_ratio_ops > 0 else None

        approval_by_op_display = approval_by_op.copy()
        approval_by_op_display["tier"] = approval_by_op_display["approval_rate"].apply(approval_rate_tier)
        fig_approval = px.bar(approval_by_op_display, x="operation", y="approval_rate",
            color="tier", color_discrete_map=APPROVAL_TIER_COLOR_MAP,
            text=approval_by_op_display["approval_rate"].astype(str) + "%",
            labels={"operation": "Операція", "approval_rate": "Коефіцієнт, %", "tier": "Категорія"},
            title="Коефіцієнт погоджень по операціях",
            category_orders={"operation": approval_by_op_display["operation"].tolist()})
        fig_approval.update_traces(textposition="outside", width=0.55)
        max_rate = approval_by_op_display["approval_rate"].max()
        fig_approval.update_layout(height=360, margin=dict(l=10, r=10, t=20, b=10),
            yaxis=dict(range=[0, max(105, max_rate * 1.12)]), bargap=0.45, legend_title_text="Категорія")

        if total_rate is not None:
            total_rate_color = approval_rate_color(total_rate) or KPO_RED
            fig_approval.add_hline(y=total_rate, line_dash="dash", line_color=total_rate_color, line_width=4,
                annotation_text=f"Тотал: {total_rate:.1f}%", annotation_position="top right",
                annotation_font=dict(size=16, color="white"))
            fig_approval.add_trace(go.Scatter(x=[None], y=[None], mode='lines',
                line=dict(color=total_rate_color, width=4, dash='dash'),
                name=f"Тотал: {total_rate:.1f}%", showlegend=True))

        st.plotly_chart(fig_approval, use_container_width=True)

        with st.expander("📋 Таблиця по операціях", expanded=False):
            st.dataframe(approval_by_op[["operation", "sum_true", "sum_false", "total", "approval_rate"]].rename(columns={
                "operation": "Операція", "sum_true": "Погоджено (TRUE)", "sum_false": "Відхилено (FALSE)",
                "total": "Всього", "approval_rate": "Коефіцієнт, %"}),
                use_container_width=True, hide_index=True)
    else:
        st.info("Немає даних для розрахунку коефіцієнта погоджень за вибраний період.")

    st.subheader("🌡️ Теплова карта коефіцієнта погоджень (Операція × Місяць)")
    st.caption("ℹ️ Карта показує повний період без урахування фільтрів по днях.")

    if period_mode == "За місяцями":
        _heat_period_mask = df["year"].isin(selected_years) & df["month"].isin(selected_months)
    else:
        _heat_period_mask = (df["date"] >= custom_range[0]) & (df["date"] <= custom_range[1])
    _heat_period_mask &= df["date"] <= today

    tf_heat = df[_heat_period_mask].groupby(["month", "operation"], as_index=False)[["sum_true", "sum_false"]].sum()
    if not tf_heat.empty:
        tf_heat["total"] = tf_heat["sum_true"] + tf_heat["sum_false"]
        tf_heat = tf_heat[tf_heat["total"] > 0]
        if not tf_heat.empty:
            tf_heat["rate"] = tf_heat["sum_true"] / tf_heat["total"] * 100
            tf_heat["month_label"] = tf_heat["month"].apply(lambda x: pd.Period(x).strftime("%m.%Y"))
            heat_rate_pivot = tf_heat.pivot_table(index="month_label", columns="operation", values="rate", aggfunc="mean")
            cols = heat_rate_pivot.columns.tolist()
            if "Тотал" in cols:
                cols.remove("Тотал"); cols = ["Тотал"] + cols
                heat_rate_pivot = heat_rate_pivot[cols]
            def _sort_ml(ms):
                try: return datetime.strptime(ms, "%m.%Y")
                except Exception: return datetime(1900, 1, 1)
            heat_rate_pivot = heat_rate_pivot.reindex(sorted(heat_rate_pivot.index, key=_sort_ml))

            fig_approval_heat = px.imshow(heat_rate_pivot, text_auto=".1f", aspect="auto",
                labels=dict(x="Операція", y="Місяць", color="Коефіцієнт, %"),
                color_continuous_scale="RdYlGn", zmin=0, zmax=100)
            heatmap_height = max(420, min(2400, len(heat_rate_pivot.index) * 38))
            fig_approval_heat.update_layout(height=heatmap_height, margin=dict(l=10, r=10, t=20, b=10))
            fs = 12 if len(heat_rate_pivot.index) <= 12 else (10 if len(heat_rate_pivot.index) <= 24 else 8)
            fig_approval_heat.update_traces(textfont=dict(size=fs))
            st.plotly_chart(fig_approval_heat, use_container_width=True)
            st.caption(f"🔴 <{APPROVAL_WARN_THRESHOLD}% 🟡 {APPROVAL_WARN_THRESHOLD}-{APPROVAL_GOOD_THRESHOLD}% 🟢 >{APPROVAL_GOOD_THRESHOLD}%")
        else: st.info("Немає даних для теплової карти за вибраний період.")
    else: st.info("Немає даних для теплової карти.")

    st.divider()

    if operation_mode == "Тотал": ops_data = period_ops
    else: ops_data = with_data(filtered[filtered["operation"] != "Тотал"])

    if not ops_data.empty:
        st.subheader("📊 Структура операцій (за період)")
        ops_structure = ops_data.groupby("operation")["value"].sum().reset_index().sort_values("value", ascending=False)
        ops_structure["percent"] = (ops_structure["value"] / ops_structure["value"].sum() * 100).round(1)
        ops_structure["text"] = ops_structure["percent"].astype(str) + "%"
        fig_ops_structure = px.bar(ops_structure, x="value", y="operation", text="text", orientation="h",
            labels={"value": "Кількість", "operation": "Операція"}, title="Структура за період")
        fig_ops_structure.update_traces(textposition="outside")
        fig_ops_structure.update_layout(height=360, margin=dict(l=10, r=10, t=20, b=10),
            yaxis={"categoryorder": "total descending"})
        st.plotly_chart(fig_ops_structure, use_container_width=True)

        st.subheader("📈 Динаміка структури операцій по місяцях")
        ops_monthly = ops_data.groupby(["month", "operation"])["value"].sum().reset_index()
        ops_monthly["month_label"] = ops_monthly["month"].apply(lambda x: pd.Period(x).strftime("%m.%Y"))
        fig_stacked = px.bar(ops_monthly, x="month_label", y="value", color="operation", barmode="stack",
            labels={"month_label": "Місяць", "value": "Кількість", "operation": "Операція"},
            title="Структура операцій по місяцях")
        fig_stacked.update_layout(height=400, margin=dict(l=10, r=10, t=20, b=10))
        st.plotly_chart(fig_stacked, use_container_width=True)

        st.subheader("📊 Pareto аналіз операцій")
        pareto_data = ops_structure.copy().sort_values("value", ascending=False)
        pareto_data["cumulative_percent"] = pareto_data["percent"].cumsum()
        fig_pareto = go.Figure()
        fig_pareto.add_trace(go.Bar(x=pareto_data["operation"], y=pareto_data["value"], name="Кількість", marker_color=KPO_CYAN, yaxis="y"))
        fig_pareto.add_trace(go.Scatter(x=pareto_data["operation"], y=pareto_data["cumulative_percent"],
            name="Накопичувальна частка, %", mode="lines+markers", marker_color=KPO_AMBER, yaxis="y2"))
        fig_pareto.add_hline(y=80, line_dash="dash", line_color="gray", annotation_text="80%", annotation_position="top right")
        fig_pareto.update_layout(title="Pareto операцій", xaxis_title="Операція",
            yaxis=dict(title="Кількість", side="left", showgrid=True),
            yaxis2=dict(title="Накопичувальна частка, %", overlaying="y", side="right", range=[0, 100]),
            legend=dict(x=0.8, y=0.9), height=400, margin=dict(l=10, r=10, t=20, b=10))
        st.plotly_chart(fig_pareto, use_container_width=True)

        if operation_mode != "Тотал" and len(selected_operations) > 1:
            st.subheader("📈 Порівняння вибраних операцій")
            fig_compare_ops = px.line(filtered, x="date", y="value", color="operation", markers=True,
                labels={"date": "Дата", "value": "Кількість", "operation": "Операція"},
                title="Динаміка вибраних операцій")
            fig_compare_ops.update_xaxes(tickformat="%d.%m", title_text="Дата")
            fig_compare_ops.update_layout(height=400, margin=dict(l=10, r=10, t=20, b=10))
            st.plotly_chart(fig_compare_ops, use_container_width=True)
    else:
        st.info("Немає даних про окремі операції для вибраного періоду.")


# ============================================================
# TAB 4: НАВАНТАЖЕННЯ
# ============================================================
with tab4:
    st.subheader("📅 Аналіз навантаження")
    st.subheader("📊 Середнє навантаження за днями тижня")
    daily_sum = filtered_stats.groupby("date")["value"].sum().reset_index()
    daily_sum["weekday_ua"] = daily_sum["date"].dt.day_name().map(WEEKDAY_UA)
    weekday_avg = daily_sum.groupby("weekday_ua")["value"].mean().reindex(WEEKDAY_ORDER_UA).reset_index()
    weekday_avg.columns = ["weekday", "avg_value"]
    weekday_avg["avg_value"] = weekday_avg["avg_value"].fillna(0)
    fig_weekday_avg = px.bar(weekday_avg, x="weekday", y="avg_value",
        text=weekday_avg["avg_value"].round(1).astype(str),
        labels={"weekday": "День тижня", "avg_value": "Середня кількість"},
        title="Середня кількість операцій по днях тижня")
    fig_weekday_avg.update_traces(textposition="outside")
    fig_weekday_avg.update_layout(height=360, margin=dict(l=10, r=10, t=20, b=10))
    st.plotly_chart(fig_weekday_avg, use_container_width=True)

    st.subheader("📅 Будні vs вихідні")
    week_data = filtered.assign(
        period_type=filtered["is_weekend"].map({False: "Будні", True: "Вихідні"})
    ).groupby(["month", "period_type"], as_index=False)["value"].sum()
    week_data["month_total"] = week_data.groupby("month")["value"].transform("sum")
    week_data["percent"] = (week_data["value"] / week_data["month_total"] * 100).round(1)
    week_data["text"] = week_data["percent"].astype(str) + "%"
    week_data["month_label"] = week_data["month"].apply(lambda x: pd.Period(x).strftime("%m.%Y"))
    fig_week = px.bar(week_data, x="month_label", y="value", color="period_type", barmode="group",
        text="text", labels={"month_label": "Місяць", "value": "Кількість", "period_type": ""})
    fig_week.update_traces(textposition="outside")
    fig_week.update_layout(height=380, margin=dict(l=10, r=10, t=20, b=10))
    st.plotly_chart(fig_week, use_container_width=True)

    st.subheader("🌡️ Теплова карта навантаження (Місяць × День тижня)")
    st.caption("ℹ️ Карта показує повний період без урахування фільтрів по днях.")

    daily_heat = with_data(df[df["operation"] == "Тотал"]).copy()
    daily_heat = daily_heat[daily_heat["date"] <= today]
    if period_mode == "За місяцями":
        daily_heat = daily_heat[daily_heat["month"].isin(selected_months)]
    else:
        daily_heat = daily_heat[(daily_heat["date"] >= custom_range[0]) & (daily_heat["date"] <= custom_range[1])]
    daily_heat = daily_heat.groupby("date")["value"].sum().reset_index()
    daily_heat["month_label"] = daily_heat["date"].dt.strftime("%m.%Y")
    daily_heat["weekday_ua"] = daily_heat["date"].dt.day_name().map(WEEKDAY_UA)
    heat_data = daily_heat.groupby(["month_label", "weekday_ua"])["value"].mean().reset_index()
    heat_pivot = heat_data.pivot(index="month_label", columns="weekday_ua", values="value").fillna(0)
    heat_pivot = heat_pivot.reindex(columns=WEEKDAY_ORDER_UA)

    def _sort_m(ms):
        try: return datetime.strptime(ms, "%m.%Y")
        except Exception: return datetime(1900, 1, 1)
    heat_pivot = heat_pivot.reindex(sorted(heat_pivot.index, key=_sort_m))
    heatmap_height = max(420, min(2400, len(heat_pivot.index) * 38))
    fig_heatmap = px.imshow(heat_pivot, text_auto=".1f", aspect="auto",
        labels=dict(x="День тижня", y="Місяць", color="Середня кількість"),
        color_continuous_scale=KPO_HEAT_SCALE)
    fig_heatmap.update_layout(height=heatmap_height, margin=dict(l=10, r=10, t=20, b=10))
    fs = 12 if len(heat_pivot.index) <= 12 else (10 if len(heat_pivot.index) <= 24 else 8)
    fig_heatmap.update_traces(textfont=dict(size=fs))
    st.plotly_chart(fig_heatmap, use_container_width=True)

    st.subheader("📊 Стабільність навантаження")
    col1, col2, col3 = st.columns(3)
    col1.markdown(custom_metric("Середнє за день", f"{daily_avg:.0f}" if daily_avg > 0 else "—"), unsafe_allow_html=True)
    col2.markdown(custom_metric("Стандартне відхилення", f"{std:.1f}" if std > 0 else "—"), unsafe_allow_html=True)
    col3.markdown(custom_metric("Коефіцієнт варіації (CV)", f"{cv:.1f}%" if cv > 0 else "—"), unsafe_allow_html=True)

    st.subheader("📊 Розподіл відхилень від середнього (крива щільності)")
    daily_totals = filtered_stats.groupby("date")["value"].sum().reset_index()
    daily_totals["is_weekend"] = daily_totals["date"].dt.dayofweek >= 5
    if len(daily_totals) >= 3:
        mean_all = daily_totals["value"].mean()
        daily_totals["dev_all"] = (daily_totals["value"] - mean_all) / mean_all * 100
        wd_local = daily_totals["is_weekend"] == False
        we_local = daily_totals["is_weekend"] == True
        mean_wd = daily_totals.loc[wd_local, "value"].mean() if wd_local.any() else None
        mean_we = daily_totals.loc[we_local, "value"].mean() if we_local.any() else None
        daily_totals["dev_weekday"] = None
        if mean_wd is not None and mean_wd != 0:
            daily_totals.loc[wd_local, "dev_weekday"] = ((daily_totals.loc[wd_local, "value"] - mean_wd) / mean_wd * 100)
        daily_totals["dev_weekend"] = None
        if mean_we is not None and mean_we != 0:
            daily_totals.loc[we_local, "dev_weekend"] = ((daily_totals.loc[we_local, "value"] - mean_we) / mean_we * 100)
        dev_data, group_names = [], []
        dev_all = daily_totals["dev_all"].dropna().values
        if len(dev_all) > 1: dev_data.append(dev_all); group_names.append("Всі дні")
        dev_wd = daily_totals.loc[wd_local, "dev_weekday"].dropna().values if wd_local.any() else np.array([])
        if len(dev_wd) > 1: dev_data.append(dev_wd); group_names.append("Будні")
        dev_we = daily_totals.loc[we_local, "dev_weekend"].dropna().values if we_local.any() else np.array([])
        if len(dev_we) > 1: dev_data.append(dev_we); group_names.append("Вихідні")

        if dev_data:
            fig_density = go.Figure()
            colors = {"Всі дні": KPO_CYAN, "Будні": KPO_GREEN, "Вихідні": KPO_AMBER}
            max_density = 0
            for gn, data in zip(group_names, dev_data):
                if len(data) > 1:
                    x_min, x_max = data.min() - 10, data.max() + 10
                    x_grid = np.linspace(x_min, x_max, 200)
                    density = gaussian_kde_np(data, x_grid)
                    max_density = max(max_density, max(density))
                    line_dash = "dash" if len(data) < 5 else "solid"
                    disp = f"{gn} (n={len(data)}, орієнтир)" if len(data) < 5 else gn
                    fig_density.add_trace(go.Scatter(x=x_grid, y=density, mode='lines', name=disp,
                        line=dict(color=colors.get(gn, "gray"), width=2.5, dash=line_dash), fill='none'))
            if max_density > 0:
                fig_density.add_trace(go.Scatter(x=[0, 0], y=[0, max_density * 1.1], mode='lines',
                    name='Середнє (0%)', line=dict(color=KPO_RED, width=2, dash='dash'), showlegend=True))
                median_all = np.median(dev_all) if len(dev_all) > 0 else None
                if median_all is not None:
                    fig_density.add_trace(go.Scatter(x=[median_all, median_all], y=[0, max_density * 1.1],
                        mode='lines', name=f'Медіана ({median_all:.1f}%)',
                        line=dict(color=KPO_TEXT, width=2, dash='dash'), showlegend=True))
            fig_density.update_layout(title="Криві щільності відхилень",
                xaxis_title="Відхилення, %", yaxis_title="Щільність", height=400,
                margin=dict(l=10, r=10, t=40, b=10))
            st.plotly_chart(fig_density, use_container_width=True)

            density_stats = analyze_density(group_names, dev_data)
            if density_stats:
                st.markdown("**📊 Статистика розподілу**")
                for name, s in density_stats.items():
                    with st.expander(f"{name} — {s['reliability_txt']}", expanded=False):
                        st.write(f"**Розкид:** {s['width_txt']}")
                        st.write(f"**Форма:** {s['skew_txt']}")
                        st.write(f"**Пік:** {s['peak']:+.1f}% (частота {s['peak_pct']:.0f}%, {s['peak_count']} днів)")
                        st.write(f"**Медіана:** {s['median']:+.1f}% · **IQR:** {s['iqr']:.1f}%")
                        st.write(f"**Діапазон:** [{s['min']:+.1f}%; {s['max']:+.1f}%]")
        else:
            st.info("Недостатньо даних для побудови кривих щільності.")
    else:
        st.info("Недостатньо даних для аналізу розподілу.")


# ============================================================
# TAB 5: ПОРІВНЯННЯ ПЕРІОДІВ
# ============================================================
with tab5:
    st.subheader("🆚 Порівняння періодів")
    st.caption("Виберіть від 2 до 6 місяців. Дельта доступна лише при порівнянні рівно двох періодів.")

    all_months_sorted = sorted(df["month"].unique(), reverse=True)
    if len(all_months_sorted) < 2:
        st.info("Потрібно щонайменше два місяці даних.")
    else:
        max_n = min(6, len(all_months_sorted))
        n_options = list(range(2, max_n + 1))
        if not n_options:
            st.info(f"Недостатньо місяців з даними (доступно {len(all_months_sorted)}).")
        else:
            n_periods = st.radio("Кількість періодів", options=n_options, index=0, horizontal=True, key="cmp_n_periods")

            if n_periods <= 3:
                cols = st.columns(n_periods)
            else:
                cols = []
                for _ in range((n_periods + 2) // 3):
                    row_cols = st.columns(3)
                    for c in row_cols:
                        if len(cols) < n_periods: cols.append(c)

            periods = []
            for i in range(n_periods):
                with cols[i]:
                    default_idx = min(i, len(all_months_sorted) - 1)
                    p = st.selectbox(f"Період {chr(65 + i)}", options=all_months_sorted,
                        format_func=lambda x: pd.Period(x).strftime("%m.%Y"),
                        index=default_idx, key=f"cmp_period_{i}")
                    periods.append(p)

            if len(set(periods)) < len(periods):
                st.warning("⚠️ Оберіть різні періоди — є дублікати.")
            else:
                today_ym = today.strftime("%Y-%m")
                any_current = today_ym in periods
                cutoff_day = None
                if any_current:
                    cur_df = with_data(df[(df["month"] == today_ym) & (df["operation"] == "Тотал")])
                    cur_df = cur_df[cur_df["date"] <= today]
                    cutoff_day = min(int(cur_df["date"].max().day), today.day) if not cur_df.empty else today.day

                def _apply_cutoff(frame, period_str, cutoff):
                    if cutoff is None: return frame
                    pd_days = pd.Period(period_str).days_in_month
                    eff = min(cutoff, pd_days)
                    return frame[frame["date"].dt.day <= eff]

                def _eff_day(period_str, cutoff):
                    if cutoff is None: return None
                    return min(cutoff, pd.Period(period_str).days_in_month)

                def _period_metrics(period_str, cutoff=None):
                    mask = (df["month"] == period_str) & (df["operation"] == "Тотал")
                    p_df = with_data(df[mask]); p_df = p_df[p_df["date"] <= today]
                    if p_df.empty: return None
                    p_df = _apply_cutoff(p_df, period_str, cutoff)
                    if p_df.empty: return None
                    daily = p_df.groupby("date")["value"].sum()
                    s_true = float(p_df["sum_true"].sum()); s_false = float(p_df["sum_false"].sum())
                    ratio = s_true + s_false
                    return {"total": float(daily.sum()), "avg": float(daily.mean()) if not daily.empty else 0.0,
                            "peak": float(daily.max()) if not daily.empty else 0.0, "days": int(len(daily)),
                            "approval": (s_true / ratio * 100) if ratio > 0 else None}

                labels = []
                for p in periods:
                    lbl = pd.Period(p).strftime("%m.%Y")
                    eff = _eff_day(p, cutoff_day)
                    if eff is not None: lbl += f" (до {eff:02d}.{pd.Period(p).month:02d})"
                    labels.append(lbl)

                if any_current and cutoff_day is not None:
                    parts = [f"**{pd.Period(p).strftime('%m.%Y')}** до **{_eff_day(p, cutoff_day):02d}-го дня**" for p in periods]
                    st.caption("⚖️ Один із періодів ще не завершено. Обрізано: " + ", ".join(parts) + ".")
                else:
                    st.caption("📊 Усі періоди завершені — порівняння повних місяців.")

                metrics = [_period_metrics(p, cutoff_day) for p in periods]

                if all(m is None for m in metrics):
                    st.info("Жоден із вибраних періодів не має даних.")
                elif n_periods == 2:
                    ma, mb = metrics[0], metrics[1]
                    if ma is None or mb is None:
                        st.info("Один із періодів не має даних.")
                    else:
                        def _delta(a, b):
                            if a is None or b is None or b == 0: return "—"
                            return f"{(a - b) / b * 100:+.1f}%"
                        label_a, label_b = labels[0], labels[1]
                        comp_rows = [
                            {"Метрика": "Всього", "A": f"{ma['total']:,.0f}", "B": f"{mb['total']:,.0f}",
                             "Δ (A vs B)": _delta(ma['total'], mb['total'])},
                            {"Метрика": "Середнє/день", "A": f"{ma['avg']:.0f}", "B": f"{mb['avg']:.0f}",
                             "Δ (A vs B)": _delta(ma['avg'], mb['avg'])},
                            {"Метрика": "Пік", "A": f"{ma['peak']:,.0f}", "B": f"{mb['peak']:,.0f}",
                             "Δ (A vs B)": _delta(ma['peak'], mb['peak'])},
                            {"Метрика": "Днів з даними", "A": f"{ma['days']}", "B": f"{mb['days']}", "Δ (A vs B)": "—"},
                            {"Метрика": "Погодження, %",
                             "A": f"{ma['approval']:.1f}%" if ma['approval'] is not None else "—",
                             "B": f"{mb['approval']:.1f}%" if mb['approval'] is not None else "—",
                             "Δ (A vs B)": _delta(ma['approval'], mb['approval'])
                                if ma['approval'] is not None and mb['approval'] is not None else "—"},
                        ]
                        st.markdown(f"**A** = `{label_a}` &nbsp;·&nbsp; **B** = `{label_b}`")
                        st.dataframe(pd.DataFrame(comp_rows), use_container_width=True, hide_index=True)
                else:
                    st.info("ℹ️ Дельта доступна лише при порівнянні **2 періодів**. Для 3+ — окремі таблиці метрик.")
                    for row_start in range(0, n_periods, 3):
                        row_indices = list(range(row_start, min(row_start + 3, n_periods)))
                        row_cols = st.columns(len(row_indices))
                        for col_idx, i in enumerate(row_indices):
                            p = periods[i]; lbl = labels[i]; m = metrics[i]
                            with row_cols[col_idx]:
                                st.markdown(f"**{chr(65 + i)} · {lbl}**")
                                if m is None: st.warning("Немає даних.")
                                else:
                                    compact = [
                                        {"Метрика": "Всього", "Значення": f"{m['total']:,.0f}"},
                                        {"Метрика": "Середнє/день", "Значення": f"{m['avg']:.0f}"},
                                        {"Метрика": "Пік", "Значення": f"{m['peak']:,.0f}"},
                                        {"Метрика": "Днів", "Значення": f"{m['days']}"},
                                        {"Метрика": "Погодж., %", "Значення": f"{m['approval']:.1f}%" if m['approval'] is not None else "—"},
                                    ]
                                    st.dataframe(pd.DataFrame(compact), use_container_width=True, hide_index=True)

                daily_data = []
                for p, lbl, m in zip(periods, labels, metrics):
                    if m is None: continue
                    daily_df = with_data(df[(df["month"] == p) & (df["operation"] == "Тотал")])
                    daily_df = daily_df[daily_df["date"] <= today]
                    daily_df = _apply_cutoff(daily_df, p, cutoff_day)
                    if daily_df.empty: continue
                    agg = daily_df.groupby("date")["value"].sum().reset_index()
                    agg["day"] = agg["date"].dt.day; agg["period"] = lbl
                    daily_data.append(agg[["day", "value", "period"]])

                if daily_data:
                    st.subheader("📈 Порівняльна динаміка по днях місяця")
                    compare_days = pd.concat(daily_data, ignore_index=True)
                    fig_compare = px.line(compare_days, x="day", y="value", color="period", markers=True,
                        labels={"day": "День місяця", "value": "Кількість", "period": "Період"},
                        title="Порівняння по днях місяця (Тотал)")
                    fig_compare.update_layout(height=420, margin=dict(l=10, r=10, t=40, b=10))
                    st.plotly_chart(fig_compare, use_container_width=True)

                ops_data_dict = {}
                for p, lbl, m in zip(periods, labels, metrics):
                    if m is None: continue
                    ops_df = with_data(df[(df["month"] == p) & (df["operation"] != "Тотал")])
                    ops_df = ops_df[ops_df["date"] <= today]
                    ops_df = _apply_cutoff(ops_df, p, cutoff_day)
                    ops_data_dict[lbl] = ops_df.groupby("operation")["value"].sum()

                ops_compare = pd.DataFrame(ops_data_dict).fillna(0)
                if not ops_compare.empty:
                    st.subheader("📊 Порівняння по операціях")
                    ops_long = ops_compare.reset_index().melt(id_vars="operation", var_name="Період", value_name="Кількість")
                    fig_ops_cmp = px.bar(ops_long, x="operation", y="Кількість", color="Період", barmode="group",
                        labels={"operation": "Операція", "Кількість": "Кількість"},
                        title="Порівняння операцій між періодами")
                    fig_ops_cmp.update_layout(height=420, margin=dict(l=10, r=10, t=20, b=10))
                    st.plotly_chart(fig_ops_cmp, use_container_width=True)


# ============================================================
# Footer
# ============================================================
st.divider()
with st.expander(f"📝 Останні зміни (останні {len(RECENT_CHANGES)})", expanded=False):
    for change in RECENT_CHANGES:
        st.markdown(f"- {change}")

_build_parts = [f"**Build {APP_BUILD_NUMBER}**", f"{APP_BUILD_DATE} {APP_BUILD_TIME}"]
if APP_COMMIT_HASH: _build_parts.append(f"`{APP_COMMIT_HASH}`")
if APP_COMMIT_SUBJECT: _build_parts.append(f"_{APP_COMMIT_SUBJECT}_")
st.caption(" · ".join(_build_parts))
