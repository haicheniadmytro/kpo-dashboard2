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
    daily = df.groupby("date")["value"].sum()
    if daily.empty:
        return 0, 0, 0, 0
    peak = daily.max()
    min_val = daily.min()
    avg = daily.mean()
    peak_avg_ratio = peak / avg if avg > 0 else 0
    return peak, min_val, avg, peak_avg_ratio


def calc_busiest_weekday(df):
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
    ops = df[df["operation"] != "Тотал"]
    if ops.empty:
        return None, None
    total_by_op = ops.groupby("operation")["value"].sum()
    busiest_op = total_by_op.idxmax()
    busiest_val = total_by_op.max()
    return busiest_op, busiest_val


def calc_stability(df, daily_avg):
    daily = df.groupby("date")["value"].sum()
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


def forecast_scenarios(df, current_month):
    if df.empty or current_month not in df["month"].values:
        return None

    month_data = df[df["month"] == current_month]
    today = pd.Timestamp.now().normalize()
    days_passed = (today - pd.Timestamp(year=today.year, month=today.month, day=1)).days + 1

    if today.month != pd.Period(current_month).month or today.year != pd.Period(current_month).year:
        return None

    fact_days = month_data[month_data["date"].dt.day <= days_passed]
    if fact_days.empty:
        return None

    daily_sums = fact_days.groupby("date")["value"].sum()
    fact_sum = daily_sums.sum()
    avg_daily = daily_sums.mean()
    std_daily = daily_sums.std()

    total_days = pd.Period(current_month).days_in_month

    base_forecast = avg_daily * total_days
    min_forecast = max(0, (avg_daily - 0.5 * std_daily) * total_days)
    max_forecast = (avg_daily + 0.5 * std_daily) * total_days

    return {
        "fact": fact_sum,
        "days_passed": days_passed,
        "total_days": total_days,
        "avg_daily": avg_daily,
        "std_daily": std_daily,
        "base_forecast": base_forecast,
        "min_forecast": min_forecast,
        "max_forecast": max_forecast,
        "daily_sums": daily_sums,
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

# --- Обробка поточного незавершеного місяця ---
today = pd.Timestamp.now().normalize()
if len(selected_months) == 1:
    period = pd.Period(selected_months[0])
    if period.start_time <= today <= period.end_time:
        filtered = filtered[filtered["date"] <= today]
        num_days = (today - period.start_time).days + 1
    else:
        num_days = period.days_in_month
else:
    num_days = sum(pd.Period(m).days_in_month for m in selected_months)

if filtered.empty:
    st.warning("За вибраними фільтрами даних немає (можливо, ще немає даних за цей місяць).")
    st.stop()

# --- Розрахунок базових метрик ---
daily_total = filtered.groupby("date")["value"].sum()
total_value = daily_total.sum()
daily_avg = total_value / num_days if num_days > 0 else 0

peak = daily_total.max() if not daily_total.empty else 0
min_val = daily_total.min() if not daily_total.empty else 0
peak_avg_ratio = peak / daily_avg if daily_avg > 0 else 0

busiest_weekday, busiest_weekday_val = calc_busiest_weekday(filtered)
busiest_op, busiest_op_val = calc_busiest_operation(filtered)
std, cv, cv_interp = calc_stability(filtered, daily_avg)

# --- Прогноз ---
forecast = None
if len(selected_months) == 1 and operation_mode == "Тотал":
    forecast_data = forecast_scenarios(df[df["operation"] == "Тотал"], selected_months[0])
    if forecast_data:
        forecast = forecast_data

# --- Порівняння ---
comparison_parts = []
if len(selected_months) == 1 and operation_mode == "Тотал":
    current_period = pd.Period(selected_months[0])
    today_comp = pd.Timestamp.now().normalize()

    prev_period = current_period - 1
    if current_period.end_time <= today_comp:
        cur_sum = daily_total.sum()
        prev_sum = df[
            (df["month"] == str(prev_period))
            & (df["operation"] == "Тотал")
        ]["value"].sum()
        delta_prev = ((cur_sum - prev_sum) / prev_sum * 100) if prev_sum > 0 else None
    else:
        day_limit = today_comp.day
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
            prev_year_sum = df[
                (df["month"] == str(prev_year_period))
                & (df["operation"] == "Тотал")
            ]["value"].sum()
            delta_year = ((cur_sum - prev_year_sum) / prev_year_sum * 100) if prev_year_sum > 0 else None
        else:
            day_limit = today_comp.day
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


# --- Допоміжна функція для кастомних метрик (збільшені шрифти) ---
def custom_metric(label, value, help_text=None):
    help_icon = f'<span class="help-icon" title="{help_text}">?</span>' if help_text else ""
    return f"""
    <div class="metric-container">
        <div class="metric-label">{label} {help_icon}</div>
        <div class="metric-value">{value}</div>
    </div>
    """


# --- CSS для кастомних метрик (збільшені шрифти, чорний колір) ---
st.markdown("""
<style>
    .metric-container {
        background: transparent;
        padding: 0.3rem 0;
        border: none;
    }
    .metric-label {
        font-size: 0.8rem !important;
        color: #262730 !important;
        font-weight: 400;
        letter-spacing: 0.02em;
        margin-bottom: 0.15rem;
    }
    .metric-value {
        font-size: 1.3rem !important;
        font-weight: 600;
        color: #262730 !important;
        line-height: 1.2;
    }
    .help-icon {
        display: inline-block;
        background: rgba(49, 51, 63, 0.15);
        border-radius: 50%;
        width: 16px;
        height: 16px;
        text-align: center;
        line-height: 16px;
        font-size: 0.65rem;
        color: #262730;
        cursor: help;
        margin-left: 3px;
    }
    .comparison-text {
        font-size: 0.8rem !important;
        line-height: 1.3 !important;
        margin: 0 !important;
        color: #262730 !important;
    }
</style>
""", unsafe_allow_html=True)

# --- Створення вкладок ---
tab1, tab2, tab3, tab4 = st.tabs(["📊 Overview", "📈 Динаміка", "🧩 Операції", "📅 Навантаження"])

# ============================================================
# TAB 1: OVERVIEW
# ============================================================
with tab1:
    # Рядок 1
    col1, col2, col3, col4, col5, col6 = st.columns(6)

    with col1:
        st.markdown(custom_metric("Всього", f"{total_value:,.0f}", "Загальна кількість операцій за вибраний період"), unsafe_allow_html=True)

    with col2:
        st.markdown(custom_metric("Середнє за день", f"{daily_avg:.0f}", f"Сумарна кількість поділена на {num_days} календарних днів у вибраному періоді"), unsafe_allow_html=True)

    with col3:
        if forecast:
            val = f"{forecast['base_forecast']:,.0f}"
            help_txt = "Базовий прогноз на поточний місяць"
        else:
            val = "—"
            help_txt = None
        st.markdown(custom_metric("Прогноз на місяць", val, help_txt), unsafe_allow_html=True)

    with col4:
        st.markdown(custom_metric("Пік за день", f"{peak:,.0f}", "Найбільша кількість операцій за один день (лише фактичні дні)"), unsafe_allow_html=True)

    with col5:
        st.markdown(custom_metric("Мінімум за день", f"{min_val:,.0f}", "Найменша кількість операцій за один день (лише фактичні дні)"), unsafe_allow_html=True)

    with col6:
        st.markdown(custom_metric("Пік / середнє", f"{peak_avg_ratio:.2f}×", "У скільки разів пік перевищує середнє"), unsafe_allow_html=True)

    # Рядок 2
    col7, col8, col9, col10 = st.columns(4)

    with col7:
        if busiest_weekday:
            days_ua = {
                "Monday": "Пн", "Tuesday": "Вт", "Wednesday": "Ср",
                "Thursday": "Чт", "Friday": "Пт", "Saturday": "Сб", "Sunday": "Нд"
            }
            day_ua = days_ua.get(busiest_weekday, busiest_weekday)
            val = f"{day_ua} — {busiest_weekday_val:.0f}/день"
            help_txt = "День тижня з найвищим середнім навантаженням (лише фактичні дні)"
        else:
            val = "—"
            help_txt = None
        st.markdown(custom_metric("Найактивніший день", val, help_txt), unsafe_allow_html=True)

    with col8:
        if busiest_op:
            display_name = busiest_op if len(busiest_op) <= 12 else busiest_op[:10] + "…"
            val = f'{display_name} — {busiest_op_val:,.0f}'
            help_txt = f"{busiest_op} — {busiest_op_val:,.0f} (повна назва)"
        else:
            val = "—"
            help_txt = None
        st.markdown(custom_metric("Найактивніша операція", val, help_txt), unsafe_allow_html=True)

    with col9:
        if len(selected_months) == 1 and operation_mode == "Тотал":
            st.markdown("**Порівняння**")
            if comparison_text != "—":
                parts = comparison_text.split("  ")
                for part in parts:
                    st.markdown(
                        f"<p class='comparison-text'>{part}</p>",
                        unsafe_allow_html=True
                    )
            else:
                st.markdown(
                    "<p class='comparison-text'>—</p>",
                    unsafe_allow_html=True
                )
        else:
            st.markdown(custom_metric("Порівняння", "—", "Доступно лише для одного місяця в режимі 'Тотал'"), unsafe_allow_html=True)

    with col10:
        st.markdown(custom_metric("Стабільність (CV)", f"{cv:.1f}%" if cv > 0 else "—", "Коефіцієнт варіації (лише фактичні дні)"), unsafe_allow_html=True)

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
        fig_overview.update_xaxes(tickformat="%d.%m", title_text="Дата")
        if smooth_enabled:
            daily["value_smooth"] = daily["value"].rolling(window=smooth_window, min_periods=1, center=True).mean()
            fig_overview.add_scatter(
                x=daily["date"],
                y=daily["value_smooth"],
                mode="lines",
                name=f"Ковзне середнє ({smooth_window} дн.)",
                line=dict(color="orange", width=3),
            )
        anomalies = detect_anomalies(filtered, window=14, threshold=1.5)
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
        fig_overview.update_xaxes(tickformat="%d.%m", title_text="Дата")

    fig_overview.update_layout(
        height=420,
        hovermode="x unified",
        margin=dict(l=10, r=10, t=20, b=10),
    )
    st.plotly_chart(fig_overview, use_container_width=True)

    # --- Прогноз ---
    if forecast:
        st.subheader("📊 Прогноз на поточний місяць")

        col1, col2, col3, col4 = st.columns(4)
        with col1:
            st.markdown(custom_metric("Факт (на сьогодні)", f"{forecast['fact']:,.0f}"), unsafe_allow_html=True)
        with col2:
            st.markdown(custom_metric("Базовий прогноз", f"{forecast['base_forecast']:,.0f}", "середнє за минулі дні × кількість днів у місяці"), unsafe_allow_html=True)
        with col3:
            st.markdown(custom_metric("Мінімальний", f"{forecast['min_forecast']:,.0f}", "(середнє - 0.5×σ) × кількість днів (не менше 0)"), unsafe_allow_html=True)
        with col4:
            st.markdown(custom_metric("Оптимістичний", f"{forecast['max_forecast']:,.0f}", "(середнє + 0.5×σ) × кількість днів"), unsafe_allow_html=True)

        fact_daily = filtered[filtered["date"].dt.day <= forecast["days_passed"]]
        fact_daily = fact_daily.groupby("date")["value"].sum().reset_index()
        fact_daily = fact_daily.sort_values("date")
        fact_daily["cumulative"] = fact_daily["value"].cumsum()

        last_fact_date = fact_daily["date"].max() if not fact_daily.empty else pd.Timestamp.now().normalize()
        future_dates = pd.date_range(
            start=last_fact_date + pd.Timedelta(days=1),
            end=pd.Timestamp(year=pd.Timestamp.now().year, month=pd.Timestamp.now().month, day=forecast["total_days"]),
            freq="D"
        )

        if len(future_dates) > 0:
            last_cum = fact_daily["cumulative"].iloc[-1] if not fact_daily.empty else 0
            avg_daily = forecast["avg_daily"]
            std_daily = forecast["std_daily"]

            cum_base = last_cum
            cum_min = last_cum
            cum_max = last_cum
            base_forecast_vals = []
            min_forecast_vals = []
            max_forecast_vals = []

            for i, _ in enumerate(future_dates):
                cum_base += avg_daily
                cum_min += max(0, avg_daily - 0.5 * std_daily)
                cum_max += avg_daily + 0.5 * std_daily
                base_forecast_vals.append(cum_base)
                min_forecast_vals.append(cum_min)
                max_forecast_vals.append(cum_max)

            fig_forecast = go.Figure()

            fig_forecast.add_trace(go.Scatter(
                x=fact_daily["date"],
                y=fact_daily["cumulative"],
                mode="lines+markers",
                name="Факт",
                line=dict(color="blue", width=2),
                marker=dict(color="blue", size=6),
                showlegend=False
            ))

            fig_forecast.add_trace(go.Scatter(
                x=list(future_dates) + list(future_dates)[::-1],
                y=max_forecast_vals + min_forecast_vals[::-1],
                fill='toself',
                fillcolor='rgba(255, 165, 0, 0.2)',
                line=dict(color='rgba(255,165,0,0)'),
                name='Прогнозний коридор',
                showlegend=False
            ))

            fig_forecast.add_trace(go.Scatter(
                x=future_dates,
                y=base_forecast_vals,
                mode="lines",
                name="Базовий прогноз",
                line=dict(color="orange", width=2, dash="dash"),
                showlegend=False
            ))

            fig_forecast.add_trace(go.Scatter(
                x=future_dates,
                y=min_forecast_vals,
                mode="lines",
                name="Мінімальний",
                line=dict(color="red", width=1.5, dash="dot"),
                showlegend=False
            ))

            fig_forecast.add_trace(go.Scatter(
                x=future_dates,
                y=max_forecast_vals,
                mode="lines",
                name="Оптимістичний",
                line=dict(color="green", width=1.5, dash="dot"),
                showlegend=False
            ))

            fig_forecast.update_layout(
                title="Накопичена кількість: факт та прогнозні сценарії",
                xaxis=dict(title="Дата", tickformat="%d.%m"),
                yaxis=dict(title="Накопичена кількість"),
                hovermode="x unified",
                height=400,
                margin=dict(l=10, r=10, t=40, b=10),
                showlegend=False,
            )

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
        fig_daily_detailed.update_xaxes(tickformat="%d.%m", title_text="Дата")
        if smooth_enabled:
            daily["value_smooth"] = daily["value"].rolling(window=smooth_window, min_periods=1, center=True).mean()
            fig_daily_detailed.add_scatter(
                x=daily["date"],
                y=daily["value_smooth"],
                mode="lines",
                name=f"Ковзне середнє ({smooth_window} дн.)",
                line=dict(color="orange", width=3),
            )
        anomalies = detect_anomalies(filtered, window=14, threshold=1.5)
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
        fig_daily_detailed.update_xaxes(tickformat="%d.%m", title_text="Дата")

    fig_daily_detailed.update_layout(
        height=400,
        hovermode="x unified",
        margin=dict(l=10, r=10, t=20, b=10),
    )
    st.plotly_chart(fig_daily_detailed, use_container_width=True)

    # YoY
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
    fig_cum.update_xaxes(tickformat="%d.%m", title_text="Дата")
    fig_cum.update_layout(height=380, margin=dict(l=10, r=10, t=20, b=10))
    st.plotly_chart(fig_cum, use_container_width=True)

    # Аномальні дні
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

    ops_data = filtered[filtered["operation"] != "Тотал"]
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
    def sort_months(month_str):
        try:
            return datetime.strptime(month_str, "%m.%Y")
        except:
            return datetime(1900, 1, 1)
    sorted_months = sorted(heat_pivot.index, key=sort_months)
    heat_pivot = heat_pivot.reindex(sorted_months)

    fig_heatmap = px.imshow(
        heat_pivot,
        text_auto=".1f",
        aspect="auto",
        labels=dict(x="День тижня", y="Місяць", color="Середня кількість"),
        color_continuous_scale="Blues",
    )
    fig_heatmap.update_layout(height=450, margin=dict(l=10, r=10, t=20, b=10))
    st.plotly_chart(fig_heatmap, use_container_width=True)

    st.subheader("📊 Стабільність навантаження")
    col1, col2, col3 = st.columns(3)
    col1.markdown(custom_metric("Середнє за день", f"{daily_avg:.0f}" if daily_avg > 0 else "—", "Розраховано за тим самим принципом, що й в Overview"), unsafe_allow_html=True)
    col2.markdown(custom_metric("Стандартне відхилення", f"{std:.1f}" if std > 0 else "—", "Стандартне відхилення денних сум (лише фактичні дні)"), unsafe_allow_html=True)
    col3.markdown(custom_metric("Коефіцієнт варіації (CV)", f"{cv:.1f}%" if cv > 0 else "—", "CV = (стандартне відхилення / середнє) × 100%"), unsafe_allow_html=True)

    st.subheader("📈 Співвідношення пік / середнє")
    st.markdown(custom_metric("Пік / середнє", f"{peak_avg_ratio:.2f}×" if peak_avg_ratio > 0 else "—", "У скільки разів максимальне денне значення перевищує середнє"), unsafe_allow_html=True)

st.caption("Джерело: Google Sheets • Оновлення даних: до 5 хвилин після зміни таблиці.")
