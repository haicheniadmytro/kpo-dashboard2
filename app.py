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
            detail_start = None
            for r in range(header_row + 1, min(header_row + 20, len(values))):
                if len(values[r]) > 3 and values[r][3] in OPERATIONS:
                    detail_start = r
                    break

            if detail_start is None:
                continue

            days = pd.Period(f"{year}-{month:02d}").days_in_month

            for r in range(detail_start, len(values)):
                if r >= len(values):
                    break

                operation = values[r][3] if len(values[r]) > 3 else ""
                operation = ALIASES.get(operation, operation)

                if operation not in OPERATIONS:
                    break

                row = values[r]
                for day_idx in range(days):
                    col = 4 + day_idx  # E = index 4
                    value = row[col] if col < len(row) else ""
                    date = pd.Timestamp(year=year, month=month, day=day_idx + 1)

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

    df["year"] = df["date"].dt.year
    df["month"] = df["date"].dt.to_period("M").astype(str)
    df["month_name"] = df["date"].dt.strftime("%m.%Y")
    df["weekday"] = df["date"].dt.day_name()
    df["is_weekend"] = df["date"].dt.weekday >= 5

    total = (
        df.groupby("date", as_index=False)["value"]
        .sum()
        .assign(operation="Тотал")
    )
    total["year"] = total["date"].dt.year
    total["month"] = total["date"].dt.to_period("M").astype(str)
    total["month_name"] = total["date"].dt.strftime("%m.%Y")
    total["weekday"] = total["date"].dt.day_name()
    total["is_weekend"] = total["date"].dt.weekday >= 5

    df = pd.concat([df, total], ignore_index=True)
    return df


def metric_delta(current, previous):
    if previous == 0:
        return None
    return (current / previous - 1) * 100


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

# Smoothing options
st.sidebar.divider()
st.sidebar.subheader("Налаштування графіків")
smooth_enabled = st.sidebar.checkbox("Згладжування динаміки (ковзне середнє)", value=False)
smooth_window = 7
if smooth_enabled:
    smooth_window = st.sidebar.selectbox("Вікно згладжування (дні)", [3, 5, 7, 14], index=2)

# Filter data
filtered = df[
    df["year"].isin(selected_years)
    & df["month"].isin(selected_months)
    & (df["operation"] == selected_operation)
].copy()

if filtered.empty:
    st.warning("За вибраними фільтрами даних немає.")
    st.stop()

# KPI
total_value = filtered["value"].sum()
daily_avg = filtered.groupby("date")["value"].sum().mean()
daily_max = filtered.groupby("date")["value"].sum().max()
daily_min = filtered.groupby("date")["value"].sum().min()

previous_period = None
if len(selected_months) == 1:
    current_period = pd.Period(selected_months[0])
    previous_period = current_period - 1
    prev_key = str(previous_period)
    prev = df[
        (df["month"] == prev_key)
        & (df["operation"] == selected_operation)
    ]["value"].sum()
    current = filtered["value"].sum()
    delta = metric_delta(current, prev)
else:
    delta = None

c1, c2, c3, c4 = st.columns(4)
c1.metric("Всього", f"{total_value:,.0f}")
c2.metric("Середнє за день", f"{daily_avg:,.1f}")
c3.metric("Максимум за день", f"{daily_max:,.0f}")
c4.metric(
    "До попереднього місяця",
    f"{delta:+.1f}%" if delta is not None else "—",
)

st.divider()

# Daily trend
st.subheader(f"📈 Динаміка: {selected_operation}")

daily = (
    filtered.groupby("date", as_index=False)["value"]
    .sum()
    .sort_values("date")
)

fig_daily = px.line(
    daily,
    x="date",
    y="value",
    markers=True,
    labels={"date": "Дата", "value": "Кількість"},
)
if smooth_enabled:
    # Додаємо згладжену лінію (ковзне середнє)
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
    # Відсотки
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
st.subheader("📅 Будні vs вихідні")

week = (
    filtered.assign(
        period_type=filtered["is_weekend"].map(
            {False: "Будні", True: "Вихідні"}
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
        "period_type": "",
    },
)
fig_week.update_layout(
    height=380,
    margin=dict(l=10, r=10, t=20, b=10),
)
st.plotly_chart(fig_week, use_container_width=True)

# --- НОВІ ВІЗУАЛІЗАЦІЇ ---
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

# 3. Боксплот за днями тижня
with st.expander("📦 Розподіл за днями тижня (Boxplot)"):
    box_data = filtered.groupby(["date", "weekday"], as_index=False)["value"].sum()
    box_data["weekday_ua"] = box_data["weekday"].map(days_ua)
    order = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Нд"]
    fig_box = px.box(
        box_data,
        x="weekday_ua",
        y="value",
        category_orders={"weekday_ua": order},
        labels={"weekday_ua": "День тижня", "value": "Кількість"},
        color="weekday_ua",
        color_discrete_sequence=px.colors.qualitative.Set2,
    )
    fig_box.update_layout(height=380, margin=dict(l=10, r=10, t=20, b=10), showlegend=False)
    st.plotly_chart(fig_box, use_container_width=True)

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

# Detailed table
with st.expander("🔎 Показати дані"):
    table = filtered[["date", "operation", "value"]].copy()
    table["date"] = table["date"].dt.strftime("%d.%m.%Y")
    st.dataframe(
        table.sort_values("date", ascending=False),
        use_container_width=True,
        hide_index=True,
    )

st.caption("Джерело: Google Sheets • Оновлення даних: до 5 хвилин після зміни таблиці.")
