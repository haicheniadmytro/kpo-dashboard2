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
    scopes = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
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
            # Знаходимо початок деталізованих операцій
            detail_start = None
            for r in range(header_row + 1, min(header_row + 30, len(values))):
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
                    # Дані починаються з колонки D (індекс 3)
                    col = 3 + day_idx
                    value = row[col] if col < len(row) else ""
                    date = pd.Timestamp(year=year, month=month, day=day_idx + 1)

                    # TRUE – парні колонки (D, F, H, ...) => day_idx парний
                    is_true_col = (day_idx % 2 == 0)

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
                            "is_true_col": is_true_col,
                        }
                    )

    df_raw = pd.DataFrame(records)

    if df_raw.empty:
        raise ValueError("Не знайдено деталізованих даних у Google Таблиці.")

    # Метадані для дат
    date_metadata = df_raw[["date", "year", "month", "month_name", "weekday", "is_weekend"]].drop_duplicates("date")

    # Групуємо за датою та операцією
    df_grouped = (
        df_raw.groupby(["date", "operation"], as_index=False)["value"]
        .sum()
        .sort_values(["date", "operation"])
    )

    df = df_grouped.merge(date_metadata, on="date", how="left")

    # Додаємо "Тотал"
    total = (
        df.groupby("date", as_index=False)["value"]
        .sum()
        .assign(operation="Тотал")
    )
    total = total.merge(date_metadata, on="date", how="left")
    df = pd.concat([df, total], ignore_index=True)

    # Агрегуємо суми TRUE і FALSE
    true_false_agg = (
        df_raw.groupby(["date", "operation", "is_true_col"])["value"]
        .sum()
        .unstack(fill_value=0)
        .reset_index()
    )
    # Перейменовуємо колонки: is_true_col=False -> sum_false, is_true_col=True -> sum_true
    true_false_agg.columns = ["date", "operation", "sum_false", "sum_true"]
    # Якщо якоїсь колонки немає, додаємо
    if "sum_true" not in true_false_agg.columns:
        true_false_agg["sum_true"] = 0
    if "sum_false" not in true_false_agg.columns:
        true_false_agg["sum_false"] = 0

    # Додаємо суми TRUE/FALSE до основного df
    df = df.merge(true_false_agg, on=["date", "operation"], how="left")
    df["sum_true"] = df["sum_true"].fillna(0)
    df["sum_false"] = df["sum_false"].fillna(0)

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

if filtered.empty:
    st.warning("За вибраними фільтрами даних немає.")
    st.stop()

# --- Розрахунок основних метрик ---
total_value = filtered["value"].sum()

# Середнє за день (всі дні)
daily_avg = filtered.groupby("date")["value"].sum().mean()

# Середнє за будні (is_weekend == False)
daily_avg_weekday = filtered[filtered["is_weekend"] == False].groupby("date")["value"].sum().mean()
# Середнє за вихідні (is_weekend == True)
daily_avg_weekend = filtered[filtered["is_weekend"] == True].groupby("date")["value"].sum().mean()

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
        ]["value"].sum()
        delta_prev = metric_delta(cur_sum, prev_sum)

    if delta_prev is not None:
        comparison_parts.append(f"Попер. міс: {format_delta(delta_prev)}")

    # До аналогічного місяця минулого року
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
            ]["value"].sum()
            delta_year = metric_delta(cur_sum, prev_year_sum)
        if delta_year is not None:
            comparison_parts.append(f"Мин. рік: {format_delta(delta_year)}")

comparison_text = "  ".join(comparison_parts) if comparison_parts else "—"

# --- Коефіцієнт погоджень (sum_true / (sum_true + sum_false)) ---
sum_true_total = filtered["sum_true"].sum()
sum_false_total = filtered["sum_false"].sum()
total_ratio = sum_true_total + sum_false_total
approval_rate = (sum_true_total / total_ratio * 100) if total_ratio > 0 else 0

# Підготовка рядків для середніх
avg_all = f"{daily_avg:.1f}" if not pd.isna(daily_avg) else "—"
avg_wd = f"{daily_avg_weekday:.1f}" if not pd.isna(daily_avg_weekday) else "—"
avg_we = f"{daily_avg_weekend:.1f}" if not pd.isna(daily_avg_weekend) else "—"

# Відображення KPI (5 колонок)
c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("Всього", f"{total_value:,.0f}")

with c2:
    st.markdown("**Середнє за день**")
    st.markdown(
        f"""
        <div style='font-size:0.85rem; line-height:1.6;'>
            Всі дні: {avg_all}<br>
            Будні: {avg_wd}<br>
            Вихідні: {avg_we}
        </div>
        """,
        unsafe_allow_html=True
    )

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
c5.metric(
    "Коефіцієнт погоджень",
    f"{approval_rate:.1f}%" if total_ratio > 0 else "—",
    help="Частка TRUE (парні колонки) від загальної кількості (TRUE+FALSE) за вибраний період"
)

# --- ДІАГНОСТИКА ---
with st.expander("🔍 Деталі розрахунку коефіцієнта погоджень"):
    st.write(f"**Сума TRUE (парні колонки):** {sum_true_total:.0f}")
    st.write(f"**Сума FALSE (непарні колонки):** {sum_false_total:.0f}")
    st.write(f"**Загальна сума (TRUE+FALSE):** {total_ratio:.0f}")
    st.write(f"**Коефіцієнт:** {approval_rate:.1f}%")
    st.write("---")
    st.write("**Перевірка: чи сума TRUE+FALSE дорівнює загальній сумі?**")
    st.write(f"Сума value: {filtered['value'].sum():.0f}")
    st.write(f"Сума TRUE+FALSE: {total_ratio:.0f}")
    if abs(filtered['value'].sum() - total_ratio) < 0.01:
        st.success("✅ Суми збігаються!")
    else:
        st.warning("⚠️ Суми не збігаються – можливо, є операції без TRUE/FALSE розбивки.")

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

# --- ДОДАТКОВІ ВІЗУАЛІЗАЦІЇ ---
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

# 3. Порівняння двох місяців (якщо вибрано рівно 2 місяці)
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

st.caption("Джерело: Google Sheets • Оновлення даних: до 5 хвилин після зміни таблиці.")
