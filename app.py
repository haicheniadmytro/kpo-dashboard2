"""
KPO Dashboard - Streamlit застосунок для аналізу даних з Google Sheets.
"""

from datetime import datetime
import html
import logging
import re
from typing import Any, Dict, List, Optional, Tuple, Union
from zoneinfo import ZoneInfo

import google.oauth2.service_account
import gspread
import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import plotly.io as pio
import streamlit as st

# ============================================================
# 1. Конфігурація сторінки та логування
# ============================================================
st.set_page_config(
    page_title="KPO Dashboard",
    page_icon="📊",
    layout="wide",
)

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger(__name__)

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

KPO_COLORWAY = [
    KPO_CYAN,
    KPO_AMBER,
    KPO_GREEN,
    KPO_RED,
    KPO_PURPLE,
    KPO_ORANGE,
    KPO_BLUE,
    KPO_PINK,
]
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
try:
    SPREADSHEET_ID = st.secrets["SPREADSHEET_ID"]
except KeyError:
    st.error("Не знайдено SPREADSHEET_ID у secrets. Додайте його у .streamlit/secrets.toml")
    st.stop()

# ============================================================
# 4. Константи та конфігурації
# ============================================================
START_YEAR = 2024
CURRENT_YEAR = datetime.now().year
SHEETS = [str(year)[-2:] for year in range(START_YEAR, CURRENT_YEAR + 1)]
KYIV_TZ = ZoneInfo("Europe/Kyiv")

MONTHS = {
    "Січень": 1,
    "Лютий": 2,
    "Березень": 3,
    "Квітень": 4,
    "Травень": 5,
    "Червень": 6,
    "Липень": 7,
    "Серпень": 8,
    "Вересень": 9,
    "Жовтень": 10,
    "Листопад": 11,
    "Грудень": 12,
}

OPERATIONS = [
    "Бонуси",
    "Призупинка",
    "Відновлення",
    "Відміна SF",
    "Переоформлення",
    "Закриття контракта",
    "Со-доступ",
    "Зміна дати активації",
]

ALIASES = {
    "Зміна дати активації": "Зміна дати активації",
}

WEEKDAY_UA = {
    "Monday": "Пн",
    "Tuesday": "Вт",
    "Wednesday": "Ср",
    "Thursday": "Чт",
    "Friday": "Пт",
    "Saturday": "Сб",
    "Sunday": "Нд",
}
WEEKDAY_ORDER_UA = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Нд"]

APPROVAL_GOOD_THRESHOLD = 85
APPROVAL_WARN_THRESHOLD = 70
COLOR_GOOD = KPO_GREEN
COLOR_WARN = KPO_AMBER
COLOR_BAD = KPO_RED

TOTAL_ROW_SEARCH_RANGE = 10
DETAIL_SEARCH_RANGE = 30
FIRST_DAY_COLUMN = 4
PER_OP_TF_SEARCH_RANGE = len(OPERATIONS) + 3


# ============================================================
# 5. Допоміжні та часові функції
# ============================================================
def normalize_operation(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.strip().split())


def now_kyiv() -> pd.Timestamp:
    return pd.Timestamp.now(tz=KYIV_TZ).replace(tzinfo=None).normalize()


def now_kyiv_exact() -> pd.Timestamp:
    return pd.Timestamp.now(tz=KYIV_TZ).replace(tzinfo=None)


def is_empty_cell(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, bool):
        return False
    return str(value).strip() == ""


def as_number(value: Any) -> float:
    if is_empty_cell(value):
        return 0.0
    val_str = str(value).replace("\xa0", "").replace(" ", "").replace(",", ".")
    match = re.search(r"^-?\d+(?:[.,]\d+)?", val_str)
    if match:
        try:
            return float(match.group(0).replace(",", "."))
        except ValueError:
            logger.warning(f"Не вдалося перетворити '{val_str}' на число")
            return 0.0
    logger.warning(f"Не вдалося розпізнати число в '{val_str}'")
    return 0.0


def parse_month_header(value: Any, sheet_year: int) -> Optional[Tuple[int, int]]:
    if not isinstance(value, str):
        return None
    match = re.match(
        r"^\s*(Січень|Лютий|Березень|Квітень|Травень|Червень|"
        r"Липень|Серпень|Вересень|Жовтень|Листопад|Грудень)\s+\d{2}\s*$",
        value,
    )
    if not match:
        return None
    return MONTHS[match.group(1)], sheet_year


# ============================================================
# 6. Робота з Google Sheets
# ============================================================
def get_client() -> gspread.Client:
    scopes = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
    credentials = google.oauth2.service_account.Credentials.from_service_account_info(
        dict(st.secrets["gcp_service_account"]),
        scopes=scopes,
    )
    return gspread.authorize(credentials)


@st.cache_data(ttl=300, show_spinner="Завантаження даних з Google Таблиці…")
def load_data() -> Tuple[pd.DataFrame, List[str]]:
    client = get_client()
    spreadsheet = client.open_by_key(SPREADSHEET_ID)
    records = []
    op_true_false = []
    warnings = []

    for sheet_name in SHEETS:
        worksheet = spreadsheet.worksheet(sheet_name)
        values = worksheet.get_all_values()
        if not values:
            continue

        sheet_year = 2000 + int(sheet_name)
        first_col = [row[0] if row else "" for row in values]
        month_rows = []

        for idx, value in enumerate(first_col):
            parsed = parse_month_header(value, sheet_year)
            if parsed:
                month_rows.append((idx, parsed[0], parsed[1]))

        for header_row, month, year in month_rows:
            month_key = f"{year}-{month:02d}"
            month_label = f"{month:02d}.{year}"
            total_row_idx = None

            try:
                named_range = worksheet.range("Тотал")
                if named_range:
                    total_row_idx = named_range[0].row - 1
            except Exception:
                for r in range(header_row + 1, min(header_row + TOTAL_ROW_SEARCH_RANGE, len(values))):
                    if len(values[r]) > 0 and normalize_operation(values[r][0]) == "Тотал":
                        total_row_idx = r
                        break

            if total_row_idx is not None:
                sum_true = as_number(values[total_row_idx][1]) if len(values[total_row_idx]) > 1 else 0
                sum_false = as_number(values[total_row_idx][2]) if len(values[total_row_idx]) > 2 else 0
                op_true_false.append(
                    {
                        "month": month_key,
                        "operation": "Тотал",
                        "sum_true": sum_true,
                        "sum_false": sum_false,
                    }
                )

                per_op_tf_found = set()
                search_end = min(total_row_idx + 1 + PER_OP_TF_SEARCH_RANGE, len(values))
                for r in range(total_row_idx + 1, search_end):
                    cell_a = values[r][0] if len(values[r]) > 0 else ""
                    op_name = normalize_operation(cell_a)
                    op_name = ALIASES.get(op_name, op_name)
                    if op_name not in OPERATIONS or op_name in per_op_tf_found:
                        continue
                    op_sum_true = as_number(values[r][1]) if len(values[r]) > 1 else 0
                    op_sum_false = as_number(values[r][2]) if len(values[r]) > 2 else 0
                    op_true_false.append(
                        {
                            "month": month_key,
                            "operation": op_name,
                            "sum_true": op_sum_true,
                            "sum_false": op_sum_false,
                        }
                    )
                    per_op_tf_found.add(op_name)

                missing_tf_ops = [op for op in OPERATIONS if op not in per_op_tf_found]
                if missing_tf_ops:
                    warnings.append(
                        f"⚠️ Аркуш «{sheet_name}», {month_label}: не знайдено TRUE/FALSE дані "
                        f"для операцій: {', '.join(missing_tf_ops)}."
                    )
            else:
                warnings.append(f"⚠️ Аркуш «{sheet_name}», {month_label}: не знайдено рядок «Тотал».")

            detail_start = None
            for r in range(header_row + 1, min(header_row + DETAIL_SEARCH_RANGE, len(values))):
                if len(values[r]) > 3 and normalize_operation(values[r][3]) in OPERATIONS:
                    detail_start = r
                    break

            if detail_start is None:
                warnings.append(f"⚠️ Аркуш «{sheet_name}», {month_label}: не знайдено таблицю деталізації.")
                continue

            days = pd.Period(f"{year}-{month:02d}").days_in_month
            for r in range(detail_start, len(values)):
                raw_operation = values[r][3] if len(values[r]) > 3 else ""
                operation = normalize_operation(raw_operation)
                operation = ALIASES.get(operation, operation)
                if operation not in OPERATIONS:
                    break
                row = values[r]
                for day_idx in range(days):
                    col = FIRST_DAY_COLUMN + day_idx
                    raw_value = row[col] if col < len(row) else ""
                    date = pd.Timestamp(year=year, month=month, day=day_idx + 1)
                    records.append(
                        {
                            "date": date,
                            "operation": operation,
                            "value": as_number(raw_value),
                            "has_data": not is_empty_cell(raw_value),
                            "year": year,
                            "month": date.strftime("%Y-%m"),
                            "month_name": date.strftime("%b %Y"),
                            "weekday": date.day_name(),
                            "is_weekend": date.weekday() >= 5,
                        }
                    )

    df_raw = pd.DataFrame(records)
    if df_raw.empty:
        raise ValueError("Не знайдено деталізованих даних у Google Таблиці.")

    df_raw["date"] = pd.to_datetime(df_raw["date"])
    date_metadata = df_raw[["date", "year", "month", "month_name", "weekday", "is_weekend"]].drop_duplicates("date")

    df_grouped = (
        df_raw.groupby(["date", "operation"], as_index=False)
        .agg(value=("value", "sum"), has_data=("has_data", "any"))
        .sort_values(["date", "operation"])
    )
    df = df_grouped.merge(date_metadata, on="date", how="left")

    total = (
        df.groupby("date", as_index=False)
        .agg(value=("value", "sum"), has_data=("has_data", "any"))
        .assign(operation="Тотал")
    )
    total = total.merge(date_metadata, on="date", how="left")

    df = pd.concat([df, total], ignore_index=True)
    tf_df = pd.DataFrame(op_true_false).drop_duplicates(subset=["month", "operation"])
    df = df.merge(tf_df, on=["month", "operation"], how="left")
    df["sum_true"] = df["sum_true"].fillna(0)
    df["sum_false"] = df["sum_false"].fillna(0)

    return df, warnings


# ============================================================
# 7. Аналітичні та математичні обчислення
# ============================================================
def with_data(df: pd.DataFrame) -> pd.DataFrame:
    if "has_data" not in df.columns:
        return df
    return df[df["has_data"]]


def calc_peak_min_avg(df: pd.DataFrame) -> Tuple[float, float, float, float]:
    daily = with_data(df).groupby("date")["value"].sum()
    if daily.empty:
        return 0.0, 0.0, 0.0, 0.0
    peak = daily.max()
    min_val = daily.min()
    avg = daily.mean()
    peak_avg_ratio = peak / avg if avg > 0 else 0.0
    return peak, min_val, avg, peak_avg_ratio


def calc_busiest_weekday(df: pd.DataFrame) -> Tuple[Optional[str], Optional[float]]:
    filtered_df = with_data(df)
    if filtered_df.empty:
        return None, None
    daily = filtered_df.groupby("date")["value"].sum().reset_index()
    daily["weekday"] = daily["date"].dt.day_name()
    weekday_avg = daily.groupby("weekday")["value"].mean()
    busiest = weekday_avg.idxmax()
    busiest_val = weekday_avg.max()
    return busiest, busiest_val


def calc_busiest_operation(df: pd.DataFrame) -> Tuple[Optional[str], Optional[float]]:
    if df.empty or df["operation"].nunique() == 0:
        return None, None
    ops = with_data(df[df["operation"] != "Тотал"])
    if ops.empty:
        return None, None
    total_by_op = ops.groupby("operation")["value"].sum()
    busiest_op = total_by_op.idxmax()
    busiest_val = total_by_op.max()
    return busiest_op, busiest_val


def calc_stability(df: pd.DataFrame, daily_avg: float) -> Tuple[float, float, str]:
    daily = with_data(df).groupby("date")["value"].sum()
    if daily.empty:
        return 0.0, 0.0, "Немає даних"
    std = daily.std()
    cv = (std / daily_avg * 100) if daily_avg > 0 else 0.0
    if cv < 15:
        interpretation = "🟢 Низька варіативність (≤15%)"
    elif cv < 30:
        interpretation = "🟡 Середня варіативність (15-30%)"
    else:
        interpretation = "🔴 Висока варіативність (>30%)"
    return std, cv, interpretation


def detect_anomalies(df: pd.DataFrame, window: int = 14, threshold: float = 3.0) -> pd.DataFrame:
    filtered_df = with_data(df)
    if filtered_df.empty:
        return pd.DataFrame()
    daily = filtered_df.groupby("date")["value"].sum().reset_index()
    daily = daily.sort_values("date")
    if len(daily) < window:
        return pd.DataFrame()

    daily["month_key"] = daily["date"].dt.to_period("M")
    all_anomalies = []

    for _, group in daily.groupby("month_key"):
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
    return result[result["value"] > 0]


def gaussian_kde_np(data: np.ndarray, x_grid: np.ndarray, bandwidth: Optional[float] = None) -> np.ndarray:
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
    return kernel.sum(axis=1) / (n * bandwidth * np.sqrt(2 * np.pi))


def forecast_scenarios(df: pd.DataFrame, current_month: str) -> Tuple[Optional[Dict], Optional[Dict]]:
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

        if not prev_fact.empty and prev_fact["value"].sum() >= 10:
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
# 8. Основна програма та завантаження
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
# 9. Бокова панель (Фільтри)
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
all_ops = [op for op in OPERATIONS if op in df["operation"].unique()]

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
# 10. Фільтрація даних
# ============================================================
op_mask = df["operation"] == "Тотал" if operation_mode == "Тотал" else df["operation"].isin(selected_operations)

if period_mode == "За місяцями":
    filtered = df[df["year"].isin(selected_years) & df["month"].isin(selected_months) & op_mask].copy()
else:
    filtered = df[(df["date"] >= custom_range[0]) & (df["date"] <= custom_range[1]) & op_mask].copy()

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
# 11. Розрахунок метрик
# ============================================================
daily_total = filtered_stats.groupby("date")["value"].sum()
total_value = daily_total.sum()
daily_avg = daily_total.mean() if not daily_total.empty else 0.0

weekday_mask = ~filtered_stats["is_weekend"]
weekend_mask = filtered_stats["is_weekend"]

daily_avg_weekday = filtered_stats[weekday_mask].groupby("date")["value"].sum().mean() if weekday_mask.any() else None
daily_avg_weekend = filtered_stats[weekend_mask].groupby("date")["value"].sum().mean() if weekend_mask.any() else None

peak = daily_total.max() if not daily_total.empty else 0.0
peak_date = daily_total.idxmax() if not daily_total.empty else None
min_val = daily_total.min() if not daily_total.empty else 0.0
peak_avg_ratio = peak / daily_avg if daily_avg > 0 else 0.0

busiest_weekday, busiest_weekday_val = calc_busiest_weekday(filtered)
busiest_op, busiest_op_val = calc_busiest_operation(filtered)
std, cv, cv_interp = calc_stability(filtered, daily_avg)

# --- Коефіцієнт погоджень ---
if period_mode == "За місяцями":
    tf_filtered = filtered[["month", "operation", "sum_true", "sum_false"]].drop_duplicates()
else:
    start_date, end_date = custom_range
    full_months = []
    current = start_date
    while current <= end_date:
        month_start = current.replace(day=1)
        month_end = (month_start + pd.offsets.MonthEnd(1)).normalize()
        if month_start >= start_date and month_end <= end_date:
            full_months.append(current.strftime("%Y-%m"))
        current = month_end + pd.Timedelta(days=1)

    if full_months:
        tf_op_mask = df["operation"].isin(selected_operations) if operation_mode != "Тотал" else (df["operation"] == "Тотал")
        tf_filtered = df[df["month"].isin(full_months) & tf_op_mask][["month", "operation", "sum_true", "sum_false"]].drop_duplicates()
    else:
        tf_filtered = pd.DataFrame()

if not tf_filtered.empty:
    sum_true_total = tf_filtered["sum_true"].sum()
    sum_false_total = tf_filtered["sum_false"].sum()
    total_ratio = sum_true_total + sum_false_total
    approval_rate_val = (sum_true_total / total_ratio * 100) if total_ratio > 0 else 0.0
    approval_rate_str = f"{approval_rate_val:.1f}%" if total_ratio > 0 else "—"
    approval_rate_available = True
else:
    approval_rate_val = 0.0
    approval_rate_str = "— (немає повних місяців)"
    approval_rate_available = False

# --- Коефіцієнт погоджень по операціях ---
if period_mode == "За місяцями":
    period_mask = df["year"].isin(selected_years) & df["month"].isin(selected_months)
else:
    period_mask = df["month"].isin(full_months) if full_months else pd.Series(False, index=df.index)

tf_by_op_all = df[period_mask & (df["operation"] != "Тотал")][["month", "operation", "sum_true", "sum_false"]].drop_duplicates()
approval_by_op = pd.DataFrame(columns=["operation", "sum_true", "sum_false", "total", "approval_rate"])

if not tf_by_op_all.empty:
    approval_by_op = tf_by_op_all.groupby("operation", as_index=False)[["sum_true", "sum_false"]].sum()
    approval_by_op["total"] = approval_by_op["sum_true"] + approval_by_op["sum_false"]
    approval_by_op = approval_by_op[approval_by_op["total"] > 0].copy()
    approval_by_op["approval_rate"] = (approval_by_op["sum_true"] / approval_by_op["total"] * 100).round(1)
    approval_by_op = approval_by_op.sort_values("approval_rate", ascending=False)

# --- Порівняння з попередніми періодами ---
comparison_parts = []
if period_mode == "За місяцями" and len(selected_months) == 1 and operation_mode == "Тотал":
    current_period = pd.Period(selected_months[0])
    today_comp = now_kyiv()
    prev_period = current_period - 1

    if current_period.end_time <= today_comp:
        cur_sum = daily_total.sum()
        prev_sum = with_data(df[(df["month"] == str(prev_period)) & (df["operation"] == "Тотал")])["value"].sum()
    else:
        day_limit = today_comp.day
        cur_sum = filtered_stats[filtered_stats["date"].dt.day <= day_limit]["value"].sum()
        day_limit_prev = min(day_limit, prev_period.days_in_month)
        prev_sum = with_data(
            df[(df["month"] == str(prev_period)) & (df["operation"] == "Тотал") & (df["date"].dt.day <= day_limit_prev)]
        )["value"].sum()

    delta_prev = ((cur_sum - prev_sum) / prev_sum * 100) if prev_sum > 0 else None
    if delta_prev is not None:
        comparison_parts.append(f"Попер. міс: {delta_prev:+.1f}%")

    prev_year_period = pd.Period(year=current_period.year - 1, month=current_period.month, freq="M")
    has_prev_year = not df[(df["month"] == str(prev_year_period)) & (df["operation"] == "Тотал")].empty

    if has_prev_year:
        if current_period.end_time <= today_comp:
            cur_sum = daily_total.sum()
            prev_year_sum = with_data(df[(df["month"] == str(prev_year_period)) & (df["operation"] == "Тотал")])["value"].sum()
        else:
            day_limit = today_comp.day
            cur_sum = filtered_stats[filtered_stats["date"].dt.day <= day_limit]["value"].sum()
            day_limit_prev_year = min(day_limit, prev_year_period.days_in_month)
            prev_year_sum = with_data(
                df[(df["month"] == str(prev_year_period)) & (df["operation"] == "Тотал") & (df["date"].dt.day <= day_limit_prev_year)]
            )["value"].sum()

        delta_year = ((cur_sum - prev_year_sum) / prev_year_sum * 100) if prev_year_sum > 0 else None
        if delta_year is not None:
            comparison_parts.append(f"Мин. рік: {delta_year:+.1f}%")

comparison_text = " ".join(comparison_parts) if comparison_parts else "—"


# ============================================================
# 12. Кастомні HTML елементи та Стилі
# ============================================================
def custom_metric(label: str, value: str, help_text: Optional[str] = None, color: Optional[str] = None) -> str:
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


def approval_rate_color(value: Optional[float]) -> Optional[str]:
    if value is None:
        return None
    if value >= APPROVAL_GOOD_THRESHOLD:
        return COLOR_GOOD
    if value >= APPROVAL_WARN_THRESHOLD:
        return COLOR_WARN
    return COLOR_BAD


def approval_rate_tier(value: float) -> str:
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


def cv_color(value: Optional[float]) -> Optional[str]:
    if value is None or value <= 0:
        return None
    if value < 15:
        return COLOR_GOOD
    if value < 30:
        return COLOR_WARN
    return COLOR_BAD


st.markdown(
    f"""
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
    """,
    unsafe_allow_html=True,
)


def forecast_cards(title: str, forecast: Optional[Dict], help_base: str = None, help_min: str = None, help_max: str = None) -> None:
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
# 13. Вкладки (Tabs)
# ============================================================
tab1, tab2, tab3, tab4, tab5 = st.tabs(["📊 Overview", "📈 Динаміка", "🧩 Операції", "📅 Навантаження", "🆚 Порівняння періодів"])

# ------------------------------------------------------------
# TAB 1: OVERVIEW
# ------------------------------------------------------------
with tab1:
    col1, col2, col3, col4, col5, col6 = st.columns(6)
    with col1:
        st.markdown(custom_metric("Всього", f"{total_value:,.0f}", "Загальна кількість операцій за вибраний період"), unsafe_allow_html=True)
    with col2:
        st.markdown(custom_metric("Середнє за день", f"{daily_avg:.0f}", "Сумарна кількість поділена на дні з даними"), unsafe_allow_html=True)
    with col3:
        avg_wd_str = f"{daily_avg_weekday:.0f}" if daily_avg_weekday is not None and not pd.isna(daily_avg_weekday) else "—"
        st.markdown(custom_metric("Середнє за будні", avg_wd_str, "Середня кількість операцій у будні"), unsafe_allow_html=True)
    with col4:
        avg_we_str = f"{daily_avg_weekend:.0f}" if daily_avg_weekend is not None and not pd.isna(daily_avg_weekend) else "—"
        st.markdown(custom_metric("Середнє за вихідні", avg_we_str, "Середня кількість операцій у вихідні"), unsafe_allow_html=True)
    with col5:
        peak_display = f"{peak:,.0f}" if peak > 0 else "—"
        if peak_date is not None:
            peak_display += f" ({peak_date.strftime('%d.%m')})"
        st.markdown(custom_metric("Пік за день", peak_display, "Найбільша кількість операцій за один день"), unsafe_allow_html=True)
    with col6:
        st.markdown(
            custom_metric(
                "Коефіцієнт погоджень",
                approval_rate_str,
                f"Частка TRUE від TRUE+FALSE. 🟢 ≥{APPROVAL_GOOD_THRESHOLD}% 🟡 {APPROVAL_WARN_THRESHOLD}-{APPROVAL_GOOD_THRESHOLD}% 🔴 <{APPROVAL_WARN_THRESHOLD}%",
                color=approval_rate_color(approval_rate_val if approval_rate_available else None),
            ),
            unsafe_allow_html=True,
        )

    col7, col8, col9, col10, col11 = st.columns(5)
    with col7:
        st.markdown(custom_metric("Пік / середнє", f"{peak_avg_ratio:.2f}×", "У скільки разів пік перевищує середнє"), unsafe_allow_html=True)
    with col8:
        st.markdown(
            custom_metric(
                "Стабільність (CV)",
                f"{cv:.1f}%" if cv > 0 else "—",
                "Коефіцієнт варіації. 🟢 <15% 🟡 15-30% 🔴 >30%",
                color=cv_color(cv if cv > 0 else None),
            ),
            unsafe_allow_html=True,
        )
    with col9:
        val = f"{WEEKDAY_UA.get(busiest_weekday, busiest_weekday)} — {busiest_weekday_val:.0f}/день" if busiest_weekday else "—"
        st.markdown(custom_metric("Найактивніший день", val, "День тижня з найвищим середнім навантаженням"), unsafe_allow_html=True)
    with col10:
        if busiest_op:
            disp_name = busiest_op if len(busiest_op) <= 12 else busiest_op[:10] + "…"
            val_op = f"{disp_name} — {busiest_op_val:,.0f}"
        else:
            val_op = "—"
        st.markdown(custom_metric("Найактивніша операція", val_op), unsafe_allow_html=True)
    with col11:
        if period_mode == "За місяцями" and len(selected_months) == 1 and operation_mode == "Тотал":
            st.markdown("**Порівняння**")
            if comparison_text != "—":
                for part in comparison_text.split(" "):
                    st.markdown(f"<p class='comparison-text'>{html.escape(part)}</p>", unsafe_allow_html=True)
            else:
                st.markdown("<p class='comparison-text'>—</p>", unsafe_allow_html=True)
        else:
            st.markdown(custom_metric("Порівняння", "—"), unsafe_allow_html=True)

    st.divider()
    st.subheader("💡 Інсайти")
    insights = []
    if approval_rate_available:
        if approval_rate_val >= APPROVAL_GOOD_THRESHOLD:
            insights.append(f"🟢 Коефіцієнт погоджень **{approval_rate_str}** — вище порогу {APPROVAL_GOOD_THRESHOLD}%, хороший показник.")
        elif approval_rate_val >= APPROVAL_WARN_THRESHOLD:
            insights.append(f"🟡 Коефіцієнт погоджень **{approval_rate_str}** — у середній зоні ({APPROVAL_WARN_THRESHOLD}-{APPROVAL_GOOD_THRESHOLD}%).")
        else:
            insights.append(f"🔴 Коефіцієнт погоджень **{approval_rate_str}** — нижче {APPROVAL_WARN_THRESHOLD}%, потребує уваги.")

    if not approval_by_op.empty and len(approval_by_op) > 1:
        best_row, worst_row = approval_by_op.iloc[0], approval_by_op.iloc[-1]
        if best_row["operation"] != worst_row["operation"]:
            insights.append(f"🧩 Найкращий % погоджень — **{best_row['operation']}** ({best_row['approval_rate']:.1f}%), найгірший — **{worst_row['operation']}** ({worst_row['approval_rate']:.1f}%).")

    if cv > 0:
        insights.append(f"{cv_interp} денного навантаження (CV = {cv:.1f}%).")
    if busiest_weekday:
        insights.append(f"📅 Найбільше навантаження припадає на **{WEEKDAY_UA.get(busiest_weekday, busiest_weekday)}** — ~{busiest_weekday_val:.0f} оп/день.")
    if busiest_op:
        insights.append(f"📈 Найактивніша операція — **{busiest_op}** ({busiest_op_val:,.0f} за період).")
    if peak_avg_ratio >= 2:
        insights.append(f"⚠️ Пік у **{peak_avg_ratio:.1f}×** перевищує середнє — можливі різкі сплески.")
    if period_mode == "За місяцями" and comparison_text != "—":
        insights.append(f"🔄 Порівняння з попер. періодами: {comparison_text}.")

    for item in insights:
        st.markdown(f"- {item}")

    st.divider()
    # --- Прогнози ---
    if period_mode != "За місяцями" or len(selected_months) != 1:
        st.info("📊 Прогнози доступні лише в режимі 'За місяцями' з одним вибраним місяцем.")
    else:
        forecast_target = st.selectbox("Прогнозувати для:", options=["Тотал"] + all_ops, index=0)
        stat_forecast, season_forecast = forecast_scenarios(df[df["operation"] == forecast_target], selected_months[0])

        if stat_forecast or season_forecast:
            st.subheader(f"📊 Прогнози на поточний місяць — {forecast_target}")
            if stat_forecast:
                st.markdown("**📈 Статистичний прогноз обсягу**")
                forecast_cards("Стат.", stat_forecast)
            if season_forecast:
                st.markdown("**📅 Сезонний прогноз обсягу**")
                forecast_cards("Сезон.", season_forecast)

    st.divider()
    st.subheader("📈 Динаміка за період")
    if operation_mode == "Тотал":
        daily_df = filtered.groupby("date")["value"].sum().reset_index()
        fig_overview = px.line(daily_df, x="date", y="value", markers=True, color_discrete_sequence=[KPO_CYAN])
        if smooth_enabled:
            daily_df["value_smooth"] = daily_df["value"].rolling(window=smooth_window, min_periods=1, center=True).mean()
            fig_overview.add_scatter(x=daily_df["date"], y=daily_df["value_smooth"], mode="lines", name=f"Ковзне середнє ({smooth_window} дн.)", line=dict(color=KPO_AMBER, width=3))
    else:
        fig_overview = px.line(filtered, x="date", y="value", color="operation", markers=True)

    fig_overview.update_xaxes(tickformat="%d.%m", title_text="Дата")
    fig_overview.update_layout(height=420, hovermode="x unified", margin=dict(l=10, r=10, t=20, b=10))
    st.plotly_chart(fig_overview, use_container_width=True)

# ------------------------------------------------------------
# TAB 2: ДИНАМІКА
# ------------------------------------------------------------
with tab2:
    st.subheader("📈 Детальна динаміка")
    if operation_mode == "Тотал":
        daily_df = filtered.groupby("date")["value"].sum().reset_index()
        fig_daily = px.line(daily_df, x="date", y="value", markers=True, title="Щоденна динаміка")
        if smooth_enabled:
            daily_df["value_smooth"] = daily_df["value"].rolling(window=smooth_window, min_periods=1, center=True).mean()
            fig_daily.add_scatter(x=daily_df["date"], y=daily_df["value_smooth"], mode="lines", name=f"Ковзне середнє ({smooth_window} дн.)", line=dict(color=KPO_AMBER, width=3))
    else:
        fig_daily = px.line(filtered, x="date", y="value", color="operation", markers=True, title="Динаміка вибраних операцій")

    fig_daily.update_xaxes(tickformat="%d.%m", title_text="Дата")
    fig_daily.update_layout(height=400, hovermode="x unified", margin=dict(l=10, r=10, t=20, b=10))
    st.plotly_chart(fig_daily, use_container_width=True)

    if operation_mode == "Тотал":
        st.subheader("📊 Порівняння по роках (YoY)")
        yoy_data = with_data(df[df["operation"] == "Тотал"])[lambda x: x["year"].isin(selected_years)]
        yoy_monthly = yoy_data.groupby(["year", "month"])["value"].sum().reset_index()
        yoy_monthly["month_num"] = yoy_monthly["month"].apply(lambda x: pd.Period(x).month)
        yoy_monthly["month_label"] = yoy_monthly["month"].apply(lambda x: pd.Period(x).strftime("%b"))
        yoy_monthly = yoy_monthly.sort_values(["year", "month_num"])

        fig_yoy = px.line(
            yoy_monthly,
            x="month_label",
            y="value",
            color="year",
            markers=True,
            title="Порівняння місячних сум по роках",
            category_orders={"month_label": yoy_monthly.drop_duplicates("month_num").sort_values("month_num")["month_label"].tolist()},
        )
        fig_yoy.update_layout(height=380, margin=dict(l=10, r=10, t=20, b=10))
        st.plotly_chart(fig_yoy, use_container_width=True)

    st.subheader("📈 Накопичувальна сума за період")
    cumsum_df = filtered.groupby("date")["value"].sum().sort_index().cumsum().reset_index(name="cumulative")
    fig_cum = px.line(cumsum_df, x="date", y="cumulative", markers=True)
    fig_cum.update_xaxes(tickformat="%d.%m", title_text="Дата")
    fig_cum.update_layout(height=380, margin=dict(l=10, r=10, t=20, b=10))
    st.plotly_chart(fig_cum, use_container_width=True)

    st.subheader("🔍 Аномальні дні")
    anomalies = detect_anomalies(filtered, window=14, threshold=3.0)
    if not anomalies.empty:
        anomaly_points = anomalies[anomalies["is_anomaly"]].copy()
        if not anomaly_points.empty:
            anomaly_points["date_str"] = anomaly_points["date"].dt.strftime("%d.%m.%Y")
            anomaly_points["deviation"] = ((anomaly_points["value"] - anomaly_points["rolling_median"]) / anomaly_points["rolling_median"] * 100).round(1)
            anomaly_points["type"] = anomaly_points["deviation"].apply(lambda x: "🔴 Високий" if x > 10 else ("🔵 Низький" if x < -10 else "🟡 Помірний"))
            st.dataframe(
                anomaly_points[["date_str", "value", "rolling_median", "deviation", "type", "z_score"]].sort_values("date", ascending=False),
                use_container_width=True,
                hide_index=True,
            )
        else:
            st.info("Аномальних днів не виявлено.")
    else:
        st.info("Недостатньо даних для виявлення аномалій.")

# ------------------------------------------------------------
# TAB 3: ОПЕРАЦІЇ
# ------------------------------------------------------------
with tab3:
    st.subheader("🧩 Аналіз операцій")
    if not approval_by_op.empty:
        approval_by_op_display = approval_by_op.copy()
        approval_by_op_display["tier"] = approval_by_op_display["approval_rate"].apply(approval_rate_tier)
        fig_approval = px.bar(
            approval_by_op_display,
            x="operation",
            y="approval_rate",
            color="tier",
            color_discrete_map=APPROVAL_TIER_COLOR_MAP,
            text=approval_by_op_display["approval_rate"].astype(str) + "%",
            title="Коефіцієнт погоджень по операціях",
        )
        fig_approval.update_layout(height=360, yaxis=dict(range=[0, 100]))
        st.plotly_chart(fig_approval, use_container_width=True)

    ops_data = with_data(filtered[filtered["operation"] != "Тотал"])
    if not ops_data.empty:
        st.subheader("📊 Структура операцій")
        ops_struct = ops_data.groupby("operation")["value"].sum().reset_index().sort_values("value", ascending=False)
        fig_ops = px.bar(ops_struct, x="value", y="operation", orientation="h", title="Обсяги за операціями")
        st.plotly_chart(fig_ops, use_container_width=True)

# ------------------------------------------------------------
# TAB 4: НАВАНТАЖЕННЯ
# ------------------------------------------------------------
with tab4:
    st.subheader("📅 Аналіз навантаження")
    daily_sum = filtered_stats.groupby("date")["value"].sum().reset_index()
    daily_sum["weekday_ua"] = daily_sum["date"].dt.day_name().map(WEEKDAY_UA)
    weekday_avg = daily_sum.groupby("weekday_ua")["value"].mean().reindex(WEEKDAY_ORDER_UA).reset_index(name="avg_value").fillna(0)

    fig_weekday = px.bar(weekday_avg, x="weekday_ua", y="avg_value", text=weekday_avg["avg_value"].round(1).astype(str), title="Середня кількість операцій за днями тижня")
    st.plotly_chart(fig_weekday, use_container_width=True)

    st.subheader("📊 Розподіл відхилень від середнього")
    daily_totals = filtered_stats.groupby("date")["value"].sum().reset_index()
    daily_totals["is_weekend"] = daily_totals["date"].dt.dayofweek >= 5

    if len(daily_totals) >= 3:
        mean_all = daily_totals["value"].mean()
        daily_totals["dev_all"] = (daily_totals["value"] - mean_all) / mean_all * 100
        dev_all = daily_totals["dev_all"].dropna().values

        if len(dev_all) > 1:
            fig_density = go.Figure()
            x_grid = np.linspace(dev_all.min() - 10, dev_all.max() + 10, 200)
            density = gaussian_kde_np(dev_all, x_grid)
            fig_density.add_trace(go.Scatter(x=x_grid, y=density, mode="lines", name="Всі дні", line=dict(color=KPO_CYAN, width=2.5)))
            fig_density.update_layout(title="Криві щільності відхилень від середнього", xaxis_title="Відхилення, %", height=400)
            st.plotly_chart(fig_density, use_container_width=True)

# ------------------------------------------------------------
# TAB 5: ПОРІВНЯННЯ ПЕРІОДІВ
# ------------------------------------------------------------
with tab5:
    st.subheader("🆚 Порівняння двох довільних періодів")
    cmp_op_mode = st.radio("Операції для порівняння", options=["Тотал", "Вибрані операції"], index=0, horizontal=True)
    cmp_ops = ["Тотал"] if cmp_op_mode == "Тотал" else st.multiselect("Операції", options=all_ops, default=all_ops)

    col_a, col_b = st.columns(2)
    with col_a:
        range_a_input = st.date_input("Діапазон A", value=(max_date - pd.Timedelta(days=27)).date(), min_value=min_date.date(), max_value=max_date.date())
    with col_b:
        range_b_input = st.date_input("Діапазон B", value=(max_date - pd.Timedelta(days=13)).date(), min_value=min_date.date(), max_value=max_date.date())


def normalize_range_input(val: Any) -> Tuple[pd.Timestamp, pd.Timestamp]:
    if isinstance(val, tuple) and len(val) == 2:
        start, end = pd.Timestamp(val[0]), pd.Timestamp(val[1])
    else:
        single = val[0] if isinstance(val, tuple) else val
        start = end = pd.Timestamp(single)
    return (end, start) if start > end else (start, end)


if isinstance(range_a_input, tuple) and len(range_a_input) == 2 and isinstance(range_b_input, tuple) and len(range_b_input) == 2:
    range_a = normalize_range_input(range_a_input)
    range_b = normalize_range_input(range_b_input)


    def build_period_metrics(date_range: Tuple[pd.Timestamp, pd.Timestamp], ops: List[str]) -> Optional[Dict]:
        start, end = date_range
        mask = (df["date"] >= start) & (df["date"] <= end) & (df["operation"].isin(ops))
        scoped_stats = with_data(df[mask])
        if scoped_stats.empty:
            return None
        daily = scoped_stats.groupby("date")["value"].sum()
        return {
            "total": daily.sum(),
            "avg": daily.mean(),
            "peak": daily.max(),
            "days": (end - start).days + 1,
        }


    metrics_a = build_period_metrics(range_a, cmp_ops)
    metrics_b = build_period_metrics(range_b, cmp_ops)

    if metrics_a and metrics_b:
        chart_metrics = pd.DataFrame(
            {
                "Метрика": ["Всього", "Середнє за день", "Пік"],
                "A": [metrics_a["total"], metrics_a["avg"], metrics_a["peak"]],
                "B": [metrics_b["total"], metrics_b["avg"], metrics_b["peak"]],
            }
        ).melt(id_vars="Метрика", var_name="Період", value_name="Значення")
        fig_cmp = px.bar(chart_metrics, x="Метрика", y="Значення", color="Період", barmode="group", title="Порівняльний аналіз A vs B")
        st.plotly_chart(fig_cmp, use_container_width=True)

st.caption("Джерело: Google Sheets • Час: Europe/Kyiv.")
