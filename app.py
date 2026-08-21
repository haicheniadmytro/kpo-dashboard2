import re
from datetime import datetime
from pathlib import Path

import gspread
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
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

    df_raw = pd.DataFrame(records)

    if df_raw.empty:
        raise ValueError("Не знайдено деталізованих даних у Google Таблиці.")

    date_metadata = df_raw[["date", "year", "month", "month_name", "weekday", "is_weekend"]].drop_duplicates("date")

    df_grouped = (
        df_raw.groupby(["date", "operation"], as_index=False)["value"]
        .sum()
        .sort_values(["date", "operation"])
    )

    df = df_grouped.merge(date_metadata, on="date", how="left")

    total = (
        df.groupby("date", as_index=False)["value"]
        .sum()
        .assign(operation="Тотал")
    )
    total = total.merge(date_metadata, on="date", how="left")
    df = pd.concat([df, total], ignore_index=True)

    return df


# --- Функції для розрахунку додаткових метрик ---

def calc_peak_min_avg(df):
    """Розраховує пік, мінімум, середнє за день та співвідношення пік/середнє."""
    daily = df.groupby("date")["value"].sum()
    if daily.empty:
        return 0, 0, 0, 0
    peak = daily.max()
    min_val = daily.min()
    avg = daily.mean()
    peak_avg_ratio = peak / avg if avg > 0 else 0
    return peak, min_val, avg, peak_avg_ratio


def calc_busiest_weekday(df):
    """Повертає день тижня з найбільшою середньою кількістю операцій."""
    if df.empty:
        return None, None
    daily = df.groupby("date")["value"].sum().reset_index()
    daily["weekday"] = daily["date"].dt.day_name()
    weekday_avg = daily.groupby("weekday")["value"].mean()
    busiest = weekday_avg.idxmax()
    busiest_val = weekday_avg.max()
    return busiest, busiest_val


def calc_busiest_operation(df):
    """Повертає операцію з найбільшою сумарною кількістю."""
    if df.empty or df["operation"].nunique() == 0:
        return None, None
    ops = df[df["operation"] != "Тотал"]
    if ops.empty:
        return None, None
    total_by_op = ops.groupby("operation")["value"].sum()
    busiest_op = total_by_op.idxmax()
    busiest_val = total_by_op.max()
    return busiest_op, busiest_val


def calc_stability(df):
    """Розраховує коефіцієнт варіації (CV) для денних сум."""
    daily = df.groupby("date")["value"].sum()
    if daily.empty:
        return 0, 0, 0, "Немає даних"
    mean = daily.mean()
    std = daily.std()
    cv = (std / mean * 100) if mean > 0 else 0
    if cv < 15:
        interpretation = "🟢 Низька варіативність"
    elif cv < 30:
        interpretation = "🟡 Середня варіативність"
    else:
        interpretation = "🔴 Висока варіативність"
    return mean, std, cv, interpretation


def detect_anomalies(df, window=7, threshold=2.0):
    """
    Визначає аномальні дні на основі ковзного середнього та std.
    Повертає df з доданими колонками: rolling_avg, rolling_std, z_score, is_anomaly.
    """
    if df.empty:
        return pd.DataFrame()
    daily = df.groupby("date")["value"].sum().reset_index()
    daily = daily.sort_values("date")
    if len(daily) < window:
        # Якщо даних менше за вікно, повертаємо порожній df
        return pd.DataFrame()
    daily["rolling_avg"] = daily["value"].rolling(window=window, min_periods=1, center=True).mean()
    daily["rolling_std"] = daily["value"].rolling(window=window, min_periods=1, center=True).std().fillna(0)
    daily["z_score"] = (daily["value"] - daily["rolling_avg"]) / daily["rolling_std"].replace(0, 1)
    daily["is_anomaly"] = abs(daily["z_score"]) > threshold
    return daily


def forecast_scenarios(df, current_month):
    """
    Базовий, мінімальний та оптимістичний прогноз для поточного місяця.
    Повертає словник зі сценаріями.
    """
    if df.empty or current_month not in df["month"].values:
        return None

    # Дані тільки для поточного місяця
    month_data = df[df["month"] == current_month]
    # Дні, які вже минули (факт)
    today = pd.Timestamp.now().normalize()
    days_passed = (today - pd.Timestamp(year=today.year, month=today.month, day=1)).days + 1
    # Якщо поточний місяць не є поточним календарним, або ми вже минули місяць – повертаємо None
    if today.month != pd.Period(current_month).month or today.year != pd.Period(current_month).year:
        return None

    # Фактичні дні поточного місяця (тільки ті, що пройшли)
    fact_days = month_data[month_data["date"].dt.day <= days_passed]
    if fact_days.empty:
        return None

    # Сума за минулі дні
    fact_sum = fact_days["value"].sum()
    # Середнє за минулі дні
    avg_fact = fact_sum / days_passed

    # Кількість днів у місяці
    total_days = pd.Period(current_month).days_in_month

    # Базовий прогноз: середнє * загальна кількість днів
    base_forecast = avg_fact * total_days

    # Мінімальний сценарій: мінімальне значення за минулі дні * кількість днів, що залишилися + факт
    daily_values = fact_days.groupby("date")["value"].sum()
    min_daily = daily_values.min() if not daily_values.empty else avg_fact
    max_daily = daily_values.max() if not daily_values.empty else avg_fact

    remaining_days = total_days - days_passed
    min_forecast = fact_sum + min_daily * remaining_days if remaining_days > 0 else fact_sum
    max_forecast = fact_sum + max_daily * remaining_days if remaining_days > 0 else fact_sum

    return {
        "fact": fact_sum,
        "days_passed": days_passed,
        "avg_fact": avg_fact,
        "base_forecast": base_forecast,
        "min_forecast": min_forecast,
        "max_forecast": max_forecast,
        "total_days": total_days,
        "daily_values": daily_values,  # для графіка
    }


# --- Завантаження та фільтри ---

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

# --- Sidebar фільтри ---
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

# --- Вибір операцій: мультиселект + перемикач Тотал / Операції ---
operation_mode = st.sidebar.radio(
    "Режим показу",
    options=["Тотал", "Вибрані операції"],
    index=0,
)

all_ops = [op for op in OPERATIONS if op in df["operation"].unique()]
if operation_mode == "Тотал":
    selected_operations = ["Тотал"]
else:
    selected_operations = st.sidebar.multiselect(
        "Операції",
        options=all_ops,
        default=all_ops[:3] if all_ops else [],
    )
    if not selected_operations:
        st.sidebar.warning("Виберіть хоча б одну операцію.")
        selected_operations = ["Тотал"]  # fallback

# Згладжування
st.sidebar.divider()
st.sidebar.subheader("Налаштування графіків")
smooth_enabled = st.sidebar.checkbox("Згладжування динаміки (ковзне середнє)", value=False)
smooth_window = 7
if smooth_enabled:
    smooth_window = st.sidebar.selectbox("Вікно згладжування (дні)", [3, 5, 7, 14], index=2)

# Фільтруємо дані для обраних операцій
if operation_mode == "Тотал":
    filtered = df[
        df["year"].isin(selected_years)
        & df["month"].isin(selected_months)
        & (df["operation"] == "Тотал")
    ].copy()
else:
    filtered = df[
        df["year"].isin(selected_years)
        & df["month"].isin(selected_months)
        & (df["operation"].isin(selected_operations))
    ].copy()

if filtered.empty:
    st.warning("За вибраними фільтрами даних немає.")
    st.stop()

# --- Розрахунок базових метрик (для всього періоду) ---
daily_total = filtered.groupby("date")["value"].sum()
total_value = daily_total.sum()
daily_avg = daily_total.mean()
peak = daily_total.max() if not daily_total.empty else 0
min_val = daily_total.min() if not daily_total.empty else 0
peak_avg_ratio = peak / daily_avg if daily_avg > 0 else 0

busiest_weekday, busiest_weekday_val = calc_busiest_weekday(filtered)
busiest_op, busiest_op_val = calc_busiest_operation(filtered)
mean, std, cv, cv_interp = calc_stability(filtered)

# --- Прогноз (тільки якщо вибрано рівно один місяць і режим "Тотал") ---
forecast = None
if len(selected_months) == 1 and operation_mode == "Тотал":
    forecast_data = forecast_scenarios(df[df["operation"] == "Тотал"], selected_months[0])
    if forecast_data:
        forecast = forecast_data

# --- Порівняння (тільки для одного місяця, для "Тотал") ---
comparison_parts = []
if len(selected_months) == 1 and operation_mode == "Тотал":
    current_period = pd.Period(selected_months[0])
    today = pd.Timestamp.now().normalize()

    prev_period = current_period - 1
    if current_period.end_time <= today:
        cur_sum = daily_total.sum()
        prev_sum = df[
            (df["month"] == str(prev_period))
            & (df["operation"] == "Тотал")
        ]["value"].sum()
        delta_prev = ((cur_sum - prev_sum) / prev_sum * 100) if prev_sum > 0 else None
    else:
        day_limit = today.day
        cur_sum = filtered[filtered["date"].dt.day <= day_limit]["value"].sum()
        days_in_prev = prev_period.days_in_month
        day_limit_prev = min(day_limit, days_in_prev)
        prev_sum = df[
            (df["month"] == str(prev_period))
            & (df["operation"] == "Тотал")
            & (df["date"].dt.day <= day_limit_prev)
        ]["value"].sum()
        delta_prev = ((cur_sum - prev_sum) / prev_sum * 100) if prev_sum > 0 else None

    if delta_prev is not None:
        comparison_parts.append(f"Попер. міс: {delta_prev:+.1f}%")

    # До аналогічного місяця минулого року
    year_prev = current_period.year - 1
    month_num = current_period.month
    prev_year_period = pd.Period(year=year_prev, month=month_num, freq="M")
    has_prev_year = not df[
        (df["month"] == str(prev_year_period))
        & (df["operation"] == "Тотал")
    ].empty

    if has_prev_year:
        if current_period.end_time <= today:
            cur_sum = daily_total.sum()
            prev_year_sum = df[
                (df["month"] == str(prev_year_period))
                & (df["operation"] == "Тотал")
            ]["value"].sum()
            delta_year = ((cur_sum - prev_year_sum) / prev_year_sum * 100) if prev_year_sum > 0 else None
        else:
            day_limit = today.day
            cur_sum = filtered[filtered["date"].dt.day <= day_limit]["value"].sum()
            days_in_prev_year = prev_year_period.days_in_month
            day_limit_prev_year = min(day_limit, days_in_prev_year)
            prev_year_sum = df[
                (df["month"] == str(prev_year_period))
                & (df["operation"] == "Тотал")
                & (df["date"].dt.day <= day_limit_prev_year)
            ]["value"].sum()
            delta_year = ((cur_sum - prev_year_sum) / prev_year_sum * 100) if prev_year_sum > 0 else None
        if delta_year is not None:
            comparison_parts.append(f"Мин. рік: {delta_year:+.1f}%")

comparison_text = "  ".join(comparison_parts) if comparison_parts else "—"

# --- Створення вкладок ---
tab1, tab2, tab3, tab4 = st.tabs(["📊 Overview", "📈 Динаміка", "🧩 Операції", "📅 Навантаження"])

# ============================================================
# TAB 1: OVERVIEW
# ============================================================
with tab1:
    # --- KPI рядок ---
    col1, col2, col3, col4, col5, col6 = st.columns(6)

    with col1:
        st.metric("Всього", f"{total_value:,.0f}")

    with col2:
        st.metric("Середнє за день", f"{daily_avg:.1f}")

    with col3:
        if forecast:
            st.metric("Прогноз на місяць", f"{forecast['base_forecast']:,.0f}")
        else:
            st.metric("Прогноз на місяць", "—")

    with col4:
        st.metric("Пік за день", f"{peak:,.0f}")

    with col5:
        st.metric("Мінімум за день", f"{min_val:,.0f}")

    with col6:
        st.metric("Пік / середнє", f"{peak_avg_ratio:.2f}×")

    # Другий рядок KPI
    col7, col8, col9, col10 = st.columns(4)

    with col7:
        if busiest_weekday:
            days_ua = {
                "Monday": "Пн", "Tuesday": "Вт", "Wednesday": "Ср",
                "Thursday": "Чт", "Friday": "Пт", "Saturday": "Сб", "Sunday": "Нд"
            }
            day_ua = days_ua.get(busiest_weekday, busiest_weekday)
            st.metric("Найактивніший день", f"{day_ua} — {busiest_weekday_val:.0f}/день")

    with col8:
        if busiest_op:
            st.metric("Найактивніша операція", f"{busiest_op} — {busiest_op_val:,.0f}")

    with col9:
        if len(selected_months) == 1 and operation_mode == "Тотал":
            st.metric("Порівняння", comparison_text)
        else:
            st.metric("Порівняння", "—")

    with col10:
        st.metric("Стабільність (CV)", f"{cv:.1f}%", help="Коефіцієнт варіації: <15% - низька, 15-30% - середня, >30% - висока варіативність")

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
            color_discrete_sequence=["blue"],
        )
        if smooth_enabled:
            daily["value_smooth"] = daily["value"].rolling(window=smooth_window, min_periods=1, center=True).mean()
            fig_overview.add_scatter(
                x=daily["date"],
                y=daily["value_smooth"],
                mode="lines",
                name=f"Ковзне середнє ({smooth_window} дн.)",
                line=dict(color="orange", width=3),
            )
        anomalies = detect_anomalies(filtered, window=7, threshold=2.0)
        if not anomalies.empty:
            anomaly_points = anomalies[anomalies["is_anomaly"]]
            if not anomaly_points.empty:
                fig_overview.add_scatter(
                    x=anomaly_points["date"],
                    y=anomaly_points["value"],
                    mode="markers",
                    marker=dict(color="red", size=10, symbol="x"),
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

    fig_overview.update_layout(
        height=420,
        hovermode="x unified",
        margin=dict(l=10, r=10, t=20, b=10),
    )
    st.plotly_chart(fig_overview, use_container_width=True)

    # --- Прогноз (якщо доступний) ---
    if forecast:
        st.subheader("📊 Прогноз на поточний місяць")
        col1, col2, col3, col4 = st.columns(4)
        col1.metric("Факт (на сьогодні)", f"{forecast['fact']:,.0f}")
        col2.metric("Базовий прогноз", f"{forecast['base_forecast']:,.0f}")
        col3.metric("Мінімальний", f"{forecast['min_forecast']:,.0f}")
        col4.metric("Оптимістичний", f"{forecast['max_forecast']:,.0f}")

        # Показуємо накопичувальний графік факту + прогнозу
        # Беремо фактичні денні суми
        fact_daily = filtered[filtered["date"].dt.day <= forecast["days_passed"]]
        fact_daily = fact_daily.groupby("date")["value"].sum().reset_index()
        fact_daily = fact_daily.sort_values("date")
        # Накопичена сума факту
        fact_daily["cumulative"] = fact_daily["value"].cumsum()

        # Прогнозовані дні (з завтра до кінця місяця)
        last_fact_date = fact_daily["date"].max() if not fact_daily.empty else pd.Timestamp.now().normalize()
        future_dates = pd.date_range(
            start=last_fact_date + pd.Timedelta(days=1),
            end=pd.Timestamp(year=pd.Timestamp.now().year, month=pd.Timestamp.now().month, day=forecast["total_days"]),
            freq="D"
        )
        if len(future_dates) > 0:
            # Прогнозовані накопичені суми: починаємо з останньої фактичної накопиченої суми
            last_cum = fact_daily["cumulative"].iloc[-1] if not fact_daily.empty else 0
            # Середнє денне значення для прогнозу (можна використовувати avg_fact)
            avg_daily = forecast["avg_fact"]
            forecast_cum = []
            cum = last_cum
            for _ in future_dates:
                cum += avg_daily
                forecast_cum.append(cum)
            forecast_df = pd.DataFrame({
                "date": future_dates,
                "cumulative": forecast_cum,
                "type": "Прогноз"
            })
            fact_df = fact_daily[["date", "cumulative"]].copy()
            fact_df["type"] = "Факт"
            combined = pd.concat([fact_df, forecast_df], ignore_index=True)
            fig_forecast = px.line(
                combined,
                x="date",
                y="cumulative",
                color="type",
                markers=True,
                labels={"date": "Дата", "cumulative": "Накопичена кількість", "type": ""},
                color_discrete_map={"Факт": "blue", "Прогноз": "orange"}
            )
            fig_forecast.update_layout(height=380, margin=dict(l=10, r=10, t=20, b=10))
            st.plotly_chart(fig_forecast, use_container_width=True)
        else:
            st.info("Місяць уже завершено, прогноз недоступний.")

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
        if smooth_enabled:
            daily["value_smooth"] = daily["value"].rolling(window=smooth_window, min_periods=1, center=True).mean()
            fig_daily_detailed.add_scatter(
                x=daily["date"],
                y=daily["value_smooth"],
                mode="lines",
                name=f"Ковзне середнє ({smooth_window} дн.)",
                line=dict(color="orange", width=3),
            )
        anomalies = detect_anomalies(filtered, window=7, threshold=2.0)
        if not anomalies.empty:
            anomaly_points = anomalies[anomalies["is_anomaly"]]
            if not anomaly_points.empty:
                fig_daily_detailed.add_scatter(
                    x=anomaly_points["date"],
                    y=anomaly_points["value"],
                    mode="markers",
                    marker=dict(color="red", size=10, symbol="x"),
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

    fig_daily_detailed.update_layout(
        height=400,
        hovermode="x unified",
        margin=dict(l=10, r=10, t=20, b=10),
    )
    st.plotly_chart(fig_daily_detailed, use_container_width=True)

    # YoY порівняння (тільки для Тотал)
    if operation_mode == "Тотал":
        st.subheader("📊 Порівняння по роках (YoY)")
        yoy_data = df[df["operation"] == "Тотал"].copy()
        yoy_data = yoy_data[yoy_data["year"].isin(selected_years)]
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
            labels={"month_label": "Місяць", "value": "Кількість", "year": "Рік"},
            title="Порівняння місячних сум по роках"
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

    # Накопичувальна сума
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
    fig_cum.update_layout(height=380, margin=dict(l=10, r=10, t=20, b=10))
    st.plotly_chart(fig_cum, use_container_width=True)

    # Таблиця аномальних днів
    st.subheader("🔍 Аномальні дні")
    anomalies = detect_anomalies(filtered, window=7, threshold=2.0)
    if not anomalies.empty:
        anomaly_points = anomalies[anomalies["is_anomaly"]]
        if not anomaly_points.empty:
            anomaly_points = anomaly_points.copy()
            anomaly_points["date_str"] = anomaly_points["date"].dt.strftime("%d.%m.%Y")
            anomaly_points["deviation"] = ((anomaly_points["value"] - anomaly_points["rolling_avg"]) / anomaly_points["rolling_avg"] * 100).round(1)
            anomaly_points["type"] = anomaly_points["deviation"].apply(lambda x: "🔴 Високий" if x > 0 else "🔵 Низький")
            anomaly_points = anomaly_points.sort_values("date", ascending=False)
            st.dataframe(
                anomaly_points[["date_str", "value", "rolling_avg", "deviation", "type"]],
                column_config={
                    "date_str": "Дата",
                    "value": "Значення",
                    "rolling_avg": "Середнє (вікно)",
                    "deviation": "Відхилення, %",
                    "type": "Тип"
                },
                use_container_width=True,
                hide_index=True
            )
        else:
            st.info("Аномальних днів не виявлено.")
    else:
        st.info("Недостатньо даних для виявлення аномалій (потрібно щонайменше 7 днів).")

# ============================================================
# TAB 3: ОПЕРАЦІЇ
# ============================================================
with tab3:
    st.subheader("🧩 Аналіз операцій")

    # Перевірка, чи є дані про операції (не "Тотал")
    ops_data = filtered[filtered["operation"] != "Тотал"]
    if ops_data.empty:
        st.info("Немає даних про окремі операції для вибраного періоду.")
    else:
        # 1. Структура операцій
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
        fig_ops_structure.update_layout(height=360, margin=dict(l=10, r=10, t=20, b=10))
        st.plotly_chart(fig_ops_structure, use_container_width=True)

        # 2. Stacked chart по місяцях
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

        # 3. Pareto аналіз
        st.subheader("📊 Pareto аналіз операцій")
        pareto_data = ops_structure.copy().sort_values("value", ascending=False)
        pareto_data["cumulative_percent"] = pareto_data["percent"].cumsum()

        fig_pareto = go.Figure()
        fig_pareto.add_trace(go.Bar(
            x=pareto_data["operation"],
            y=pareto_data["value"],
            name="Кількість",
            marker_color="steelblue",
            yaxis="y",
        ))
        fig_pareto.add_trace(go.Scatter(
            x=pareto_data["operation"],
            y=pareto_data["cumulative_percent"],
            name="Накопичувальна частка, %",
            mode="lines+markers",
            marker_color="red",
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

        # 4. Порівняння вибраних операцій (якщо вибрано кілька)
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
            fig_compare_ops.update_layout(height=400, margin=dict(l=10, r=10, t=20, b=10))
            st.plotly_chart(fig_compare_ops, use_container_width=True)

# ============================================================
# TAB 4: НАВАНТАЖЕННЯ
# ============================================================
with tab4:
    st.subheader("📅 Аналіз навантаження")

    # 1. Середнє навантаження по днях тижня
    st.subheader("📊 Середнє навантаження за днями тижня")
    daily_sum = filtered.groupby("date")["value"].sum().reset_index()
    daily_sum["weekday_ua"] = daily_sum["date"].dt.day_name().map({
        "Monday": "Пн", "Tuesday": "Вт", "Wednesday": "Ср",
        "Thursday": "Чт", "Friday": "Пт", "Saturday": "Сб", "Sunday": "Нд"
    })
    order = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Нд"]
    weekday_avg = daily_sum.groupby("weekday_ua")["value"].mean().reindex(order).reset_index()
    weekday_avg.columns = ["weekday", "avg_value"]
    weekday_avg["avg_value"] = weekday_avg["avg_value"].fillna(0)

    fig_weekday_avg = px.bar(
        weekday_avg,
        x="weekday",
        y="avg_value",
        text=weekday_avg["avg_value"].round(1).astype(str),
        labels={"weekday": "День тижня", "avg_value": "Середня кількість"},
        title="Середня кількість операцій по днях тижня"
    )
    fig_weekday_avg.update_traces(textposition="outside")
    fig_weekday_avg.update_layout(height=360, margin=dict(l=10, r=10, t=20, b=10))
    st.plotly_chart(fig_weekday_avg, use_container_width=True)

    # 2. Будні vs вихідні
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

    # 3. Теплова карта навантаження (Місяць × День тижня)
    st.subheader("🌡️ Теплова карта навантаження")
    daily_heat = filtered.groupby("date")["value"].sum().reset_index()
    daily_heat["month_label"] = daily_heat["date"].dt.strftime("%m.%Y")
    daily_heat["weekday_ua"] = daily_heat["date"].dt.day_name().map({
        "Monday": "Пн", "Tuesday": "Вт", "Wednesday": "Ср",
        "Thursday": "Чт", "Friday": "Пт", "Saturday": "Сб", "Sunday": "Нд"
    })
    heat_data = daily_heat.groupby(["month_label", "weekday_ua"])["value"].mean().reset_index()
    heat_pivot = heat_data.pivot(index="month_label", columns="weekday_ua", values="value").fillna(0)
    order = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Нд"]
    heat_pivot = heat_pivot.reindex(columns=order)
    # Сортуємо індекси (місяці) за датою
    heat_pivot.index = pd.PeriodIndex(heat_pivot.index, freq="M")
    heat_pivot = heat_pivot.sort_index()
    # Встановлюємо мітки для відображення
    heat_pivot.index = heat_pivot.index.strftime("%m.%Y")

    fig_heatmap = px.imshow(
        heat_pivot,
        text_auto=".1f",
        aspect="auto",
        labels=dict(x="День тижня", y="Місяць", color="Середня кількість"),
        color_continuous_scale="Blues",
    )
    fig_heatmap.update_layout(height=450, margin=dict(l=10, r=10, t=20, b=10))
    st.plotly_chart(fig_heatmap, use_container_width=True)

    # 4. Стабільність навантаження (CV)
    st.subheader("📊 Стабільність навантаження")
    col1, col2, col3 = st.columns(3)
    col1.metric("Середнє за день", f"{mean:.1f}" if mean > 0 else "—")
    col2.metric("Стандартне відхилення", f"{std:.1f}" if std > 0 else "—")
    col3.metric("Коефіцієнт варіації (CV)", f"{cv:.1f}%" if cv > 0 else "—",
                help="Коефіцієнт варіації: <15% - низька, 15-30% - середня, >30% - висока варіативність")

    # 5. Пік / середнє
    st.subheader("📈 Співвідношення пік / середнє")
    st.metric("Пік / середнє", f"{peak_avg_ratio:.2f}×" if peak_avg_ratio > 0 else "—",
              help="У скільки разів максимальне денне значення перевищує середнє")

st.caption("Джерело: Google Sheets • Оновлення даних: до 5 хвилин після зміни таблиці.")
