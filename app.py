import html
import re
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


st.set_page_config(
    page_title="KPO Dashboard",
    page_icon="📊",
    layout="wide",
)

# ============================================================
# DARK ANALYTICS THEME — кольорова палітра + глобальний Plotly template.
# Реєструється ОДИН раз і застосовується автоматично до ВСІХ графіків
# (px.*, go.Figure), без потреби правити кожен виклик окремо.
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

# Кастомна теплова шкала "темна навігація → неоновий ціан" для градієнтів навантаження
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

SPREADSHEET_ID = "1STX1vgDAk3zVDshXdZmTgJJSvQNCN4WmmftOskwymYI"
SHEETS = ["24", "25", "26"]

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
    "Зміна дати активації ": "Зміна дати активації",
}

WEEKDAY_UA = {
    "Monday": "Пн", "Tuesday": "Вт", "Wednesday": "Ср",
    "Thursday": "Чт", "Friday": "Пт", "Saturday": "Сб", "Sunday": "Нд",
}
WEEKDAY_ORDER_UA = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Нд"]

# Пороги для кольорового маркування коефіцієнта погоджень
APPROVAL_GOOD_THRESHOLD = 85
APPROVAL_WARN_THRESHOLD = 70

COLOR_GOOD = KPO_GREEN
COLOR_WARN = KPO_AMBER
COLOR_BAD = KPO_RED


def approval_rate_color(value):
    """Колір за порогом коефіцієнта погоджень: >=85% зелений, 70-85% жовтий, <70% червоний."""
    if value is None:
        return None
    if value >= APPROVAL_GOOD_THRESHOLD:
        return COLOR_GOOD
    if value >= APPROVAL_WARN_THRESHOLD:
        return COLOR_WARN
    return COLOR_BAD


def approval_rate_tier(value):
    """Текстова категорія за тим самим порогом — для кольорових легенд на графіках."""
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
    """Колір за порогом коефіцієнта варіації: <15% зелений, 15-30% жовтий, >30% червоний."""
    if value is None or value <= 0:
        return None
    if value < 15:
        return COLOR_GOOD
    if value < 30:
        return COLOR_WARN
    return COLOR_BAD

TOTAL_ROW_SEARCH_RANGE = 10
DETAIL_SEARCH_RANGE = 30
FIRST_DAY_COLUMN = 4
# Таблиця TRUE/FALSE по операціях (колонки A/B/C) розташована одразу під рядком "Тотал"
PER_OP_TF_SEARCH_RANGE = len(OPERATIONS) + 3


def now_kyiv() -> pd.Timestamp:
    """Поточний час у Києві, як naive Timestamp (сумісний з датами в df)."""
    return pd.Timestamp.now(tz=KYIV_TZ).replace(tzinfo=None).normalize()


def now_kyiv_exact() -> pd.Timestamp:
    """Поточний момент у Києві без нормалізації до півночі."""
    return pd.Timestamp.now(tz=KYIV_TZ).replace(tzinfo=None)


def is_empty_cell(value) -> bool:
    """Клітинка вважається порожньою (даних ще не внесли), а не фактичним нулем."""
    if value is None:
        return True
    if isinstance(value, bool):
        return False
    return str(value).strip() == ""


def as_number(value):
    if is_empty_cell(value):
        return 0.0
    val_str = str(value).replace("\xa0", "").replace(" ", "").replace(",", ".")
    val_str = re.sub(r"[^\d.-]", "", val_str)
    try:
        return float(val_str)
    except (TypeError, ValueError):
        return 0.0


def parse_month_header(value, sheet_year):
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

            # --- Зчитуємо рядок "Тотал" (колонки B і C), а також таблицю TRUE/FALSE
            # по кожній операції, яка розташована одразу під рядком "Тотал":
            # колонка A = назва операції, B = сума TRUE (погоджено), C = сума FALSE (відхилено) ---
            total_row_idx = None
            for r in range(header_row + 1, min(header_row + TOTAL_ROW_SEARCH_RANGE, len(values))):
                if len(values[r]) > 0 and values[r][0] == "Тотал":
                    total_row_idx = r
                    break

            if total_row_idx is not None:
                sum_true = as_number(values[total_row_idx][1]) if len(values[total_row_idx]) > 1 else 0
                sum_false = as_number(values[total_row_idx][2]) if len(values[total_row_idx]) > 2 else 0
                op_true_false.append({
                    "month": month_key,
                    "operation": "Тотал",
                    "sum_true": sum_true,
                    "sum_false": sum_false
                })

                per_op_tf_found = set()
                for r in range(total_row_idx + 1, min(total_row_idx + 1 + PER_OP_TF_SEARCH_RANGE, len(values))):
                    cell_a = values[r][0] if len(values[r]) > 0 else ""
                    op_name = cell_a.strip() if isinstance(cell_a, str) else ""
                    op_name = ALIASES.get(op_name, op_name)
                    if op_name not in OPERATIONS or op_name in per_op_tf_found:
                        continue
                    op_sum_true = as_number(values[r][1]) if len(values[r]) > 1 else 0
                    op_sum_false = as_number(values[r][2]) if len(values[r]) > 2 else 0
                    op_true_false.append({
                        "month": month_key,
                        "operation": op_name,
                        "sum_true": op_sum_true,
                        "sum_false": op_sum_false,
                    })
                    per_op_tf_found.add(op_name)

                missing_tf_ops = [op for op in OPERATIONS if op not in per_op_tf_found]
                if missing_tf_ops:
                    warnings.append(
                        f"⚠️ Аркуш «{sheet_name}», {month_label}: не знайдено TRUE/FALSE дані "
                        f"для операцій: {', '.join(missing_tf_ops)}."
                    )
            else:
                warnings.append(
                    f"⚠️ Аркуш «{sheet_name}», {month_label}: не знайдено рядок «Тотал» "
                    f"(перевірте структуру таблиці)."
                )

            # --- Зчитуємо деталізовані дані ---
            detail_start = None
            for r in range(header_row + 1, min(header_row + DETAIL_SEARCH_RANGE, len(values))):
                if len(values[r]) > 3 and values[r][3].strip() in OPERATIONS:
                    detail_start = r
                    break

            if detail_start is None:
                warnings.append(
                    f"⚠️ Аркуш «{sheet_name}», {month_label}: не знайдено таблицю деталізації "
                    f"операцій — місяць пропущено."
                )
                continue

            days = pd.Period(f"{year}-{month:02d}").days_in_month

            for r in range(detail_start, len(values)):
                if r >= len(values):
                    break

                raw_operation = values[r][3] if len(values[r]) > 3 else ""
                operation = raw_operation.strip()
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


def with_data(df):
    """Повертає лише рядки, де дані фактично внесені (виключає порожні клітинки)."""
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


def detect_anomalies(df, window=14, threshold=1.5):
    df = with_data(df)
    if df.empty:
        return pd.DataFrame()
    daily = df.groupby("date")["value"].sum().reset_index()
    daily = daily.sort_values("date")
    if len(daily) < window:
        return pd.DataFrame()
    daily["rolling_avg"] = daily["value"].rolling(window=window, min_periods=1, center=True).mean()
    daily["rolling_std"] = daily["value"].rolling(window=window, min_periods=1, center=True).std().fillna(0)
    daily["z_score"] = (daily["value"] - daily["rolling_avg"]) / daily["rolling_std"].replace(0, 1)
    daily["is_anomaly"] = abs(daily["z_score"]) > threshold
    daily = daily[(daily["value"] > 0)]
    return daily


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

        if not prev_fact.empty and prev_fact["value"].sum() > 0:
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


def forecast_approval_rate(df, current_month):
    """Прогноз коефіцієнта погоджень на кінець поточного місяця.

    ВАЖЛИВО: TRUE/FALSE дані є в таблиці лише як МІСЯЧНИЙ підсумок (без розбивки
    по днях), тому точний прогноз "факт до сьогодні + залишок" тут неможливий,
    на відміну від прогнозу обсягів. Натомість рахуємо орієнтовну оцінку:
      - "поточний" коефіцієнт = TRUE / (TRUE+FALSE) станом на зараз (як записано
        в таблиці для цього місяця);
      - для очікуваного коефіцієнта на кінець місяця він змішується з коефіцієнтом
        аналогічного місяця минулого року (якщо є) з вагою, що зростає в міру
        того, скільки днів місяця ще залишилось (на початку місяця більше довіри
        до "історичного" значення, ближче до кінця — до поточного факту).
    Це орієнтовна оцінка, а не точний прогноз, і позначена як така в інтерфейсі.
    """
    if df.empty or current_month not in df["month"].values:
        return None

    today = now_kyiv()
    if today.month != pd.Period(current_month).month or today.year != pd.Period(current_month).year:
        return None

    month_rows = df[df["month"] == current_month][["sum_true", "sum_false"]].drop_duplicates()
    if month_rows.empty:
        return None
    fact_true = month_rows["sum_true"].sum()
    fact_false = month_rows["sum_false"].sum()
    fact_total = fact_true + fact_false
    if fact_total <= 0:
        return None
    current_rate = fact_true / fact_total * 100

    total_days = pd.Period(current_month).days_in_month
    days_passed = (today - pd.Timestamp(year=today.year, month=today.month, day=1)).days + 1
    days_passed = min(max(days_passed, 1), total_days)

    prev_period = pd.Period(current_month) - 12
    prev_rows = df[df["month"] == str(prev_period)][["sum_true", "sum_false"]].drop_duplicates()
    prev_rate = None
    if not prev_rows.empty:
        prev_true = prev_rows["sum_true"].sum()
        prev_false = prev_rows["sum_false"].sum()
        prev_total = prev_true + prev_false
        if prev_total > 0:
            prev_rate = prev_true / prev_total * 100

    proxy_rate = prev_rate if prev_rate is not None else current_rate
    weight_fact = days_passed / total_days if total_days > 0 else 1.0
    forecast_rate = current_rate * weight_fact + proxy_rate * (1 - weight_fact)

    return {
        "current_rate": current_rate,
        "forecast_rate": forecast_rate,
        "proxy_rate": proxy_rate,
        "has_prev_year": prev_rate is not None,
        "prev_period": str(prev_period),
        "fact_true": fact_true,
        "fact_false": fact_false,
        "days_passed": days_passed,
        "total_days": total_days,
    }


# --- Завантаження та фільтри ---

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

st.sidebar.header("Фільтри")

period_mode = st.sidebar.radio(
    "Тип періоду",
    options=["За місяцями", "Довільний діапазон дат"],
    index=0,
    help=(
        "'За місяцями' — стандартний вибір Рік + Місяць з усіма функціями "
        "(прогнози, MoM/YoY порівняння). 'Довільний діапазон' — будь-які дати "
        "(напр. останні 2 тижні); прогнози та MoM/YoY порівняння в цьому режимі недоступні."
    ),
)

min_date = df["date"].min()
max_date = df["date"].max()

years = sorted(df["year"].unique())
current_year = now_kyiv().year
if current_year in years:
    default_years = [current_year]
else:
    default_years = [years[-1]] if years else []

custom_range = None

if period_mode == "За місяцями":
    selected_years = st.sidebar.multiselect(
        "Рік",
        options=years,
        default=default_years,
    )

    available_months = (
        df[df["year"].isin(selected_years)]["month"]
        .drop_duplicates()
        .sort_values()
        .tolist()
    )

    # Нова логіка: якщо вибрано рівно один рік і він не поточний – вибираємо всі місяці
    if len(selected_years) == 1 and selected_years[0] != current_year:
        default_months = available_months
    else:
        current_month_str = now_kyiv().strftime("%Y-%m")
        if current_month_str in available_months:
            default_months = [current_month_str]
        else:
            default_months = available_months[-1:] if available_months else []

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

    # Похідні набори років/місяців — потрібні іншим частинам дашборду
    # (напр. для розбивки по операціях), що орієнтуються на ці списки.
    selected_years = sorted({custom_range[0].year, custom_range[1].year})
    selected_months = sorted({
        d.strftime("%Y-%m") for d in pd.date_range(custom_range[0], custom_range[1], freq="D")
    })

# --- Вибір операцій ---
operation_mode = st.sidebar.radio(
    "Режим показу",
    options=["Тотал", "Вибрані операції"],
    index=0,
)

all_ops = [op for op in OPERATIONS if op in df["operation"].unique()]
if operation_mode == "Тотал":
    selected_operations = ["Тотал"]
else:
    default_ops = all_ops if all_ops else []
    selected_operations = st.sidebar.multiselect(
        "Операції",
        options=all_ops,
        default=default_ops,
    )
    if not selected_operations:
        st.sidebar.warning("Виберіть хоча б одну операцію.")
        selected_operations = ["Тотал"]

# Згладжування
st.sidebar.divider()
st.sidebar.subheader("Налаштування графіків")
smooth_enabled = st.sidebar.checkbox("Згладжування динаміки (ковзне середнє)", value=False)
smooth_window = 7
if smooth_enabled:
    smooth_window = st.sidebar.selectbox("Вікно згладжування (дні)", [3, 5, 7, 14], index=2)

# Фільтруємо дані
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

# --- Обробка поточного незавершеного місяця (лише в режимі "За місяцями") ---
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

# Дані з фактично внесеними значеннями (без порожніх клітинок) — для статистики
filtered_stats = with_data(filtered)
if filtered_stats.empty:
    st.info("Дані ще не внесені для жодного дня у вибраному періоді — статистика недоступна, показано лише графіки.")

# --- Розрахунок базових метрик (тільки по днях з реальними даними) ---
daily_total = filtered_stats.groupby("date")["value"].sum()
total_value = daily_total.sum()
# "Всього" за період враховує ВСІ дні (в т.ч. невнесені = 0), а середнє за день —
# тільки фактичні дні, щоб порожні клітинки не занижували показник штучно
daily_avg = daily_total.mean() if not daily_total.empty else 0

weekday_mask = filtered_stats["is_weekend"] == False
weekend_mask = filtered_stats["is_weekend"] == True

if weekday_mask.any():
    daily_avg_weekday = filtered_stats[weekday_mask].groupby("date")["value"].sum().mean()
else:
    daily_avg_weekday = None

if weekend_mask.any():
    daily_avg_weekend = filtered_stats[weekend_mask].groupby("date")["value"].sum().mean()
else:
    daily_avg_weekend = None

peak = daily_total.max() if not daily_total.empty else 0
min_val = daily_total.min() if not daily_total.empty else 0
peak_avg_ratio = peak / daily_avg if daily_avg > 0 else 0

busiest_weekday, busiest_weekday_val = calc_busiest_weekday(filtered)
busiest_op, busiest_op_val = calc_busiest_operation(filtered)
std, cv, cv_interp = calc_stability(filtered, daily_avg)

# --- Коефіцієнт погоджень: рахується як для "Тотал", так і для вибраних операцій.
# tf-дані по кожній (місяць, операція) продубльовані на кожен день місяця при мерджі,
# тож drop_duplicates повертає точні місячні суми незалежно від truncation поточного місяця.
tf_filtered = filtered[["month", "operation", "sum_true", "sum_false"]].drop_duplicates()
sum_true_total = tf_filtered["sum_true"].sum()
sum_false_total = tf_filtered["sum_false"].sum()
total_ratio = sum_true_total + sum_false_total
approval_rate_val = (sum_true_total / total_ratio * 100) if total_ratio > 0 else 0
approval_rate_str = f"{approval_rate_val:.1f}%" if total_ratio > 0 else "—"

# --- Коефіцієнт погоджень по кожній операції за вибраний період (для теплокарти,
# графіка в Tab3 та інсайтів у Tab1) — не залежить від "Режиму показу" в сайдбарі,
# лише від обраного Рік/Місяць або довільного діапазону дат.
if period_mode == "За місяцями":
    period_mask = df["year"].isin(selected_years) & df["month"].isin(selected_months)
else:
    period_mask = (df["date"] >= custom_range[0]) & (df["date"] <= custom_range[1])

tf_by_op_all = df[period_mask & (df["operation"] != "Тотал")][
    ["month", "operation", "sum_true", "sum_false"]
].drop_duplicates()

approval_by_op = pd.DataFrame(columns=["operation", "sum_true", "sum_false", "total", "approval_rate"])
if not tf_by_op_all.empty:
    approval_by_op = tf_by_op_all.groupby("operation", as_index=False)[["sum_true", "sum_false"]].sum()
    approval_by_op["total"] = approval_by_op["sum_true"] + approval_by_op["sum_false"]
    approval_by_op = approval_by_op[approval_by_op["total"] > 0].copy()
    approval_by_op["approval_rate"] = (approval_by_op["sum_true"] / approval_by_op["total"] * 100).round(1)
    approval_by_op = approval_by_op.sort_values("approval_rate", ascending=False)

# --- Порівняння (лише в режимі "За місяцями", для одного місяця, режим "Тотал") ---
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


def custom_metric(label, value, help_text=None, color=None):
    """Рендерить метрику як HTML. Усі значення екрануються (html.escape),
    щоб дані з Google Таблиці не могли інʼєктувати довільний HTML/JS.
    color: опційний hex-колір значення (для порогового маркування)."""
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


st.markdown(f"""
<style>
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@500;600;700&display=swap');

    html, body, [class*="css"] {{
        font-family: 'Inter', sans-serif;
    }}

    .stApp {{
        background-color: {KPO_BG};
    }}

    /* --- Картки метрик --- */
    .metric-container {{
        background: {KPO_CARD_BG};
        border: 1px solid {KPO_BORDER};
        border-left: 3px solid {KPO_CYAN};
        border-radius: 8px;
        padding: 0.65rem 0.9rem;
        margin-bottom: 0.5rem;
        transition: border-left-color 0.15s ease;
    }}
    .metric-container:hover {{
        border-left-color: {KPO_AMBER};
    }}
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

    /* --- Вкладки --- */
    .stTabs [data-baseweb="tab-list"] {{
        gap: 4px;
        border-bottom: 1px solid {KPO_BORDER};
    }}
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

    /* --- Сайдбар --- */
    section[data-testid="stSidebar"] {{
        background-color: #0e131d;
        border-right: 1px solid {KPO_BORDER};
    }}

    /* --- Заголовки --- */
    h1, h2, h3 {{
        font-family: 'Inter', sans-serif;
        letter-spacing: -0.01em;
    }}

    /* --- Розділювачі --- */
    hr {{
        border-color: {KPO_BORDER} !important;
    }}
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
        st.markdown(custom_metric("Пік за день", f"{peak:,.0f}", "Найбільша кількість операцій за один день (лише дні з внесеними даними)"), unsafe_allow_html=True)

    with col6:
        st.markdown(custom_metric(
            "Коефіцієнт погоджень",
            approval_rate_str,
            "Частка TRUE (погоджено) від TRUE+FALSE. У режимі 'Вибрані операції' — сукупно по обраних операціях. "
            f"🟢 ≥{APPROVAL_GOOD_THRESHOLD}% 🟡 {APPROVAL_WARN_THRESHOLD}-{APPROVAL_GOOD_THRESHOLD}% 🔴 <{APPROVAL_WARN_THRESHOLD}%",
            color=approval_rate_color(approval_rate_val if total_ratio > 0 else None),
        ), unsafe_allow_html=True)

    col7, col8, col9, col10, col11 = st.columns(5)

    with col7:
        st.markdown(custom_metric("Пік / середнє", f"{peak_avg_ratio:.2f}×", "У скільки разів пік перевищує середнє"), unsafe_allow_html=True)

    with col8:
        st.markdown(custom_metric(
            "Стабільність (CV)",
            f"{cv:.1f}%" if cv > 0 else "—",
            "Коефіцієнт варіації (лише дні з внесеними даними). 🟢 <15% 🟡 15-30% 🔴 >30%",
            color=cv_color(cv if cv > 0 else None),
        ), unsafe_allow_html=True)

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
                    st.markdown(
                        f"<p class='comparison-text'>{html.escape(part)}</p>",
                        unsafe_allow_html=True
                    )
            else:
                st.markdown(
                    "<p class='comparison-text'>—</p>",
                    unsafe_allow_html=True
                )
        else:
            st.markdown(custom_metric("Порівняння", "—", "Доступно лише для одного місяця в режимі 'За місяцями' + 'Тотал'"), unsafe_allow_html=True)

    st.divider()

    # --- АВТОМАТИЧНІ ІНСАЙТИ ---
    st.subheader("💡 Інсайти")
    insights = []

    if total_ratio > 0:
        if approval_rate_val >= APPROVAL_GOOD_THRESHOLD:
            insights.append(f"🟢 Коефіцієнт погоджень **{approval_rate_str}** — вище порогу {APPROVAL_GOOD_THRESHOLD}%, хороший показник.")
        elif approval_rate_val >= APPROVAL_WARN_THRESHOLD:
            insights.append(f"🟡 Коефіцієнт погоджень **{approval_rate_str}** — у середній зоні ({APPROVAL_WARN_THRESHOLD}-{APPROVAL_GOOD_THRESHOLD}%), варто стежити за динамікою.")
        else:
            insights.append(f"🔴 Коефіцієнт погоджень **{approval_rate_str}** — нижче {APPROVAL_WARN_THRESHOLD}%, потребує уваги.")

    if not approval_by_op.empty and len(approval_by_op) > 1:
        best_row = approval_by_op.iloc[0]
        worst_row = approval_by_op.iloc[-1]
        if best_row["operation"] != worst_row["operation"]:
            insights.append(
                f"🧩 Найкращий % погоджень — **{best_row['operation']}** ({best_row['approval_rate']:.1f}%), "
                f"найгірший — **{worst_row['operation']}** ({worst_row['approval_rate']:.1f}%)."
            )

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

    if insights:
        for i in insights:
            st.markdown(f"- {i}")
    else:
        st.caption("Недостатньо даних для інсайтів у вибраному періоді.")

    st.divider()

    # --- БЛОК ПРОГНОЗІВ ---
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
        approval_forecast = forecast_approval_rate(
            df[df["operation"] == forecast_target], selected_months[0]
        )

        if stat_forecast or season_forecast or approval_forecast:
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

            if approval_forecast:
                st.markdown("**✅ Прогноз коефіцієнта погоджень** (орієнтовна оцінка — TRUE/FALSE є лише як місячний підсумок, без розбивки по днях)")
                col_ar1, col_ar2 = st.columns(2)
                with col_ar1:
                    st.markdown(custom_metric(
                        "Поточний коефіцієнт",
                        f"{approval_forecast['current_rate']:.1f}%",
                        f"TRUE {approval_forecast['fact_true']:,.0f} / FALSE {approval_forecast['fact_false']:,.0f}, станом на {approval_forecast['days_passed']} з {approval_forecast['total_days']} днів місяця",
                        color=approval_rate_color(approval_forecast['current_rate']),
                    ), unsafe_allow_html=True)
                with col_ar2:
                    proxy_note = (
                        f"змішано з коефіцієнтом за {approval_forecast['prev_period']} (той самий місяць минулого року)"
                        if approval_forecast["has_prev_year"]
                        else "історичних даних за минулий рік немає — використано поточний коефіцієнт без змін"
                    )
                    st.markdown(custom_metric(
                        "Прогноз на кінець місяця",
                        f"{approval_forecast['forecast_rate']:.1f}%",
                        f"Орієнтовна оцінка: {proxy_note}.",
                        color=approval_rate_color(approval_forecast['forecast_rate']),
                    ), unsafe_allow_html=True)
        else:
            st.info("Прогноз недоступний: немає фактичних даних за поточний місяць для цієї операції.")

    st.divider()

    # --- Загальна динаміка ---
    st.subheader("📈 Динаміка за період")

    if operation_mode == "Тотал":
        daily = filtered.groupby("date")["value"].sum().reset_index()
        fig_overview = px.line(
            daily,
            x="date",
            y="value",
            markers=True,
            labels={"date": "Дата", "value": "Кількість"},
            color_discrete_sequence=[KPO_CYAN],
        )
        fig_overview.update_xaxes(tickformat="%d.%m", title_text="Дата")
        if smooth_enabled:
            daily["value_smooth"] = daily["value"].rolling(window=smooth_window, min_periods=1, center=True).mean()
            fig_overview.add_scatter(
                x=daily["date"],
                y=daily["value_smooth"],
                mode="lines",
                name=f"Ковзне середнє ({smooth_window} дн.)",
                line=dict(color=KPO_AMBER, width=3),
            )
        anomalies = detect_anomalies(filtered, window=14, threshold=1.5)
        if not anomalies.empty:
            anomaly_points = anomalies[anomalies["is_anomaly"]]
            if not anomaly_points.empty:
                fig_overview.add_scatter(
                    x=anomaly_points["date"],
                    y=anomaly_points["value"],
                    mode="markers",
                    marker=dict(color=KPO_RED, size=10, symbol="x"),
                    name="Аномалія",
                )
    else:
        fig_overview = px.line(
            filtered,
            x="date",
            y="value",
            color="operation",
            markers=True,
            labels={"date": "Дата", "value": "Кількість", "operation": "Операція"},
        )
        fig_overview.update_xaxes(tickformat="%d.%m", title_text="Дата")

    fig_overview.update_layout(
        height=420,
        hovermode="x unified",
        margin=dict(l=10, r=10, t=20, b=10),
    )
    st.plotly_chart(fig_overview, use_container_width=True)


# ============================================================
# TAB 2: ДИНАМІКА
# ============================================================
with tab2:
    st.subheader("📈 Детальна динаміка")

    if operation_mode == "Тотал":
        daily = filtered.groupby("date")["value"].sum().reset_index()
        fig_daily_detailed = px.line(
            daily,
            x="date",
            y="value",
            markers=True,
            labels={"date": "Дата", "value": "Кількість"},
            title="Щоденна динаміка"
        )
        fig_daily_detailed.update_xaxes(tickformat="%d.%m", title_text="Дата")
        if smooth_enabled:
            daily["value_smooth"] = daily["value"].rolling(window=smooth_window, min_periods=1, center=True).mean()
            fig_daily_detailed.add_scatter(
                x=daily["date"],
                y=daily["value_smooth"],
                mode="lines",
                name=f"Ковзне середнє ({smooth_window} дн.)",
                line=dict(color=KPO_AMBER, width=3),
            )
        anomalies = detect_anomalies(filtered, window=14, threshold=1.5)
        if not anomalies.empty:
            anomaly_points = anomalies[anomalies["is_anomaly"]]
            if not anomaly_points.empty:
                fig_daily_detailed.add_scatter(
                    x=anomaly_points["date"],
                    y=anomaly_points["value"],
                    mode="markers",
                    marker=dict(color=KPO_RED, size=10, symbol="x"),
                    name="Аномалія",
                )
    else:
        fig_daily_detailed = px.line(
            filtered,
            x="date",
            y="value",
            color="operation",
            markers=True,
            labels={"date": "Дата", "value": "Кількість", "operation": "Операція"},
            title="Динаміка вибраних операцій"
        )
        fig_daily_detailed.update_xaxes(tickformat="%d.%m", title_text="Дата")

    fig_daily_detailed.update_layout(
        height=400,
        hovermode="x unified",
        margin=dict(l=10, r=10, t=20, b=10),
    )
    st.plotly_chart(fig_daily_detailed, use_container_width=True)

    if operation_mode == "Тотал":
        st.subheader("📊 Порівняння по роках (YoY)")
        yoy_data = with_data(df[df["operation"] == "Тотал"]).copy()
        yoy_data = yoy_data[yoy_data["year"].isin(selected_years)]
        yoy_monthly = yoy_data.groupby(["year", "month"])["value"].sum().reset_index()
        yoy_monthly["month_num"] = yoy_monthly["month"].apply(lambda x: pd.Period(x).month)
        yoy_monthly["month_label"] = yoy_monthly["month"].apply(lambda x: pd.Period(x).strftime("%b"))
        yoy_monthly = yoy_monthly.sort_values(["year", "month_num"])

        # Календарний порядок місяців (Jan → Dec) для осі X, незалежно від того,
        # який рік/місяць першим зустрічається в даних (у нас таблиця починається з жовтня 2024)
        month_axis_order = (
            yoy_monthly.drop_duplicates("month_num")
            .sort_values("month_num")["month_label"]
            .tolist()
        )

        fig_yoy = px.line(
            yoy_monthly,
            x="month_label",
            y="value",
            color="year",
            markers=True,
            labels={"month_label": "Місяць", "value": "Кількість", "year": "Рік"},
            title="Порівняння місячних сум по роках",
            category_orders={"month_label": month_axis_order},
        )
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
    fig_cum = px.line(
        cumsum,
        x="date",
        y="cumulative",
        markers=True,
        labels={"date": "Дата", "cumulative": "Накопичена кількість"},
    )
    fig_cum.update_xaxes(tickformat="%d.%m", title_text="Дата")
    fig_cum.update_layout(height=380, margin=dict(l=10, r=10, t=20, b=10))
    st.plotly_chart(fig_cum, use_container_width=True)

    st.subheader("🔍 Аномальні дні")
    anomalies = detect_anomalies(filtered, window=14, threshold=1.5)
    if not anomalies.empty:
        anomaly_points = anomalies[anomalies["is_anomaly"]]
        if not anomaly_points.empty:
            anomaly_points = anomaly_points.copy()
            anomaly_points["date_str"] = anomaly_points["date"].dt.strftime("%d.%m.%Y")
            anomaly_points["deviation"] = ((anomaly_points["value"] - anomaly_points["rolling_avg"]) / anomaly_points["rolling_avg"] * 100).round(1)
            anomaly_points["type"] = anomaly_points["deviation"].apply(lambda x: "🔴 Високий" if x > 10 else "🔵 Низький" if x < -10 else "🟡 Помірний")
            anomaly_points = anomaly_points.sort_values("date", ascending=False)
            st.dataframe(
                anomaly_points[["date_str", "value", "rolling_avg", "deviation", "type", "z_score"]],
                column_config={
                    "date_str": "Дата",
                    "value": "Значення",
                    "rolling_avg": "Середнє (14 днів)",
                    "deviation": "Відхилення, %",
                    "type": "Тип",
                    "z_score": st.column_config.NumberColumn(
                        "Z-score",
                        help="Кількість стандартних відхилень від середнього. Значення >1.5 вважається аномалією."
                    )
                },
                use_container_width=True,
                hide_index=True
            )
        else:
            st.info("Аномальних днів не виявлено.")
    else:
        st.info("Недостатньо даних для виявлення аномалій (потрібно щонайменше 14 днів з ненульовими значеннями).")


# ============================================================
# TAB 3: ОПЕРАЦІЇ
# ============================================================
with tab3:
    st.subheader("🧩 Аналіз операцій")

    st.subheader("✅ Коефіцієнт погоджень по операціях")
    st.caption("Рахується для вибраного періоду незалежно від режиму показу в сайдбарі.")

    if approval_by_op.empty:
        st.info("Немає TRUE/FALSE даних по операціях для вибраного періоду.")
    else:
        # Коефіцієнт погоджень по "Тоталу" за той самий період — для лінії-орієнтира
        tf_total = df[period_mask & (df["operation"] == "Тотал")][
            ["month", "sum_true", "sum_false"]
        ].drop_duplicates()
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
        fig_approval.update_layout(
            height=360,
            margin=dict(l=10, r=10, t=20, b=10),
            yaxis=dict(range=[0, 100]),
            bargap=0.45,
            legend_title_text="Категорія",
        )
        if total_rate is not None:
            fig_approval.add_hline(
                y=total_rate,
                line_dash="dash",
                line_color=KPO_RED,
                annotation_text=f"Тотал: {total_rate:.1f}%",
                annotation_position="top left",
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

    st.subheader("🌡️ Теплова карта коефіцієнта погоджень (Операція × Місяць)")
    # FIX: Додаємо "Тотал" до теплової карти
    tf_heat = df[period_mask][["month", "operation", "sum_true", "sum_false"]].drop_duplicates()

    if tf_heat.empty:
        st.info("Немає TRUE/FALSE даних для побудови теплової карти у вибраному періоді.")
    else:
        tf_heat = tf_heat.copy()
        tf_heat["total"] = tf_heat["sum_true"] + tf_heat["sum_false"]
        tf_heat = tf_heat[tf_heat["total"] > 0]
        if tf_heat.empty:
            st.info("Немає TRUE/FALSE даних для побудови теплової карти у вибраному періоді.")
        else:
            tf_heat["rate"] = tf_heat["sum_true"] / tf_heat["total"] * 100
            tf_heat["month_label"] = tf_heat["month"].apply(lambda x: pd.Period(x).strftime("%m.%Y"))
            heat_rate_pivot = tf_heat.pivot_table(index="month_label", columns="operation", values="rate", aggfunc="mean")

            # FIX: Переставляємо колонки, щоб "Тотал" була першою зліва
            cols = heat_rate_pivot.columns.tolist()
            if "Тотал" in cols:
                cols.remove("Тотал")
                cols = ["Тотал"] + cols
                heat_rate_pivot = heat_rate_pivot[cols]

            def _sort_month_label(month_str):
                try:
                    return datetime.strptime(month_str, "%m.%Y")
                except Exception:
                    return datetime(1900, 1, 1)

            sorted_month_labels = sorted(heat_rate_pivot.index, key=_sort_month_label)
            heat_rate_pivot = heat_rate_pivot.reindex(sorted_month_labels)

            fig_approval_heat = px.imshow(
                heat_rate_pivot,
                text_auto=".1f",
                aspect="auto",
                labels=dict(x="Операція", y="Місяць", color="Коефіцієнт погоджень, %"),
                color_continuous_scale="RdYlGn",
                zmin=0,
                zmax=100,
            )
            # FIX: Динамічна висота та авто-зменшення шрифту
            row_height = 38
            min_height = 420
            max_height = 2400
            heatmap_height = max(min_height, min(max_height, len(heat_rate_pivot.index) * row_height))
            fig_approval_heat.update_layout(
                height=heatmap_height,
                margin=dict(l=10, r=10, t=20, b=10),
            )
            font_size = 12 if len(heat_rate_pivot.index) <= 12 else (10 if len(heat_rate_pivot.index) <= 24 else 8)
            fig_approval_heat.update_traces(textfont=dict(size=font_size))

            st.plotly_chart(fig_approval_heat, use_container_width=True)
            st.caption("🔴 <70% 🟡 70-85% 🟢 >85% — кольорова шкала неперервна, орієнтир — ті самі пороги.")

    st.divider()

    ops_data = with_data(filtered[filtered["operation"] != "Тотал"])
    if ops_data.empty:
        st.info("Немає даних про окремі операції для вибраного періоду.")
    else:
        st.subheader("📊 Структура операцій (за період)")
        ops_structure = ops_data.groupby("operation")["value"].sum().reset_index()
        ops_structure = ops_structure.sort_values("value", ascending=False)
        total_ops = ops_structure["value"].sum()
        ops_structure["percent"] = (ops_structure["value"] / total_ops * 100).round(1)
        ops_structure["text"] = ops_structure["percent"].astype(str) + "%"

        fig_ops_structure = px.bar(
            ops_structure,
            x="value",
            y="operation",
            text="text",
            orientation="h",
            labels={"value": "Кількість", "operation": "Операція"},
            title="Структура за період"
        )
        fig_ops_structure.update_traces(textposition="outside")
        fig_ops_structure.update_layout(
            height=360,
            margin=dict(l=10, r=10, t=20, b=10),
            yaxis={"categoryorder": "total descending"}
        )
        st.plotly_chart(fig_ops_structure, use_container_width=True)

        st.subheader("📈 Динаміка структури операцій по місяцях")
        ops_monthly = ops_data.groupby(["month", "operation"])["value"].sum().reset_index()
        ops_monthly["month_label"] = ops_monthly["month"].apply(lambda x: pd.Period(x).strftime("%m.%Y"))
        fig_stacked = px.bar(
            ops_monthly,
            x="month_label",
            y="value",
            color="operation",
            barmode="stack",
            labels={"month_label": "Місяць", "value": "Кількість", "operation": "Операція"},
            title="Структура операцій по місяцях"
        )
        fig_stacked.update_layout(height=400, margin=dict(l=10, r=10, t=20, b=10))
        st.plotly_chart(fig_stacked, use_container_width=True)

        st.subheader("📊 Pareto аналіз операцій")
        pareto_data = ops_structure.copy().sort_values("value", ascending=False)
        pareto_data["cumulative_percent"] = pareto_data["percent"].cumsum()

        fig_pareto = go.Figure()
        fig_pareto.add_trace(go.Bar(
            x=pareto_data["operation"],
            y=pareto_data["value"],
            name="Кількість",
            marker_color=KPO_CYAN,
            yaxis="y",
        ))
        fig_pareto.add_trace(go.Scatter(
            x=pareto_data["operation"],
            y=pareto_data["cumulative_percent"],
            name="Накопичувальна частка, %",
            mode="lines+markers",
            marker_color=KPO_AMBER,
            yaxis="y2",
        ))
        fig_pareto.add_hline(y=80, line_dash="dash", line_color="gray", annotation_text="80%", annotation_position="top right")

        fig_pareto.update_layout(
            title="Pareto операцій",
            xaxis_title="Операція",
            yaxis=dict(title="Кількість", side="left", showgrid=True),
            yaxis2=dict(title="Накопичувальна частка, %", overlaying="y", side="right", range=[0, 100]),
            legend=dict(x=0.8, y=0.9),
            height=400,
            margin=dict(l=10, r=10, t=20, b=10),
        )
        st.plotly_chart(fig_pareto, use_container_width=True)

        if operation_mode != "Тотал" and len(selected_operations) > 1:
            st.subheader("📈 Порівняння вибраних операцій")
            fig_compare_ops = px.line(
                filtered,
                x="date",
                y="value",
                color="operation",
                markers=True,
                labels={"date": "Дата", "value": "Кількість", "operation": "Операція"},
                title="Динаміка вибраних операцій"
            )
            fig_compare_ops.update_xaxes(tickformat="%d.%m", title_text="Дата")
            fig_compare_ops.update_layout(height=400, margin=dict(l=10, r=10, t=20, b=10))
            st.plotly_chart(fig_compare_ops, use_container_width=True)


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

    fig_weekday_avg = px.bar(
        weekday_avg,
        x="weekday",
        y="avg_value",
        text=weekday_avg["avg_value"].round(1).astype(str),
        labels={"weekday": "День тижня", "avg_value": "Середня кількість"},
        title="Середня кількість операцій по днях тижня (лише дні з внесеними даними)"
    )
    fig_weekday_avg.update_traces(textposition="outside")
    fig_weekday_avg.update_layout(height=360, margin=dict(l=10, r=10, t=20, b=10))
    st.plotly_chart(fig_weekday_avg, use_container_width=True)

    st.subheader("📅 Будні vs вихідні")
    week_data = (
        filtered.assign(
            period_type=filtered["is_weekend"].map(
                {False: "Будні", True: "Вихідні"}
            )
        )
        .groupby(["month", "period_type"], as_index=False)["value"]
        .sum()
    )
    week_data["month_total"] = week_data.groupby("month")["value"].transform("sum")
    week_data["percent"] = (week_data["value"] / week_data["month_total"] * 100).round(1)
    week_data["text"] = week_data["percent"].astype(str) + "%"
    week_data["month_label"] = week_data["month"].apply(lambda x: pd.Period(x).strftime("%m.%Y"))

    fig_week = px.bar(
        week_data,
        x="month_label",
        y="value",
        color="period_type",
        barmode="group",
        text="text",
        labels={"month_label": "Місяць", "value": "Кількість", "period_type": ""},
    )
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
        except Exception:
            return datetime(1900, 1, 1)

    sorted_months = sorted(heat_pivot.index, key=sort_months)
    heat_pivot = heat_pivot.reindex(sorted_months)

    # FIX: Динамічна висота, авто-зменшення шрифту та контейнер з прокруткою
    row_height = 38
    min_height = 420
    max_height = 2400
    heatmap_height = max(min_height, min(max_height, len(heat_pivot.index) * row_height))

    fig_heatmap = px.imshow(
        heat_pivot,
        text_auto=".1f",
        aspect="auto",
        labels=dict(x="День тижня", y="Місяць", color="Середня кількість"),
        color_continuous_scale=KPO_HEAT_SCALE,
    )
    fig_heatmap.update_layout(
        height=heatmap_height,
        margin=dict(l=10, r=10, t=20, b=10),
    )
    font_size = 12 if len(heat_pivot.index) <= 12 else (10 if len(heat_pivot.index) <= 24 else 8)
    fig_heatmap.update_traces(textfont=dict(size=font_size))

    st.markdown('<div style="overflow-x: auto; max-height: 90vh; position: relative;">', unsafe_allow_html=True)
    st.plotly_chart(fig_heatmap, use_container_width=True)
    st.markdown('</div>', unsafe_allow_html=True)

    st.subheader("📊 Стабільність навантаження")
    col1, col2, col3 = st.columns(3)
    col1.markdown(custom_metric("Середнє за день", f"{daily_avg:.0f}" if daily_avg > 0 else "—", "Розраховано за тим самим принципом, що й в Overview"), unsafe_allow_html=True)
    col2.markdown(custom_metric("Стандартне відхилення", f"{std:.1f}" if std > 0 else "—", "Стандартне відхилення денних сум (лише дні з внесеними даними)"), unsafe_allow_html=True)
    col3.markdown(custom_metric("Коефіцієнт варіації (CV)", f"{cv:.1f}%" if cv > 0 else "—", "CV = (стандартне відхилення / середнє) × 100%"), unsafe_allow_html=True)

    st.subheader("📊 Розподіл відхилень від середнього (крива щільності)")

    daily_totals = filtered_stats.groupby("date")["value"].sum().reset_index()
    daily_totals["is_weekend"] = daily_totals["date"].dt.dayofweek >= 5

    median_all = None
    median_wd = None
    median_we = None
    mean_all = None
    mean_weekday = None
    mean_weekend = None

    if daily_totals.empty or len(daily_totals) < 3:
        st.info("Недостатньо даних для побудови графіка розподілу.")
    else:
        mean_all = daily_totals["value"].mean()
        daily_totals["dev_all"] = (daily_totals["value"] - mean_all) / mean_all * 100

        weekday_mask = daily_totals["is_weekend"] == False
        weekend_mask = daily_totals["is_weekend"] == True

        mean_weekday = daily_totals.loc[weekday_mask, "value"].mean() if weekday_mask.any() else None
        mean_weekend = daily_totals.loc[weekend_mask, "value"].mean() if weekend_mask.any() else None

        daily_totals["dev_weekday"] = None
        daily_totals.loc[weekday_mask, "dev_weekday"] = (
            (daily_totals.loc[weekday_mask, "value"] - mean_weekday) / mean_weekday * 100
        ) if mean_weekday is not None and mean_weekday != 0 else None

        daily_totals["dev_weekend"] = None
        daily_totals.loc[weekend_mask, "dev_weekend"] = (
            (daily_totals.loc[weekend_mask, "value"] - mean_weekend) / mean_weekend * 100
        ) if mean_weekend is not None and mean_weekend != 0 else None

        dev_data = []
        group_names = []
        dev_all = daily_totals["dev_all"].dropna().values
        if len(dev_all) > 1:
            dev_data.append(dev_all)
            group_names.append("Всі дні")
        dev_wd = daily_totals.loc[weekday_mask, "dev_weekday"].dropna().values if weekday_mask.any() else np.array([])
        if len(dev_wd) > 1:
            dev_data.append(dev_wd)
            group_names.append("Будні")
        dev_we = daily_totals.loc[weekend_mask, "dev_weekend"].dropna().values if weekend_mask.any() else np.array([])
        if len(dev_we) > 1:
            dev_data.append(dev_we)
            group_names.append("Вихідні")

        if not dev_data:
            st.info("Недостатньо даних для побудови кривих щільності.")
        else:
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
                    fig_density.add_trace(go.Scatter(
                        x=x_grid,
                        y=density,
                        mode='lines',
                        name=group_name,
                        line=dict(color=colors.get(group_name, "gray"), width=2.5),
                        fill='none',
                    ))

            if max_density > 0:
                fig_density.add_trace(go.Scatter(
                    x=[0, 0],
                    y=[0, max_density * 1.1],
                    mode='lines',
                    name='Середнє (0%)',
                    line=dict(color=KPO_RED, width=2, dash='dash'),
                    showlegend=True
                ))
                median_all = np.median(dev_all) if len(dev_all) > 0 else None
                if median_all is not None:
                    fig_density.add_trace(go.Scatter(
                        x=[median_all, median_all],
                        y=[0, max_density * 1.1],
                        mode='lines',
                        name=f'Медіана ({median_all:.1f}%)',
                        line=dict(color=KPO_TEXT, width=2, dash='dash'),
                        showlegend=True
                    ))
                median_wd = np.median(dev_wd) if len(dev_wd) > 0 else None
                median_we = np.median(dev_we) if len(dev_we) > 0 else None

            fig_density.update_layout(
                title="Криві щільності відхилень від середнього",
                xaxis_title="Відхилення, %",
                yaxis_title="Щільність",
                height=400,
                margin=dict(l=10, r=10, t=40, b=10),
                legend=dict(
                    title="Група / лінії",
                    x=0.98,
                    y=0.98,
                    xanchor='right',
                    yanchor='top',
                    bgcolor='rgba(0,0,0,0)',
                ),
                hovermode="x unified"
            )

            st.plotly_chart(fig_density, use_container_width=True)

            with st.expander("❓ Що означає форма кривих?"):
                st.markdown("""
                **Крива щільності** показує, як часто зустрічаються дні з різним відхиленням від середнього.

                - **Пік кривої** – найчастіше значення відхилення (мода).
                - **Медіана** (чорна пунктирна лінія) – значення, відносно якого половина днів мають відхилення менше, половина – більше. Це **типовий день**.
                - **Середнє (0%)** – зміщене в бік великих значень через асиметрію. Воно вище медіани, якщо є довгий правий хвіст.
                - **Ширина кривої** – розкид даних.
                - **Асиметрія** – довгий правий хвіст означає, що є дні з набагато вищим навантаженням.
                """)

            with st.expander("❓ Як це інтерпретувати для бізнесу?"):
                st.markdown("""
                - **Медіана** – показує типовий робочий день. Плануйте ресурси на цей рівень.
                - **Різниця між середнім і медіаною** – чим вона більша, тим сильніше впливають пікові дні на загальну статистику.
                - **Правий хвіст** – це дні з аномально високим навантаженням. Варто мати резерв потужності або процедури для таких випадків.
                - **Порівняння груп** – якщо медіани буднів і вихідних різняться, це вказує на різні сценарії навантаження, що потрібно враховувати в плануванні.
                """)

    median_all_str = f"{median_all:.1f}" if median_all is not None else "—"
    median_wd_str = f"{median_wd:.1f}" if median_wd is not None else "—"
    median_we_str = f"{median_we:.1f}" if median_we is not None else "—"

    stats = []
    if mean_all is not None:
        stats.append(f"**Всі дні:** середнє = {mean_all:.1f}, медіана = {median_all_str}, CV = {cv:.1f}%")
    if mean_weekday is not None:
        stats.append(f"**Будні:** середнє = {mean_weekday:.1f}, медіана = {median_wd_str}")
    if mean_weekend is not None:
        stats.append(f"**Вихідні:** середнє = {mean_weekend:.1f}, медіана = {median_we_str}")

    if stats:
        st.markdown(" ".join(stats), unsafe_allow_html=True)

    st.subheader("📈 Співвідношення пік / середнє")
    st.markdown(custom_metric("Пік / середнє", f"{peak_avg_ratio:.2f}×" if peak_avg_ratio > 0 else "—", "У скільки разів максимальне денне значення перевищує середнє"), unsafe_allow_html=True)


# ============================================================
# TAB 5: ПОРІВНЯННЯ ПЕРІОДІВ
# ============================================================
with tab5:
    st.subheader("🆚 Порівняння двох довільних періодів")
    st.caption("Незалежно від фільтрів у сайдбарі — оберіть два будь-які діапазони дат для порівняння.")

    cmp_op_mode = st.radio(
        "Операції для порівняння",
        options=["Тотал", "Вибрані операції"],
        index=0,
        horizontal=True,
        key="cmp_op_mode",
    )
    if cmp_op_mode == "Тотал":
        cmp_ops = ["Тотал"]
    else:
        cmp_ops = st.multiselect(
            "Операції",
            options=all_ops,
            default=all_ops,
            key="cmp_ops_select",
        )
        if not cmp_ops:
            st.warning("Виберіть хоча б одну операцію.")
            cmp_ops = ["Тотал"]
            cmp_op_mode = "Тотал"

    col_a, col_b = st.columns(2)
    default_end_b = max_date.date()
    default_start_b = max(min_date, max_date - pd.Timedelta(days=13)).date()
    default_end_a = (max(min_date, max_date - pd.Timedelta(days=14))).date()
    default_start_a = max(min_date, max_date - pd.Timedelta(days=27)).date()

    with col_a:
        st.markdown("**Період A**")
        range_a_input = st.date_input(
            "Діапазон A",
            value=(default_start_a, default_end_a),
            min_value=min_date.date(),
            max_value=max_date.date(),
            key="cmp_range_a",
        )
    with col_b:
        st.markdown("**Період B**")
        range_b_input = st.date_input(
            "Діапазон B",
            value=(default_start_b, default_end_b),
            min_value=min_date.date(),
            max_value=max_date.date(),
            key="cmp_range_b",
        )

    def _normalize_range(range_input):
        if isinstance(range_input, tuple) and len(range_input) == 2:
            start, end = pd.Timestamp(range_input[0]), pd.Timestamp(range_input[1])
        else:
            single = range_input[0] if isinstance(range_input, tuple) else range_input
            start = end = pd.Timestamp(single)
        if start > end:
            start, end = end, start
        return start, end

    range_a_valid = isinstance(range_a_input, tuple) and len(range_a_input) == 2
    range_b_valid = isinstance(range_b_input, tuple) and len(range_b_input) == 2

    if not range_a_valid or not range_b_valid:
        st.info("Оберіть повний діапазон (початкову і кінцеву дату) для обох періодів.")
    else:
        range_a = _normalize_range(range_a_input)
        range_b = _normalize_range(range_b_input)

        def build_period_metrics(date_range, ops):
            start, end = date_range
            mask = (df["date"] >= start) & (df["date"] <= end) & (df["operation"].isin(ops))
            scoped = df[mask]
            scoped_stats = with_data(scoped)
            if scoped_stats.empty:
                return None
            daily = scoped_stats.groupby("date")["value"].sum()
            total = daily.sum()
            avg = daily.mean()
            peak = daily.max()
            tf = scoped[["month", "operation", "sum_true", "sum_false"]].drop_duplicates()
            s_true = tf["sum_true"].sum()
            s_false = tf["sum_false"].sum()
            rate = (s_true / (s_true + s_false) * 100) if (s_true + s_false) > 0 else None
            return {
                "total": total,
                "avg": avg,
                "peak": peak,
                "rate": rate,
                "days": (end - start).days + 1,
            }

        metrics_a = build_period_metrics(range_a, cmp_ops)
        metrics_b = build_period_metrics(range_b, cmp_ops)

        if metrics_a is None or metrics_b is None:
            st.warning("Немає даних (з внесеними значеннями) для одного з обраних періодів.")
        else:
            def _fmt_delta_pct(a_val, b_val):
                if a_val is None or b_val is None or a_val == 0:
                    return "—"
                return f"{(b_val - a_val) / a_val * 100:+.1f}%"

            rows = [
                {
                    "Метрика": "Період (днів)",
                    "A": metrics_a["days"],
                    "B": metrics_b["days"],
                    "Δ": metrics_b["days"] - metrics_a["days"],
                    "Δ %": "—",
                },
                {
                    "Метрика": "Всього операцій",
                    "A": f"{metrics_a['total']:,.0f}",
                    "B": f"{metrics_b['total']:,.0f}",
                    "Δ": f"{metrics_b['total'] - metrics_a['total']:+,.0f}",
                    "Δ %": _fmt_delta_pct(metrics_a["total"], metrics_b["total"]),
                },
                {
                    "Метрика": "Середнє за день",
                    "A": f"{metrics_a['avg']:.1f}",
                    "B": f"{metrics_b['avg']:.1f}",
                    "Δ": f"{metrics_b['avg'] - metrics_a['avg']:+.1f}",
                    "Δ %": _fmt_delta_pct(metrics_a["avg"], metrics_b["avg"]),
                },
                {
                    "Метрика": "Пік за день",
                    "A": f"{metrics_a['peak']:,.0f}",
                    "B": f"{metrics_b['peak']:,.0f}",
                    "Δ": f"{metrics_b['peak'] - metrics_a['peak']:+,.0f}",
                    "Δ %": _fmt_delta_pct(metrics_a["peak"], metrics_b["peak"]),
                },
                {
                    "Метрика": "Коефіцієнт погоджень, %",
                    "A": f"{metrics_a['rate']:.1f}%" if metrics_a["rate"] is not None else "—",
                    "B": f"{metrics_b['rate']:.1f}%" if metrics_b["rate"] is not None else "—",
                    "Δ": (
                        f"{metrics_b['rate'] - metrics_a['rate']:+.1f} п.п."
                        if metrics_a["rate"] is not None and metrics_b["rate"] is not None
                        else "—"
                    ),
                    "Δ %": "—",
                },
            ]

            st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

            chart_metrics = pd.DataFrame({
                "Метрика": ["Всього", "Середнє за день", "Пік"],
                "A": [metrics_a["total"], metrics_a["avg"], metrics_a["peak"]],
                "B": [metrics_b["total"], metrics_b["avg"], metrics_b["peak"]],
            }).melt(id_vars="Метрика", var_name="Період", value_name="Значення")

            fig_cmp = px.bar(
                chart_metrics,
                x="Метрика",
                y="Значення",
                color="Період",
                barmode="group",
                labels={"Значення": "Кількість"},
                title=f"A: {range_a[0].strftime('%d.%m.%Y')}–{range_a[1].strftime('%d.%m.%Y')}  vs  "
                      f"B: {range_b[0].strftime('%d.%m.%Y')}–{range_b[1].strftime('%d.%m.%Y')}",
            )
            fig_cmp.update_layout(height=380, margin=dict(l=10, r=10, t=40, b=10))
            st.plotly_chart(fig_cmp, use_container_width=True)

st.caption("Джерело: Google Sheets • Оновлення даних: до 5 хвилин після зміни таблиці • Час: Europe/Kyiv.")
