import html
import re
import logging
from datetime import datetime
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
# 1. Конфігурація сторінки
# ============================================================
st.set_page_config(
    page_title="KPO Dashboard",
    page_icon="📊",
    layout="wide",
)

# ============================================================
# 2. Тема оформлення
# ============================================================
KPO_BG = "#0b0f17"
KPO_CARD_BG = "#131824"
KPO_BORDER = "#1f2733"
KPO_TEXT = "#e6edf3"
KPO_TEXT_MUTED = "#8b98a9"

KPO_CYAN = "#00d9ff"
KPO_AMBER = "#ffb703"
KPO_GREEN = "#06d6a0"
KPO_RED = "#ef476f"
KPO_PURPLE = "#8338ec"
KPO_ORANGE = "#f4a261"
KPO_BLUE = "#4cc9f0"
KPO_PINK = "#ff6b9d"

KPO_COLORWAY = [KPO_CYAN, KPO_AMBER, KPO_GREEN, KPO_RED, KPO_PURPLE, KPO_ORANGE, KPO_BLUE, KPO_PINK]
KPO_HEAT_SCALE = [[0.0, "#0b0f17"], [0.5, "#0d5c73"], [1.0, KPO_CYAN]]

_kpo_dark_template = go.layout.Template(
    layout=go.Layout(
        paper_bgcolor=KPO_BG,
        plot_bgcolor=KPO_BG,
        font=dict(color=KPO_TEXT, family="Inter, sans-serif"),
        colorway=KPO_COLORWAY,
        xaxis=dict(gridcolor=KPO_BORDER, zerolinecolor=KPO_BORDER, linecolor=KPO_BORDER),
        yaxis=dict(gridcolor=KPO_BORDER, zerolinecolor=KPO_BORDER, linecolor=KPO_BORDER),
        legend=dict(bgcolor="rgba(0,0,0,0)"),
        hoverlabel=dict(bgcolor=KPO_CARD_BG, font_color=KPO_TEXT, bordercolor=KPO_BORDER),
        colorscale=dict(sequential=KPO_HEAT_SCALE),
    )
)
pio.templates["kpo_dark"] = _kpo_dark_template
pio.templates.default = "kpo_dark"

# ============================================================
# 3. Безпечне зчитування конфіденційних даних
# ============================================================
SPREADSHEET_ID = "1STX1vgDAk3zVDshXdZmTgJJSvQNCN4WmmftOskwymYI"

# ============================================================
# 4. Автоматичне визначення років
# ============================================================
START_YEAR = 2024
CURRENT_YEAR = datetime.now().year
SHEETS = [str(year)[-2:] for year in range(START_YEAR, CURRENT_YEAR + 1)]

KYIV_TZ = ZoneInfo("Europe/Kyiv")

MONTHS = {
    "Січень": 1, "Лютий": 2, "Березень": 3, "Квітень": 4,
    "Травень": 5, "Червень": 6, "Липень": 7, "Серпень": 8,
    "Вересень": 9, "Жовтень": 10, "Листопад": 11, "Грудень": 12,
}

OPERATIONS = [
    "Бонуси", "Призупинка", "Відновлення", "Відміна SF",
    "Переоформлення", "Закриття контракта", "Со-доступ", "Зміна дати активації",
    "Loyalty",
]

def normalize_operation(value):
    if not isinstance(value, str):
        return ""
    return " ".join(value.strip().split())

ALIASES = {
    "Зміна дати активації": "Зміна дати активації",
}

WEEKDAY_UA = {
    "Monday": "Пн", "Tuesday": "Вт", "Wednesday": "Ср",
    "Thursday": "Чт", "Friday": "Пт", "Saturday": "Сб", "Sunday": "Нд",
}
WEEKDAY_ORDER_UA = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Нд"]

APPROVAL_GOOD_THRESHOLD = 85
APPROVAL_WARN_THRESHOLD = 70
COLOR_GOOD = KPO_GREEN
COLOR_WARN = KPO_AMBER
COLOR_BAD = KPO_RED

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger(__name__)

# ============================================================
# 5. Часові функції
# ============================================================
def now_kyiv() -> pd.Timestamp:
    return pd.Timestamp.now(tz=KYIV_TZ).replace(tzinfo=None).normalize()

def now_kyiv_exact() -> pd.Timestamp:
    return pd.Timestamp.now(tz=KYIV_TZ).replace(tzinfo=None)

# ============================================================
# 6. Парсинг комірок
# ============================================================

# Заголовок дня: 01.09, 1.9, 01/09, 01.09.2026
DAY_HEADER_RE = re.compile(r"^\s*(\d{1,2})\s*[./\-]\s*(\d{1,2})\s*(?:[./\-]\s*\d{2,4})?\s*$")

# Символи, які Google Sheets любить підсовувати замість звичайних
_CHAR_FIXES = {
    "\xa0": " ", "\u202f": " ", "\u2007": " ",
    "\u2010": "-", "\u2011": "-", "\u2012": "-", "\u2013": "-", "\u2014": "-", "\u2212": "-",
    "\u2019": "'", "\u02bc": "'", "`": "'",
}

def is_empty_cell(value) -> bool:
    if value is None:
        return True
    if isinstance(value, bool):
        return False
    return str(value).strip() == ""

def normalize_operation(value):
    """Нормалізує назву операції: NBSP, довгі тире, апострофи, подвійні пробіли."""
    if not isinstance(value, str):
        return ""
    for bad, good in _CHAR_FIXES.items():
        value = value.replace(bad, good)
    return " ".join(value.strip().split())

def as_number(value):
    """Дістає число з комірки. Повертає (число, розпізнано_чи)."""
    if is_empty_cell(value):
        return 0.0, True
    val_str = str(value)
    for bad, good in _CHAR_FIXES.items():
        val_str = val_str.replace(bad, good)
    val_str = val_str.replace(" ", "").replace("'", "")
    # шукаємо число будь-де в рядку, не тільки на початку: "+4", "~4", "4шт"
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
    if not isinstance(value, str):
        return None
    value = normalize_operation(value)
    match = re.match(
        r"^(Січень|Лютий|Березень|Квітень|Травень|Червень|"
        r"Липень|Серпень|Вересень|Жовтень|Листопад|Грудень)\s+(\d{2,4})$",
        value,
    )
    if not match:
        return None
    year_part = match.group(2)
    year = int(year_part) if len(year_part) == 4 else 2000 + int(year_part)
    return MONTHS[match.group(1)], year

def _find_month_blocks(values, sheet_year):
    """Повертає [(header_row, month, year, block_end), ...] — межі кожного місячного блоку."""
    blocks = []
    for idx, row in enumerate(values):
        parsed = parse_month_header(row[0] if row else "", sheet_year)
        if parsed:
            blocks.append([idx, parsed[0], parsed[1], None])
    for i, block in enumerate(blocks):
        block[3] = blocks[i + 1][0] if i + 1 < len(blocks) else len(values)
    return [tuple(b) for b in blocks]

def _find_total_row(values, start, end):
    """Шукає рядок з написом «Тотал». Повертає (row_idx, label_col)."""
    for r in range(start, end):
        row = values[r]
        for c in range(min(len(row), 6)):
            if normalize_operation(row[c]).lower() == "тотал":
                return r, c
    return None, None

def _detect_day_columns(values, start, end, month):
    """
    Знаходить рядок із заголовками днів (01.09, 02.09, ...) і повертає
    (header_row, [(col, day), ...]) для того рядка, де таких заголовків найбільше.
    """
    best = None
    for r in range(start, min(start + 8, end)):
        found = []
        for c, cell in enumerate(values[r]):
            match = DAY_HEADER_RE.match(str(cell))
            if not match:
                continue
            day, mon = int(match.group(1)), int(match.group(2))
            if mon == month and 1 <= day <= 31:
                found.append((c, day))
        if found and (best is None or len(found) > len(best[1])):
            best = (r, sorted(found))
    return best if best else (None, [])

def _build_day_spans(day_cols):
    """
    Перетворює [(col, day)] у [(day, col, span)], де span — скільки колонок
    займає день (1 = просто кількість, 2 = пара TRUE/FALSE).
    """
    if not day_cols:
        return []
    gaps = [day_cols[i + 1][0] - day_cols[i][0] for i in range(len(day_cols) - 1)]
    default_span = int(np.median(gaps)) if gaps else 1
    default_span = max(1, min(default_span, 2))

    spans = []
    for i, (col, day) in enumerate(day_cols):
        if i + 1 < len(day_cols):
            span = day_cols[i + 1][0] - col
        else:
            span = default_span
        spans.append((day, col, max(1, min(span, 2))))
    return spans

# ============================================================
# 7. Робота з Google Sheets
# ============================================================
def get_client():
    scopes = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
    credentials = Credentials.from_service_account_info(
        dict(st.secrets["gcp_service_account"]),
        scopes=scopes,
    )
    return gspread.authorize(credentials)

@st.cache_data(ttl=300, show_spinner="Завантаження даних з Google Таблиці…")
def load_data():
    client = get_client()
    spreadsheet = client.open_by_key(SPREADSHEET_ID)

    records = []
    warnings = []
    unknown_ops = {}          # назва -> список місяців, де зустрілась
    sheet_month_totals = {}   # month_key -> (true, false) з рядка «Тотал»

    for sheet_name in SHEETS:
        try:
            worksheet = spreadsheet.worksheet(sheet_name)
        except Exception:
            continue
        values = worksheet.get_all_values()
        if not values:
            continue

        sheet_year = 2000 + int(sheet_name)

        for header_row, month, year, block_end in _find_month_blocks(values, sheet_year):
            month_key = f"{year}-{month:02d}"
            month_label = f"{month:02d}.{year}"
            where = f"аркуш «{sheet_name}», {month_label}"

            total_row_idx, label_col = _find_total_row(values, header_row, block_end)
            if total_row_idx is None:
                warnings.append(f"⚠️ {where}: не знайдено рядок «Тотал» — блок пропущено.")
                continue

            day_header_row, day_cols = _detect_day_columns(values, header_row, block_end, month)
            if not day_cols:
                warnings.append(
                    f"⚠️ {where}: не знайдено заголовків днів (очікується формат «01.{month:02d}») "
                    f"— блок пропущено."
                )
                continue

            day_spans = _build_day_spans(day_cols)
            first_day_col = day_spans[0][1]

            # Колонки місячного підсумку: між назвою операції та першим днем.
            # Порядок позиційний: перша = погоджено (TRUE), друга = відмова (FALSE).
            sum_cols = [c for c in range(label_col + 1, first_day_col)][:2]

            expected_days = pd.Period(month_key).days_in_month
            if len(day_spans) != expected_days:
                warnings.append(
                    f"ℹ️ {where}: знайдено {len(day_spans)} колонок днів із {expected_days}. "
                    f"Дані читаються лише за знайдені дні."
                )

            # --- Місячний «Тотал» із таблиці (для звірки) ---
            total_row = values[total_row_idx]
            if len(sum_cols) == 2:
                t_true, _ = as_number(total_row[sum_cols[0]] if sum_cols[0] < len(total_row) else "")
                t_false, _ = as_number(total_row[sum_cols[1]] if sum_cols[1] < len(total_row) else "")
                sheet_month_totals[month_key] = (t_true, t_false)

            # --- Рядки операцій під «Тоталом» ---
            rows_read = 0
            for r in range(total_row_idx + 1, block_end):
                row = values[r]
                label = normalize_operation(row[label_col] if label_col < len(row) else "")

                if not label:
                    break                       # порожня назва = кінець таблиці
                if label.lower() == "тотал":
                    continue                    # службовий рядок
                if parse_month_header(label, sheet_year):
                    break                        # почався наступний місяць

                operation = ALIASES.get(label, label)
                if operation not in OPERATIONS:
                    unknown_ops.setdefault(operation, []).append(month_label)

                rows_read += 1
                for day, col, span in day_spans:
                    raw_true = row[col] if col < len(row) else ""
                    raw_false = row[col + 1] if (span >= 2 and col + 1 < len(row)) else ""

                    val_true, ok_true = as_number(raw_true)
                    val_false, ok_false = as_number(raw_false)

                    if not ok_true or not ok_false:
                        bad = raw_true if not ok_true else raw_false
                        warnings.append(
                            f"⚠️ {where}, {operation}, {day:02d}.{month:02d}: "
                            f"не розпізнано число в «{bad}» — враховано як 0."
                        )

                    date = pd.Timestamp(year=year, month=month, day=day)
                    records.append({
                        "date": date,
                        "operation": operation,
                        "value": val_true + val_false,
                        "sum_true": val_true,
                        "sum_false": val_false,
                        "has_data": not (is_empty_cell(raw_true) and is_empty_cell(raw_false)),
                    })

            if rows_read == 0:
                warnings.append(f"⚠️ {where}: під рядком «Тотал» не знайдено жодної операції.")

    if not records:
        raise ValueError("Не знайдено деталізованих даних у Google Таблиці.")

    df_raw = pd.DataFrame(records)
    df_raw["date"] = pd.to_datetime(df_raw["date"])

    # --- Агрегація по (дата, операція) ---
    df = (
        df_raw.groupby(["date", "operation"], as_index=False)
        .agg(
            value=("value", "sum"),
            sum_true=("sum_true", "sum"),
            sum_false=("sum_false", "sum"),
            has_data=("has_data", "any"),
        )
    )

    # --- Рядок «Тотал» = сума операцій за день ---
    total = (
        df.groupby("date", as_index=False)
        .agg(
            value=("value", "sum"),
            sum_true=("sum_true", "sum"),
            sum_false=("sum_false", "sum"),
            has_data=("has_data", "any"),
        )
        .assign(operation="Тотал")
    )
    df = pd.concat([df, total], ignore_index=True)

    # --- Метадані дат ---
    df["year"] = df["date"].dt.year
    df["month"] = df["date"].dt.strftime("%Y-%m")
    df["month_name"] = df["date"].dt.strftime("%b %Y")
    df["weekday"] = df["date"].dt.day_name()
    df["is_weekend"] = df["date"].dt.weekday >= 5
    df = df.sort_values(["date", "operation"]).reset_index(drop=True)

    # --- Попередження про невідомі операції (не з хардкоду OPERATIONS) ---
    for op, months in unknown_ops.items():
        warnings.append(
            f"🆕 Операція «{op}» відсутня у списку OPERATIONS "
            f"({', '.join(sorted(set(months)))}). Її враховано в розрахунках — "
            f"додай назву в OPERATIONS, щоб вона з'явилась у фільтрах у правильному порядку."
        )

    # --- Звірка з рядком «Тотал» таблиці ---
    computed = df[df["operation"] == "Тотал"].groupby("month")["value"].sum()
    for month_key, (t_true, t_false) in sheet_month_totals.items():
        sheet_sum = t_true + t_false
        calc_sum = float(computed.get(month_key, 0))
        if sheet_sum > 0 and abs(calc_sum - sheet_sum) > 0.5:
            warnings.append(
                f"❗ {month_key}: сума по днях = {calc_sum:,.0f}, рядок «Тотал» = {sheet_sum:,.0f} "
                f"(різниця {calc_sum - sheet_sum:+,.0f}). Перевір, чи всі рядки операцій "
                f"потрапляють у діапазон блоку."
            )

    return df, warnings

# ============================================================
# 8. Допоміжні функції
# ============================================================
def with_data(df):
    if "has_data" not in df.columns:
        return df
    return df[df["has_data"]]

def calc_peak_min_avg(df):
    daily = with_data(df).groupby("date")["value"].sum()
    if daily.empty:
        return 0, 0, 0, 0
    peak = daily.max()
    min_val = daily.min()
    avg = daily.mean()
    peak_avg_ratio = peak / avg if avg > 0 else 0
    return peak, min_val, avg, peak_avg_ratio

def calc_busiest_weekday(df):
    df = with_data(df)
    if df.empty:
        return None, None
    daily = df.groupby("date")["value"].sum().reset_index()
    daily["weekday"] = daily["date"].dt.day_name()
    weekday_avg = daily.groupby("weekday")["value"].mean()
    busiest = weekday_avg.idxmax()
    busiest_val = weekday_avg.max()
    return busiest, busiest_val

def calc_busiest_operation(df):
    if df.empty or df["operation"].nunique() == 0:
        return None, None
    ops = with_data(df[df["operation"] != "Тотал"])
    if ops.empty:
        return None, None
    total_by_op = ops.groupby("operation")["value"].sum()
    busiest_op = total_by_op.idxmax()
    busiest_val = total_by_op.max()
    return busiest_op, busiest_val

def calc_stability(df, daily_avg):
    daily = with_data(df).groupby("date")["value"].sum()
    if daily.empty:
        return 0, 0, "Немає даних"
    std = daily.std()
    cv = (std / daily_avg * 100) if daily_avg > 0 else 0
    if cv < 15:
        interpretation = "🟢 Низька варіативність (≤15%)"
    elif cv < 30:
        interpretation = "🟡 Середня варіативність (15-30%)"
    else:
        interpretation = "🔴 Висока варіативність (>30%)"
    return std, cv, interpretation

def detect_anomalies(df, window=14, threshold=3.0):
    df = with_data(df)
    if df.empty:
        return pd.DataFrame()
    daily = df.groupby("date")["value"].sum().reset_index()
    daily = daily.sort_values("date")
    if len(daily) < window:
        return pd.DataFrame()

    daily["month_key"] = daily["date"].dt.to_period("M")
    all_anomalies = []
    for month, group in daily.groupby("month_key"):
        if len(group) < 3:
            continue
        group = group.sort_values("date").copy()
        group["rolling_median"] = group["value"].rolling(window=window, min_periods=1, center=True).median()
        group["rolling_mad"] = group["value"].rolling(window=window, min_periods=1, center=True).apply(
            lambda x: np.median(np.abs(x - np.median(x))) if len(x) > 1 else 0
        )
        group["z_score"] = (group["value"] - group["rolling_median"]) / (group["rolling_mad"] * 1.4826).replace(0, 1)
        group["is_anomaly"] = abs(group["z_score"]) > threshold
        all_anomalies.append(group)
    if not all_anomalies:
        return pd.DataFrame()
    result = pd.concat(all_anomalies, ignore_index=True)
    result = result[(result["value"] > 0)]
    return result

def gaussian_kde_np(data, x_grid, bandwidth=None):
    data = np.asarray(data, dtype=float)
    data = data[np.isfinite(data)]
    n = len(data)
    if n == 0:
        return np.zeros_like(x_grid)
    std = np.std(data)
    if std == 0:
        bandwidth = 1.0
    elif bandwidth is None:
        bandwidth = 1.06 * std * n ** (-0.2)
    x_grid = np.asarray(x_grid)
    u = (x_grid[:, None] - data[None, :]) / bandwidth
    kernel = np.exp(-0.5 * u * u)
    density = kernel.sum(axis=1) / (n * bandwidth * np.sqrt(2 * np.pi))
    return density

def analyze_density(group_names, dev_data):
    """
    Обчислює статистичні характеристики кожної кривої щільності
    на основі реальних даних поточного періоду.
    """
    stats = {}
    for name, data in zip(group_names, dev_data):
        if data is None or len(data) < 2:
            continue
        data = np.asarray(data, dtype=float)
        data = data[np.isfinite(data)]
        if len(data) < 2:
            continue
        n = len(data)
        std_d = float(np.std(data))
        median_d = float(np.median(data))
        p25, p75 = np.percentile(data, [25, 75])
        iqr = float(p75 - p25)
        try:
            skew_val = float(pd.Series(data).skew())
        except Exception:
            skew_val = 0.0
        bins = max(5, min(12, n))
        hist, edges = np.histogram(data, bins=bins)
        peak_idx = int(np.argmax(hist))
        peak_center = float((edges[peak_idx] + edges[peak_idx + 1]) / 2)
        peak_height_pct = float(hist[peak_idx] / n * 100)

        if std_d < 15:
            width_key = "вузька"
            width_txt = f"вузька (σ = {std_d:.1f}%) — дні стабільні"
        elif std_d < 30:
            width_key = "середня"
            width_txt = f"середня (σ = {std_d:.1f}%) — помірна варіативність"
        else:
            width_key = "широка"
            width_txt = f"широка (σ = {std_d:.1f}%) — великий розкид"

        if abs(skew_val) < 0.3:
            skew_txt = "симетричний"
        elif skew_val > 0:
            skew_txt = f"зміщений вправо (хвіст у бік підвищених днів, skew = {skew_val:+.2f})"
        else:
            skew_txt = f"зміщений вліво (хвіст у бік знижених днів, skew = {skew_val:+.2f})"

        stats[name] = {
            "n": n, "std": std_d, "median": median_d, "iqr": iqr,
            "skew": skew_val, "peak": peak_center, "peak_pct": peak_height_pct,
            "width_key": width_key, "width_txt": width_txt,
            "skew_txt": skew_txt,
            "min": float(data.min()), "max": float(data.max()),
        }
    return stats

def forecast_scenarios(df, current_month):
    if df.empty or current_month not in df["month"].values:
        return None, None

    month_data = with_data(df[df["month"] == current_month])
    today = now_kyiv()
    days_passed = (today - pd.Timestamp(year=today.year, month=today.month, day=1)).days + 1

    if today.month != pd.Period(current_month).month or today.year != pd.Period(current_month).year:
        return None, None

    fact_days = month_data[month_data["date"].dt.day <= days_passed]
    if fact_days.empty:
        return None, None

    daily_sums = fact_days.groupby("date")["value"].sum()
    fact_sum = daily_sums.sum()
    avg_daily = daily_sums.mean()
    std_daily = daily_sums.std()

    total_days = pd.Period(current_month).days_in_month
    remaining_days = total_days - days_passed

    stat_base = fact_sum + avg_daily * remaining_days
    stat_min = fact_sum + max(0, avg_daily - 0.5 * std_daily) * remaining_days
    stat_max = fact_sum + (avg_daily + 0.5 * std_daily) * remaining_days

    stat_forecast = {
        "base": stat_base,
        "min": stat_min,
        "max": stat_max,
        "avg_daily": avg_daily,
        "std_daily": std_daily,
        "fact": fact_sum,
        "days_passed": days_passed,
        "total_days": total_days,
        "remaining_days": remaining_days,
    }

    current_period = pd.Period(current_month)
    prev_period = current_period - 12
    prev_period_str = str(prev_period)

    if prev_period_str in df["month"].unique():
        prev_data = with_data(df[df["month"] == prev_period_str])
        prev_fact = prev_data[prev_data["date"].dt.day <= days_passed]
        prev_remaining = prev_data[prev_data["date"].dt.day > days_passed]

        if not prev_fact.empty and prev_fact["value"].sum() > 0 and prev_fact["value"].sum() >= 10:
            seasonality_factor = fact_sum / prev_fact["value"].sum()
            seasonality_factor = max(0.3, min(3.0, seasonality_factor))
            forecast_remaining = prev_remaining["value"].sum() * seasonality_factor
            seas_base = fact_sum + forecast_remaining
            seas_min = fact_sum + forecast_remaining * 0.9
            seas_max = fact_sum + forecast_remaining * 1.1

            season_forecast = {
                "base": seas_base,
                "min": seas_min,
                "max": seas_max,
                "seasonality_factor": seasonality_factor,
                "fact": fact_sum,
                "days_passed": days_passed,
                "total_days": total_days,
                "remaining_days": remaining_days,
                "prev_fact_sum": prev_fact["value"].sum(),
                "prev_remaining_sum": prev_remaining["value"].sum(),
                "forecast_remaining": forecast_remaining,
                "has_prev_year": True,
                "prev_period": prev_period_str,
            }
        else:
            season_forecast = None
    else:
        season_forecast = None

    return stat_forecast, season_forecast

# ============================================================
# 9. Основна програма
# ============================================================
st.title("📊 Dashboard погоджень КПО")
st.caption("Дані завантажуються напряму з Google Таблиці. Кеш оновлюється кожні 5 хвилин. Час — за Києвом.")

try:
    df, load_warnings = load_data()
except Exception as exc:
    st.error("Не вдалося завантажити Google Таблицю.")
    st.code(str(exc))
    st.info(
        "Перевір: 1) чи увімкнений Google Sheets API, "
        "2) чи надано service account доступ до таблиці, "
        "3) чи правильно додані secrets у Streamlit."
    )
    st.stop()

if load_warnings:
    with st.expander(f"⚠️ Попередження при завантаженні даних ({len(load_warnings)})", expanded=False):
        for w in load_warnings:
            st.warning(w)

# ============================================================
# 10. Бокова панель (фільтри)
# ============================================================
st.sidebar.header("Фільтри")

period_mode = st.sidebar.radio(
    "Тип періоду",
    options=["За місяцями", "Довільний діапазон дат"],
    index=0,
)

min_date = df["date"].min()
max_date = df["date"].max()
years = sorted(df["year"].unique())
current_year = now_kyiv().year
default_years = [current_year] if current_year in years else [years[-1]] if years else []

custom_range = None

if period_mode == "За місяцями":
    selected_years = st.sidebar.multiselect("Рік", options=years, default=default_years)
    available_months = df[df["year"].isin(selected_years)]["month"].drop_duplicates().sort_values().tolist()
    if len(selected_years) == 1 and selected_years[0] != current_year:
        default_months = available_months
    else:
        current_month_str = now_kyiv().strftime("%Y-%m")
        default_months = [current_month_str] if current_month_str in available_months else available_months[-1:] if available_months else []
    selected_months = st.sidebar.multiselect(
        "Місяць",
        options=available_months,
        default=default_months,
        format_func=lambda x: pd.Period(x).strftime("%m.%Y"),
    )
else:
    default_start = max(min_date, max_date - pd.Timedelta(days=13))
    date_range_input = st.sidebar.date_input(
        "Діапазон дат",
        value=(default_start.date(), max_date.date()),
        min_value=min_date.date(),
        max_value=max_date.date(),
    )
    if isinstance(date_range_input, tuple) and len(date_range_input) == 2:
        custom_range = (pd.Timestamp(date_range_input[0]), pd.Timestamp(date_range_input[1]))
    else:
        single = date_range_input[0] if isinstance(date_range_input, tuple) else date_range_input
        custom_range = (pd.Timestamp(single), pd.Timestamp(single))
        st.sidebar.info("Оберіть другу дату діапазону.")
    if custom_range[0] > custom_range[1]:
        custom_range = (custom_range[1], custom_range[0])
    selected_years = sorted({custom_range[0].year, custom_range[1].year})
    selected_months = sorted({d.strftime("%Y-%m") for d in pd.date_range(custom_range[0], custom_range[1], freq="D")})

operation_mode = st.sidebar.radio("Режим показу", options=["Тотал", "Вибрані операції"], index=0)
_present_ops = set(df["operation"].unique()) - {"Тотал"}
all_ops = [op for op in OPERATIONS if op in _present_ops]
all_ops += sorted(_present_ops - set(all_ops))
if operation_mode == "Тотал":
    selected_operations = ["Тотал"]
else:
    default_ops = all_ops if all_ops else []
    selected_operations = st.sidebar.multiselect("Операції", options=all_ops, default=default_ops)
    if not selected_operations:
        st.sidebar.warning("Виберіть хоча б одну операцію.")
        selected_operations = ["Тотал"]

st.sidebar.divider()
st.sidebar.subheader("Налаштування графіків")
smooth_enabled = st.sidebar.checkbox("Згладжування динаміки (ковзне середнє)", value=False)
smooth_window = 7
if smooth_enabled:
    smooth_window = st.sidebar.selectbox("Вікно згладжування (дні)", [3, 5, 7, 14], index=2)

# ============================================================
# 11. Фільтрація даних
# ============================================================
if operation_mode == "Тотал":
    op_mask = df["operation"] == "Тотал"
else:
    op_mask = df["operation"].isin(selected_operations)

if period_mode == "За місяцями":
    filtered = df[
        df["year"].isin(selected_years)
        & df["month"].isin(selected_months)
        & op_mask
    ].copy()
else:
    filtered = df[
        (df["date"] >= custom_range[0])
        & (df["date"] <= custom_range[1])
        & op_mask
    ].copy()

if filtered.empty:
    st.warning("За вибраними фільтрами даних немає.")
    st.stop()

today = now_kyiv()
if period_mode == "За місяцями":
    if len(selected_months) == 1:
        period = pd.Period(selected_months[0])
        if period.start_time <= today <= period.end_time:
            filtered = filtered[filtered["date"] <= today]
            num_days = (today - period.start_time).days + 1
        else:
            num_days = period.days_in_month
    else:
        num_days = sum(pd.Period(m).days_in_month for m in selected_months)
else:
    filtered = filtered[filtered["date"] <= today]
    num_days = (custom_range[1] - custom_range[0]).days + 1

if filtered.empty:
    st.warning("За вибраними фільтрами даних немає (можливо, ще немає даних за цей період).")
    st.stop()

filtered_stats = with_data(filtered)
if filtered_stats.empty:
    st.info("Дані ще не внесені для жодного дня у вибраному періоді — статистика недоступна, показано лише графіки.")

# ============================================================
# 12. Розрахунок метрик
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
busiest_op, busiest_op_val = calc_busiest_operation(filtered)
std, cv, cv_interp = calc_stability(filtered, daily_avg)

# --- Коефіцієнт погоджень ---
# TRUE/FALSE тепер зберігаються в кожному рядку (дата, операція), а не одним
# значенням на місяць, тому рахуємо напряму з filtered_stats: працює для
# будь-якого діапазону дат і будь-якого набору вибраних операцій, без
# обмеження "лише повні місяці".
if not filtered_stats.empty:
    sum_true_total = float(filtered_stats["sum_true"].sum())
    sum_false_total = float(filtered_stats["sum_false"].sum())
    total_ratio = sum_true_total + sum_false_total
    approval_rate_val = (sum_true_total / total_ratio * 100) if total_ratio > 0 else 0
    approval_rate_str = f"{approval_rate_val:.1f}%" if total_ratio > 0 else "—"
    approval_rate_available = total_ratio > 0
else:
    sum_true_total = sum_false_total = 0.0
    approval_rate_val = 0
    approval_rate_str = "—"
    approval_rate_available = False

# --- Коефіцієнт погоджень по операціях (той самий період, всі операції) ---
if period_mode == "За місяцями":
    period_mask = df["year"].isin(selected_years) & df["month"].isin(selected_months)
else:
    period_mask = (df["date"] >= custom_range[0]) & (df["date"] <= custom_range[1])
period_mask &= df["date"] <= today

period_ops = with_data(df[period_mask & (df["operation"] != "Тотал")])

approval_by_op = pd.DataFrame(columns=["operation", "sum_true", "sum_false", "total", "approval_rate"])
if not period_ops.empty:
    approval_by_op = period_ops.groupby("operation", as_index=False)[["sum_true", "sum_false"]].sum()
    approval_by_op["total"] = approval_by_op["sum_true"] + approval_by_op["sum_false"]
    approval_by_op = approval_by_op[approval_by_op["total"] > 0].copy()
    approval_by_op["approval_rate"] = (approval_by_op["sum_true"] / approval_by_op["total"] * 100).round(1)
    approval_by_op = approval_by_op.sort_values("approval_rate", ascending=False)

# --- Порівняння ---
comparison_parts = []
if period_mode == "За місяцями" and len(selected_months) == 1 and operation_mode == "Тотал":
    current_period = pd.Period(selected_months[0])
    today_comp = now_kyiv()
    prev_period = current_period - 1
    if current_period.end_time <= today_comp:
        cur_sum = daily_total.sum()
        prev_sum = with_data(df[
            (df["month"] == str(prev_period))
            & (df["operation"] == "Тотал")
        ])["value"].sum()
        delta_prev = ((cur_sum - prev_sum) / prev_sum * 100) if prev_sum > 0 else None
    else:
        day_limit = today_comp.day
        cur_sum = filtered_stats[filtered_stats["date"].dt.day <= day_limit]["value"].sum()
        days_in_prev = prev_period.days_in_month
        day_limit_prev = min(day_limit, days_in_prev)
        prev_sum = with_data(df[
            (df["month"] == str(prev_period))
            & (df["operation"] == "Тотал")
            & (df["date"].dt.day <= day_limit_prev)
        ])["value"].sum()
        delta_prev = ((cur_sum - prev_sum) / prev_sum * 100) if prev_sum > 0 else None
    if delta_prev is not None:
        comparison_parts.append(f"Попер. міс: {delta_prev:+.1f}%")

    year_prev = current_period.year - 1
    month_num = current_period.month
    prev_year_period = pd.Period(year=year_prev, month=month_num, freq="M")
    has_prev_year = not df[
        (df["month"] == str(prev_year_period))
        & (df["operation"] == "Тотал")
    ].empty
    if has_prev_year:
        if current_period.end_time <= today_comp:
            cur_sum = daily_total.sum()
            prev_year_sum = with_data(df[
                (df["month"] == str(prev_year_period))
                & (df["operation"] == "Тотал")
            ])["value"].sum()
            delta_year = ((cur_sum - prev_year_sum) / prev_year_sum * 100) if prev_year_sum > 0 else None
        else:
            day_limit = today_comp.day
            cur_sum = filtered_stats[filtered_stats["date"].dt.day <= day_limit]["value"].sum()
            days_in_prev_year = prev_year_period.days_in_month
            day_limit_prev_year = min(day_limit, days_in_prev_year)
            prev_year_sum = with_data(df[
                (df["month"] == str(prev_year_period))
                & (df["operation"] == "Тотал")
                & (df["date"].dt.day <= day_limit_prev_year)
            ])["value"].sum()
            delta_year = ((cur_sum - prev_year_sum) / prev_year_sum * 100) if prev_year_sum > 0 else None
        if delta_year is not None:
            comparison_parts.append(f"Мин. рік: {delta_year:+.1f}%")
comparison_text = "  ".join(comparison_parts) if comparison_parts else "—"

# ============================================================
# 13. Функція custom_metric та CSS
# ============================================================
def custom_metric(label, value, help_text=None, color=None):
    safe_label = html.escape(str(label))
    safe_value = html.escape(str(value))
    help_icon = ""
    if help_text:
        safe_help = html.escape(str(help_text))
        help_icon = f'<span class="help-icon" title="{safe_help}">?</span>'
    value_style = f' style="color:{html.escape(color)};"' if color else ""
    return f"""
    <div class="metric-container">
        <div class="metric-label">{safe_label} {help_icon}</div>
        <div class="metric-value"{value_style}>{safe_value}</div>
    </div>
    """

def approval_rate_color(value):
    if value is None:
        return None
    if value >= APPROVAL_GOOD_THRESHOLD:
        return COLOR_GOOD
    if value >= APPROVAL_WARN_THRESHOLD:
        return COLOR_WARN
    return COLOR_BAD

def approval_rate_tier(value):
    if value >= APPROVAL_GOOD_THRESHOLD:
        return f"🟢 Високий (≥{APPROVAL_GOOD_THRESHOLD}%)"
    if value >= APPROVAL_WARN_THRESHOLD:
        return f"🟡 Середній ({APPROVAL_WARN_THRESHOLD}-{APPROVAL_GOOD_THRESHOLD}%)"
    return f"🔴 Низький (<{APPROVAL_WARN_THRESHOLD}%)"

APPROVAL_TIER_COLOR_MAP = {
    approval_rate_tier(100): COLOR_GOOD,
    approval_rate_tier(APPROVAL_WARN_THRESHOLD): COLOR_WARN,
    approval_rate_tier(0): COLOR_BAD,
}

def cv_color(value):
    if value is None or value <= 0:
        return None
    if value < 15:
        return COLOR_GOOD
    if value < 30:
        return COLOR_WARN
    return COLOR_BAD

st.markdown(f"""
<style>
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@500;600;700&display=swap');
    html, body, [class*="css"] {{ font-family: 'Inter', sans-serif; }}
    .stApp {{ background-color: {KPO_BG}; }}
    .metric-container {{
        background: {KPO_CARD_BG};
        border: 1px solid {KPO_BORDER};
        border-left: 3px solid {KPO_CYAN};
        border-radius: 8px;
        padding: 0.65rem 0.9rem;
        margin-bottom: 0.5rem;
        transition: border-left-color 0.15s ease;
    }}
    .metric-container:hover {{ border-left-color: {KPO_AMBER}; }}
    .metric-label {{
        font-size: 0.7rem !important;
        font-weight: 500;
        text-transform: uppercase;
        letter-spacing: 0.06em;
        margin-bottom: 0.3rem;
        color: {KPO_TEXT_MUTED} !important;
    }}
    .metric-value {{
        font-family: 'JetBrains Mono', monospace;
        font-size: 1.45rem !important;
        font-weight: 700;
        line-height: 1.2;
        color: {KPO_TEXT} !important;
    }}
    .help-icon {{
        display: inline-block;
        background: rgba(0, 217, 255, 0.15);
        border-radius: 50%;
        width: 15px;
        height: 15px;
        text-align: center;
        line-height: 15px;
        font-size: 0.62rem;
        color: {KPO_CYAN} !important;
        cursor: help;
        margin-left: 3px;
    }}
    .comparison-text {{
        font-family: 'JetBrains Mono', monospace;
        font-size: 0.85rem !important;
        line-height: 1.4 !important;
        margin: 0 !important;
        color: {KPO_TEXT} !important;
    }}
    .stTabs [data-baseweb="tab-list"] {{ gap: 4px; border-bottom: 1px solid {KPO_BORDER}; }}
    .stTabs [data-baseweb="tab"] {{
        background-color: transparent;
        border-radius: 6px 6px 0 0;
        color: {KPO_TEXT_MUTED};
        padding: 8px 18px;
        font-weight: 500;
    }}
    .stTabs [aria-selected="true"] {{
        background-color: {KPO_CARD_BG} !important;
        color: {KPO_CYAN} !important;
        border-bottom: 2px solid {KPO_CYAN} !important;
    }}
    section[data-testid="stSidebar"] {{ background-color: #0e131d; border-right: 1px solid {KPO_BORDER}; }}
    h1, h2, h3 {{ font-family: 'Inter', sans-serif; letter-spacing: -0.01em; }}
    hr {{ border-color: {KPO_BORDER} !important; }}
</style>
""", unsafe_allow_html=True)

def forecast_cards(title, forecast, help_base=None, help_min=None, help_max=None):
    if forecast is None:
        return
    col1, col2, col3 = st.columns(3)
    with col1:
        st.markdown(custom_metric(f"{title} (базовий)", f"{forecast['base']:,.0f}", help_base), unsafe_allow_html=True)
    with col2:
        st.markdown(custom_metric(f"{title} (консервативний)", f"{forecast['min']:,.0f}", help_min), unsafe_allow_html=True)
    with col3:
        st.markdown(custom_metric(f"{title} (оптимістичний)", f"{forecast['max']:,.0f}", help_max), unsafe_allow_html=True)

# ============================================================
# 14. Вкладки
# ============================================================
tab1, tab2, tab3, tab4, tab5 = st.tabs(["📊 Overview", "📈 Динаміка", "🧩 Операції", "📅 Навантаження", "🆚 Порівняння періодів"])

# ============================================================
# TAB 1: OVERVIEW
# ============================================================
with tab1:
    col1, col2, col3, col4, col5, col6 = st.columns(6)
    with col1:
        st.markdown(custom_metric("Всього", f"{total_value:,.0f}", "Загальна кількість операцій за вибраний період (включно з календарними днями без внесених даних)"), unsafe_allow_html=True)
    with col2:
        st.markdown(custom_metric("Середнє за день", f"{daily_avg:.0f}", "Сумарна кількість поділена на кількість днів, за які реально внесені дані (порожні клітинки не враховуються як нулі)"), unsafe_allow_html=True)
    with col3:
        avg_weekday_str = f"{daily_avg_weekday:.0f}" if daily_avg_weekday is not None and not pd.isna(daily_avg_weekday) else "—"
        st.markdown(custom_metric("Середнє за будні", avg_weekday_str, "Середня кількість операцій у будні (лише дні з внесеними даними)"), unsafe_allow_html=True)
    with col4:
        avg_weekend_str = f"{daily_avg_weekend:.0f}" if daily_avg_weekend is not None and not pd.isna(daily_avg_weekend) else "—"
        st.markdown(custom_metric("Середнє за вихідні", avg_weekend_str, "Середня кількість операцій у вихідні (лише дні з внесеними даними)"), unsafe_allow_html=True)
    with col5:
        peak_display = f"{peak:,.0f}" if peak > 0 else "—"
        if peak_date is not None:
            peak_display += f" ({peak_date.strftime('%d.%m')})"
        st.markdown(custom_metric("Пік за день", peak_display, "Найбільша кількість операцій за один день (лише дні з внесеними даними). У дужках – дата піку."), unsafe_allow_html=True)
    with col6:
        st.markdown(custom_metric(
            "Коефіцієнт погоджень",
            approval_rate_str,
            f"Частка TRUE (погоджено) від TRUE+FALSE за вибраний період і вибрані операції. "
            f"🟢 ≥{APPROVAL_GOOD_THRESHOLD}% 🟡 {APPROVAL_WARN_THRESHOLD}-{APPROVAL_GOOD_THRESHOLD}% 🔴 <{APPROVAL_WARN_THRESHOLD}%",
            color=approval_rate_color(approval_rate_val if approval_rate_available else None),
        ), unsafe_allow_html=True)

    col7, col8, col9, col10, col11 = st.columns(5)
    with col7:
        st.markdown(custom_metric("Пік / середнє", f"{peak_avg_ratio:.2f}×", "У скільки разів пік перевищує середнє"), unsafe_allow_html=True)
    with col8:
        st.markdown(custom_metric("Стабільність (CV)", f"{cv:.1f}%" if cv > 0 else "—", "Коефіцієнт варіації (лише дні з внесеними даними). 🟢 <15% 🟡 15-30% 🔴 >30%", color=cv_color(cv if cv > 0 else None)), unsafe_allow_html=True)
    with col9:
        if busiest_weekday:
            day_ua = WEEKDAY_UA.get(busiest_weekday, busiest_weekday)
            val = f"{day_ua} — {busiest_weekday_val:.0f}/день"
            help_txt = "День тижня з найвищим середнім навантаженням (лише дні з внесеними даними)"
        else:
            val = "—"
            help_txt = None
        st.markdown(custom_metric("Найактивніший день", val, help_txt), unsafe_allow_html=True)
    with col10:
        if busiest_op:
            display_name = busiest_op if len(busiest_op) <= 12 else busiest_op[:10] + "…"
            val = f'{display_name} — {busiest_op_val:,.0f}'
            help_txt = f"{busiest_op} — {busiest_op_val:,.0f} (повна назва)"
        else:
            val = "—"
            help_txt = None
        st.markdown(custom_metric("Найактивніша операція", val, help_txt), unsafe_allow_html=True)
    with col11:
        if period_mode == "За місяцями" and len(selected_months) == 1 and operation_mode == "Тотал":
            st.markdown("**Порівняння**")
            if comparison_text != "—":
                parts = comparison_text.split("  ")
                for part in parts:
                    st.markdown(f"<p class='comparison-text'>{html.escape(part)}</p>", unsafe_allow_html=True)
            else:
                st.markdown("<p class='comparison-text'>—</p>", unsafe_allow_html=True)
        else:
            st.markdown(custom_metric("Порівняння", "—", "Доступно лише для одного місяця в режимі 'За місяцями' + 'Тотал'"), unsafe_allow_html=True)

    st.divider()
    st.subheader("💡 Інсайти")
    insights = []
    if approval_rate_available:
        if approval_rate_val >= APPROVAL_GOOD_THRESHOLD:
            insights.append(f"🟢 Коефіцієнт погоджень **{approval_rate_str}** — вище порогу {APPROVAL_GOOD_THRESHOLD}%, хороший показник.")
        elif approval_rate_val >= APPROVAL_WARN_THRESHOLD:
            insights.append(f"🟡 Коефіцієнт погоджень **{approval_rate_str}** — у середній зоні ({APPROVAL_WARN_THRESHOLD}-{APPROVAL_GOOD_THRESHOLD}%), варто стежити за динамікою.")
        else:
            insights.append(f"🔴 Коефіцієнт погоджень **{approval_rate_str}** — нижче {APPROVAL_WARN_THRESHOLD}%, потребує уваги.")
    else:
        insights.append("ℹ️ Коефіцієнт погоджень недоступний для вибраного діапазону (потрібні повні календарні місяці).")

    if not approval_by_op.empty and len(approval_by_op) > 1:
        best_row = approval_by_op.iloc[0]
        worst_row = approval_by_op.iloc[-1]
        if best_row["operation"] != worst_row["operation"]:
            insights.append(f"🧩 Найкращий % погоджень — **{best_row['operation']}** ({best_row['approval_rate']:.1f}%), найгірший — **{worst_row['operation']}** ({worst_row['approval_rate']:.1f}%).")
    if cv > 0:
        insights.append(f"{cv_interp} денного навантаження (CV = {cv:.1f}%).")
    if busiest_weekday:
        day_ua = WEEKDAY_UA.get(busiest_weekday, busiest_weekday)
        insights.append(f"📅 Найбільше навантаження припадає на **{day_ua}** — в середньому {busiest_weekday_val:.0f} операцій/день.")
    if busiest_op:
        insights.append(f"📈 Найактивніша операція за обсягом — **{busiest_op}** ({busiest_op_val:,.0f} за період).")
    if peak_avg_ratio >= 2:
        insights.append(f"⚠️ Пік у **{peak_avg_ratio:.1f}×** перевищує середнє — можливі різкі сплески навантаження, варто мати запас потужності.")
    if period_mode == "За місяцями" and comparison_text != "—":
        insights.append(f"🔄 Порівняння з попередніми періодами: {comparison_text}.")
    for i in insights:
        st.markdown(f"- {i}")

    st.divider()

    # --- Прогнози ---
    if period_mode != "За місяцями":
        st.info("📊 Прогнози доступні лише в режимі 'За місяцями' з одним обраним місяцем.")
    elif len(selected_months) != 1:
        st.info("📊 Прогнози доступні лише для одного обраного місяця (виберіть один місяць у сайдбарі).")
    else:
        forecast_target_options = ["Тотал"] + all_ops
        forecast_target = st.selectbox(
            "Прогнозувати для:",
            options=forecast_target_options,
            index=0,
            help="Прогноз можна будувати для Тоталу або окремої операції — незалежно від 'Режиму показу' графіків нижче.",
        )
        stat_forecast, season_forecast = forecast_scenarios(
            df[df["operation"] == forecast_target], selected_months[0]
        )
        if stat_forecast or season_forecast:
            st.subheader(f"📊 Прогнози на поточний місяць — {forecast_target}")
            if stat_forecast:
                st.markdown("**📈 Статистичний прогноз обсягу** (на основі середнього та варіативності минулих днів)")
                forecast_cards(
                    "Стат.",
                    stat_forecast,
                    help_base="факт + середнє × залишок днів",
                    help_min="факт + (середнє − 0.5×σ) × залишок (не менше 0)",
                    help_max="факт + (середнє + 0.5×σ) × залишок"
                )
            if season_forecast:
                st.markdown("**📅 Сезонний прогноз обсягу** (на основі динаміки аналогічного періоду минулого року)")
                with st.expander("🔍 Деталі розрахунку сезонного прогнозу"):
                    st.write(f"**Період минулого року:** {season_forecast['prev_period']}")
                    st.write(f"**Днів минуло:** {season_forecast['days_passed']}")
                    st.write(f"**Сума за поточний період (факт):** {season_forecast['fact']:,.0f}")
                    st.write(f"**Сума за аналогічний період минулого року:** {season_forecast['prev_fact_sum']:,.0f}")
                    st.write(f"**Коефіцієнт сезонності:** {season_forecast['seasonality_factor']:.3f}")
                    st.write(f"**Сума за залишок місяця (минулий рік):** {season_forecast['prev_remaining_sum']:,.0f}")
                    st.write(f"**Прогноз на залишок (з урахуванням коефіцієнта):** {season_forecast['forecast_remaining']:,.0f}")
                    st.write(f"**Загальний прогноз (факт + прогноз на залишок):** {season_forecast['base']:,.0f}")
                forecast_cards(
                    "Сезон.",
                    season_forecast,
                    help_base="факт + (залишок минулого року × коеф. сезонності)",
                    help_min="факт + 0.9 × прогноз на залишок",
                    help_max="факт + 1.1 × прогноз на залишок"
                )
        else:
            st.info("Прогноз недоступний: немає фактичних даних за поточний місяць для цієї операції.")

    st.divider()

    # --- Динаміка за період ---
    st.subheader("📈 Динаміка за період")
    if operation_mode == "Тотал":
        daily = filtered.groupby("date")["value"].sum().reset_index()
        fig_overview = px.line(daily, x="date", y="value", markers=True, labels={"date": "Дата", "value": "Кількість"}, color_discrete_sequence=[KPO_CYAN])
        fig_overview.update_xaxes(tickformat="%d.%m", title_text="Дата")
        if smooth_enabled:
            daily["value_smooth"] = daily["value"].rolling(window=smooth_window, min_periods=1, center=True).mean()
            fig_overview.add_scatter(x=daily["date"], y=daily["value_smooth"], mode="lines", name=f"Ковзне середнє ({smooth_window} дн.)", line=dict(color=KPO_AMBER, width=3))
        anomalies = detect_anomalies(filtered, window=14, threshold=3.0)
        if not anomalies.empty:
            anomaly_points = anomalies[anomalies["is_anomaly"]]
            if not anomaly_points.empty:
                fig_overview.add_scatter(x=anomaly_points["date"], y=anomaly_points["value"], mode="markers", marker=dict(color=KPO_RED, size=10, symbol="x"), name="Аномалія")
    else:
        fig_overview = px.line(filtered, x="date", y="value", color="operation", markers=True, labels={"date": "Дата", "value": "Кількість", "operation": "Операція"})
        fig_overview.update_xaxes(tickformat="%d.%m", title_text="Дата")
    fig_overview.update_layout(height=420, hovermode="x unified", margin=dict(l=10, r=10, t=20, b=10))
    st.plotly_chart(fig_overview, use_container_width=True)

# ============================================================
# TAB 2: ДИНАМІКА
# ============================================================
with tab2:
    st.subheader("📈 Детальна динаміка")
    if operation_mode == "Тотал":
        daily = filtered.groupby("date")["value"].sum().reset_index()
        fig_daily_detailed = px.line(daily, x="date", y="value", markers=True, labels={"date": "Дата", "value": "Кількість"}, title="Щоденна динаміка")
        fig_daily_detailed.update_xaxes(tickformat="%d.%m", title_text="Дата")
        if smooth_enabled:
            daily["value_smooth"] = daily["value"].rolling(window=smooth_window, min_periods=1, center=True).mean()
            fig_daily_detailed.add_scatter(x=daily["date"], y=daily["value_smooth"], mode="lines", name=f"Ковзне середнє ({smooth_window} дн.)", line=dict(color=KPO_AMBER, width=3))
        anomalies = detect_anomalies(filtered, window=14, threshold=3.0)
        if not anomalies.empty:
            anomaly_points = anomalies[anomalies["is_anomaly"]]
            if not anomaly_points.empty:
                fig_daily_detailed.add_scatter(x=anomaly_points["date"], y=anomaly_points["value"], mode="markers", marker=dict(color=KPO_RED, size=10, symbol="x"), name="Аномалія")
    else:
        fig_daily_detailed = px.line(filtered, x="date", y="value", color="operation", markers=True, labels={"date": "Дата", "value": "Кількість", "operation": "Операція"}, title="Динаміка вибраних операцій")
        fig_daily_detailed.update_xaxes(tickformat="%d.%m", title_text="Дата")
    fig_daily_detailed.update_layout(height=400, hovermode="x unified", margin=dict(l=10, r=10, t=20, b=10))
    st.plotly_chart(fig_daily_detailed, use_container_width=True)

    if operation_mode == "Тотал":
        st.subheader("📊 Порівняння по роках (YoY)")
        yoy_data = with_data(df[df["operation"] == "Тотал"]).copy()
        yoy_data = yoy_data[yoy_data["year"].isin(selected_years)]
        yoy_monthly = yoy_data.groupby(["year", "month"])["value"].sum().reset_index()
        yoy_monthly["month_num"] = yoy_monthly["month"].apply(lambda x: pd.Period(x).month)
        yoy_monthly["month_label"] = yoy_monthly["month"].apply(lambda x: pd.Period(x).strftime("%b"))
        yoy_monthly = yoy_monthly.sort_values(["year", "month_num"])
        month_axis_order = yoy_monthly.drop_duplicates("month_num").sort_values("month_num")["month_label"].tolist()
        fig_yoy = px.line(yoy_monthly, x="month_label", y="value", color="year", markers=True, labels={"month_label": "Місяць", "value": "Кількість", "year": "Рік"}, title="Порівняння місячних сум по роках", category_orders={"month_label": month_axis_order})
        fig_yoy.update_layout(height=380, margin=dict(l=10, r=10, t=20, b=10))
        st.plotly_chart(fig_yoy, use_container_width=True)
        if len(selected_years) >= 2:
            years_sorted = sorted(selected_years)
            if len(years_sorted) >= 2:
                y1, y2 = years_sorted[-2], years_sorted[-1]
                y1_data = yoy_monthly[yoy_monthly["year"] == y1].set_index("month_label")["value"]
                y2_data = yoy_monthly[yoy_monthly["year"] == y2].set_index("month_label")["value"]
                compare_df = pd.DataFrame({str(y1): y1_data, str(y2): y2_data}).fillna(0)
                compare_df["Різниця"] = compare_df[str(y2)] - compare_df[str(y1)]
                compare_df["%"] = (compare_df["Різниця"] / compare_df[str(y1)] * 100).fillna(0)
                compare_df["%"] = compare_df["%"].apply(lambda x: f"{x:+.1f}%")
                st.dataframe(compare_df, use_container_width=True)

    st.subheader("📈 Накопичувальна сума за період")
    cumsum = filtered.groupby("date")["value"].sum().sort_index().cumsum().reset_index()
    cumsum.columns = ["date", "cumulative"]
    fig_cum = px.line(cumsum, x="date", y="cumulative", markers=True, labels={"date": "Дата", "cumulative": "Накопичена кількість"})
    fig_cum.update_xaxes(tickformat="%d.%m", title_text="Дата")
    fig_cum.update_layout(height=380, margin=dict(l=10, r=10, t=20, b=10))
    st.plotly_chart(fig_cum, use_container_width=True)

    st.subheader("🔍 Аномальні дні")
    anomalies = detect_anomalies(filtered, window=14, threshold=3.0)
    if not anomalies.empty:
        anomaly_points = anomalies[anomalies["is_anomaly"]]
        if not anomaly_points.empty:
            anomaly_points = anomaly_points.copy()
            anomaly_points["date_str"] = anomaly_points["date"].dt.strftime("%d.%m.%Y")
            anomaly_points["deviation"] = ((anomaly_points["value"] - anomaly_points["rolling_median"]) / anomaly_points["rolling_median"] * 100).round(1)
            anomaly_points["type"] = anomaly_points["deviation"].apply(lambda x: "🔴 Високий" if x > 10 else "🔵 Низький" if x < -10 else "🟡 Помірний")
            anomaly_points = anomaly_points.sort_values("date", ascending=False)
            st.dataframe(
                anomaly_points[["date_str", "value", "rolling_median", "deviation", "type", "z_score"]],
                column_config={
                    "date_str": "Дата",
                    "value": "Значення",
                    "rolling_median": "Медіана (14 днів)",
                    "deviation": "Відхилення, %",
                    "type": "Тип",
                    "z_score": st.column_config.NumberColumn("Z-score (MAD)", help="Кількість MAD від медіани. Значення >3.0 вважається аномалією.")
                },
                use_container_width=True,
                hide_index=True
            )
        else:
            st.info("Аномальних днів не виявлено.")
    else:
        st.info("Недостатньо даних для виявлення аномалій.")

# ============================================================
# TAB 3: ОПЕРАЦІЇ
# ============================================================
with tab3:
    st.subheader("🧩 Аналіз операцій")
    st.subheader("✅ Коефіцієнт погоджень по операціях")
    if not approval_by_op.empty:
        tf_total = df[period_mask & (df["operation"] == "Тотал")]
        total_true = tf_total["sum_true"].sum()
        total_false = tf_total["sum_false"].sum()
        total_ratio_ops = total_true + total_false
        total_rate = (total_true / total_ratio_ops * 100) if total_ratio_ops > 0 else None

        approval_by_op_display = approval_by_op.copy()
        approval_by_op_display["tier"] = approval_by_op_display["approval_rate"].apply(approval_rate_tier)
        fig_approval = px.bar(
            approval_by_op_display,
            x="operation",
            y="approval_rate",
            color="tier",
            color_discrete_map=APPROVAL_TIER_COLOR_MAP,
            text=approval_by_op_display["approval_rate"].astype(str) + "%",
            labels={"operation": "Операція", "approval_rate": "Коефіцієнт погоджень, %", "tier": "Категорія"},
            title="Коефіцієнт погоджень по операціях (за вибраний період)",
            category_orders={"operation": approval_by_op_display["operation"].tolist()},
        )
        fig_approval.update_traces(textposition="outside", width=0.55)

        max_rate = approval_by_op_display["approval_rate"].max()
        y_max = max(105, max_rate * 1.12)
        fig_approval.update_layout(
            height=360,
            margin=dict(l=10, r=10, t=20, b=10),
            yaxis=dict(range=[0, y_max]),
            bargap=0.45,
            legend_title_text="Категорія"
        )

        if total_rate is not None:
            fig_approval.add_hline(
                y=total_rate,
                line_dash="dash",
                line_color=KPO_RED,
                line_width=4,
                annotation_text=f"Тотал: {total_rate:.1f}%",
                annotation_position="top right",
                annotation_font=dict(size=16, color="white")
            )
            fig_approval.add_trace(
                go.Scatter(
                    x=[None], y=[None],
                    mode='lines',
                    line=dict(color=KPO_RED, width=4, dash='dash'),
                    name=f"Тотал: {total_rate:.1f}%",
                    showlegend=True
                )
            )

        st.plotly_chart(fig_approval, use_container_width=True)

        with st.expander("📋 Таблиця по операціях", expanded=False):
            st.dataframe(
                approval_by_op[["operation", "sum_true", "sum_false", "total", "approval_rate"]].rename(columns={
                    "operation": "Операція",
                    "sum_true": "Погоджено (TRUE)",
                    "sum_false": "Відхилено (FALSE)",
                    "total": "Всього",
                    "approval_rate": "Коефіцієнт погоджень, %",
                }),
                use_container_width=True,
                hide_index=True,
            )
    else:
        st.info("Немає даних для розрахунку коефіцієнта погоджень за вибраний період.")

    st.subheader("🌡️ Теплова карта коефіцієнта погоджень (Операція × Місяць)")
    tf_heat = (
        df[period_mask]
        .groupby(["month", "operation"], as_index=False)[["sum_true", "sum_false"]]
        .sum()
    )
    if not tf_heat.empty:
        tf_heat["total"] = tf_heat["sum_true"] + tf_heat["sum_false"]
        tf_heat = tf_heat[tf_heat["total"] > 0]
        if not tf_heat.empty:
            tf_heat["rate"] = tf_heat["sum_true"] / tf_heat["total"] * 100
            tf_heat["month_label"] = tf_heat["month"].apply(lambda x: pd.Period(x).strftime("%m.%Y"))
            heat_rate_pivot = tf_heat.pivot_table(index="month_label", columns="operation", values="rate", aggfunc="mean")
            cols = heat_rate_pivot.columns.tolist()
            if "Тотал" in cols:
                cols.remove("Тотал")
                cols = ["Тотал"] + cols
                heat_rate_pivot = heat_rate_pivot[cols]
            def _sort_month_label(month_str):
                try:
                    return datetime.strptime(month_str, "%m.%Y")
                except:
                    return datetime(1900, 1, 1)
            sorted_month_labels = sorted(heat_rate_pivot.index, key=_sort_month_label)
            heat_rate_pivot = heat_rate_pivot.reindex(sorted_month_labels)

            fig_approval_heat = px.imshow(heat_rate_pivot, text_auto=".1f", aspect="auto", labels=dict(x="Операція", y="Місяць", color="Коефіцієнт погоджень, %"), color_continuous_scale="RdYlGn", zmin=0, zmax=100)
            row_height = 38
            min_height = 420
            max_height = 2400
            heatmap_height = max(min_height, min(max_height, len(heat_rate_pivot.index) * row_height))
            fig_approval_heat.update_layout(height=heatmap_height, margin=dict(l=10, r=10, t=20, b=10))
            font_size = 12 if len(heat_rate_pivot.index) <= 12 else (10 if len(heat_rate_pivot.index) <= 24 else 8)
            fig_approval_heat.update_traces(textfont=dict(size=font_size))
            st.plotly_chart(fig_approval_heat, use_container_width=True)
            st.caption("🔴 <70% 🟡 70-85% 🟢 >85% — кольорова шкала неперервна.")
        else:
            st.info("Немає даних для теплової карти за вибраний період.")
    else:
        st.info("Немає даних для теплової карти.")

    st.divider()
    ops_data = with_data(filtered[filtered["operation"] != "Тотал"])
    if not ops_data.empty:
        st.subheader("📊 Структура операцій (за період)")
        ops_structure = ops_data.groupby("operation")["value"].sum().reset_index().sort_values("value", ascending=False)
        ops_structure["percent"] = (ops_structure["value"] / ops_structure["value"].sum() * 100).round(1)
        ops_structure["text"] = ops_structure["percent"].astype(str) + "%"
        fig_ops_structure = px.bar(ops_structure, x="value", y="operation", text="text", orientation="h", labels={"value": "Кількість", "operation": "Операція"}, title="Структура за період")
        fig_ops_structure.update_traces(textposition="outside")
        fig_ops_structure.update_layout(height=360, margin=dict(l=10, r=10, t=20, b=10), yaxis={"categoryorder": "total descending"})
        st.plotly_chart(fig_ops_structure, use_container_width=True)

        st.subheader("📈 Динаміка структури операцій по місяцях")
        ops_monthly = ops_data.groupby(["month", "operation"])["value"].sum().reset_index()
        ops_monthly["month_label"] = ops_monthly["month"].apply(lambda x: pd.Period(x).strftime("%m.%Y"))
        fig_stacked = px.bar(ops_monthly, x="month_label", y="value", color="operation", barmode="stack", labels={"month_label": "Місяць", "value": "Кількість", "operation": "Операція"}, title="Структура операцій по місяцях")
        fig_stacked.update_layout(height=400, margin=dict(l=10, r=10, t=20, b=10))
        st.plotly_chart(fig_stacked, use_container_width=True)

        st.subheader("📊 Pareto аналіз операцій")
        pareto_data = ops_structure.copy().sort_values("value", ascending=False)
        pareto_data["cumulative_percent"] = pareto_data["percent"].cumsum()
        fig_pareto = go.Figure()
        fig_pareto.add_trace(go.Bar(x=pareto_data["operation"], y=pareto_data["value"], name="Кількість", marker_color=KPO_CYAN, yaxis="y"))
        fig_pareto.add_trace(go.Scatter(x=pareto_data["operation"], y=pareto_data["cumulative_percent"], name="Накопичувальна частка, %", mode="lines+markers", marker_color=KPO_AMBER, yaxis="y2"))
        fig_pareto.add_hline(y=80, line_dash="dash", line_color="gray", annotation_text="80%", annotation_position="top right")
        fig_pareto.update_layout(title="Pareto операцій", xaxis_title="Операція", yaxis=dict(title="Кількість", side="left", showgrid=True), yaxis2=dict(title="Накопичувальна частка, %", overlaying="y", side="right", range=[0, 100]), legend=dict(x=0.8, y=0.9), height=400, margin=dict(l=10, r=10, t=20, b=10))
        st.plotly_chart(fig_pareto, use_container_width=True)

        if operation_mode != "Тотал" and len(selected_operations) > 1:
            st.subheader("📈 Порівняння вибраних операцій")
            fig_compare_ops = px.line(filtered, x="date", y="value", color="operation", markers=True, labels={"date": "Дата", "value": "Кількість", "operation": "Операція"}, title="Динаміка вибраних операцій")
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
    fig_weekday_avg = px.bar(weekday_avg, x="weekday", y="avg_value", text=weekday_avg["avg_value"].round(1).astype(str), labels={"weekday": "День тижня", "avg_value": "Середня кількість"}, title="Середня кількість операцій по днях тижня (лише дні з внесеними даними)")
    fig_weekday_avg.update_traces(textposition="outside")
    fig_weekday_avg.update_layout(height=360, margin=dict(l=10, r=10, t=20, b=10))
    st.plotly_chart(fig_weekday_avg, use_container_width=True)

    st.subheader("📅 Будні vs вихідні")
    week_data = filtered.assign(period_type=filtered["is_weekend"].map({False: "Будні", True: "Вихідні"})).groupby(["month", "period_type"], as_index=False)["value"].sum()
    week_data["month_total"] = week_data.groupby("month")["value"].transform("sum")
    week_data["percent"] = (week_data["value"] / week_data["month_total"] * 100).round(1)
    week_data["text"] = week_data["percent"].astype(str) + "%"
    week_data["month_label"] = week_data["month"].apply(lambda x: pd.Period(x).strftime("%m.%Y"))
    fig_week = px.bar(week_data, x="month_label", y="value", color="period_type", barmode="group", text="text", labels={"month_label": "Місяць", "value": "Кількість", "period_type": ""})
    fig_week.update_traces(textposition="outside")
    fig_week.update_layout(height=380, margin=dict(l=10, r=10, t=20, b=10))
    st.plotly_chart(fig_week, use_container_width=True)

    st.subheader("🌡️ Теплова карта навантаження")
    daily_heat = filtered_stats.groupby("date")["value"].sum().reset_index()
    daily_heat["month_label"] = daily_heat["date"].dt.strftime("%m.%Y")
    daily_heat["weekday_ua"] = daily_heat["date"].dt.day_name().map(WEEKDAY_UA)
    heat_data = daily_heat.groupby(["month_label", "weekday_ua"])["value"].mean().reset_index()
    heat_pivot = heat_data.pivot(index="month_label", columns="weekday_ua", values="value").fillna(0)
    heat_pivot = heat_pivot.reindex(columns=WEEKDAY_ORDER_UA)
    def sort_months(month_str):
        try:
            return datetime.strptime(month_str, "%m.%Y")
        except:
            return datetime(1900, 1, 1)
    sorted_months = sorted(heat_pivot.index, key=sort_months)
    heat_pivot = heat_pivot.reindex(sorted_months)
    row_height = 38
    min_height = 420
    max_height = 2400
    heatmap_height = max(min_height, min(max_height, len(heat_pivot.index) * row_height))
    fig_heatmap = px.imshow(heat_pivot, text_auto=".1f", aspect="auto", labels=dict(x="День тижня", y="Місяць", color="Середня кількість"), color_continuous_scale=KPO_HEAT_SCALE)
    fig_heatmap.update_layout(height=heatmap_height, margin=dict(l=10, r=10, t=20, b=10))
    font_size = 12 if len(heat_pivot.index) <= 12 else (10 if len(heat_pivot.index) <= 24 else 8)
    fig_heatmap.update_traces(textfont=dict(size=font_size))
    st.markdown('<div style="overflow-x: auto; max-height: 90vh; position: relative;">', unsafe_allow_html=True)
    st.plotly_chart(fig_heatmap, use_container_width=True)
    st.markdown('</div>', unsafe_allow_html=True)

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
        weekday_mask = daily_totals["is_weekend"] == False
        weekend_mask = daily_totals["is_weekend"] == True
        mean_weekday = daily_totals.loc[weekday_mask, "value"].mean() if weekday_mask.any() else None
        mean_weekend = daily_totals.loc[weekend_mask, "value"].mean() if weekend_mask.any() else None
        daily_totals["dev_weekday"] = None
        daily_totals.loc[weekday_mask, "dev_weekday"] = ((daily_totals.loc[weekday_mask, "value"] - mean_weekday) / mean_weekday * 100) if mean_weekday is not None and mean_weekday != 0 else None
        daily_totals["dev_weekend"] = None
        daily_totals.loc[weekend_mask, "dev_weekend"] = ((daily_totals.loc[weekend_mask, "value"] - mean_weekend) / mean_weekend * 100) if mean_weekend is not None and mean_weekend != 0 else None
        dev_data = []
        group_names = []
        dev_all = daily_totals["dev_all"].dropna().values
        if len(dev_all) > 1:
            dev_data.append(dev_all); group_names.append("Всі дні")
        dev_wd = daily_totals.loc[weekday_mask, "dev_weekday"].dropna().values if weekday_mask.any() else np.array([])
        if len(dev_wd) > 1:
            dev_data.append(dev_wd); group_names.append("Будні")
        dev_we = daily_totals.loc[weekend_mask, "dev_weekend"].dropna().values if weekend_mask.any() else np.array([])
        if len(dev_we) > 1:
            dev_data.append(dev_we); group_names.append("Вихідні")
        if dev_data:
            fig_density = go.Figure()
            colors = {"Всі дні": KPO_CYAN, "Будні": KPO_GREEN, "Вихідні": KPO_AMBER}
            max_density = 0
            for group_name, data in zip(group_names, dev_data):
                if len(data) > 1:
                    x_min = data.min() - 10
                    x_max = data.max() + 10
                    x_grid = np.linspace(x_min, x_max, 200)
                    density = gaussian_kde_np(data, x_grid)
                    max_density = max(max_density, max(density))
                    fig_density.add_trace(go.Scatter(x=x_grid, y=density, mode='lines', name=group_name, line=dict(color=colors.get(group_name, "gray"), width=2.5), fill='none'))
            if max_density > 0:
                fig_density.add_trace(go.Scatter(x=[0,0], y=[0, max_density*1.1], mode='lines', name='Середнє (0%)', line=dict(color=KPO_RED, width=2, dash='dash'), showlegend=True))
                median_all = np.median(dev_all) if len(dev_all) > 0 else None
                if median_all is not None:
                    fig_density.add_trace(go.Scatter(x=[median_all, median_all], y=[0, max_density*1.1], mode='lines', name=f'Медіана ({median_all:.1f}%)', line=dict(color=KPO_TEXT, width=2, dash='dash'), showlegend=True))
            fig_density.update_layout(title="Криві щільності відхилень від середнього", xaxis_title="Відхилення, %", yaxis_title="Щільність", height=400, margin=dict(l=10, r=10, t=40, b=10), legend=dict(title="Група / лінії", x=0.98, y=0.98, xanchor='right', yanchor='top', bgcolor='rgba(0,0,0,0)'), hovermode="x unified")
            st.plotly_chart(fig_density, use_container_width=True)

            # ---- ДИНАМІЧНИЙ АНАЛІЗ КРИВИХ (на основі реальних даних) ----
            density_stats = analyze_density(group_names, dev_data)

            # Аномалії для згадки у бізнес-частині
            density_anomalies = detect_anomalies(filtered, window=14, threshold=3.0)
            if not density_anomalies.empty:
                density_anomaly_points = density_anomalies[density_anomalies["is_anomaly"]].copy()
            else:
                density_anomaly_points = pd.DataFrame()

            # ============ Expander 1: Що означає форма кривих? ============
            with st.expander("❓ Що означає форма кривих? (автоматичний опис ваших даних)"):
                if not density_stats:
                    st.info("Недостатньо даних для опису.")
                else:
                    st.markdown(
                        "Нижче — опис **саме ваших кривих**, обчислений за поточний період. "
                        "Відхилення вимірюється у % від середнього: 0% — типовий день, "
                        "+20% — день на 20% інтенсивніший за середній."
                    )
                    for name, s in density_stats.items():
                        st.markdown(
                            f"**🔹 {name}** (n = {s['n']} днів)\n"
                            f"- Пік кривої припадає на **{s['peak']:+.1f}%** — "
                            f"тобто найчастіше значення навантаження близьке до цієї точки "
                            f"(тут зосереджено ≈ {s['peak_pct']:.0f}% усіх днів).\n"
                            f"- Медіана = **{s['median']:+.1f}%**, IQR = **{s['iqr']:.1f} п.п.** "
                            f"(50% днів лежать у межах ±{s['iqr']/2:.1f} п.п. навколо медіани).\n"
                            f"- Розподіл **{s['skew_txt']}**.\n"
                            f"- Форма кривої **{s['width_txt']}**.\n"
                            f"- Діапазон відхилень: від **{s['min']:+.1f}%** до **{s['max']:+.1f}%**."
                        )
                        st.markdown("")

                    if len(density_stats) > 1:
                        widest = max(density_stats.items(), key=lambda kv: kv[1]["std"])
                        narrowest = min(density_stats.items(), key=lambda kv: kv[1]["std"])
                        st.markdown(
                            f"**Порівняння груп:** найширший розкид — у **{widest[0]}** "
                            f"(σ = {widest[1]['std']:.1f}%), найвужчий — у **{narrowest[0]}** "
                            f"(σ = {narrowest[1]['std']:.1f}%). "
                            f"Різниця у стабільності ≈ **{widest[1]['std'] - narrowest[1]['std']:.1f} п.п.**"
                        )

            # ============ Expander 2: Як це інтерпретувати для бізнесу? ============
            with st.expander("❓ Як це інтерпретувати для бізнесу? (висновки за вашими даними)"):
                if not density_stats:
                    st.info("Недостатньо даних для інтерпретації.")
                else:
                    business_lines = []

                    # 1. Порівняння будні ↔ вихідні
                    if "Будні" in density_stats and "Вихідні" in density_stats:
                        wd, we = density_stats["Будні"], density_stats["Вихідні"]
                        ratio = we["std"] / wd["std"] if wd["std"] > 0 else 1.0
                        if ratio > 1.3:
                            business_lines.append(
                                f"🔴 **Вихідні менш передбачувані за будні.** "
                                f"Розкид у вихідні σ = {we['std']:.1f}% проти σ = {wd['std']:.1f}% у будні "
                                f"(у {ratio:.1f}× більше). Варто тримати додатковий резерв потужності саме на вихідні."
                            )
                        elif ratio < 0.7:
                            business_lines.append(
                                f"🟢 **Вихідні стабільніші за будні** (σ = {we['std']:.1f}% проти {wd['std']:.1f}%). "
                                f"Пікові навантаження концентруються у будні — плануйте ресурси саме туди."
                            )
                        else:
                            business_lines.append(
                                f"ℹ️ **Стабільність буднів і вихідних схожа** "
                                f"(σ = {wd['std']:.1f}% і {we['std']:.1f}%). "
                                f"Окремий резерв під вихідні не потрібен."
                            )

                        diff_med = wd["median"] - we["median"]
                        if abs(diff_med) > 5:
                            if diff_med > 0:
                                business_lines.append(
                                    f"📊 **У будні типове навантаження вище** — медіана відхилення "
                                    f"{wd['median']:+.1f}% проти {we['median']:+.1f}% у вихідні "
                                    f"(різниця ≈ {diff_med:.1f} п.п.)."
                                )
                            else:
                                business_lines.append(
                                    f"📊 **У вихідні типове навантаження вище** — медіана "
                                    f"{we['median']:+.1f}% проти {wd['median']:+.1f}% у будні "
                                    f"(різниця ≈ {-diff_med:.1f} п.п.)."
                                )

                    # 2. Асиметрія — куди «хвіст»
                    for name, s in density_stats.items():
                        if s["skew"] > 0.5:
                            business_lines.append(
                                f"⚠️ **{name}: асиметрія вправо** (skew = {s['skew']:+.2f}). "
                                f"Більшість днів нижче середнього, але трапляються рідкісні "
                                f"пікові дні (макс. {s['max']:+.1f}%). Це «дорогі» дні — потрібен запас."
                            )
                        elif s["skew"] < -0.5:
                            business_lines.append(
                                f"⚠️ **{name}: асиметрія вліво** (skew = {s['skew']:+.2f}). "
                                f"Більшість днів вище середнього, зрідка — провали "
                                f"(мін. {s['min']:+.1f}%). Можливі простої або недозавантаження."
                            )

                    # 3. Загальний висновок по стабільності
                    avg_std = float(np.mean([s["std"] for s in density_stats.values()]))
                    if avg_std < 15:
                        business_lines.append(
                            f"✅ **Загальна стабільність висока** (середнє σ = {avg_std:.1f}%). "
                            f"Можна планувати ресурси за середнім без великого запасу."
                        )
                    elif avg_std < 30:
                        business_lines.append(
                            f"🟡 **Помірна варіативність** (середнє σ = {avg_std:.1f}%). "
                            f"Рекомендується тримати резерв ≈ {avg_std:.0f}% від середнього на пікові дні."
                        )
                    else:
                        business_lines.append(
                            f"🔴 **Висока варіативність** (середнє σ = {avg_std:.1f}%). "
                            f"Навантаження погано прогнозується — потрібне гнучке планування змін і "
                            f"резерв ≥ {avg_std:.0f}%."
                        )

                    # 4. Вузький пік → дуже типовий день
                    for name, s in density_stats.items():
                        if s["peak_pct"] >= 40:
                            business_lines.append(
                                f"🎯 **{name}: {s['peak_pct']:.0f}% днів групуються навколо "
                                f"{s['peak']:+.1f}%** — є чітко виражений «типовий день». "
                                f"Можна стандартизувати зміни під це значення."
                            )

                    # 5. Посилання на конкретні аномальні дні
                    if not density_anomaly_points.empty:
                        top_anom = density_anomaly_points.copy()
                        top_anom["deviation"] = (
                            (top_anom["value"] - top_anom["rolling_median"])
                            / top_anom["rolling_median"].replace(0, np.nan) * 100
                        )
                        top_anom = top_anom.dropna(subset=["deviation"])
                        if not top_anom.empty:
                            top_pos = top_anom.sort_values("deviation", ascending=False).head(1)
                            top_neg = top_anom.sort_values("deviation", ascending=True).head(1)

                            if not top_pos.empty and top_pos.iloc[0]["deviation"] > 5:
                                r = top_pos.iloc[0]
                                business_lines.append(
                                    f"📌 **Найбільший сплеск**: {r['date'].strftime('%d.%m.%Y')} — "
                                    f"{r['value']:,.0f} операцій (відхилення {r['deviation']:+.1f}% "
                                    f"від локальної медіани). Деталі — у табі «📈 Динаміка», блок «🔍 Аномальні дні»."
                                )
                            if not top_neg.empty and top_neg.iloc[0]["deviation"] < -5:
                                r = top_neg.iloc[0]
                                business_lines.append(
                                    f"📌 **Найбільший провал**: {r['date'].strftime('%d.%m.%Y')} — "
                                    f"{r['value']:,.0f} операцій (відхилення {r['deviation']:+.1f}% "
                                    f"від локальної медіани). Можлива причина — свято, збій у подачі заявок "
                                    f"або неповне внесення даних."
                                )

                    for line in business_lines:
                        st.markdown(f"- {line}")

        else:
            st.info("Недостатньо даних для побудови кривих щільності.")
    else:
        st.info("Недостатньо даних для побудови графіка розподілу.")

    st.subheader("📈 Співвідношення пік / середнє")
    st.markdown(custom_metric("Пік / середнє", f"{peak_avg_ratio:.2f}×" if peak_avg_ratio > 0 else "—"), unsafe_allow_html=True)

# ============================================================
# TAB 5: ПОРІВНЯННЯ ПЕРІОДІВ
# ============================================================
with tab5:
    st.subheader("🆚 Порівняння двох довільних періодів")
    st.caption("Незалежно від фільтрів у сайдбарі — оберіть два будь-які діапазони дат для порівняння.")
    cmp_op_mode = st.radio("Операції для порівняння", options=["Тотал", "Вибрані операції"], index=0, horizontal=True, key="cmp_op_mode")
    if cmp_op_mode == "Тотал":
        cmp_ops = ["Тотал"]
    else:
        cmp_ops = st.multiselect("Операції", options=all_ops, default=all_ops, key="cmp_ops_select")
        if not cmp_ops:
            cmp_ops = ["Тотал"]
            cmp_op_mode = "Тотал"
    col_a, col_b = st.columns(2)
    default_end_b = max_date.date()
    default_start_b = max(min_date, max_date - pd.Timedelta(days=13)).date()
    default_end_a = (max(min_date, max_date - pd.Timedelta(days=14))).date()
    default_start_a = max(min_date, max_date - pd.Timedelta(days=27)).date()
    with col_a:
        st.markdown("**Період A**")
        range_a_input = st.date_input("Діапазон A", value=(default_start_a, default_end_a), min_value=min_date.date(), max_value=max_date.date(), key="cmp_range_a")
    with col_b:
        st.markdown("**Період B**")
        range_b_input = st.date_input("Діапазон B", value=(default_start_b, default_end_b), min_value=min_date.date(), max_value=max_date.date(), key="cmp_range_b")
    def _normalize_range(range_input):
        if isinstance(range_input, tuple) and len(range_input) == 2:
            start, end = pd.Timestamp(range_input[0]), pd.Timestamp(range_input[1])
        else:
            single = range_input[0] if isinstance(range_input, tuple) else range_input
            start = end = pd.Timestamp(single)
        if start > end:
            start, end = end, start
        return start, end
    if not (isinstance(range_a_input, tuple) and len(range_a_input) == 2 and isinstance(range_b_input, tuple) and len(range_b_input) == 2):
        st.info("Оберіть повний діапазон (початкову і кінцеву дату) для обох періодів.")
    else:
        range_a = _normalize_range(range_a_input)
        range_b = _normalize_range(range_b_input)

        def build_period_metrics(date_range, ops):
            start, end = date_range
            mask = (df["date"] >= start) & (df["date"] <= end) & (df["operation"].isin(ops))
            scoped_stats = with_data(df[mask])
            if scoped_stats.empty:
                return None
            daily = scoped_stats.groupby("date")["value"].sum()
            s_true = float(scoped_stats["sum_true"].sum())
            s_false = float(scoped_stats["sum_false"].sum())
            rate = (s_true / (s_true + s_false) * 100) if (s_true + s_false) > 0 else None
            return {
                "total": daily.sum(),
                "avg": daily.mean(),
                "peak": daily.max(),
                "rate": rate,
                "days": (end - start).days + 1,
            }

        metrics_a = build_period_metrics(range_a, cmp_ops)
        metrics_b = build_period_metrics(range_b, cmp_ops)
        if metrics_a is None or metrics_b is None:
            st.warning("Немає даних для одного з обраних періодів.")
        else:
            def _fmt_delta_pct(a_val, b_val):
                if a_val is None or b_val is None or a_val == 0:
                    return "—"
                return f"{(b_val - a_val) / a_val * 100:+.1f}%"
            rows = [
                {"Метрика": "Період (днів)", "A": metrics_a["days"], "B": metrics_b["days"], "Δ": metrics_b["days"] - metrics_a["days"], "Δ %": "—"},
                {"Метрика": "Всього операцій", "A": f"{metrics_a['total']:,.0f}", "B": f"{metrics_b['total']:,.0f}", "Δ": f"{metrics_b['total'] - metrics_a['total']:+,.0f}", "Δ %": _fmt_delta_pct(metrics_a["total"], metrics_b["total"])},
                {"Метрика": "Середнє за день", "A": f"{metrics_a['avg']:.1f}", "B": f"{metrics_b['avg']:.1f}", "Δ": f"{metrics_b['avg'] - metrics_a['avg']:+.1f}", "Δ %": _fmt_delta_pct(metrics_a["avg"], metrics_b["avg"])},
                {"Метрика": "Пік за день", "A": f"{metrics_a['peak']:,.0f}", "B": f"{metrics_b['peak']:,.0f}", "Δ": f"{metrics_b['peak'] - metrics_a['peak']:+,.0f}", "Δ %": _fmt_delta_pct(metrics_a["peak"], metrics_b["peak"])},
                {"Метрика": "Коефіцієнт погоджень, %", "A": f"{metrics_a['rate']:.1f}%" if metrics_a["rate"] is not None else "—", "B": f"{metrics_b['rate']:.1f}%" if metrics_b["rate"] is not None else "—", "Δ": (f"{metrics_b['rate'] - metrics_a['rate']:+.1f} п.п." if metrics_a["rate"] is not None and metrics_b["rate"] is not None else "—"), "Δ %": "—"},
            ]
            st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

            chart_metrics = pd.DataFrame({
                "Метрика": ["Всього", "Середнє за день", "Пік"],
                "A": [metrics_a["total"], metrics_a["avg"], metrics_a["peak"]],
                "B": [metrics_b["total"], metrics_b["avg"], metrics_b["peak"]],
            }).melt(id_vars="Метрика", var_name="Період", value_name="Значення")
            fig_cmp = px.bar(chart_metrics, x="Метрика", y="Значення", color="Період", barmode="group", labels={"Значення": "Кількість"}, title=f"A: {range_a[0].strftime('%d.%m.%Y')}–{range_a[1].strftime('%d.%m.%Y')}  vs  B: {range_b[0].strftime('%d.%m.%Y')}–{range_b[1].strftime('%d.%m.%Y')}")
            fig_cmp.update_layout(height=380, margin=dict(l=10, r=10, t=40, b=10))
            st.plotly_chart(fig_cmp, use_container_width=True)

st.caption("Джерело: Google Sheets • Оновлення даних: до 5 хвилин після зміни таблиці • Час: Europe/Kyiv.")
