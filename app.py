import re
from datetime import datetime
from pathlib import Path

import gspread
import pandas as pd
import plotly.express as px
import streamlit as st
from google.oauth2.service_account import Credentials


st.set_page_config(
    page_title="KPO Dashboard",
    page_icon="📊",
    layout="wide",
)

SPREADSHEET_ID = "1STX1vgDAk3zVDshXdZmTgJJSvQNCN4WmmftOskwymYI"
SHEETS = ["24", "25", "26"]

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


def as_number(value):
    """Convert Google Sheets values to numeric values.
    Empty cells and booleans are treated as zero because the source
    sheet uses FALSE/TRUE in cells where a count may be absent.
    """
    if value is None or value == "":
        return 0.0
    if isinstance(value, bool):
        return 0.0
    try:
        return float(str(value).replace(",", "."))
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
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets.readonly",
    ]
    credentials = Credentials.from_service_account_info(
        dict(st.secrets["gcp_service_account"]),
        scopes=scopes,
    )
    return gspread.authorize(credentials)


@st.cache_data(ttl=300, show_spinner=False)
def load_data():
    client = get_client()
    spreadsheet = client.open_by_key(SPREADSHEET_ID)

    records = []

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

        for month_idx, (header_row, month, year) in enumerate(month_rows):
            # Знаходимо рядок з TRUE/FALSE
            bool_row_idx = None
            for r in range(header_row + 1, min(header_row + 20, len(values))):
                row = values[r]
                bool_vals = [v for v in row[4:] if v and v.strip() in ("TRUE", "FALSE", "True", "False")]
                if len(bool_vals) > 0 and len(bool_vals) == len(row[4:]):
                    bool_row_idx = r
                    break

            day_type_flags = None
            if bool_row_idx is not None:
                bool_row = values[bool_row_idx]
                day_type_flags = []
                for idx, val in enumerate(bool_row[4:]):
                    if val and val.strip().lower() == "true":
                        day_type_flags.append(True)
                    elif val and val.strip().lower() == "false":
                        day_type_flags.append(False)
                    else:
                        day_type_flags.append(None)
            else:
                day_type_flags = None

            # Знаходимо початок деталізованих операцій
            detail_start = None
            for r in range(header_row + 1, min(header_row + 30, len(values))):
                if r == bool_row_idx:
                    continue
                if len(values[r]) > 3 and values[r][3] in OPERATIONS:
                    detail_start = r
                    break

            if detail_start is None:
                continue

            days = pd.Period(f"{year}-{month:02d}").days_in_month

            if day_type_flags is not None and len(day_type_flags) < days:
                day_type_flags = day_type_flags + [None] * (days - len(day_type_flags))
            elif day_type_flags is not None and len(day_type_flags) > days:
                day_type_flags = day_type_flags[:days]

            for r in range(detail_start, len(values)):
                if r >= len(values):
                    break

                operation = values[r][3] if len(values[r]) > 3 else ""
                operation = ALIASES.get(operation, operation)

                if operation not in OPERATIONS:
                    break

                row = values[r]
                for day_idx in range(days):
                    col = 4 + day_idx
                    value = row[col] if col < len(row) else ""
                    date = pd.Timestamp(year=year, month=month, day=day_idx + 1)

                    day_flag = None
                    if day_type_flags is not None and day_idx < len(day_type_flags):
                        day_flag = day_type_flags[day_idx]

                    if day_flag is None:
                        day_flag = date.weekday() < 5

                    records.append(
                        {
                            "date": date,
                            "operation": operation,
                            "value": as_number(value),
                            "year": year,
                            "month": date.strftime("%Y-%m"),
                            "month_name": date.strftime("%b %Y"),
                            "weekday": date.day_name(),
                            "is_weekend": date.weekday() >= 5,
                            "day_type_flag": day_flag,
                        }
                    )

    df = pd.DataFrame(records)

    if df.empty:
        raise ValueError("Не знайдено деталізованих даних у Google Таблиці.")

    df = (
        df.groupby(["date", "operation"], as_index=False)["value"]
        .sum()
        .sort_values(["date", "operation"])
    )

    df = df.merge(
        df[["date", "year", "month", "month_name", "weekday", "is_weekend", "day_type_flag"]].drop_duplicates("date"),
        on="date",
        how="left"
    )

    total = (
        df.groupby("date", as_index=False)["value"]
        .sum()
        .assign(operation="Тотал")
    )
    total = total.merge(
        df[["date", "year", "month", "month_name", "weekday", "is_weekend", "day_type_flag"]].drop_duplicates("date"),
        on="date",
        how="left"
    )
    df = pd.concat([df, total], ignore_index=True)

    df["day_type_flag"] = df["day_type_flag"].astype(bool)

    return df


def metric_delta(current, previous):
    if previous == 0:
        return None
    return (current / previous - 1) * 100


def format_delta(delta):
    if delta is None:
        return "—"
    return f"{delta:+.1f}%"


st.title("📊 Dashboard погоджень КПО")
st.caption("Дані завантажуються напряму з Google Таблиці. Кеш оновлюється кожні 5 хвилин.")

try:
    df = load_data()
except Exception as exc:
    st.error("Не вдалося завантажити Google Таблицю.")
    st.code(str(exc))
    st.info(
        "Перевір: 1) чи увімкнений Google Sheets API, "
        "2) чи надано service account доступ до таблиці, "
        "3) чи правильно додані secrets у Streamlit."
    )
    st.stop()

# Sidebar filters
st.sidebar.header("Фільтри")

years = sorted(df["year"].unique())
selected_years = st.sidebar.multiselect(
    "Рік",
    options=years,
    default=years,
)

available_months = (
    df[df["year"].isin(selected_years)]["month"]
    .drop_duplicates()
    .sort_values()
    .tolist()
)

selected_months = st.sidebar.multiselect(
    "Місяць",
    options=available_months,
    default=available_months,
    format_func=lambda x: pd.Period(x).strftime("%m.%Y"),
)

operation_options = ["Тотал"] + [x for x in OPERATIONS if x in df["operation"].unique()]
selected_operation = st.sidebar.selectbox(
    "Показник",
    options=operation_options,
    index=0,
)

day_type_options = ["Всі", "TRUE", "FALSE"]
selected_day_type = st.sidebar.selectbox(
    "Тип дня (з таблиці)",
    options=day_type_options,
    index=0,
)

st.sidebar.divider()
st.sidebar.subheader("Налаштування графіків")
smooth_enabled = st.sidebar.checkbox("Згладжування динаміки (ковзне середнє)", value=False)
smooth_window = 7
if smooth_enabled:
    smooth_window = st.sidebar.selectbox("Вікно згладжування (дні)", [3, 5, 7, 14], index=2)

# Фільтруємо дані
filtered = df[
    df["year"].isin(selected_years)
    & df["month"].isin(selected_months)
    & (df["operation"] == selected_operation)
].copy()

if selected_day_type != "Всі":
    flag_val = True if selected_day_type == "TRUE" else False
    filtered = filtered[filtered["day_type_flag"] == flag_val]

if filtered.empty:
    st.warning("За вибраними фільтрами даних немає.")
    st.stop()

# --- Розрахунок основних метрик ---
total_value = filtered["value"].sum()
daily_avg = filtered.groupby("date")["value"].sum().mean()

forecast = None
if len(selected_months) == 1:
    current_period = pd.Period(selected_months[0])
    today = pd.Timestamp.now().normalize()
    if current_period.start_time <= today < current_period.end_time:
        days_passed = (today - current_period.start_time).days + 1
        if days_passed > 0:
            sum_so_far = filtered[filtered["date"].dt.day <= days_passed]["value"].sum()
            avg_so_far = sum_so_far / days_passed
            forecast = avg_so_far * current_period.days_in_month

# Порівняння (якщо вибрано рівно 1 місяць)
comparison_parts = []
if len(selected_months) == 1:
    current_period = pd.Period(selected_months[0])
    today = pd.Timestamp.now().normalize()

    prev_period = current_period - 1
    if current_period.end_time <= today:
        cur_sum = filtered["value"].sum()
        prev_sum = df[
            (df["month"] == str(prev_period))
            & (df["operation"] == selected_operation)
            & (df["day_type_flag"].isin(filtered["day_type_flag"].unique()) if selected_day_type != "Всі" else True)
        ]["value"].sum()
        delta_prev = metric_delta(cur_sum, prev_sum)
    else:
        day_limit = today.day
        cur_sum = filtered[filtered["date"].dt.day <= day_limit]["value"].sum()
        days_in_prev = prev_period.days_in_month
        day_limit_prev = min(day_limit, days_in_prev)
        prev_sum = df[
            (df["month"] == str(prev_period))
            & (df["operation"] == selected_operation)
            & (df["date"].dt.day <= day_limit_prev)
            & (df["day_type_flag"].isin(filtered["day_type_flag"].unique()) if selected_day_type != "Всі" else True)
        ]["value"].sum()
        delta_prev = metric_delta(cur_sum, prev_sum)

    if delta_prev is not None:
        comparison_parts.append(f"Попер. міс: {format_delta(delta_prev)}")

    year_prev = current_period.year - 1
    month_num = current_period.month
    prev_year_period = pd.Period(year=year_prev, month=month_num, freq="M")
    has_prev_year = not df[
        (df["month"] == str(prev_year_period))
        & (df["operation"] == selected_operation)
    ].empty

    if has_prev_year:
        if current_period.end_time <= today:
            cur_sum = filtered["value"].sum()
            prev_year_sum = df[
                (df["month"] == str(prev_year_period))
                & (df["operation"] == selected_operation)
                & (df["day_type_flag"].isin(filtered["day_type_flag"].unique()) if selected_day_type != "Всі" else True)
            ]["value"].sum()
            delta_year = metric_delta(cur_sum, prev_year_sum)
        else:
            day_limit = today.day
            cur_sum = filtered[filtered["date"].dt.day <= day_limit]["value"].sum()
            days_in_prev_year = prev_year_period.days_in_month
            day_limit_prev_year = min(day_limit, days_in_prev_year)
            prev_year_sum = df[
                (df["month"] == str(prev_year_period))
                & (df["operation"] == selected_operation)
                & (df["date"].dt.day <= day_limit_prev_year)
                & (df["day_type_flag"].isin(filtered["day_type_flag"].unique()) if selected_day_type != "Всі" else True)
            ]["value"].sum()
            delta_year = metric_delta(cur_sum, prev_year_sum)
        if delta_year is not None:
            comparison_parts.append(f"Мин. рік: {format_delta(delta_year)}")

comparison_text = "  ".join(comparison_parts) if comparison_parts else "—"

# --- Коефіцієнт погоджень (TRUE / (TRUE+FALSE)) ---
# Для цього нам потрібні дані без фільтра за типом дня (але з урахуванням вибраного показника)
df_for_ratio = df[
    df["year"].isin(selected_years)
    & df["month"].isin(selected_months)
    & (df["operation"] == selected_operation)
].copy()

# Якщо вибрано конкретний тип дня – рахуємо тільки для нього, інакше для всіх
if selected_day_type != "Всі":
    flag_val = True if selected_day_type == "TRUE" else False
    df_for_ratio = df_for_ratio[df_for_ratio["day_type_flag"] == flag_val]

# Суми TRUE і FALSE за вибраний період
sum_true = df_for_ratio[df_for_ratio["day_type_flag"] == True]["value"].sum()
sum_false = df_for_ratio[df_for_ratio["day_type_flag"] == False]["value"].sum()
total_ratio_sum = sum_true + sum_false
approval_rate = (sum_true / total_ratio_sum * 100) if total_ratio_sum > 0 else 0

# --- Середнє для TRUE/FALSE (якщо вибрано "Всі") ---
avg_true = None
avg_false = None
if selected_day_type == "Всі":
    daily_true = filtered[filtered["day_type_flag"] == True].groupby("date")["value"].sum().mean()
    daily_false = filtered[filtered["day_type_flag"] == False].groupby("date")["value"].sum().mean()
    avg_true = daily_true if not pd.isna(daily_true) else None
    avg_false = daily_false if not pd.isna(daily_false) else None

# Відображення KPI (тепер 6 колонок)
c1, c2, c3, c4, c5, c6 = st.columns(6)
c1.metric("Всього", f"{total_value:,.0f}")
c2.metric("Середнє за день", f"{daily_avg:,.1f}")
c3.metric(
    "Прогноз на місяць",
    f"{forecast:,.0f}" if forecast is not None else "—",
    help="Прогноз на поточний місяць, розрахований на основі середнього за дні, що минули"
)
with c4:
    st.markdown("**Порівняння**")
    if comparison_text != "—":
        parts = comparison_text.split("  ")
        for part in parts:
            st.markdown(
                f"<p style='font-size:0.85rem; margin:0; line-height:1.4;'>{part}</p>",
                unsafe_allow_html=True
            )
    else:
        st.markdown(
            "<p style='font-size:0.85rem; margin:0;'>—</p>",
            unsafe_allow_html=True
        )
with c5:
    st.markdown("**Середнє за типом**")
    if selected_day_type == "Всі":
        true_str = f"{avg_true:.1f}" if avg_true is not None else "—"
        false_str = f"{avg_false:.1f}" if avg_false is not None else "—"
        st.markdown(
            f"<p style='font-size:0.85rem; margin:0; line-height:1.4;'>TRUE: {true_str}</p>",
            unsafe_allow_html=True
        )
        st.markdown(
            f"<p style='font-size:0.85rem; margin:0; line-height:1.4;'>FALSE: {false_str}</p>",
            unsafe_allow_html=True
        )
    else:
        st.markdown(
            f"<p style='font-size:0.85rem; margin:0;'>{selected_day_type} обрано</p>",
            unsafe_allow_html=True
        )
with c6:
    st.metric(
        "Коефіцієнт погоджень",
        f"{approval_rate:.1f}%",
        help="Частка TRUE від загальної кількості (TRUE+FALSE) за вибраний період"
    )

st.divider()

# Daily trend
st.subheader(f"📈 Динаміка: {selected_operation}")

daily = (
    filtered.groupby("date", as_index=False)["value"]
    .sum()
    .sort_values("date")
)

if selected_day_type == "Всі":
    daily_with_type = filtered.groupby(["date", "day_type_flag"], as_index=False)["value"].sum().sort_values("date")
    fig_daily = px.line(
        daily_with_type,
        x="date",
        y="value",
        color="day_type_flag",
        color_map={True: "green", False: "red"},
        labels={"date": "Дата", "value": "Кількість", "day_type_flag": "Тип дня"},
        markers=True,
    )
    fig_daily.add_scatter(
        x=daily["date"],
        y=daily["value"],
        mode="lines",
        name="Загалом",
        line=dict(color="blue", width=2, dash="dash"),
    )
else:
    fig_daily = px.line(
        daily,
        x="date",
        y="value",
        markers=True,
        labels={"date": "Дата", "value": "Кількість"},
        color_discrete_sequence=["blue"],
    )

if smooth_enabled and selected_day_type != "Всі":
    daily_smooth = daily.copy()
    daily_smooth["value_smooth"] = daily_smooth["value"].rolling(
        window=smooth_window, min_periods=1, center=True
    ).mean()
    fig_daily.add_scatter(
        x=daily_smooth["date"],
        y=daily_smooth["value_smooth"],
        mode="lines",
        name=f"Ковзне середнє ({smooth_window} дн.)",
        line=dict(color="orange", width=3),
    )

fig_daily.update_layout(
    height=420,
    hovermode="x unified",
    margin=dict(l=10, r=10, t=20, b=10),
)
st.plotly_chart(fig_daily, use_container_width=True)

# Two-column section
left, right = st.columns(2)

with left:
    st.subheader("📊 Порівняння місяців")
    monthly = (
        filtered.groupby("month", as_index=False)["value"]
        .sum()
        .sort_values("month")
    )
    monthly["month_label"] = monthly["month"].apply(
        lambda x: pd.Period(x).strftime("%m.%Y")
    )
    total_monthly = monthly["value"].sum()
    monthly["percent"] = (monthly["value"] / total_monthly * 100).round(1)
    monthly["text"] = monthly["percent"].astype(str) + "%"

    fig_month = px.bar(
        monthly,
        x="month_label",
        y="value",
        text="text",
        labels={"month_label": "Місяць", "value": "Кількість"},
    )
    fig_month.update_traces(textposition="outside")
    fig_month.update_layout(
        height=360,
        margin=dict(l=10, r=10, t=20, b=10),
    )
    st.plotly_chart(fig_month, use_container_width=True)

with right:
    st.subheader("🧩 Структура операцій")
    mix = (
        df[
            df["year"].isin(selected_years)
            & df["month"].isin(selected_months)
            & (df["operation"] != "Тотал")
            & (df["day_type_flag"].isin(filtered["day_type_flag"].unique()) if selected_day_type != "Всі" else True)
        ]
        .groupby("operation", as_index=False)["value"]
        .sum()
        .sort_values("value", ascending=False)
    )
    total_ops = mix["value"].sum()
    mix["percent"] = (mix["value"] / total_ops * 100).round(1)
    mix["text"] = mix["percent"].astype(str) + "%"

    fig_mix = px.bar(
        mix,
        x="value",
        y="operation",
        text="text",
        orientation="h",
        labels={"value": "Кількість", "operation": "Операція"},
    )
    fig_mix.update_traces(textposition="outside")
    fig_mix.update_layout(
        height=360,
        margin=dict(l=10, r=10, t=20, b=10),
        yaxis={"categoryorder": "total ascending"},
    )
    st.plotly_chart(fig_mix, use_container_width=True)

# Weekday/weekend
st.subheader("📅 Будні vs вихідні (на основі мітки з таблиці)")

week = (
    filtered.assign(
        period_type=filtered["day_type_flag"].map(
            {True: "TRUE", False: "FALSE"}
        )
    )
    .groupby(["month", "period_type"], as_index=False)["value"]
    .mean()
)

week["month_label"] = week["month"].apply(
    lambda x: pd.Period(x).strftime("%m.%Y")
)

fig_week = px.bar(
    week,
    x="month_label",
    y="value",
    color="period_type",
    barmode="group",
    labels={
        "month_label": "Місяць",
        "value": "Середнє за день",
        "period_type": "Тип дня",
    },
)
fig_week.update_layout(
    height=380,
    margin=dict(l=10, r=10, t=20, b=10),
)
st.plotly_chart(fig_week, use_container_width=True)

# --- НОВИЙ БЛОК: Аналітика за рішеннями (TRUE/FALSE) ---
st.divider()
st.subheader("📊 Аналітика за рішеннями (TRUE – погоджено, FALSE – відмовлено)")

# 1. Динаміка коефіцієнта погоджень
with st.expander("📈 Динаміка коефіцієнта погоджень", expanded=True):
    # Для цього графіка нам потрібні денні суми TRUE і FALSE без фільтра за типом дня
    daily_ratio_data = df[
        df["year"].isin(selected_years)
        & df["month"].isin(selected_months)
        & (df["operation"] == selected_operation)
    ].copy()

    # Якщо вибрано конкретний тип дня – показуємо тільки його, але тоді коефіцієнт буде 100% або 0%
    # Тому для цього графіка ігноруємо фільтр типу дня, щоб показувати реальний коефіцієнт
    if selected_day_type != "Всі":
        # Якщо вибрано конкретний тип – попереджаємо, що графік показує загальний коефіцієнт
        st.info("Графік показує загальний коефіцієнт погоджень (без урахування фільтра типу дня)")

    # Групуємо по днях, обчислюємо суми TRUE і FALSE
    daily_agg = daily_ratio_data.groupby("date").agg(
        sum_true=("value", lambda x: x[daily_ratio_data["day_type_flag"] == True].sum()),
        sum_false=("value", lambda x: x[daily_ratio_data["day_type_flag"] == False].sum())
    ).reset_index()
    daily_agg["total"] = daily_agg["sum_true"] + daily_agg["sum_false"]
    daily_agg["approval_rate"] = (daily_agg["sum_true"] / daily_agg["total"] * 100).fillna(0)

    fig_ratio = px.line(
        daily_agg,
        x="date",
        y="approval_rate",
        markers=True,
        labels={"date": "Дата", "approval_rate": "Коефіцієнт погоджень, %"},
        title="Динаміка коефіцієнта погоджень (TRUE / (TRUE+FALSE))"
    )
    fig_ratio.update_layout(
        height=380,
        hovermode="x unified",
        margin=dict(l=10, r=10, t=20, b=10),
    )
    fig_ratio.add_hline(y=50, line_dash="dash", line_color="gray", annotation_text="50%")
    st.plotly_chart(fig_ratio, use_container_width=True)

# 2. Погодження vs Відмови по місяцях
with st.expander("📊 Погодження vs Відмови по місяцях", expanded=True):
    monthly_decision = df[
        df["year"].isin(selected_years)
        & df["month"].isin(selected_months)
        & (df["operation"] == selected_operation)
    ].copy()

    # Якщо вибрано конкретний тип дня – показуємо тільки його (але тоді один стовпець буде порожнім)
    if selected_day_type != "Всі":
        flag_val = True if selected_day_type == "TRUE" else False
        monthly_decision = monthly_decision[monthly_decision["day_type_flag"] == flag_val]

    monthly_decision = (
        monthly_decision.groupby(["month", "day_type_flag"], as_index=False)["value"]
        .sum()
        .sort_values("month")
    )
    monthly_decision["month_label"] = monthly_decision["month"].apply(
        lambda x: pd.Period(x).strftime("%m.%Y")
    )
    monthly_decision["day_type_flag"] = monthly_decision["day_type_flag"].map({True: "Погоджено", False: "Відмовлено"})

    fig_decision = px.bar(
        monthly_decision,
        x="month_label",
        y="value",
        color="day_type_flag",
        barmode="group",
        color_map={"Погоджено": "green", "Відмовлено": "red"},
        labels={"month_label": "Місяць", "value": "Кількість", "day_type_flag": "Рішення"},
    )
    fig_decision.update_layout(
        height=380,
        margin=dict(l=10, r=10, t=20, b=10),
    )
    st.plotly_chart(fig_decision, use_container_width=True)

# 3. Структура операцій з розбивкою за рішенням
with st.expander("🧩 Структура операцій за рішенням (TRUE/FALSE)", expanded=True):
    ops_decision = df[
        df["year"].isin(selected_years)
        & df["month"].isin(selected_months)
        & (df["operation"] != "Тотал")
    ].copy()

    # Якщо вибрано конкретний тип дня – фільтруємо
    if selected_day_type != "Всі":
        flag_val = True if selected_day_type == "TRUE" else False
        ops_decision = ops_decision[ops_decision["day_type_flag"] == flag_val]

    ops_decision = (
        ops_decision.groupby(["operation", "day_type_flag"], as_index=False)["value"]
        .sum()
        .sort_values(["operation", "day_type_flag"])
    )
    ops_decision["day_type_flag"] = ops_decision["day_type_flag"].map({True: "Погоджено", False: "Відмовлено"})

    # Стовпчаста діаграма з угрупованням (або стекова – обираємо з угрупованням для кращої читаності)
    fig_ops_dec = px.bar(
        ops_decision,
        x="operation",
        y="value",
        color="day_type_flag",
        barmode="group",
        color_map={"Погоджено": "green", "Відмовлено": "red"},
        labels={"operation": "Операція", "value": "Кількість", "day_type_flag": "Рішення"},
    )
    fig_ops_dec.update_layout(
        height=400,
        margin=dict(l=10, r=10, t=20, b=10),
    )
    st.plotly_chart(fig_ops_dec, use_container_width=True)

# --- ДОДАТКОВІ ВІЗУАЛІЗАЦІЇ (залишаємо без змін) ---
st.divider()
st.subheader("➕ Додаткові аналітичні графіки")

# 1. Теплова карта: день тижня × місяць (середнє)
with st.expander("🌡️ Теплова карта «День тижня × Місяць»"):
    heat_data = filtered.groupby(["month", "weekday"], as_index=False)["value"].mean()
    heat_pivot = heat_data.pivot(index="month", columns="weekday", values="value").fillna(0)
    weekdays_order = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    days_ua = {
        "Monday": "Пн", "Tuesday": "Вт", "Wednesday": "Ср",
        "Thursday": "Чт", "Friday": "Пт", "Saturday": "Сб", "Sunday": "Нд"
    }
    heat_pivot = heat_pivot.reindex(columns=weekdays_order)
    heat_pivot.columns = [days_ua[col] for col in heat_pivot.columns]
    heat_pivot.index = pd.PeriodIndex(heat_pivot.index, freq="M")
    heat_pivot = heat_pivot.sort_index()
    heat_pivot.index = heat_pivot.index.strftime("%m.%Y")

    fig_heat = px.imshow(
        heat_pivot,
        text_auto=".1f",
        aspect="auto",
        labels=dict(x="День тижня", y="Місяць", color="Середнє"),
        color_continuous_scale="Blues",
    )
    fig_heat.update_layout(height=400, margin=dict(l=10, r=10, t=20, b=10))
    st.plotly_chart(fig_heat, use_container_width=True)

# 2. Накопичувальна сума
with st.expander("📈 Накопичувальна сума за період"):
    cumsum = filtered.groupby("date", as_index=False)["value"].sum().sort_values("date")
    cumsum["cumulative"] = cumsum["value"].cumsum()
    fig_cum = px.line(
        cumsum,
        x="date",
        y="cumulative",
        markers=True,
        labels={"date": "Дата", "cumulative": "Накопичена кількість"},
    )
    fig_cum.update_layout(height=380, margin=dict(l=10, r=10, t=20, b=10))
    st.plotly_chart(fig_cum, use_container_width=True)

# 3. Боксплот за днями тижня (замінено на violin plot)
with st.expander("🎻 Розподіл за днями тижня (Violin plot)"):
    box_data = filtered.groupby(["date", "weekday"], as_index=False)["value"].sum()
    box_data["weekday_ua"] = box_data["weekday"].map(days_ua)
    order = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Нд"]
    fig_violin = px.violin(
        box_data,
        x="weekday_ua",
        y="value",
        category_orders={"weekday_ua": order},
        labels={"weekday_ua": "День тижня", "value": "Кількість"},
        color="weekday_ua",
        color_discrete_sequence=px.colors.qualitative.Set2,
        box=True,
        points="all",
    )
    fig_violin.update_layout(height=380, margin=dict(l=10, r=10, t=20, b=10), showlegend=False)
    st.plotly_chart(fig_violin, use_container_width=True)

# 4. Порівняння двох місяців (якщо вибрано рівно 2 місяці)
if len(selected_months) == 2:
    with st.expander("📊 Порівняння двох місяців (денна динаміка)"):
        two_months = filtered[filtered["month"].isin(selected_months)].copy()
        two_months["month_label"] = two_months["month"].apply(
            lambda x: pd.Period(x).strftime("%m.%Y")
        )
        daily_two = (
            two_months.groupby(["date", "month_label"], as_index=False)["value"]
            .sum()
            .sort_values("date")
        )
        fig_two = px.line(
            daily_two,
            x="date",
            y="value",
            color="month_label",
            markers=True,
            labels={"date": "Дата", "value": "Кількість", "month_label": "Місяць"},
        )
        fig_two.update_layout(height=400, margin=dict(l=10, r=10, t=20, b=10))
        st.plotly_chart(fig_two, use_container_width=True)

# Видалено блок "Показати дані"

st.caption("Джерело: Google Sheets • Оновлення даних: до 5 хвилин після зміни таблиці.")
