# Smart Finance Insights

A personal finance management web app built with Flask: income/expense tracking,
budgets, debts, bills, investments, financial goals, portfolio analytics,
an Intelligence Dashboard (financial health score, budget recommendations,
AI insights, savings trend, goal progress, risk analysis), live budget/goal/
bill/investment alerts, dark mode, a collapsible sidebar, and a profile
dropdown (My Profile / Settings / Help / Logout).

## Requirements

- Python 3.9+
- pip

## Setup

1. Unzip this project and open a terminal in the `Smart_Finance_Insight` folder.
2. (Recommended) Create a virtual environment:
   ```bash
   python3 -m venv venv
   source venv/bin/activate      # on Windows: venv\Scripts\activate
   ```
3. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

## Running the app

```bash
python app.py
```

The app starts a local development server. Open your browser to:

```
http://127.0.0.1:5000
```

`finance.db` (SQLite) is created automatically on first run if it doesn't
already exist, and any missing tables (including newly added ones, like
`debts` and `bills`) are created on every startup — so it's safe to run
even with the `finance.db` already included in this zip; your existing
data is never touched.

## First use

1. Go to `/register` and create an account.
2. Log in at `/login`.
3. Start adding income, expenses, budgets, investments, and goals from the
   sidebar.

## Notes

- This runs Flask's built-in development server (`debug=True`) — fine for
  local use and demos, but **not** meant for production/public hosting.
  For real deployment, use a production WSGI server (e.g. `gunicorn`) behind
  a reverse proxy, and move `app.secret_key` out of the source file into an
  environment variable.
- Passwords are hashed with Werkzeug's `generate_password_hash`. If you have
  an older `finance.db` with plaintext passwords from before this was added,
  those accounts still log in normally — the app detects the old format and
  silently upgrades it to a proper hash the next time that user logs in.
- PDF export (`/export_investments_pdf`) requires the `reportlab` package,
  already listed in `requirements.txt`.
- Dark Mode, Privacy Mode, and the collapsed/expanded sidebar state are saved
  in the browser (`localStorage`), not the database — they're per-browser
  preferences, not per-account, so they won't follow you to a different
  browser or device.
