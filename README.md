# KPO Dashboard — Streamlit + Google Sheets

Це перша версія інтерактивного Dashboard для таблиці «Узгодження КПО».

## Що вміє

- підключається напряму до Google Sheets;
- читає листи `24`, `25`, `26`;
- автоматично розбирає місячні блоки;
- будує денну динаміку;
- порівнює місяці;
- показує структуру операцій;
- порівнює будні та вихідні;
- має фільтри за роком, місяцем і показником;
- оновлює дані максимум із 5-хвилинним кешем.

## Важливо

У коді вже прописаний ID твоєї таблиці:

`1STX1vgDAk3zVDshXdZmTgJJSvQNCN4WmmftOskwymYI`

Не потрібно робити таблицю публічною. Рекомендований варіант — створити Google Cloud service account і надати йому доступ Viewer до таблиці.

## Локальний запуск

1. Встановити Python 3.12.
2. Створити virtual environment.
3. Встановити залежності:

```bash
pip install -r requirements.txt
```

4. Створити `.streamlit/secrets.toml` на основі `.streamlit/secrets.toml.example`.
5. Заповнити в ньому дані JSON-ключа service account.
6. Запустити:

```bash
streamlit run app.py
```

## Streamlit Community Cloud

Завантажити файли в GitHub repository, підключити repository у Streamlit Community Cloud і в Settings -> Secrets вставити вміст `secrets.toml`.

Файл `.streamlit/secrets.toml` не комітити в GitHub.

## Безпека

Service account повинен мати доступ до Google Sheet лише на читання.
