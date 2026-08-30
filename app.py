import html
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Iterable

import gspread
import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import plotly.io as pio
import streamlit as st
from google.oauth2.service_account import Credentials


# ============================================================
# 1. Конфігурація
# ============================================================

st.set_page_config(
    page_title="KPO Dashboard",
    page_icon="📊",
    layout="wide",
)

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
KPO_HEAT_SCALE = [
    [0.0, "#0b0f17"],
    [0.5, "#0d5c73"],
    [1.0, KPO_CYAN],
]

_kpo_dark_template = go.layout.Template(
    layout=go.Layout(
        paper_bgcolor=KPO_BG,
        plot_bgcolor=KPO_BG,
        font=dict(color=KPO_TEXT, family="Inter, sans-serif"),
        colorway=KPO_COLORWAY,
        xaxis=dict(
            gridcolor=KPO_BORDER,
            zerolinecolor=KPO_BORDER,
            linecolor=KPO_BORDER,
        ),
        yaxis=dict(
            gridcolor=KPO_BORDER,
            zerolinecolor=KPO_BORDER,
            linecolor=KPO_BORDER,
        ),
        legend=dict(bgcolor="rgba(0,0,0,0)"),
        hoverlabel=dict(
            bgcolor=KPO_CARD_BG,
            font_color=KPO_TEXT,
            bordercolor=KPO_BORDER,
        ),
        colorscale=dict(sequential=KPO_HEAT_SCALE),
    )
)
pio.templates["kpo_dark"] = _kpo_dark_template
pio.templates.default = "kpo_dark"

START_YEAR = 2024
KYIV_TZ = "Europe/Kyiv"

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

MONTHS_RE = (
    r"Січень|Лютий|Березень|Квітень|Травень|Червень|"
    r"Липень|Серпень|Вересень|Жовтень|Листопад|Грудень"
)

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

TOTAL_ROW_SEARCH_RANGE = 12
DETAIL_SEARCH_RANGE = 40
FIRST_DAY_COLUMN = 4
MIN_SEASONALITY_OBSERVED_DAYS = 7
ANOMALY_WINDOW = 14
ANOMALY_THRESHOLD = 3.0

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger(__name__)


class Operation(StrEnum):
    BONUS = "Бонуси"
    SUSPEND = "Призупинка"
    RESUME = "Відновлення"
    CANCEL_SF = "Відміна SF"
    REREGISTRATION = "Переоформлення"
    CLOSE_CONTRACT = "Закриття контракта"
    CO_ACCESS = "Со-доступ"
    CHANGE_ACTIVATION = "Зміна дати активації"


OPERATIONS = [op.value for op in Operation]

# Реальні aliases можна додавати сюди, не змінюючи решту pipeline.
ALIASES: dict[str, str] = {}


@dataclass(frozen=True)
class PeriodRange:
    start: pd.Timestamp
    end: pd.Timestamp

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1


# ============================================================
# 2. Поточний час та службові функції
# ============================================================

def now_kyiv() -> pd.Timestamp:
    """Поточна дата в Europe/Kyiv як naive midnight Timestamp."""
    return (
        pd.Timestamp.now(tz=KYIV_TZ)
        .tz_localize(None)
        .normalize()
    )


def now_kyiv_exact() -> pd.Timestamp:
    """Поточний локальний час Києва як naive Timestamp."""
    return pd.Timestamp.now(tz=KYIV_TZ).tz_localize(None)


def sheet_names() -> list[str]:
    current_year = now_kyiv().year
    return [str(year)[-2:] for year in range(START_YEAR, current_year + 1)]


def normalize_operation(value) -> str:
    if not isinstance(value, str):
        return ""
    normalized = " ".join(value.strip().split())
    return ALIASES.get(normalized, normalized)


def is_empty_cell(value) -> bool:
    if value is None:
        return True
    if isinstance(value, bool):
        return False
    return str(value).strip() == ""


def as_number(value, warnings: list[str] | None = None, context: str = "") -> float:
    """
    Strict-ish parser:
    - підтримує 123, 123.4, 1 234, 1 234,5, -123
    - не вирізає число з '123abc'
    - неоднозначні тисячні/десяткові формати обробляє консервативно
    """
    if is_empty_cell(value):
        return 0.0

    if isinstance(value, bool):
        parsed = float(value)
        return parsed

    if isinstance(value, (int, float, np.integer, np.floating)):
        if pd.isna(value):
            return 0.0
        return float(value)

    raw = str(value).strip().replace("\xa0", " ")
    compact = raw.replace(" ", "")

    if re.fullmatch(r"-?\d+", compact):
        return float(compact)

    if re.fullmatch(r"-?\d+[.,]\d+", compact):
        separators = compact.count(",") + compact.count(".")
        if separators == 1:
            return float(compact.replace(",", "."))

    # Варіант 1 234,56 / 1 234.56
    if re.fullmatch(r"-?\d{1,3}(?:\s\d{3})+[.,]\d+", raw):
        return float(raw.replace(" ", "").replace(",", "."))

    # Варіант 1,234 або 1.234:
    # якщо одна цифра групи праворуч — трактуємо як decimal.
    # Якщо три — трактуємо як thousand separator.
    thousand_match = re.fullmatch(r"-?\d{1,3}(?:[.,]\d{3})+", compact)
    if thousand_match:
        # Однаковий separator повторюється -> thousands.
        sep = "," if "," in compact and "." not in compact else "."
        if compact.count(sep) >= 1 and ("," not in compact or "." not in compact):
            return float(compact.replace(sep, ""))

    msg = f"Некоректне числове значення: {raw!r}"
    if context:
        msg += f" ({context})"
    logger.warning(msg)
    if warnings is not None and len(warnings) < 100:
        warnings.append(msg)
    return 0.0


def parse_month_header(value, sheet_year: int):
    if not isinstance(value, str):
        return None

    match = re.fullmatch(
        rf"\s*({MONTHS_RE})\s+\d{{2}}\s*",
        value,
    )
    if not match:
        return None

    return MONTHS[match.group(1)], sheet_year


def normalize_date_range(range_input) -> PeriodRange:
    if isinstance(range_input, tuple) and len(range_input) == 2:
        start = pd.Timestamp(range_input[0]).normalize()
        end = pd.Timestamp(range_input[1]).normalize()
    else:
        single = (
            range_input[0]
            if isinstance(range_input, tuple) and range_input
            else range_input
        )
        start = end = pd.Timestamp(single).normalize()

    if start > end:
        start, end = end, start

    return PeriodRange(start, end)


# ============================================================
# 3. Google Sheets + parsing
# ============================================================

@st.cache_resource
def get_client():
    scopes = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
    credentials = Credentials.from_service_account_info(
        dict(st.secrets["gcp_service_account"]),
        scopes=scopes,
    )
    return gspread.authorize(credentials)


def find_total_row(values: list[list[str]], header_row: int) -> int | None:
    start = header_row + 1
    end = min(header_row + 1 + TOTAL_ROW_SEARCH_RANGE, len(values))

    for row_idx in range(start, end):
        first_cell = values[row_idx][0] if values[row_idx] else ""
        if normalize_operation(first_cell) == "Тотал":
            return row_idx

    return None


def find_detail_start(values: list[list[str]], header_row: int) -> int | None:
    start = header_row + 1
    end = min(header_row + 1 + DETAIL_SEARCH_RANGE, len(values))

    for row_idx in range(start, end):
        op = (
            normalize_operation(values[row_idx][3])
            if len(values[row_idx]) > 3
            else ""
        )
        if op in OPERATIONS:
            return row_idx

    return None


def extract_true_false_rows(
    values: list[list[str]],
    total_row_idx: int,
    month_key: str,
    sheet_name: str,
    month_label: str,
    warnings: list[str],
) -> list[dict]:
    result = []

    sum_true = as_number(
        values[total_row_idx][1] if len(values[total_row_idx]) > 1 else "",
        warnings,
        f"{sheet_name} {month_label} Тотал TRUE",
    )
    sum_false = as_number(
        values[total_row_idx][2] if len(values[total_row_idx]) > 2 else "",
        warnings,
        f"{sheet_name} {month_label} Тотал FALSE",
    )

    result.append(
        {
            "month": month_key,
            "operation": "Тотал",
            "sum_true": sum_true,
            "sum_false": sum_false,
        }
    )

    found_ops: set[str] = set()
    search_end = min(
        total_row_idx + 1 + len(OPERATIONS) + 3,
        len(values),
    )

    for row_idx in range(total_row_idx + 1, search_end):
        row = values[row_idx]
        op_name = normalize_operation(row[0] if row else "")
        if op_name not in OPERATIONS or op_name in found_ops:
            continue

        op_sum_true = as_number(
            row[1] if len(row) > 1 else "",
            warnings,
            f"{sheet_name} {month_label} {op_name} TRUE",
        )
        op_sum_false = as_number(
            row[2] if len(row) > 2 else "",
            warnings,
            f"{sheet_name} {month_label} {op_name} FALSE",
        )

        result.append(
            {
                "month": month_key,
                "operation": op_name,
                "sum_true": op_sum_true,
                "sum_false": op_sum_false,
            }
        )
        found_ops.add(op_name)

    missing = [op for op in OPERATIONS if op not in found_ops]
    if missing:
        warnings.append(
            f"⚠️ Аркуш «{sheet_name}», {month_label}: "
            f"не знайдено TRUE/FALSE дані для операцій: {', '.join(missing)}."
        )

    return result


def build_daily_records(
    values: list[list[str]],
    detail_start: int,
    year: int,
    month: int,
    sheet_name: str,
    month_label: str,
    warnings: list[str],
) -> list[dict]:
    records = []
    days = pd.Period(f"{year}-{month:02d}").days_in_month

    for row_idx in range(detail_start, len(values)):
        row = values[row_idx]
        operation = normalize_operation(row[3] if len(row) > 3 else "")

        if operation not in OPERATIONS:
            break

        for day_idx in range(days):
            col = FIRST_DAY_COLUMN + day_idx
            raw_value = row[col] if col < len(row) else ""
            has_data = not is_empty_cell(raw_value)
            date = pd.Timestamp(year=year, month=month, day=day_idx + 1)

            value = as_number(
                raw_value,
                warnings,
                f"{sheet_name} {month_label} {operation} {date.strftime('%d.%m.%Y')}",
            )

            records.append(
                {
                    "date": date,
                    "operation": operation,
                    "value": value,
                    "has_data": has_data,
                    "year": year,
                    "month": date.strftime("%Y-%m"),
                    "month_num": date.month,
                    "month_name": date.strftime("%b %Y"),
                    "weekday": date.day_name(),
                    "is_weekend": date.weekday() >= 5,
                }
            )

    return records


@st.cache_data(ttl=300, show_spinner="Завантаження даних з Google Таблиці…")
def load_data(spreadsheet_id: str, sheets: tuple[str, ...]):
    client = get_client()
    spreadsheet = client.open_by_key(spreadsheet_id)

    records: list[dict] = []
    op_true_false: list[dict] = []
    load_warnings: list[str] = []

    for sheet_name in sheets:
        try:
            worksheet = spreadsheet.worksheet(sheet_name)
        except gspread.WorksheetNotFound:
            load_warnings.append(
                f"ℹ️ Аркуш «{sheet_name}» не знайдено — пропущено."
            )
            continue

        values = worksheet.get_all_values()
        if not values:
            load_warnings.append(
                f"ℹ️ Аркуш «{sheet_name}» порожній."
            )
            continue

        sheet_year = 2000 + int(sheet_name)
        first_col = [row[0] if row else "" for row in values]

        month_rows = []
        for row_idx, value in enumerate(first_col):
            parsed = parse_month_header(value, sheet_year)
            if parsed:
                month_rows.append((row_idx, parsed[0], parsed[1]))

        if not month_rows:
            load_warnings.append(
                f"⚠️ Аркуш «{sheet_name}»: не знайдено заголовків місяців."
            )
            continue

        seen_months: set[str] = set()

        for header_row, month, year in month_rows:
            month_key = f"{year}-{month:02d}"
            month_label = f"{month:02d}.{year}"

            if month_key in seen_months:
                load_warnings.append(
                    f"⚠️ Аркуш «{sheet_name}»: дубль місяця {month_label}."
                )
                continue

            seen_months.add(month_key)

            total_row_idx = find_total_row(values, header_row)
            if total_row_idx is None:
                load_warnings.append(
                    f"⚠️ Аркуш «{sheet_name}», {month_label}: "
                    f"не знайдено рядок «Тотал» у межах блоку."
                )
            else:
                op_true_false.extend(
                    extract_true_false_rows(
                        values=values,
                        total_row_idx=total_row_idx,
                        month_key=month_key,
                        sheet_name=sheet_name,
                        month_label=month_label,
                        warnings=load_warnings,
                    )
                )

            detail_start = find_detail_start(values, header_row)
            if detail_start is None:
                load_warnings.append(
                    f"⚠️ Аркуш «{sheet_name}», {month_label}: "
                    f"не знайдено таблицю деталізації."
                )
                continue

            records.extend(
                build_daily_records(
                    values=values,
                    detail_start=detail_start,
                    year=year,
                    month=month,
                    sheet_name=sheet_name,
                    month_label=month_label,
                    warnings=load_warnings,
                )
            )

    df_raw = pd.DataFrame(records)
    if df_raw.empty:
        raise ValueError("Не знайдено деталізованих даних у Google Таблиці.")

    df_raw["date"] = pd.to_datetime(df_raw["date"])

    # Канонічний observed layer.
    grouped = (
        df_raw.groupby(["date", "operation"], as_index=False, observed=True)
        .agg(
            value=("value", "sum"),
            has_data=("has_data", "any"),
        )
        .sort_values(["date", "operation"])
    )

    date_metadata = (
        df_raw[
            [
                "date",
                "year",
                "month",
                "month_num",
                "month_name",
                "weekday",
                "is_weekend",
            ]
        ]
        .drop_duplicates("date")
    )

    df = grouped.merge(date_metadata, on="date", how="left")

    # Тотал агрегує лише фактично наявні операції.
    total = (
        grouped[grouped["has_data"]]
        .groupby("date", as_index=False)["value"]
        .sum()
        .assign(
            operation="Тотал",
            has_data=True,
        )
    )

    total = total.merge(date_metadata, on="date", how="left")
    df = pd.concat([df, total], ignore_index=True)

    df["operation"] = pd.Categorical(
        df["operation"],
        categories=["Тотал"] + OPERATIONS,
        ordered=True,
    )

    tf_df = pd.DataFrame(op_true_false).drop_duplicates(
        subset=["month", "operation"],
        keep="last",
    )

    if not tf_df.empty:
        df = df.merge(
            tf_df,
            on=["month", "operation"],
            how="left",
        )
    else:
        df["sum_true"] = np.nan
        df["sum_false"] = np.nan

    # Для approval NaN важливий: 0 означає нуль TRUE/FALSE,
    # NaN означає "даних про approval немає".
    df["sum_true"] = pd.to_numeric(df["sum_true"], errors="coerce")
    df["sum_false"] = pd.to_numeric(df["sum_false"], errors="coerce")

    # Порядок колонок.
    ordered_columns = [
        "date",
        "operation",
        "value",
        "has_data",
        "year",
        "month",
        "month_num",
        "month_name",
        "weekday",
        "is_weekend",
        "sum_true",
        "sum_false",
    ]
    df = df[ordered_columns].sort_values(["date", "operation"]).reset_index(drop=True)

    # Deduplicate warnings, щоб лог/expander не розростався.
    load_warnings = list(dict.fromkeys(load_warnings))

    return df, load_warnings


# ============================================================
# 4. Data semantics / aggregation
# ============================================================

def observed(df: pd.DataFrame) -> pd.DataFrame:
    if "has_data" not in df.columns:
        return df.iloc[0:0].copy()
    return df[df["has_data"]].copy()


def daily_values(
    df: pd.DataFrame,
    operation_filter: Iterable[str] | None = None,
    observed_only: bool = True,
) -> pd.DataFrame:
    scoped = df

    if operation_filter is not None:
        scoped = scoped[scoped["operation"].isin(list(operation_filter))]

    if observed_only:
        scoped = observed(scoped)

    if scoped.empty:
        return pd.DataFrame(columns=["date", "value"])

    daily = (
        scoped.groupby("date", as_index=False, observed=True)["value"]
        .sum()
        .sort_values("date")
    )
    return daily


def daily_calendar_series(
    df: pd.DataFrame,
    start: pd.Timestamp,
    end: pd.Timestamp,
    operation_filter: Iterable[str],
) -> pd.DataFrame:
    """
    Calendar series:
    missing days stay NaN, not 0.
    This is useful for charts and data-quality visibility.
    """
    dates = pd.date_range(start, end, freq="D")
    daily = daily_values(
        df,
        operation_filter=operation_filter,
        observed_only=True,
    )

    result = pd.DataFrame({"date": dates}).merge(
        daily,
        on="date",
        how="left",
    )
    return result


def get_full_months(
    start: pd.Timestamp,
    end: pd.Timestamp,
    today: pd.Timestamp | None = None,
) -> list[str]:
    """
    Повний календарний місяць:
    1) повністю лежить у вибраному діапазоні;
    2) не є поточним незавершеним місяцем.
    """
    today = today or now_kyiv()
    months: list[str] = []

    current = pd.Period(start, freq="M")
    last = pd.Period(end, freq="M")

    while current <= last:
        month_start = current.start_time.normalize()
        month_end = current.end_time.normalize()

        if (
            month_start >= start
            and month_end <= end
            and month_end < today
        ):
            months.append(str(current))

        current += 1

    return months


def is_current_month(month_key: str, today: pd.Timestamp | None = None) -> bool:
    today = today or now_kyiv()
    return str(pd.Period(month_key, freq="M")) == str(
        pd.Period(today, freq="M")
    )


def observed_days_in_month(
    df: pd.DataFrame,
    month_key: str,
    operation: str,
) -> int:
    scoped = df[
        (df["month"] == month_key)
        & (df["operation"] == operation)
        & (df["has_data"])
    ]
    return scoped["date"].nunique()


def daily_metric_table(
    df: pd.DataFrame,
    operations: list[str],
    start: pd.Timestamp,
    end: pd.Timestamp,
) -> pd.DataFrame:
    daily = daily_calendar_series(
        df,
        start=start,
        end=end,
        operation_filter=operations,
    )

    daily["weekday_en"] = daily["date"].dt.day_name()
    daily["weekday"] = daily["weekday_en"].map(WEEKDAY_UA)
    daily["is_weekend"] = daily["date"].dt.dayofweek >= 5
    return daily


# ============================================================
# 5. Analytics
# ============================================================

def calc_peak_min_avg(
    df: pd.DataFrame,
    operations: list[str],
):
    daily = daily_values(df, operation_filter=operations)

    if daily.empty:
        return 0.0, 0.0, 0.0, 0.0

    peak = float(daily["value"].max())
    min_val = float(daily["value"].min())
    avg = float(daily["value"].mean())
    peak_avg_ratio = peak / avg if avg > 0 else 0.0
    return peak, min_val, avg, peak_avg_ratio


def calc_busiest_weekday(
    df: pd.DataFrame,
    operations: list[str],
):
    daily = daily_values(df, operation_filter=operations)
    if daily.empty:
        return None, None

    daily["weekday"] = daily["date"].dt.day_name()
    weekday_avg = daily.groupby("weekday")["value"].mean()

    if weekday_avg.empty:
        return None, None

    busiest = weekday_avg.idxmax()
    return busiest, float(weekday_avg.max())


def calc_busiest_operation(df: pd.DataFrame):
    ops = observed(df[df["operation"] != "Тотал"])
    if ops.empty:
        return None, None

    total_by_op = (
        ops.groupby("operation", observed=True)["value"]
        .sum()
        .sort_values(ascending=False)
    )
    if total_by_op.empty:
        return None, None

    return str(total_by_op.index[0]), float(total_by_op.iloc[0])


def calc_stability(df: pd.DataFrame, operations: list[str], daily_avg: float):
    daily = daily_values(df, operation_filter=operations)

    if daily.empty:
        return 0.0, 0.0, "Немає даних"

    std = float(daily["value"].std())
    cv = (std / daily_avg * 100) if daily_avg > 0 else 0.0

    if cv < 15:
        interpretation = "🟢 Низька варіативність (<15%)"
    elif cv < 30:
        interpretation = "🟡 Середня варіативність (15–30%)"
    else:
        interpretation = "🔴 Висока варіативність (≥30%)"

    return std, cv, interpretation


def detect_anomalies(
    df: pd.DataFrame,
    operations: list[str],
    window: int = ANOMALY_WINDOW,
    threshold: float = ANOMALY_THRESHOLD,
) -> pd.DataFrame:
    """
    Robust rolling z-score на одному безперервному часовому ряді.
    Важливо: rolling НЕ скидається на межі місяця.
    Використовується center=True для retrospective analytics.
    """
    daily = daily_values(df, operation_filter=operations)

    if len(daily) < 3:
        return pd.DataFrame()

    daily = daily.sort_values("date").set_index("date")
    # Для anomaly detection не вставляємо missing days як 0.
    series = daily["value"].astype(float)

    rolling_median = series.rolling(
        window=window,
        min_periods=max(3, window // 3),
        center=True,
    ).median()

    rolling_mad = series.rolling(
        window=window,
        min_periods=max(3, window // 3),
        center=True,
    ).apply(
        lambda x: np.median(np.abs(x - np.median(x))),
        raw=True,
    )

    scale = rolling_mad * 1.4826

    result = pd.DataFrame(
        {
            "date": series.index,
            "value": series.values,
            "rolling_median": rolling_median.values,
            "rolling_mad": rolling_mad.values,
        }
    )

    result["z_score"] = np.nan

    nonzero_scale = scale.notna() & (scale > 0)
    result.loc[nonzero_scale.values, "z_score"] = (
        (
            series[nonzero_scale]
            - rolling_median[nonzero_scale]
        )
        / scale[nonzero_scale]
    ).values

    # Якщо MAD=0, але значення не дорівнює медіані —
    # вважаємо відхилення потенційною аномалією.
    zero_scale = (
        scale.notna()
        & (scale == 0)
        & rolling_median.notna()
    )
    if zero_scale.any():
        diff = (series - rolling_median).abs()
        result.loc[zero_scale.values, "z_score"] = np.where(
            (diff[zero_scale] > 0).values,
            np.sign((series - rolling_median)[zero_scale].values) * np.inf,
            0.0,
        )

    result["is_anomaly"] = result["z_score"].abs() > threshold
    result = result[result["value"] > 0].copy()

    return result.reset_index(drop=True)


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
    density = kernel.sum(axis=1) / (
        n * bandwidth * np.sqrt(2 * np.pi)
    )
    return density


# ============================================================
# 6. Approval metrics
# ============================================================

def approval_rows_for_period(
    df: pd.DataFrame,
    period_start: pd.Timestamp,
    period_end: pd.Timestamp,
    operations: list[str],
) -> pd.DataFrame:
    full_months = get_full_months(
        period_start,
        period_end,
        today=now_kyiv(),
    )

    if not full_months:
        return pd.DataFrame(
            columns=[
                "month",
                "operation",
                "sum_true",
                "sum_false",
                "total",
                "approval_rate",
            ]
        )

    scoped = df[
        df["month"].isin(full_months)
        & df["operation"].isin(operations)
    ][
        ["month", "operation", "sum_true", "sum_false"]
    ].drop_duplicates()

    scoped = scoped.dropna(subset=["sum_true", "sum_false"])

    if scoped.empty:
        return pd.DataFrame(
            columns=[
                "month",
                "operation",
                "sum_true",
                "sum_false",
                "total",
                "approval_rate",
            ]
        )

    scoped["total"] = scoped["sum_true"] + scoped["sum_false"]
    scoped = scoped[scoped["total"] > 0].copy()
    scoped["approval_rate"] = (
        scoped["sum_true"] / scoped["total"] * 100
    )

    return scoped


def aggregate_approval_by_operation(
    approval_rows: pd.DataFrame,
) -> pd.DataFrame:
    if approval_rows.empty:
        return pd.DataFrame(
            columns=[
                "operation",
                "sum_true",
                "sum_false",
                "total",
                "approval_rate",
            ]
        )

    result = (
        approval_rows.groupby("operation", as_index=False)["sum_true", "sum_false"]
        .sum()
    )
    result["total"] = result["sum_true"] + result["sum_false"]
    result = result[result["total"] > 0].copy()
    result["approval_rate"] = (
        result["sum_true"] / result["total"] * 100
    ).round(1)

    return result.sort_values("approval_rate", ascending=False)


def aggregate_approval_total(
    approval_rows: pd.DataFrame,
) -> tuple[float | None, float, float, list[str]]:
    if approval_rows.empty:
        return None, 0.0, 0.0, []

    sum_true = float(approval_rows["sum_true"].sum())
    sum_false = float(approval_rows["sum_false"].sum())
    total = sum_true + sum_false

    if total <= 0:
        return None, sum_true, sum_false, sorted(
            approval_rows["month"].unique().tolist()
        )

    rate = sum_true / total * 100
    return (
        float(rate),
        sum_true,
        sum_false,
        sorted(approval_rows["month"].unique().tolist()),
    )


# ============================================================
# 7. Forecast
# ============================================================

def forecast_scenarios(
    df: pd.DataFrame,
    current_month: str,
    operation: str,
):
    """
    Forecast лише для поточного календарного місяця.

    Статистичний сценарій:
      - base = MTD fact + average observed daily * remaining calendar days
      - conservative / optimistic = base +/- 0.5 * std * remaining observed-independent days

    Seasonal сценарій:
      - порівнюємо current MTD average/day з MTD аналогічного
        періоду минулого року;
      - вимагаємо мінімум MIN_SEASONALITY_OBSERVED_DAYS
        observed days у кожному році;
      - застосовуємо clamp 0.5..1.5, щоб одна аномальна рання
        точка не множила весь залишок.
    """
    today = now_kyiv()
    current_period = pd.Period(current_month, freq="M")

    if current_period != pd.Period(today, freq="M"):
        return None, None

    scoped = observed(
        df[
            (df["month"] == current_month)
            & (df["operation"] == operation)
        ]
    )

    if scoped.empty:
        return None, None

    first_day = current_period.start_time.normalize()
    last_day = current_period.end_time.normalize()

    calendar_elapsed_days = (today - first_day).days + 1
    total_days = current_period.days_in_month
    remaining_days = total_days - calendar_elapsed_days

    fact_daily = (
        scoped[scoped["date"] <= today]
        .groupby("date")["value"]
        .sum()
        .sort_index()
    )

    if fact_daily.empty:
        return None, None

    fact_sum = float(fact_daily.sum())
    observed_days = int(fact_daily.index.nunique())
    avg_daily = float(fact_daily.mean())
    std_daily = float(fact_daily.std(ddof=1)) if observed_days > 1 else 0.0

    base = fact_sum + avg_daily * remaining_days
    lower_daily = max(0.0, avg_daily - 0.5 * std_daily)
    upper_daily = avg_daily + 0.5 * std_daily

    stat_forecast = {
        "base": float(base),
        "min": float(fact_sum + lower_daily * remaining_days),
        "max": float(fact_sum + upper_daily * remaining_days),
        "avg_daily": avg_daily,
        "std_daily": std_daily,
        "fact": fact_sum,
        "calendar_elapsed_days": calendar_elapsed_days,
        "observed_days": observed_days,
        "total_days": total_days,
        "remaining_days": remaining_days,
        "last_observed_date": fact_daily.index.max(),
    }

    # ---- Seasonal ----
    prev_period = current_period - 12
    prev_month = str(prev_period)

    prev_scoped = observed(
        df[
            (df["month"] == prev_month)
            & (df["operation"] == operation)
        ]
    )

    season_forecast = None

    if not prev_scoped.empty:
        current_mtd_days = fact_daily.index.day
        prev_mtd = prev_scoped[
            prev_scoped["date"].dt.day.isin(current_mtd_days)
        ].copy()

        # Не вимагаємо, щоб конкретний календарний день існував;
        # вимагаємо достатню кількість observed days.
        prev_daily = prev_mtd.groupby("date")["value"].sum()
        prev_days = int(prev_daily.index.nunique())

        if observed_days >= MIN_SEASONALITY_OBSERVED_DAYS and prev_days >= MIN_SEASONALITY_OBSERVED_DAYS:
            prev_mtd_sum = float(prev_daily.sum())
            prev_remaining = prev_scoped[
                ~prev_scoped["date"].dt.day.isin(current_mtd_days)
            ]["value"].sum()

            prev_avg = prev_mtd_sum / prev_days if prev_days else 0.0
            if prev_avg > 0 and prev_remaining >= 0:
                seasonality_factor = avg_daily / prev_avg
                seasonality_factor = max(
                    0.5,
                    min(1.5, seasonality_factor),
                )

                forecast_remaining = float(
                    prev_remaining * seasonality_factor
                )
                season_base = fact_sum + forecast_remaining

                season_forecast = {
                    "base": float(season_base),
                    "min": float(
                        fact_sum + forecast_remaining * 0.90
                    ),
                    "max": float(
                        fact_sum + forecast_remaining * 1.10
                    ),
                    "seasonality_factor": float(seasonality_factor),
                    "fact": fact_sum,
                    "calendar_elapsed_days": calendar_elapsed_days,
                    "observed_days": observed_days,
                    "total_days": total_days,
                    "remaining_days": remaining_days,
                    "prev_month": prev_month,
                    "prev_mtd_sum": prev_mtd_sum,
                    "prev_mtd_observed_days": prev_days,
                    "prev_avg_daily": prev_avg,
                    "prev_remaining_sum": float(prev_remaining),
                    "forecast_remaining": forecast_remaining,
                    "last_observed_date": fact_daily.index.max(),
                    "has_prev_year": True,
                }

    return stat_forecast, season_forecast


# ============================================================
# 8. UI helpers
# ============================================================

def custom_metric(
    label,
    value,
    help_text=None,
    color=None,
):
    safe_label = html.escape(str(label))
    safe_value = html.escape(str(value))

    help_icon = ""
    if help_text:
        safe_help = html.escape(str(help_text))
        help_icon = (
            f'<span class="help-icon" title="{safe_help}">?</span>'
        )

    value_style = (
        f' style="color:{html.escape(color)};"'
        if color
        else ""
    )

    return f"""
    <div class="metric-container">
        <div class="metric-label">{safe_label} {help_icon}</div>
        <div class="metric-value"{value_style}>{safe_value}</div>
    </div>
    """


def approval_rate_color(value):
    if value is None or pd.isna(value):
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
        return (
            f"🟡 Середній (≥{APPROVAL_WARN_THRESHOLD}% "
            f"і <{APPROVAL_GOOD_THRESHOLD}%)"
        )
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


def forecast_cards(title, forecast, help_base=None, help_min=None, help_max=None):
    if forecast is None:
        return

    col1, col2, col3 = st.columns(3)

    with col1:
        st.markdown(
            custom_metric(
                f"{title} (базовий)",
                f"{forecast['base']:,.0f}",
                help_base,
            ),
            unsafe_allow_html=True,
        )

    with col2:
        st.markdown(
            custom_metric(
                f"{title} (консервативний)",
                f"{forecast['min']:,.0f}",
                help_min,
            ),
            unsafe_allow_html=True,
        )

    with col3:
        st.markdown(
            custom_metric(
                f"{title} (оптимістичний)",
                f"{forecast['max']:,.0f}",
                help_max,
            ),
            unsafe_allow_html=True,
        )


def render_quality_summary(
    df: pd.DataFrame,
    period_start: pd.Timestamp,
    period_end: pd.Timestamp,
    operations: list[str],
):
    daily = daily_calendar_series(
        df,
        period_start,
        period_end,
        operations,
    )

    total_calendar_days = len(daily)
    observed_day_count = int(daily["value"].notna().sum())
    missing_day_count = total_calendar_days - observed_day_count
    coverage = (
        observed_day_count / total_calendar_days * 100
        if total_calendar_days
        else 0
    )

    last_observed = (
        daily.loc[daily["value"].notna(), "date"].max()
        if observed_day_count
        else None
    )

    c1, c2, c3 = st.columns(3)

    with c1:
        st.markdown(
            custom_metric(
                "Покриття даними",
                f"{coverage:.1f}%",
                "Частка календарних днів, для яких реально є внесені дані.",
                color=(
                    COLOR_GOOD
                    if coverage >= 95
                    else COLOR_WARN
                    if coverage >= 80
                    else COLOR_BAD
                ),
            ),
            unsafe_allow_html=True,
        )

    with c2:
        st.markdown(
            custom_metric(
                "Дані / календар",
                f"{observed_day_count} / {total_calendar_days}",
                "Кількість днів із внесеними даними проти всіх календарних днів періоду.",
            ),
            unsafe_allow_html=True,
        )

    with c3:
        last_observed_text = (
            last_observed.strftime("%d.%m.%Y")
            if last_observed is not None
            else "—"
        )
        st.markdown(
            custom_metric(
                "Останній день з даними",
                last_observed_text,
                "Остання дата, за яку у вибраному наборі операцій є фактичні дані.",
            ),
            unsafe_allow_html=True,
        )

    if missing_day_count > 0:
        st.caption(
            f"ℹ️ Днів без даних: {missing_day_count}. "
            f"Вони не трактуються як нуль у статистичних метриках."
        )


def apply_filters(
    df: pd.DataFrame,
    period_mode: str,
    selected_months: list[str],
    custom_range: PeriodRange | None,
    selected_operations: list[str],
) -> pd.DataFrame:
    operation_mask = df["operation"].isin(selected_operations)

    if period_mode == "За місяцями":
        filtered = df[
            df["month"].isin(selected_months)
            & operation_mask
        ].copy()

        if len(selected_months) == 1:
            period = pd.Period(selected_months[0], freq="M")
            today = now_kyiv()

            if period == pd.Period(today, freq="M"):
                filtered = filtered[filtered["date"] <= today]

        return filtered

    assert custom_range is not None

    return df[
        (df["date"] >= custom_range.start)
        & (df["date"] <= custom_range.end)
        & operation_mask
    ].copy()


def selected_period_bounds(
    period_mode: str,
    selected_months: list[str],
    custom_range: PeriodRange | None,
) -> PeriodRange:
    if period_mode == "За місяцями":
        starts = [pd.Period(m, freq="M").start_time.normalize() for m in selected_months]
        ends = [pd.Period(m, freq="M").end_time.normalize() for m in selected_months]
        return PeriodRange(min(starts), max(ends))

    assert custom_range is not None
    return custom_range


# ============================================================
# 9. Secrets
# ============================================================

try:
    SPREADSHEET_ID = st.secrets["SPREADSHEET_ID"]
except KeyError:
    st.error(
        "Не знайдено SPREADSHEET_ID у secrets. "
        "Додайте його у .streamlit/secrets.toml"
    )
    st.stop()


# ============================================================
# 10. CSS
# ============================================================

st.markdown(
    f"""
<style>
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@500;600;700&display=swap');

    html, body, [class*="css"] {{
        font-family: 'Inter', sans-serif;
    }}

    .stApp {{
        background-color: {KPO_BG};
    }}

    .metric-container {{
        background: {KPO_CARD_BG};
        border: 1px solid {KPO_BORDER};
        border-left: 3px solid {KPO_CYAN};
        border-radius: 8px;
        padding: 0.65rem 0.9rem;
        margin-bottom: 0.5rem;
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

    section[data-testid="stSidebar"] {{
        background-color: #0e131d;
        border-right: 1px solid {KPO_BORDER};
    }}

    h1, h2, h3 {{
        font-family: 'Inter', sans-serif;
        letter-spacing: -0.01em;
    }}

    hr {{
        border-color: {KPO_BORDER} !important;
    }}
</style>
""",
    unsafe_allow_html=True,
)


# ============================================================
# 11. Завантаження
# ============================================================

st.title("📊 Dashboard погоджень КПО")
st.caption(
    "Дані завантажуються напряму з Google Таблиці. "
    "Кеш оновлюється кожні 5 хвилин. Час — Europe/Kyiv."
)

try:
    df, load_warnings = load_data(
        SPREADSHEET_ID,
        tuple(sheet_names()),
    )
except Exception:
    logger.exception("Помилка завантаження Google Sheets")
    st.error("Не вдалося завантажити Google Таблицю.")
    st.info(
        "Перевір доступ service account, Google Sheets API "
        "та secrets Streamlit."
    )
    st.stop()

if load_warnings:
    with st.expander(
        f"⚠️ Попередження при завантаженні даних ({len(load_warnings)})",
        expanded=False,
    ):
        for warning in load_warnings:
            st.warning(warning)


# ============================================================
# 12. Sidebar
# ============================================================

st.sidebar.header("Фільтри")

period_mode = st.sidebar.radio(
    "Тип періоду",
    options=["За місяцями", "Довільний діапазон дат"],
    index=0,
)

min_date = df["date"].min()
max_date = df["date"].max()
years = sorted(df["year"].dropna().unique().tolist())

current_year = now_kyiv().year
default_years = (
    [current_year]
    if current_year in years
    else [years[-1]]
    if years
    else []
)

custom_range: PeriodRange | None = None

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

    if (
        len(selected_years) == 1
        and selected_years[0] != current_year
    ):
        default_months = available_months
    else:
        current_month_str = now_kyiv().strftime("%Y-%m")
        default_months = (
            [current_month_str]
            if current_month_str in available_months
            else available_months[-1:]
            if available_months
            else []
        )

    selected_months = st.sidebar.multiselect(
        "Місяць",
        options=available_months,
        default=default_months,
        format_func=lambda x: pd.Period(x).strftime("%m.%Y"),
    )
else:
    default_start = max(
        min_date,
        max_date - pd.Timedelta(days=13),
    )

    date_range_input = st.sidebar.date_input(
        "Діапазон дат",
        value=(default_start.date(), max_date.date()),
        min_value=min_date.date(),
        max_value=max_date.date(),
    )

    custom_range = normalize_date_range(date_range_input)
    selected_years = sorted(
        {custom_range.start.year, custom_range.end.year}
    )
    selected_months = sorted(
        {
            d.strftime("%Y-%m")
            for d in pd.date_range(
                custom_range.start,
                custom_range.end,
                freq="D",
            )
        }
    )

operation_mode = st.sidebar.radio(
    "Режим показу",
    options=["Тотал", "Вибрані операції"],
    index=0,
)

all_ops = [
    op for op in OPERATIONS
    if op in df["operation"].astype(str).unique()
]

if operation_mode == "Тотал":
    selected_operations = ["Тотал"]
else:
    selected_operations = st.sidebar.multiselect(
        "Операції",
        options=all_ops,
        default=all_ops,
    )

    if not selected_operations:
        st.sidebar.warning("Виберіть хоча б одну операцію.")
        selected_operations = ["Тотал"]

st.sidebar.divider()
st.sidebar.subheader("Налаштування графіків")

smooth_enabled = st.sidebar.checkbox(
    "Згладжування динаміки (ковзне середнє)",
    value=False,
)

smooth_window = (
    st.sidebar.selectbox(
        "Вікно згладжування (дні)",
        [3, 5, 7, 14],
        index=2,
    )
    if smooth_enabled
    else 7
)


# ============================================================
# 13. Фільтрація
# ============================================================

if not selected_months and period_mode == "За місяцями":
    st.warning("Оберіть хоча б один рік і місяць.")
    st.stop()

filtered = apply_filters(
    df=df,
    period_mode=period_mode,
    selected_months=selected_months,
    custom_range=custom_range,
    selected_operations=selected_operations,
)

if filtered.empty:
    st.warning("За вибраними фільтрами даних немає.")
    st.stop()

period_bounds = selected_period_bounds(
    period_mode,
    selected_months,
    custom_range,
)

filtered_stats = observed(filtered)

if filtered_stats.empty:
    st.info(
        "Дані ще не внесені для жодного дня "
        "у вибраному періоді — статистика недоступна."
    )


# ============================================================
# 14. Загальні метрики
# ============================================================

daily_total = daily_values(
    filtered,
    operation_filter=selected_operations,
)

total_value = float(daily_total["value"].sum()) if not daily_total.empty else 0.0
daily_avg = float(daily_total["value"].mean()) if not daily_total.empty else 0.0

weekday_daily = daily_total.copy()
if not weekday_daily.empty:
    weekday_daily["is_weekend"] = weekday_daily["date"].dt.dayofweek >= 5

daily_avg_weekday = (
    float(
        weekday_daily.loc[
            ~weekday_daily["is_weekend"],
            "value",
        ].mean()
    )
    if not weekday_daily.empty
    and (~weekday_daily["is_weekend"]).any()
    else None
)

daily_avg_weekend = (
    float(
        weekday_daily.loc[
            weekday_daily["is_weekend"],
            "value",
        ].mean()
    )
    if not weekday_daily.empty
    and weekday_daily["is_weekend"].any()
    else None
)

peak = float(daily_total["value"].max()) if not daily_total.empty else 0.0
peak_date = daily_total.loc[
    daily_total["value"].idxmax(),
    "date",
] if not daily_total.empty else None

peak_avg_ratio = peak / daily_avg if daily_avg > 0 else 0.0

busiest_weekday, busiest_weekday_val = calc_busiest_weekday(
    filtered,
    selected_operations,
)

busiest_op, busiest_op_val = (
    calc_busiest_operation(filtered)
    if operation_mode != "Тотал"
    else (
        "Тотал",
        total_value,
    )
)

std, cv, cv_interp = calc_stability(
    filtered,
    selected_operations,
    daily_avg,
)


# ============================================================
# 15. Approval rate — тільки повні календарні місяці
# ============================================================

approval_rows = approval_rows_for_period(
    df=df,
    period_start=period_bounds.start,
    period_end=period_bounds.end,
    operations=selected_operations,
)

approval_rate_val, sum_true_total, sum_false_total, approval_months = (
    aggregate_approval_total(approval_rows)
)

approval_rate_available = approval_rate_val is not None
approval_rate_str = (
    f"{approval_rate_val:.1f}%"
    if approval_rate_available
    else "—"
)

approval_by_op = aggregate_approval_by_operation(
    approval_rows_for_period(
        df=df,
        period_start=period_bounds.start,
        period_end=period_bounds.end,
        operations=OPERATIONS,
    )
)

# Залишаємо лише selected ops для operation mode.
if operation_mode != "Тотал":
    approval_by_op = approval_by_op[
        approval_by_op["operation"].isin(selected_operations)
    ].copy()


# ============================================================
# 16. Порівняння попередній місяць / рік
# ============================================================

def comparable_mtd_sum(
    df: pd.DataFrame,
    month_key: str,
    end_day: int,
    operation: str,
) -> float:
    period = pd.Period(month_key, freq="M")
    max_day = min(end_day, period.days_in_month)

    scoped = observed(
        df[
            (df["month"] == month_key)
            & (df["operation"] == operation)
            & (df["date"].dt.day <= max_day)
        ]
    )

    return float(
        scoped.groupby("date")["value"].sum().sum()
    )


comparison_parts: list[str] = []

if (
    period_mode == "За місяцями"
    and len(selected_months) == 1
    and operation_mode == "Тотал"
):
    current_period = pd.Period(selected_months[0], freq="M")
    today = now_kyiv()

    current_is_complete = current_period.end_time.normalize() < today

    current_end_day = (
        current_period.days_in_month
        if current_is_complete
        else today.day
    )

    current_sum = comparable_mtd_sum(
        df,
        str(current_period),
        current_end_day,
        "Тотал",
    )

    prev_period = current_period - 1
    prev_sum = comparable_mtd_sum(
        df,
        str(prev_period),
        min(current_end_day, prev_period.days_in_month),
        "Тотал",
    )

    if prev_sum > 0:
        delta_prev = (
            (current_sum - prev_sum)
            / prev_sum
            * 100
        )
        comparison_parts.append(
            f"Попер. міс: {delta_prev:+.1f}%"
        )

    prev_year_period = current_period - 12
    prev_year_exists = not df[
        (df["month"] == str(prev_year_period))
        & (df["operation"] == "Тотал")
    ].empty

    if prev_year_exists:
        prev_year_sum = comparable_mtd_sum(
            df,
            str(prev_year_period),
            min(
                current_end_day,
                prev_year_period.days_in_month,
            ),
            "Тотал",
        )

        if prev_year_sum > 0:
            delta_year = (
                (current_sum - prev_year_sum)
                / prev_year_sum
                * 100
            )
            comparison_parts.append(
                f"Мин. рік: {delta_year:+.1f}%"
            )

comparison_text = (
    "  ".join(comparison_parts)
    if comparison_parts
    else "—"
)


# ============================================================
# 17. Tabs
# ============================================================

tab1, tab2, tab3, tab4, tab5 = st.tabs(
    [
        "📊 Overview",
        "📈 Динаміка",
        "🧩 Операції",
        "📅 Навантаження",
        "🆚 Порівняння періодів",
    ]
)


# ============================================================
# TAB 1 — OVERVIEW
# ============================================================

with tab1:
    col1, col2, col3, col4, col5, col6 = st.columns(6)

    with col1:
        st.markdown(
            custom_metric(
                "Всього",
                f"{total_value:,.0f}",
                "Сума фактичних значень. Календарні дні без внесених даних не трактуються як нуль.",
            ),
            unsafe_allow_html=True,
        )

    with col2:
        st.markdown(
            custom_metric(
                "Середнє за день",
                f"{daily_avg:.0f}" if daily_avg else "—",
                "Середнє лише за дні, для яких є фактичні дані.",
            ),
            unsafe_allow_html=True,
        )

    with col3:
        avg_weekday_str = (
            f"{daily_avg_weekday:.0f}"
            if daily_avg_weekday is not None
            and not pd.isna(daily_avg_weekday)
            else "—"
        )
        st.markdown(
            custom_metric(
                "Середнє за будні",
                avg_weekday_str,
                "Середня кількість операцій у будні. Дні без даних не рахуються як нуль.",
            ),
            unsafe_allow_html=True,
        )

    with col4:
        avg_weekend_str = (
            f"{daily_avg_weekend:.0f}"
            if daily_avg_weekend is not None
            and not pd.isna(daily_avg_weekend)
            else "—"
        )
        st.markdown(
            custom_metric(
                "Середнє за вихідні",
                avg_weekend_str,
                "Середня кількість операцій у вихідні. Дні без даних не рахуються як нуль.",
            ),
            unsafe_allow_html=True,
        )

    with col5:
        peak_display = f"{peak:,.0f}" if peak > 0 else "—"
        if peak_date is not None:
            peak_display += f" ({peak_date.strftime('%d.%m')})"

        st.markdown(
            custom_metric(
                "Пік за день",
                peak_display,
                "Найбільше значення серед днів із фактичними даними.",
            ),
            unsafe_allow_html=True,
        )

    with col6:
        st.markdown(
            custom_metric(
                "Коефіцієнт погоджень",
                (
                    approval_rate_str
                    if approval_rate_available
                    else "— (немає повних місяців)"
                ),
                (
                    "TRUE / (TRUE + FALSE). "
                    "Розраховується лише за повні календарні місяці."
                ),
                color=(
                    approval_rate_color(approval_rate_val)
                    if approval_rate_available
                    else None
                ),
            ),
            unsafe_allow_html=True,
        )

    col7, col8, col9, col10, col11 = st.columns(5)

    with col7:
        st.markdown(
            custom_metric(
                "Пік / середнє",
                f"{peak_avg_ratio:.2f}×"
                if peak_avg_ratio
                else "—",
                "У скільки разів максимальний фактичний день перевищує середній фактичний день.",
            ),
            unsafe_allow_html=True,
        )

    with col8:
        st.markdown(
            custom_metric(
                "Стабільність (CV)",
                f"{cv:.1f}%"
                if cv
                else "—",
                "Коефіцієнт варіації лише за днями з фактичними даними. 🟢 <15% 🟡 15–30% 🔴 ≥30%.",
                color=cv_color(cv if cv else None),
            ),
            unsafe_allow_html=True,
        )

    with col9:
        if busiest_weekday:
            day_ua = WEEKDAY_UA.get(
                busiest_weekday,
                busiest_weekday,
            )
            value = (
                f"{day_ua} — "
                f"{busiest_weekday_val:.0f}/день"
            )
            help_txt = (
                "День тижня з найвищим середнім "
                "фактичним навантаженням."
            )
        else:
            value = "—"
            help_txt = None

        st.markdown(
            custom_metric(
                "Найактивніший день",
                value,
                help_txt,
            ),
            unsafe_allow_html=True,
        )

    with col10:
        if busiest_op:
            display_name = (
                busiest_op
                if len(str(busiest_op)) <= 16
                else str(busiest_op)[:14] + "…"
            )
            value = (
                f"{display_name} — "
                f"{busiest_op_val:,.0f}"
            )
            help_txt = (
                f"{busiest_op} — "
                f"{busiest_op_val:,.0f}"
            )
        else:
            value = "—"
            help_txt = None

        st.markdown(
            custom_metric(
                "Найактивніша операція",
                value,
                help_txt,
            ),
            unsafe_allow_html=True,
        )

    with col11:
        if (
            period_mode == "За місяцями"
            and len(selected_months) == 1
            and operation_mode == "Тотал"
        ):
            st.markdown("**Порівняння**")
            if comparison_text != "—":
                for part in comparison_text.split("  "):
                    st.markdown(
                        f"<p class='comparison-text'>"
                        f"{html.escape(part)}</p>",
                        unsafe_allow_html=True,
                    )
            else:
                st.markdown(
                    "<p class='comparison-text'>—</p>",
                    unsafe_allow_html=True,
                )
        else:
            st.markdown(
                custom_metric(
                    "Порівняння",
                    "—",
                    "Доступно лише для одного місяця в режимі «За місяцями» + «Тотал».",
                ),
                unsafe_allow_html=True,
            )

    st.divider()

    st.subheader("🧪 Якість даних")
    render_quality_summary(
        df=df,
        period_start=period_bounds.start,
        period_end=min(period_bounds.end, now_kyiv()),
        operations=selected_operations,
    )

    st.divider()

    st.subheader("💡 Інсайти")
    insights: list[str] = []

    if approval_rate_available:
        if approval_rate_val >= APPROVAL_GOOD_THRESHOLD:
            insights.append(
                f"🟢 Коефіцієнт погоджень **{approval_rate_str}** — "
                f"вище порогу {APPROVAL_GOOD_THRESHOLD}%."
            )
        elif approval_rate_val >= APPROVAL_WARN_THRESHOLD:
            insights.append(
                f"🟡 Коефіцієнт погоджень **{approval_rate_str}** — "
                f"у середній зоні."
            )
        else:
            insights.append(
                f"🔴 Коефіцієнт погоджень **{approval_rate_str}** — "
                f"нижче {APPROVAL_WARN_THRESHOLD}%."
            )
    else:
        insights.append(
            "ℹ️ Коефіцієнт погоджень недоступний: "
            "для розрахунку потрібні повні календарні місяці "
            "та наявні TRUE/FALSE дані."
        )

    if not approval_by_op.empty and len(approval_by_op) > 1:
        best_row = approval_by_op.iloc[0]
        worst_row = approval_by_op.iloc[-1]
        if best_row["operation"] != worst_row["operation"]:
            insights.append(
                f"🧩 Найкращий % погоджень — "
                f"**{best_row['operation']}** "
                f"({best_row['approval_rate']:.1f}%), "
                f"найгірший — **{worst_row['operation']}** "
                f"({worst_row['approval_rate']:.1f}%)."
            )

    if cv > 0:
        insights.append(
            f"{cv_interp} денного навантаження "
            f"(CV = {cv:.1f}%)."
        )

    if busiest_weekday:
        day_ua = WEEKDAY_UA.get(
            busiest_weekday,
            busiest_weekday,
        )
        insights.append(
            f"📅 Найбільше навантаження припадає на "
            f"**{day_ua}** — у середньому "
            f"{busiest_weekday_val:.0f} операцій/день."
        )

    if busiest_op:
        insights.append(
            f"📈 Найактивніша операція за обсягом — "
            f"**{busiest_op}** "
            f"({busiest_op_val:,.0f} за період)."
        )

    if peak_avg_ratio >= 2:
        insights.append(
            f"⚠️ Пік у **{peak_avg_ratio:.1f}×** "
            f"перевищує середнє — можливі різкі сплески навантаження."
        )

    if period_mode == "За місяцями" and comparison_text != "—":
        insights.append(
            f"🔄 Порівняння з попередніми періодами: "
            f"{comparison_text}."
        )

    for insight in insights:
        st.markdown(f"- {insight}")

    st.divider()

    # ---- Forecast ----
    if period_mode != "За місяцями":
        st.info(
            "📊 Прогнози доступні лише в режимі "
            "«За місяцями» з одним поточним місяцем."
        )
    elif len(selected_months) != 1:
        st.info(
            "📊 Прогнози доступні лише для одного "
            "обраного місяця."
        )
    elif not is_current_month(selected_months[0]):
        st.info(
            "📊 Прогноз доступний лише для поточного "
            "календарного місяця."
        )
    else:
        forecast_target_options = ["Тотал"] + all_ops

        forecast_target = st.selectbox(
            "Прогнозувати для:",
            options=forecast_target_options,
            index=0,
            help=(
                "Прогноз для поточного місяця. "
                "Порожні дні не трактуються як нулі."
            ),
        )

        stat_forecast, season_forecast = forecast_scenarios(
            df=df,
            current_month=selected_months[0],
            operation=forecast_target,
        )

        if stat_forecast or season_forecast:
            st.subheader(
                f"📊 Прогнози на поточний місяць — "
                f"{forecast_target}"
            )

            if stat_forecast:
                observed_days = stat_forecast["observed_days"]
                last_observed_date = stat_forecast["last_observed_date"]

                st.caption(
                    f"Факт: {stat_forecast['fact']:,.0f}; "
                    f"календарних днів минуло: "
                    f"{stat_forecast['calendar_elapsed_days']}; "
                    f"днів з даними: {observed_days}; "
                    f"останній факт: "
                    f"{last_observed_date.strftime('%d.%m.%Y')}."
                )

                st.markdown(
                    "**📈 Сценарний прогноз** "
                    "(на основі середнього фактичного дня "
                    "та варіативності)"
                )

                forecast_cards(
                    "Сценар.",
                    stat_forecast,
                    help_base="факт + середнє фактичне денне навантаження × залишок календарних днів",
                    help_min="факт + max(0, середнє − 0.5×σ) × залишок календарних днів",
                    help_max="факт + (середнє + 0.5×σ) × залишок календарних днів",
                )

            if season_forecast:
                st.markdown(
                    "**📅 Сезонний прогноз** "
                    "(аналогічний MTD-період минулого року)"
                )

                with st.expander(
                    "🔍 Деталі сезонного прогнозу"
                ):
                    st.write(
                        f"**Місяць минулого року:** "
                        f"{season_forecast['prev_month']}"
                    )
                    st.write(
                        f"**Днів з поточними даними:** "
                        f"{season_forecast['observed_days']}"
                    )
                    st.write(
                        f"**Поточний MTD:** "
                        f"{season_forecast['fact']:,.0f}"
                    )
                    st.write(
                        f"**Попередній MTD:** "
                        f"{season_forecast['prev_mtd_sum']:,.0f}"
                    )
                    st.write(
                        f"**Поточний середній фактичний день:** "
                        f"{stat_forecast['avg_daily']:.2f}"
                    )
                    st.write(
                        f"**Середній фактичний день минулого року:** "
                        f"{season_forecast['prev_avg_daily']:.2f}"
                    )
                    st.write(
                        f"**Коефіцієнт сезонності:** "
                        f"{season_forecast['seasonality_factor']:.3f}"
                    )
                    st.write(
                        f"**Залишок минулого року:** "
                        f"{season_forecast['prev_remaining_sum']:,.0f}"
                    )
                    st.write(
                        f"**Прогноз залишку:** "
                        f"{season_forecast['forecast_remaining']:,.0f}"
                    )

                forecast_cards(
                    "Сезон.",
                    season_forecast,
                    help_base="факт + прогноз залишку минулого року × коефіцієнт сезонності",
                    help_min="факт + 0.9 × сезонний прогноз залишку",
                    help_max="факт + 1.1 × сезонний прогноз залишку",
                )
        else:
            st.info(
                "Прогноз недоступний: недостатньо "
                "фактичних даних за поточний місяць."
            )

    st.divider()

    # ---- Overview dynamics ----
    st.subheader("📈 Динаміка за період")

    daily_chart = daily_calendar_series(
        df=df,
        start=period_bounds.start,
        end=min(period_bounds.end, now_kyiv()),
        operation_filter=selected_operations,
    )

    if operation_mode == "Тотал":
        fig_overview = px.line(
            daily_chart,
            x="date",
            y="value",
            markers=True,
            labels={
                "date": "Дата",
                "value": "Кількість",
            },
        )

        if smooth_enabled:
            smooth_data = daily_chart.dropna(subset=["value"]).copy()
            smooth_data["value_smooth"] = (
                smooth_data["value"]
                .rolling(
                    window=smooth_window,
                    min_periods=1,
                    center=True,
                )
                .mean()
            )

            fig_overview.add_scatter(
                x=smooth_data["date"],
                y=smooth_data["value_smooth"],
                mode="lines",
                name=f"Ковзне середнє ({smooth_window} дн.)",
                line=dict(
                    color=KPO_AMBER,
                    width=3,
                ),
            )

        anomalies = detect_anomalies(
            filtered,
            operations=selected_operations,
        )
        anomaly_points = anomalies[
            anomalies["is_anomaly"]
        ] if not anomalies.empty else pd.DataFrame()

        if not anomaly_points.empty:
            fig_overview.add_scatter(
                x=anomaly_points["date"],
                y=anomaly_points["value"],
                mode="markers",
                marker=dict(
                    color=KPO_RED,
                    size=10,
                    symbol="x",
                ),
                name="Аномалія",
            )
    else:
        fig_overview = px.line(
            filtered_stats,
            x="date",
            y="value",
            color="operation",
            markers=True,
            labels={
                "date": "Дата",
                "value": "Кількість",
                "operation": "Операція",
            },
        )

    fig_overview.update_xaxes(
        tickformat="%d.%m",
        title_text="Дата",
    )
    fig_overview.update_layout(
        height=420,
        hovermode="x unified",
        margin=dict(l=10, r=10, t=20, b=10),
    )
    st.plotly_chart(
        fig_overview,
        use_container_width=True,
    )


# ============================================================
# TAB 2 — ДИНАМІКА
# ============================================================

with tab2:
    st.subheader("📈 Детальна динаміка")

    daily_chart = daily_calendar_series(
        df=df,
        start=period_bounds.start,
        end=min(period_bounds.end, now_kyiv()),
        operation_filter=selected_operations,
    )

    if operation_mode == "Тотал":
        fig_daily = px.line(
            daily_chart,
            x="date",
            y="value",
            markers=True,
            labels={
                "date": "Дата",
                "value": "Кількість",
            },
            title="Щоденна динаміка",
        )

        if smooth_enabled:
            smooth_data = daily_chart.dropna(subset=["value"]).copy()
            smooth_data["value_smooth"] = (
                smooth_data["value"]
                .rolling(
                    window=smooth_window,
                    min_periods=1,
                    center=True,
                )
                .mean()
            )

            fig_daily.add_scatter(
                x=smooth_data["date"],
                y=smooth_data["value_smooth"],
                mode="lines",
                name=f"Ковзне середнє ({smooth_window} дн.)",
                line=dict(
                    color=KPO_AMBER,
                    width=3,
                ),
            )

        anomalies = detect_anomalies(
            filtered,
            operations=selected_operations,
        )
        anomaly_points = (
            anomalies[anomalies["is_anomaly"]]
            if not anomalies.empty
            else pd.DataFrame()
        )

        if not anomaly_points.empty:
            fig_daily.add_scatter(
                x=anomaly_points["date"],
                y=anomaly_points["value"],
                mode="markers",
                marker=dict(
                    color=KPO_RED,
                    size=10,
                    symbol="x",
                ),
                name="Аномалія",
            )
    else:
        fig_daily = px.line(
            filtered_stats,
            x="date",
            y="value",
            color="operation",
            markers=True,
            labels={
                "date": "Дата",
                "value": "Кількість",
                "operation": "Операція",
            },
            title="Динаміка вибраних операцій",
        )

    fig_daily.update_xaxes(
        tickformat="%d.%m",
        title_text="Дата",
    )
    fig_daily.update_layout(
        height=400,
        hovermode="x unified",
        margin=dict(l=10, r=10, t=20, b=10),
    )
    st.plotly_chart(
        fig_daily,
        use_container_width=True,
    )

    if operation_mode == "Тотал":
        st.subheader("📊 Порівняння по роках (YoY)")

        yoy = observed(
            df[df["operation"] == "Тотал"]
        ).copy()

        yoy = yoy[
            yoy["year"].isin(selected_years)
        ]

        # Поточний незавершений місяць обрізаємо до MTD
        today = now_kyiv()
        current_month = today.strftime("%Y-%m")
        if current_month in yoy["month"].unique():
            yoy.loc[
                yoy["month"] == current_month,
                "date",
            ] = pd.to_datetime(
                yoy.loc[
                    yoy["month"] == current_month,
                    "date",
                ]
            )

            yoy = yoy[
                ~(
                    (yoy["month"] == current_month)
                    & (yoy["date"].dt.day > today.day)
                )
            ]

        yoy_monthly = (
            yoy.groupby(
                ["year", "month"],
                as_index=False,
                observed=True,
            )["value"]
            .sum()
        )

        yoy_monthly["month_num"] = yoy_monthly[
            "month"
        ].apply(lambda x: pd.Period(x).month)

        yoy_monthly["month_label"] = yoy_monthly[
            "month"
        ].apply(lambda x: pd.Period(x).strftime("%b"))

        yoy_monthly = yoy_monthly.sort_values(
            ["year", "month_num"]
        )

        month_axis_order = (
            yoy_monthly
            .drop_duplicates("month_num")
            .sort_values("month_num")["month_label"]
            .tolist()
        )

        fig_yoy = px.line(
            yoy_monthly,
            x="month_label",
            y="value",
            color="year",
            markers=True,
            labels={
                "month_label": "Місяць",
                "value": "Кількість",
                "year": "Рік",
            },
            title="Місячні суми по роках",
            category_orders={
                "month_label": month_axis_order,
            },
        )

        fig_yoy.update_layout(
            height=380,
            margin=dict(l=10, r=10, t=20, b=10),
        )
        st.plotly_chart(
            fig_yoy,
            use_container_width=True,
        )

        if len(selected_years) >= 2:
            years_sorted = sorted(selected_years)
            y1, y2 = years_sorted[-2], years_sorted[-1]

            y1_data = (
                yoy_monthly[
                    yoy_monthly["year"] == y1
                ]
                .set_index("month_label")["value"]
            )

            y2_data = (
                yoy_monthly[
                    yoy_monthly["year"] == y2
                ]
                .set_index("month_label")["value"]
            )

            compare_df = pd.DataFrame(
                {
                    str(y1): y1_data,
                    str(y2): y2_data,
                }
            )

            compare_df["Різниця"] = (
                compare_df[str(y2)]
                - compare_df[str(y1)]
            )

            compare_df["%"] = (
                (
                    compare_df["Різниця"]
                    / compare_df[str(y1)]
                    * 100
                )
                .replace([np.inf, -np.inf], np.nan)
            )

            display_compare = compare_df.copy()
            display_compare["%"] = display_compare["%"].apply(
                lambda x: f"{x:+.1f}%"
                if pd.notna(x)
                else "—"
            )

            st.dataframe(
                display_compare,
                use_container_width=True,
            )

    st.subheader("📈 Накопичувальна сума за період")

    cumsum = (
        daily_values(
            filtered,
            operation_filter=selected_operations,
        )
        .sort_values("date")
        .copy()
    )

    if not cumsum.empty:
        cumsum["cumulative"] = cumsum["value"].cumsum()

        fig_cum = px.line(
            cumsum,
            x="date",
            y="cumulative",
            markers=True,
            labels={
                "date": "Дата",
                "cumulative": "Накопичена кількість",
            },
        )

        fig_cum.update_xaxes(
            tickformat="%d.%m",
            title_text="Дата",
        )

        fig_cum.update_layout(
            height=380,
            margin=dict(l=10, r=10, t=20, b=10),
        )
        st.plotly_chart(
            fig_cum,
            use_container_width=True,
        )

    st.subheader("🔍 Аномальні дні")

    anomalies = detect_anomalies(
        filtered,
        operations=selected_operations,
    )

    if not anomalies.empty:
        anomaly_points = anomalies[
            anomalies["is_anomaly"]
        ].copy()

        if not anomaly_points.empty:
            anomaly_points["date_str"] = (
                anomaly_points["date"]
                .dt.strftime("%d.%m.%Y")
            )

            anomaly_points["deviation"] = np.where(
                anomaly_points["rolling_median"] != 0,
                (
                    (
                        anomaly_points["value"]
                        - anomaly_points["rolling_median"]
                    )
                    / anomaly_points["rolling_median"]
                    * 100
                ).round(1),
                np.nan,
            )

            anomaly_points["type"] = anomaly_points[
                "deviation"
            ].apply(
                lambda x: (
                    "🔴 Високий"
                    if pd.notna(x) and x > 10
                    else "🔵 Низький"
                    if pd.notna(x) and x < -10
                    else "🟡 Помірний"
                )
            )

            anomaly_points = anomaly_points.sort_values(
                "date",
                ascending=False,
            )

            st.dataframe(
                anomaly_points[
                    [
                        "date_str",
                        "value",
                        "rolling_median",
                        "deviation",
                        "type",
                        "z_score",
                    ]
                ],
                column_config={
                    "date_str": "Дата",
                    "value": "Значення",
                    "rolling_median": "Rolling медіана",
                    "deviation": "Відхилення, %",
                    "type": "Тип",
                    "z_score": st.column_config.NumberColumn(
                        "Robust Z-score",
                        help=(
                            "Медіана + MAD. "
                            "Поріг >3."
                        ),
                    ),
                },
                use_container_width=True,
                hide_index=True,
            )
        else:
            st.info("Аномальних днів не виявлено.")
    else:
        st.info(
            "Недостатньо даних для виявлення аномалій."
        )


# ============================================================
# TAB 3 — ОПЕРАЦІЇ
# ============================================================

with tab3:
    st.subheader("🧩 Аналіз операцій")

    st.subheader("✅ Коефіцієнт погоджень по операціях")

    if not approval_by_op.empty:
        total_approval_rows = approval_rows_for_period(
            df=df,
            period_start=period_bounds.start,
            period_end=period_bounds.end,
            operations=["Тотал"],
        )

        total_rate, _, _, _ = aggregate_approval_total(
            total_approval_rows
        )

        approval_display = approval_by_op.copy()
        approval_display["tier"] = approval_display[
            "approval_rate"
        ].apply(approval_rate_tier)

        fig_approval = px.bar(
            approval_display,
            x="operation",
            y="approval_rate",
            color="tier",
            color_discrete_map=APPROVAL_TIER_COLOR_MAP,
            text=(
                approval_display["approval_rate"]
                .astype(str)
                + "%"
            ),
            labels={
                "operation": "Операція",
                "approval_rate": "Коефіцієнт погоджень, %",
                "tier": "Категорія",
            },
            title=(
                "Коефіцієнт погоджень по операціях "
                "(за повні календарні місяці)"
            ),
            category_orders={
                "operation": approval_display[
                    "operation"
                ].tolist()
            },
        )

        fig_approval.update_traces(
            textposition="outside",
            width=0.55,
        )

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

        st.plotly_chart(
            fig_approval,
            use_container_width=True,
        )

        with st.expander(
            "📋 Таблиця по операціях",
            expanded=False,
        ):
            st.dataframe(
                approval_by_op[
                    [
                        "operation",
                        "sum_true",
                        "sum_false",
                        "total",
                        "approval_rate",
                    ]
                ].rename(
                    columns={
                        "operation": "Операція",
                        "sum_true": "Погоджено (TRUE)",
                        "sum_false": "Відхилено (FALSE)",
                        "total": "Всього",
                        "approval_rate": "Коефіцієнт погоджень, %",
                    }
                ),
                use_container_width=True,
                hide_index=True,
            )
    else:
        st.info(
            "Немає даних для розрахунку коефіцієнта погоджень. "
            "Потрібні повні календарні місяці та TRUE/FALSE дані."
        )

    st.subheader(
        "🌡️ Теплова карта коефіцієнта погоджень "
        "(Операція × Місяць)"
    )

    heat_approval_rows = approval_rows_for_period(
        df=df,
        period_start=period_bounds.start,
        period_end=period_bounds.end,
        operations=["Тотал"] + OPERATIONS,
    )

    if not heat_approval_rows.empty:
        heat_approval = heat_approval_rows.copy()

        heat_approval = heat_approval[
            heat_approval["total"] > 0
        ].copy()

        if not heat_approval.empty:
            heat_approval["rate"] = (
                heat_approval["sum_true"]
                / heat_approval["total"]
                * 100
            )

            heat_approval["month_period"] = pd.PeriodIndex(
                heat_approval["month"],
                freq="M",
            )

            heat_pivot = heat_approval.pivot_table(
                index="month_period",
                columns="operation",
                values="rate",
                aggfunc="mean",
                observed=True,
            )

            heat_pivot = heat_pivot.sort_index()

            cols = heat_pivot.columns.tolist()
            if "Тотал" in cols:
                cols.remove("Тотал")
                cols = ["Тотал"] + cols
                heat_pivot = heat_pivot[cols]

            heat_pivot.index = heat_pivot.index.strftime(
                "%m.%Y"
            )

            fig_approval_heat = px.imshow(
                heat_pivot,
                text_auto=".1f",
                aspect="auto",
                labels=dict(
                    x="Операція",
                    y="Місяць",
                    color="Коефіцієнт погоджень, %",
                ),
                color_continuous_scale="RdYlGn",
                zmin=0,
                zmax=100,
            )

            heatmap_height = max(
                420,
                min(
                    2400,
                    len(heat_pivot.index) * 38,
                ),
            )

            fig_approval_heat.update_layout(
                height=heatmap_height,
                margin=dict(l=10, r=10, t=20, b=10),
            )

            font_size = (
                12
                if len(heat_pivot.index) <= 12
                else 10
                if len(heat_pivot.index) <= 24
                else 8
            )

            fig_approval_heat.update_traces(
                textfont=dict(size=font_size)
            )

            st.plotly_chart(
                fig_approval_heat,
                use_container_width=True,
            )

            st.caption(
                "🔴 <70%  🟡 70–85%  🟢 ≥85%. "
                "Розрахунок — лише за повні календарні місяці."
            )
        else:
            st.info(
                "Немає даних для теплової карти."
            )
    else:
        st.info(
            "Немає повних місяців або TRUE/FALSE даних."
        )

    st.divider()

    ops_data = observed(
        filtered[
            filtered["operation"] != "Тотал"
        ]
    )

    if not ops_data.empty:
        st.subheader("📊 Структура операцій")

        ops_structure = (
            ops_data.groupby(
                "operation",
                as_index=False,
                observed=True,
            )["value"]
            .sum()
            .sort_values("value", ascending=False)
        )

        ops_total = ops_structure["value"].sum()
        ops_structure["percent"] = (
            ops_structure["value"]
            / ops_total
            * 100
            if ops_total > 0
            else 0
        ).round(1)

        ops_structure["text"] = (
            ops_structure["percent"].astype(str)
            + "%"
        )

        fig_ops_structure = px.bar(
            ops_structure,
            x="value",
            y="operation",
            text="text",
            orientation="h",
            labels={
                "value": "Кількість",
                "operation": "Операція",
            },
            title="Структура за період",
        )

        fig_ops_structure.update_traces(
            textposition="outside"
        )

        fig_ops_structure.update_layout(
            height=360,
            margin=dict(l=10, r=10, t=20, b=10),
            yaxis={"categoryorder": "total descending"},
        )

        st.plotly_chart(
            fig_ops_structure,
            use_container_width=True,
        )

        st.subheader(
            "📈 Динаміка структури операцій по місяцях"
        )

        ops_monthly = (
            ops_data.groupby(
                ["month", "operation"],
                as_index=False,
                observed=True,
            )["value"]
            .sum()
        )

        ops_monthly["month_period"] = pd.PeriodIndex(
            ops_monthly["month"],
            freq="M",
        )
        ops_monthly = ops_monthly.sort_values(
            "month_period"
        )
        ops_monthly["month_label"] = (
            ops_monthly["month_period"]
            .astype(str)
            .str.replace("-", ".", regex=False)
        )

        fig_stacked = px.bar(
            ops_monthly,
            x="month_label",
            y="value",
            color="operation",
            barmode="stack",
            labels={
                "month_label": "Місяць",
                "value": "Кількість",
                "operation": "Операція",
            },
            title="Структура операцій по місяцях",
        )

        fig_stacked.update_layout(
            height=400,
            margin=dict(l=10, r=10, t=20, b=10),
        )

        st.plotly_chart(
            fig_stacked,
            use_container_width=True,
        )

        st.subheader("📊 Pareto аналіз операцій")

        pareto_data = (
            ops_structure
            .sort_values("value", ascending=False)
            .copy()
        )

        pareto_data["cumulative_percent"] = (
            pareto_data["percent"].cumsum()
        )

        fig_pareto = go.Figure()

        fig_pareto.add_trace(
            go.Bar(
                x=pareto_data["operation"],
                y=pareto_data["value"],
                name="Кількість",
                marker_color=KPO_CYAN,
                yaxis="y",
            )
        )

        fig_pareto.add_trace(
            go.Scatter(
                x=pareto_data["operation"],
                y=pareto_data["cumulative_percent"],
                name="Накопичувальна частка, %",
                mode="lines+markers",
                marker_color=KPO_AMBER,
                yaxis="y2",
            )
        )

        fig_pareto.add_hline(
            y=80,
            line_dash="dash",
            line_color="gray",
            annotation_text="80%",
            annotation_position="top right",
        )

        fig_pareto.update_layout(
            title="Pareto операцій",
            xaxis_title="Операція",
            yaxis=dict(
                title="Кількість",
                side="left",
                showgrid=True,
            ),
            yaxis2=dict(
                title="Накопичувальна частка, %",
                overlaying="y",
                side="right",
                range=[0, 100],
            ),
            legend=dict(
                x=0.8,
                y=0.9,
            ),
            height=400,
            margin=dict(l=10, r=10, t=20, b=10),
        )

        st.plotly_chart(
            fig_pareto,
            use_container_width=True,
        )

        if operation_mode != "Тотал" and len(selected_operations) > 1:
            st.subheader("📈 Порівняння вибраних операцій")

            fig_compare_ops = px.line(
                filtered_stats,
                x="date",
                y="value",
                color="operation",
                markers=True,
                labels={
                    "date": "Дата",
                    "value": "Кількість",
                    "operation": "Операція",
                },
                title="Динаміка вибраних операцій",
            )

            fig_compare_ops.update_xaxes(
                tickformat="%d.%m",
                title_text="Дата",
            )

            fig_compare_ops.update_layout(
                height=400,
                margin=dict(l=10, r=10, t=20, b=10),
            )

            st.plotly_chart(
                fig_compare_ops,
                use_container_width=True,
            )

    else:
        st.info(
            "Немає даних про окремі операції "
            "для вибраного періоду."
        )


# ============================================================
# TAB 4 — НАВАНТАЖЕННЯ
# ============================================================

with tab4:
    st.subheader("📅 Аналіз навантаження")

    st.subheader(
        "📊 Середнє навантаження за днями тижня"
    )

    daily_sum = daily_values(
        filtered,
        operation_filter=selected_operations,
    )

    if not daily_sum.empty:
        daily_sum["weekday_ua"] = (
            daily_sum["date"]
            .dt.day_name()
            .map(WEEKDAY_UA)
        )

        weekday_avg = (
            daily_sum.groupby(
                "weekday_ua",
                as_index=False,
            )["value"]
            .mean()
        )

        weekday_avg["weekday"] = pd.Categorical(
            weekday_avg["weekday_ua"],
            categories=WEEKDAY_ORDER_UA,
            ordered=True,
        )

        weekday_avg = weekday_avg.sort_values(
            "weekday"
        )

        fig_weekday_avg = px.bar(
            weekday_avg,
            x="weekday",
            y="value",
            text=weekday_avg["value"].round(1).astype(str),
            labels={
                "weekday": "День тижня",
                "value": "Середня кількість",
            },
            title=(
                "Середня кількість операцій "
                "по днях тижня"
            ),
        )

        fig_weekday_avg.update_traces(
            textposition="outside"
        )

        fig_weekday_avg.update_layout(
            height=360,
            margin=dict(l=10, r=10, t=20, b=10),
        )

        st.plotly_chart(
            fig_weekday_avg,
            use_container_width=True,
        )

    st.subheader("📅 Будні vs вихідні")

    week_observed = observed(filtered).copy()

    if not week_observed.empty:
        week_daily = (
            week_observed.groupby(
                ["date", "month", "is_weekend"],
                as_index=False,
            )["value"]
            .sum()
        )

        week_daily["period_type"] = week_daily[
            "is_weekend"
        ].map(
            {
                False: "Будні",
                True: "Вихідні",
            }
        )

        week_data = (
            week_daily.groupby(
                ["month", "period_type"],
                as_index=False,
            )["value"]
            .sum()
        )

        month_totals = (
            week_daily.groupby("month")["value"]
            .sum()
            .rename("month_total")
            .reset_index()
        )

        week_data = week_data.merge(
            month_totals,
            on="month",
            how="left",
        )

        week_data["percent"] = (
            week_data["value"]
            / week_data["month_total"]
            * 100
        ).round(1)

        week_data["text"] = (
            week_data["percent"].astype(str)
            + "%"
        )

        week_data["month_period"] = pd.PeriodIndex(
            week_data["month"],
            freq="M",
        )

        week_data = week_data.sort_values(
            "month_period"
        )

        week_data["month_label"] = (
            week_data["month_period"]
            .astype(str)
            .str.replace("-", ".", regex=False)
        )

        fig_week = px.bar(
            week_data,
            x="month_label",
            y="value",
            color="period_type",
            barmode="group",
            text="text",
            labels={
                "month_label": "Місяць",
                "value": "Кількість",
                "period_type": "",
            },
        )

        fig_week.update_traces(
            textposition="outside"
        )

        fig_week.update_layout(
            height=380,
            margin=dict(l=10, r=10, t=20, b=10),
        )

        st.plotly_chart(
            fig_week,
            use_container_width=True,
        )

    st.subheader("🌡️ Теплова карта навантаження")

    daily_heat = daily_values(
        filtered,
        operation_filter=selected_operations,
    )

    if not daily_heat.empty:
        daily_heat["month_period"] = pd.PeriodIndex(
            daily_heat["date"].dt.strftime("%Y-%m"),
            freq="M",
        )
        daily_heat["weekday_ua"] = (
            daily_heat["date"]
            .dt.day_name()
            .map(WEEKDAY_UA)
        )

        heat_data = (
            daily_heat.groupby(
                ["month_period", "weekday_ua"],
                as_index=False,
            )["value"]
            .mean()
        )

        heat_pivot = heat_data.pivot(
            index="month_period",
            columns="weekday_ua",
            values="value",
        )

        heat_pivot = heat_pivot.reindex(
            columns=WEEKDAY_ORDER_UA
        ).sort_index()

        heat_pivot.index = heat_pivot.index.strftime(
            "%m.%Y"
        )

        fig_heatmap = px.imshow(
            heat_pivot,
            text_auto=".1f",
            aspect="auto",
            labels=dict(
                x="День тижня",
                y="Місяць",
                color="Середня кількість",
            ),
            color_continuous_scale=KPO_HEAT_SCALE,
        )

        heatmap_height = max(
            420,
            min(
                2400,
                len(heat_pivot.index) * 38,
            ),
        )

        fig_heatmap.update_layout(
            height=heatmap_height,
            margin=dict(l=10, r=10, t=20, b=10),
        )

        font_size = (
            12
            if len(heat_pivot.index) <= 12
            else 10
            if len(heat_pivot.index) <= 24
            else 8
        )

        fig_heatmap.update_traces(
            textfont=dict(size=font_size)
        )

        st.plotly_chart(
            fig_heatmap,
            use_container_width=True,
        )

        st.caption(
            "Порожні дні не перетворюються на 0: "
            "у середніх показниках беруть участь лише "
            "дні з фактичними даними."
        )

    st.subheader("📊 Стабільність навантаження")

    col1, col2, col3 = st.columns(3)

    with col1:
        st.markdown(
            custom_metric(
                "Середнє за день",
                f"{daily_avg:.0f}"
                if daily_avg > 0
                else "—",
            ),
            unsafe_allow_html=True,
        )

    with col2:
        st.markdown(
            custom_metric(
                "Стандартне відхилення",
                f"{std:.1f}"
                if std > 0
                else "—",
            ),
            unsafe_allow_html=True,
        )

    with col3:
        st.markdown(
            custom_metric(
                "Коефіцієнт варіації (CV)",
                f"{cv:.1f}%"
                if cv > 0
                else "—",
                color=cv_color(cv if cv > 0 else None),
            ),
            unsafe_allow_html=True,
        )

    st.subheader(
        "📊 Розподіл відхилень від середнього "
        "(крива щільності)"
    )

    daily_totals = daily_values(
        filtered,
        operation_filter=selected_operations,
    ).copy()

    if len(daily_totals) >= 3:
        mean_all = daily_totals["value"].mean()

        if mean_all > 0:
            daily_totals["is_weekend"] = (
                daily_totals["date"].dt.dayofweek >= 5
            )
            daily_totals["dev_all"] = (
                (
                    daily_totals["value"]
                    - mean_all
                )
                / mean_all
                * 100
            )

            weekday_mask = (
                daily_totals["is_weekend"] == False
            )
            weekend_mask = (
                daily_totals["is_weekend"] == True
            )

            mean_weekday = (
                daily_totals.loc[
                    weekday_mask, "value"
                ].mean()
                if weekday_mask.any()
                else None
            )

            mean_weekend = (
                daily_totals.loc[
                    weekend_mask, "value"
                ].mean()
                if weekend_mask.any()
                else None
            )

            daily_totals["dev_weekday"] = np.nan
            daily_totals["dev_weekend"] = np.nan

            if mean_weekday and mean_weekday != 0:
                daily_totals.loc[
                    weekday_mask,
                    "dev_weekday",
                ] = (
                    (
                        daily_totals.loc[
                            weekday_mask,
                            "value",
                        ]
                        - mean_weekday
                    )
                    / mean_weekday
                    * 100
                )

            if mean_weekend and mean_weekend != 0:
                daily_totals.loc[
                    weekend_mask,
                    "dev_weekend",
                ] = (
                    (
                        daily_totals.loc[
                            weekend_mask,
                            "value",
                        ]
                        - mean_weekend
                    )
                    / mean_weekend
                    * 100
                )

            dev_groups = {
                "Всі дні": daily_totals[
                    "dev_all"
                ].dropna().values,
                "Будні": daily_totals[
                    "dev_weekday"
                ].dropna().values,
                "Вихідні": daily_totals[
                    "dev_weekend"
                ].dropna().values,
            }

            fig_density = go.Figure()
            max_density = 0.0

            density_colors = {
                "Всі дні": KPO_CYAN,
                "Будні": KPO_GREEN,
                "Вихідні": KPO_AMBER,
            }

            for group_name, data in dev_groups.items():
                if len(data) > 1:
                    x_min = data.min() - 10
                    x_max = data.max() + 10

                    x_grid = np.linspace(
                        x_min,
                        x_max,
                        200,
                    )

                    density = gaussian_kde_np(
                        data,
                        x_grid,
                    )

                    if len(density):
                        max_density = max(
                            max_density,
                            float(density.max()),
                        )

                    fig_density.add_trace(
                        go.Scatter(
                            x=x_grid,
                            y=density,
                            mode="lines",
                            name=group_name,
                            line=dict(
                                color=density_colors[group_name],
                                width=2.5,
                            ),
                        )
                    )

            if max_density > 0:
                fig_density.add_trace(
                    go.Scatter(
                        x=[0, 0],
                        y=[
                            0,
                            max_density * 1.1,
                        ],
                        mode="lines",
                        name="Середнє (0%)",
                        line=dict(
                            color=KPO_RED,
                            width=2,
                            dash="dash",
                        ),
                        showlegend=True,
                    )
                )

                median_all = np.median(
                    dev_groups["Всі дні"]
                )

                fig_density.add_trace(
                    go.Scatter(
                        x=[median_all, median_all],
                        y=[
                            0,
                            max_density * 1.1,
                        ],
                        mode="lines",
                        name=f"Медіана ({median_all:.1f}%)",
                        line=dict(
                            color=KPO_TEXT,
                            width=2,
                            dash="dash",
                        ),
                        showlegend=True,
                    )
                )

            fig_density.update_layout(
                title=(
                    "Криві щільності "
                    "відхилень від середнього"
                ),
                xaxis_title="Відхилення, %",
                yaxis_title="Щільність",
                height=400,
                margin=dict(l=10, r=10, t=40, b=10),
                legend=dict(
                    title="Група / лінії",
                    x=0.98,
                    y=0.98,
                    xanchor="right",
                    yanchor="top",
                    bgcolor="rgba(0,0,0,0)",
                ),
                hovermode="x unified",
            )

            st.plotly_chart(
                fig_density,
                use_container_width=True,
            )

            with st.expander(
                "❓ Що означає форма кривих?"
            ):
                st.markdown(
                    """
Крива показує, де найчастіше знаходяться фактичні
відхилення від середнього.

Вузька крива означає, що навантаження зазвичай близьке
до типового рівня. Широка — що денні значення сильніше
коливаються.

Криві «Будні» та «Вихідні» нормалізуються відносно
власного середнього, тому їх можна порівнювати за
стабільністю, а не лише за абсолютним обсягом.
"""
                )

            with st.expander(
                "❓ Як це інтерпретувати для бізнесу?"
            ):
                st.markdown(
                    """
Якщо значна частина значень знаходиться далеко праворуч
або ліворуч від 0%, навантаження систематично відхиляється
від типового рівня.

Для операційного планування важливі не тільки середні
значення, а й хвости розподілу: саме вони показують дні,
коли потрібен запас пропускної здатності.
"""
                )
        else:
            st.info(
                "Середнє дорівнює нулю — "
                "розподіл відхилень неможливо побудувати."
            )
    else:
        st.info(
            "Недостатньо даних для побудови "
            "графіка розподілу."
        )

    st.subheader("📈 Співвідношення пік / середнє")

    st.markdown(
        custom_metric(
            "Пік / середнє",
            f"{peak_avg_ratio:.2f}×"
            if peak_avg_ratio > 0
            else "—",
        ),
        unsafe_allow_html=True,
    )


# ============================================================
# TAB 5 — ПОРІВНЯННЯ
# ============================================================

with tab5:
    st.subheader("🆚 Порівняння двох довільних періодів")
    st.caption(
        "Незалежно від основних фільтрів у сайдбарі — "
        "оберіть два будь-які діапазони для порівняння."
    )

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

    col_a, col_b = st.columns(2)

    default_end_b = max_date.date()
    default_start_b = max(
        min_date,
        max_date - pd.Timedelta(days=13),
    ).date()

    default_end_a = max(
        min_date,
        max_date - pd.Timedelta(days=14),
    ).date()

    default_start_a = max(
        min_date,
        max_date - pd.Timedelta(days=27),
    ).date()

    with col_a:
        st.markdown("**Період A**")
        range_a_input = st.date_input(
            "Діапазон A",
            value=(
                default_start_a,
                default_end_a,
            ),
            min_value=min_date.date(),
            max_value=max_date.date(),
            key="cmp_range_a",
        )

    with col_b:
        st.markdown("**Період B**")
        range_b_input = st.date_input(
            "Діапазон B",
            value=(
                default_start_b,
                default_end_b,
            ),
            min_value=min_date.date(),
            max_value=max_date.date(),
            key="cmp_range_b",
        )

    range_a = normalize_date_range(range_a_input)
    range_b = normalize_date_range(range_b_input)

    def build_period_metrics(
        date_range: PeriodRange,
        ops: list[str],
    ):
        scoped = df[
            (df["date"] >= date_range.start)
            & (df["date"] <= date_range.end)
            & (df["operation"].isin(ops))
        ]

        daily = daily_values(
            scoped,
            operation_filter=ops,
        )

        if daily.empty:
            return None

        total = float(daily["value"].sum())
        avg = float(daily["value"].mean())
        peak = float(daily["value"].max())

        full_months = get_full_months(
            date_range.start,
            date_range.end,
            today=now_kyiv(),
        )

        if full_months:
            tf = df[
                df["month"].isin(full_months)
                & df["operation"].isin(ops)
            ][
                [
                    "month",
                    "operation",
                    "sum_true",
                    "sum_false",
                ]
            ].drop_duplicates()

            tf = tf.dropna(
                subset=["sum_true", "sum_false"]
            )

            if not tf.empty:
                s_true = float(tf["sum_true"].sum())
                s_false = float(tf["sum_false"].sum())
                total_tf = s_true + s_false
                rate = (
                    s_true / total_tf * 100
                    if total_tf > 0
                    else None
                )
            else:
                rate = None
        else:
            rate = None

        return {
            "total": total,
            "avg": avg,
            "peak": peak,
            "rate": rate,
            "days": date_range.days,
            "observed_days": int(daily["date"].nunique()),
        }

    metrics_a = build_period_metrics(
        range_a,
        cmp_ops,
    )
    metrics_b = build_period_metrics(
        range_b,
        cmp_ops,
    )

    if metrics_a is None or metrics_b is None:
        st.warning(
            "Немає фактичних даних для "
            "одного з обраних періодів."
        )
    else:
        def fmt_delta_pct(a_val, b_val):
            if (
                a_val is None
                or b_val is None
                or a_val == 0
            ):
                return "—"

            return (
                f"{(b_val - a_val) / a_val * 100:+.1f}%"
            )

        rows = [
            {
                "Метрика": "Період (календарних днів)",
                "A": metrics_a["days"],
                "B": metrics_b["days"],
                "Δ": metrics_b["days"] - metrics_a["days"],
                "Δ %": "—",
            },
            {
                "Метрика": "Днів з даними",
                "A": metrics_a["observed_days"],
                "B": metrics_b["observed_days"],
                "Δ": (
                    metrics_b["observed_days"]
                    - metrics_a["observed_days"]
                ),
                "Δ %": "—",
            },
            {
                "Метрика": "Всього операцій",
                "A": f"{metrics_a['total']:,.0f}",
                "B": f"{metrics_b['total']:,.0f}",
                "Δ": (
                    f"{metrics_b['total'] - metrics_a['total']:+,.0f}"
                ),
                "Δ %": fmt_delta_pct(
                    metrics_a["total"],
                    metrics_b["total"],
                ),
            },
            {
                "Метрика": "Середнє за фактичний день",
                "A": f"{metrics_a['avg']:.1f}",
                "B": f"{metrics_b['avg']:.1f}",
                "Δ": (
                    f"{metrics_b['avg'] - metrics_a['avg']:+.1f}"
                ),
                "Δ %": fmt_delta_pct(
                    metrics_a["avg"],
                    metrics_b["avg"],
                ),
            },
            {
                "Метрика": "Пік за день",
                "A": f"{metrics_a['peak']:,.0f}",
                "B": f"{metrics_b['peak']:,.0f}",
                "Δ": (
                    f"{metrics_b['peak'] - metrics_a['peak']:+,.0f}"
                ),
                "Δ %": fmt_delta_pct(
                    metrics_a["peak"],
                    metrics_b["peak"],
                ),
            },
            {
                "Метрика": "Коефіцієнт погоджень, %",
                "A": (
                    f"{metrics_a['rate']:.1f}%"
                    if metrics_a["rate"] is not None
                    else "— (немає повних місяців)"
                ),
                "B": (
                    f"{metrics_b['rate']:.1f}%"
                    if metrics_b["rate"] is not None
                    else "— (немає повних місяців)"
                ),
                "Δ": (
                    f"{metrics_b['rate'] - metrics_a['rate']:+.1f} п.п."
                    if (
                        metrics_a["rate"] is not None
                        and metrics_b["rate"] is not None
                    )
                    else "—"
                ),
                "Δ %": "—",
            },
        ]

        st.dataframe(
            pd.DataFrame(rows),
            use_container_width=True,
            hide_index=True,
        )

        # Нормалізоване порівняння — не змішуємо total/avg/peak
        # на одному axis.
        chart_total = pd.DataFrame(
            {
                "Період": ["A", "B"],
                "Всього": [
                    metrics_a["total"],
                    metrics_b["total"],
                ],
            }
        )

        fig_total_cmp = px.bar(
            chart_total,
            x="Період",
            y="Всього",
            text_auto=".0f",
            title="Всього операцій",
        )

        fig_total_cmp.update_layout(
            height=300,
            margin=dict(l=10, r=10, t=40, b=10),
        )

        st.plotly_chart(
            fig_total_cmp,
            use_container_width=True,
        )

        chart_daily = pd.DataFrame(
            {
                "Період": ["A", "B"],
                "Середнє": [
                    metrics_a["avg"],
                    metrics_b["avg"],
                ],
                "Пік": [
                    metrics_a["peak"],
                    metrics_b["peak"],
                ],
            }
        )

        chart_daily_long = chart_daily.melt(
            id_vars="Період",
            var_name="Метрика",
            value_name="Значення",
        )

        fig_daily_cmp = px.bar(
            chart_daily_long,
            x="Метрика",
            y="Значення",
            color="Період",
            barmode="group",
            text_auto=".1f",
            title="Середнє та пік",
        )

        fig_daily_cmp.update_layout(
            height=320,
            margin=dict(l=10, r=10, t=40, b=10),
        )

        st.plotly_chart(
            fig_daily_cmp,
            use_container_width=True,
        )


# ============================================================
# 18. Footer
# ============================================================

st.caption(
    "Джерело: Google Sheets • Кеш даних: 5 хвилин • Час: Europe/Kyiv • "
    "Порожні дні не трактуються як нуль у статистиці."
)
