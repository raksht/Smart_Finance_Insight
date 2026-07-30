from flask import Flask, render_template, request, redirect, session, url_for, make_response, flash
import sqlite3
import csv
import math
import os
from io import StringIO, BytesIO
import datetime
import statistics
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)

# Secret key is required for session and flash messages. Reads from the
# SECRET_KEY environment variable when set (real deployment), and falls
# back to the original hardcoded value so local/demo use still works
# out of the box with zero setup.
app.secret_key = os.environ.get("SECRET_KEY", "smart_finance_secret_key")

# Function to connect with SQLite database
def get_connection():
    conn = sqlite3.connect("finance.db")
    conn.row_factory = sqlite3.Row
    return conn

# Function to create required tables
def create_tables():
    conn = get_connection()
    cursor = conn.cursor()

    # --- USERS TABLE ---
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT,
        email TEXT UNIQUE,
        password TEXT,
        phone TEXT,
        occupation TEXT,
        monthly_income REAL,
        member_since TEXT
    )
    """)

    # --- INCOME TABLE ---
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS income (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        source TEXT,
        amount REAL,
        date TEXT
    )
    """)

    # --- EXPENSES TABLE ---
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS expenses (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        category TEXT,
        amount REAL,
        date TEXT,
        note TEXT
    )
    """)

    # --- BUDGET TABLE ---
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS budget (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        category TEXT NOT NULL,
        amount REAL NOT NULL,
        FOREIGN KEY (user_id) REFERENCES users (id)
    )
    ''')

    # --- INVESTMENTS TABLE ---
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS investments(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        asset TEXT,
        investment_name TEXT,
        invested_amount REAL,
        current_value REAL,
        FOREIGN KEY (user_id) REFERENCES users (id)
    )
    """)

    # --- GOALS TABLE ---
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS goals(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        goal_name TEXT,
        target_amount REAL,
        saved_amount REAL,
        target_date TEXT,
        FOREIGN KEY (user_id) REFERENCES users (id)
    )
    """)

    # --- DEBTS TABLE (Milestone 3 Day 3: Debt-to-Income Ratio indicator) ---
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS debts(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        debt_name TEXT,
        monthly_payment REAL,
        outstanding_amount REAL,
        FOREIGN KEY (user_id) REFERENCES users (id)
    )
    """)

    # --- BILLS TABLE (Milestone 3 Day 4: Bill Reminder notifications) ---
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS bills(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        bill_name TEXT,
        amount REAL,
        due_date TEXT,
        FOREIGN KEY (user_id) REFERENCES users (id)
    )
    """)

    conn.commit()
    conn.close()

def migrate_schema():
    """CREATE TABLE IF NOT EXISTS only creates a table the first time - if a
    table already exists (e.g. from an older version of finance.db shipped
    with the app) it will NOT gain new columns automatically. That's exactly
    what happened here: the bundled finance.db had a 'users' table without
    'member_since', so every /register attempt raised an exception that got
    swallowed by the bare except and mis-reported as "Email already
    registered". This walks each table's expected columns and ALTERs in any
    that are missing, so older database files self-heal on startup instead
    of failing silently.
    """
    expected_columns = {
        "users": {
            "phone": "TEXT",
            "occupation": "TEXT",
            "monthly_income": "REAL",
            "member_since": "TEXT",
            "last_login": "TEXT",
            "currency_symbol": "TEXT DEFAULT '₹'",
            "failed_attempts": "INTEGER DEFAULT 0",
            "locked_until": "TEXT",
        },
        "bills": {
            "is_paid": "INTEGER DEFAULT 0",
            "is_recurring": "INTEGER DEFAULT 0",
        },
        "income": {
            "is_recurring": "INTEGER DEFAULT 0",
        },
    }

    conn = get_connection()
    cursor = conn.cursor()
    for table, columns in expected_columns.items():
        cursor.execute(f"PRAGMA table_info({table})")
        existing = {row["name"] for row in cursor.fetchall()}
        for col_name, col_type in columns.items():
            if col_name not in existing:
                cursor.execute(f"ALTER TABLE {table} ADD COLUMN {col_name} {col_type}")
    conn.commit()
    conn.close()

# Create tables when application starts, then backfill any columns missing
# from an older copy of finance.db.
create_tables()
migrate_schema()

# ==========================================
# ALERT & NOTIFICATION SYSTEM (Milestone 3, Day 4)
# Computed live from current data rather than stored, so alerts never go
# stale or duplicate - always reflects the latest budgets/goals/investments.
# ==========================================
def get_currency_symbol(user_id):
    """Server-side equivalent of the inject_currency context processor, for
    use in notification/recommendation strings and exports (not templates)."""
    conn = get_connection()
    row = conn.execute("SELECT currency_symbol FROM users WHERE id = ?", (user_id,)).fetchone()
    conn.close()
    return row["currency_symbol"] if row and row["currency_symbol"] else "₹"


def get_notifications(user_id):
    cur = get_currency_symbol(user_id)
    conn = get_connection()
    cursor = conn.cursor()
    notifications = []

    # --- Budget overspending alerts ---
    cursor.execute("SELECT category, amount FROM budget WHERE user_id = ?", (user_id,))
    for b in cursor.fetchall():
        cursor.execute(
            "SELECT SUM(amount) AS spent FROM expenses WHERE user_id = ? AND category = ?",
            (user_id, b["category"])
        )
        spent = cursor.fetchone()["spent"] or 0
        if spent > b["amount"]:
            over = round(spent - b["amount"], 2)
            notifications.append({
                "type": "Budget Alert",
                "icon": "bi-exclamation-triangle-fill",
                "message": f"⚠ {b['category']} budget exceeded by {cur}{over}",
                "priority": "High",
                "status": "Active"
            })

    # --- Overall budget balance (proxy for "low balance" since there's no
    #     bank-balance tracking in this app) ---
    cursor.execute("SELECT SUM(amount) AS total FROM budget WHERE user_id = ?", (user_id,))
    total_budget = cursor.fetchone()["total"] or 0
    cursor.execute("SELECT SUM(amount) AS total FROM expenses WHERE user_id = ?", (user_id,))
    total_expense = cursor.fetchone()["total"] or 0
    if total_budget > 0 and total_expense > total_budget:
        notifications.append({
            "type": "Low Balance",
            "icon": "bi-wallet2",
            "message": f"⚠ Overall monthly budget exceeded by {cur}{round(total_expense - total_budget, 2)}",
            "priority": "High",
            "status": "Active"
        })

    # --- Low savings balance (proxy for "account balance", since there's no
    #     bank-balance tracking - uses income minus expenses instead) ---
    cursor.execute("SELECT SUM(amount) AS total FROM income WHERE user_id = ?", (user_id,))
    total_income_for_balance = cursor.fetchone()["total"] or 0
    available_balance = total_income_for_balance - total_expense
    LOW_BALANCE_THRESHOLD = 10000
    if total_income_for_balance > 0 and 0 <= available_balance < LOW_BALANCE_THRESHOLD:
        notifications.append({
            "type": "Low Balance",
            "icon": "bi-wallet2",
            "message": f"💰 Available balance is {cur}{round(available_balance, 2)}, below the {cur}{LOW_BALANCE_THRESHOLD:,} safety threshold",
            "priority": "High",
            "status": "Active"
        })

    # --- Savings rate dropped below 20% ---
    if total_income_for_balance > 0:
        current_savings_rate = round(((total_income_for_balance - total_expense) / total_income_for_balance) * 100, 1)
        if 0 <= current_savings_rate < 20:
            notifications.append({
                "type": "Savings Alert",
                "icon": "bi-piggy-bank-fill",
                "message": f"💰 Savings rate dropped to {current_savings_rate}% - aim for at least 20%",
                "priority": "Medium",
                "status": "Active"
            })
        elif current_savings_rate < 0:
            notifications.append({
                "type": "Savings Alert",
                "icon": "bi-piggy-bank-fill",
                "message": f"💰 Expenses currently exceed income by {cur}{round(total_expense - total_income_for_balance, 2)}",
                "priority": "High",
                "status": "Active"
            })

    # --- Goal reminders and overdue goals ---
    cursor.execute(
        "SELECT goal_name, target_amount, saved_amount, target_date FROM goals WHERE user_id = ?",
        (user_id,)
    )
    today = datetime.date.today()
    for g in cursor.fetchall():
        remaining = g["target_amount"] - g["saved_amount"]
        progress = round((g["saved_amount"] / g["target_amount"] * 100), 1) if g["target_amount"] > 0 else 0
        if remaining <= 0:
            continue  # goal already completed, no alert needed

        # --- Goal nearing completion (>=90%) ---
        if progress >= 90:
            notifications.append({
                "type": "Goal Milestone",
                "icon": "bi-trophy-fill",
                "message": f"🎯 {g['goal_name']} goal is {progress}% complete - almost there!",
                "priority": "Low",
                "status": "Active"
            })

        t_date = None
        if g["target_date"]:
            try:
                t_date = datetime.datetime.strptime(g["target_date"], "%Y-%m-%d").date()
            except ValueError:
                t_date = None
        if t_date:
            days_left = (t_date - today).days
            if days_left < 0:
                notifications.append({
                    "type": "Goal Overdue",
                    "icon": "bi-exclamation-circle-fill",
                    "message": f"🎯 {g['goal_name']} missed its target date - {cur}{round(remaining, 2)} still needed",
                    "priority": "High",
                    "status": "Active"
                })
            elif days_left <= 30:
                notifications.append({
                    "type": "Savings Goal",
                    "icon": "bi-flag-fill",
                    "message": f"🎯 Save {cur}{round(remaining, 2)} more for {g['goal_name']} in the next {days_left} days",
                    "priority": "Medium",
                    "status": "Active"
                })

    # --- Bill reminders ---
    cursor.execute("SELECT bill_name, amount, due_date FROM bills WHERE user_id = ? AND (is_paid IS NULL OR is_paid = 0)", (user_id,))
    for b in cursor.fetchall():
        if not b["due_date"]:
            continue
        try:
            d_date = datetime.datetime.strptime(b["due_date"], "%Y-%m-%d").date()
        except ValueError:
            continue
        days_left = (d_date - today).days
        if days_left < 0:
            notifications.append({
                "type": "Bill Reminder",
                "icon": "bi-calendar-x-fill",
                "message": f"💳 {b['bill_name']} bill of {cur}{b['amount']} was due on {d_date.strftime('%d-%b-%Y')}",
                "priority": "High",
                "status": "Active"
            })
        elif days_left <= 7:
            notifications.append({
                "type": "Bill Reminder",
                "icon": "bi-calendar-event",
                "message": f"💳 {b['bill_name']} bill of {cur}{b['amount']} due in {days_left} day{'s' if days_left != 1 else ''} ({d_date.strftime('%d-%b-%Y')})",
                "priority": "Medium",
                "status": "Pending"
            })

    # --- Investment performance alerts ---
    cursor.execute(
        "SELECT investment_name, invested_amount, current_value FROM investments WHERE user_id = ?",
        (user_id,)
    )
    for inv in cursor.fetchall():
        if inv["invested_amount"] > 0:
            roi = round(((inv["current_value"] - inv["invested_amount"]) / inv["invested_amount"]) * 100, 1)
            if roi >= 15:
                notifications.append({
                    "type": "Investment Alert",
                    "icon": "bi-graph-up-arrow",
                    "message": f"📈 {inv['investment_name']} gained {roi}%",
                    "priority": "Low",
                    "status": "Completed"
                })
            elif roi <= -10:
                notifications.append({
                    "type": "Investment Alert",
                    "icon": "bi-graph-down-arrow",
                    "message": f"📉 {inv['investment_name']} dropped {roi}%",
                    "priority": "High",
                    "status": "Active"
                })

    conn.close()

    # Each notification type routes to the page it's actually about, so
    # clicking one in the bell dropdown (or the /notifications table) takes
    # you straight there instead of doing nothing.
    type_links = {
        "Budget Alert": "/budget",
        "Low Balance": "/budget",
        "Savings Alert": "/analysis",
        "Goal Milestone": "/goals",
        "Goal Overdue": "/goals",
        "Savings Goal": "/goals",
        "Bill Reminder": "/bills",
        "Investment Alert": "/investments",
    }
    for n in notifications:
        n["link"] = type_links.get(n["type"], "/notifications")

    priority_order = {"High": 0, "Medium": 1, "Low": 2}
    status_order = {"Active": 0, "Pending": 1, "Completed": 2}
    notifications.sort(key=lambda n: (priority_order.get(n["priority"], 3), status_order.get(n["status"], 3)))
    return notifications


@app.context_processor
def inject_nav_now():
    """Current timestamp for the topbar page header (date shown next to the
    page title, replacing the old search box)."""
    return dict(nav_now=datetime.datetime.now())


@app.context_processor
def inject_notifications():
    """
    cur = get_currency_symbol(user_id)Makes the notification bell + badge available on every template
    (via base.html) without needing every single route to pass it in."""
    if "user_id" not in session:
        return {}
    try:
        notes = get_notifications(session["user_id"])
    except Exception:
        notes = []
    return dict(
        nav_notifications=notes[:5],
        nav_notification_count=len([n for n in notes if n["status"] == "Active"])
    )


@app.context_processor
def inject_nav_user():
    """Small extra profile info (occupation) for the topbar profile dropdown
    subtitle, without needing every route to fetch and pass it individually."""
    if "user_id" not in session:
        return {}
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT occupation FROM users WHERE id = ?", (session["user_id"],))
        row = cursor.fetchone()
        conn.close()
        occupation = row["occupation"] if row and row["occupation"] else None
    except Exception:
        occupation = None
    return dict(nav_user_occupation=occupation)


@app.context_processor
def inject_currency():
    """Multi-currency support: every template can use {{ currency_symbol }}
    instead of a hardcoded ₹, driven by the user's Settings choice."""
    if "user_id" not in session:
        return dict(currency_symbol="₹")
    try:
        conn = get_connection()
        row = conn.execute("SELECT currency_symbol FROM users WHERE id = ?", (session["user_id"],)).fetchone()
        conn.close()
        symbol = row["currency_symbol"] if row and row["currency_symbol"] else "₹"
    except Exception:
        symbol = "₹"
    return dict(currency_symbol=symbol)


@app.route("/settings", methods=["GET", "POST"])
def settings():
    if "user_id" not in session:
        return redirect("/login")
    if request.method == "POST":
        symbol = request.form.get("currency_symbol", "₹")
        conn = get_connection()
        conn.execute("UPDATE users SET currency_symbol = ? WHERE id = ?", (symbol, session["user_id"]))
        conn.commit()
        conn.close()
        flash("✓ Currency preference updated", "success")
        return redirect("/settings")
    return render_template("settings.html")


@app.route("/export_backup")
def export_backup():
    """Data backup: exports all of the user's data (every table) as a
    single downloadable JSON file."""
    if "user_id" not in session:
        return redirect("/login")
    uid = session["user_id"]
    conn = get_connection()
    backup = {}
    for table in ["income", "expenses", "budget", "investments", "goals", "debts", "bills"]:
        rows = conn.execute(f"SELECT * FROM {table} WHERE user_id = ?", (uid,)).fetchall()
        backup[table] = [dict(r) for r in rows]
    conn.close()
    import json
    output = make_response(json.dumps(backup, indent=2))
    output.headers["Content-Disposition"] = "attachment; filename=smart_finance_backup.json"
    output.headers["Content-type"] = "application/json"
    return output


@app.route("/import_backup", methods=["POST"])
def import_backup():
    """Data restore: replaces the current user's data (all tables) with
    the contents of a previously exported backup JSON file."""
    if "user_id" not in session:
        return redirect("/login")
    file = request.files.get("backup_file")
    if not file or file.filename == "":
        flash("Please choose a backup file to restore.", "danger")
        return redirect("/settings")
    import json
    try:
        data = json.load(file.stream)
    except Exception:
        flash("That file isn't valid backup JSON.", "danger")
        return redirect("/settings")

    uid = session["user_id"]
    table_columns = {
        "income": ["source", "amount", "date", "is_recurring"],
        "expenses": ["category", "amount", "date", "note"],
        "budget": ["category", "amount"],
        "investments": ["asset", "investment_name", "invested_amount", "current_value"],
        "goals": ["goal_name", "target_amount", "saved_amount", "target_date"],
        "debts": ["debt_name", "monthly_payment", "outstanding_amount"],
        "bills": ["bill_name", "amount", "due_date", "is_paid", "is_recurring"],
    }
    conn = get_connection()
    cursor = conn.cursor()
    for table, cols in table_columns.items():
        cursor.execute(f"DELETE FROM {table} WHERE user_id = ?", (uid,))
        for row in data.get(table, []):
            values = [row.get(c) for c in cols]
            placeholders = ", ".join(["?"] * (len(cols) + 1))
            cursor.execute(
                f"INSERT INTO {table} (user_id, {', '.join(cols)}) VALUES ({placeholders})",
                [uid] + values
            )
    conn.commit()
    conn.close()
    flash("✓ Backup restored successfully", "success")
    return redirect("/settings")


@app.route("/help")
def help_page():
    if "user_id" not in session:
        return redirect("/login")
    return render_template("help.html")


@app.route("/notifications")
def notifications_page():
    if "user_id" not in session:
        return redirect("/login")
    all_notes = get_notifications(session["user_id"])
    priority_filter = request.args.get("priority", "")
    notes = [n for n in all_notes if n["priority"] == priority_filter] if priority_filter else all_notes
    counts = {
        "High": len([n for n in all_notes if n["priority"] == "High"]),
        "Medium": len([n for n in all_notes if n["priority"] == "Medium"]),
        "Low": len([n for n in all_notes if n["priority"] == "Low"]),
    }
    return render_template("notifications.html", notifications=notes, counts=counts, priority_filter=priority_filter, total_count=len(all_notes))


# ==========================================
# INTELLIGENCE & INSIGHTS HELPERS (Milestone 3)
# Shared by both /analysis and the Intelligence Dashboard so the same
# numbers show up consistently everywhere instead of being computed twice
# with slightly different logic.
# ==========================================
def get_financial_health(user_id):
    """Financial Health Score (Day 3): uses income, expenses, savings,
    investments, and debt per the milestone spec - classifies as
    Excellent/Good/Fair/Poor."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT SUM(amount) AS t FROM income WHERE user_id = ?", (user_id,))
    total_income = cursor.fetchone()["t"] or 0
    cursor.execute("SELECT SUM(amount) AS t FROM expenses WHERE user_id = ?", (user_id,))
    total_expense = cursor.fetchone()["t"] or 0
    cursor.execute("SELECT SUM(amount) AS t FROM budget WHERE user_id = ?", (user_id,))
    total_budget = cursor.fetchone()["t"] or 0
    cursor.execute("SELECT SUM(invested_amount) AS inv, SUM(current_value) AS cur FROM investments WHERE user_id = ?", (user_id,))
    inv_row = cursor.fetchone()
    total_invested = inv_row["inv"] or 0
    total_current_value = inv_row["cur"] or 0
    conn.close()

    savings = total_income - total_expense
    savings_rate = round((savings / total_income * 100), 1) if total_income > 0 else 0
    investment_growth = round(((total_current_value - total_invested) / total_invested) * 100, 1) if total_invested > 0 else 0
    debt_to_income = get_debt_to_income_ratio(user_id)

    score = 0
    breakdown = {"expense_score": 0, "savings_score": 0, "budget_score": 0, "investment_score": 0, "debt_score": 0}
    if total_income > 0:
        expense_ratio = (total_expense / total_income) * 100
        expense_score = max(0, 100 - expense_ratio)
        savings_score = max(0, min(100, savings_rate * 2.5))  # 40%+ savings rate = full marks
        budget_score = 100 if total_budget == 0 else max(0, 100 - abs(total_expense - total_budget) / total_budget * 50)
        investment_score = max(0, min(100, 50 + investment_growth * 2))  # 0% growth -> 50, scales from there
        debt_score = max(0, 100 - debt_to_income * 2)  # 50%+ debt-to-income -> 0

        breakdown = {
            "expense_score": round(expense_score),
            "savings_score": round(savings_score),
            "budget_score": round(budget_score),
            "investment_score": round(investment_score),
            "debt_score": round(debt_score),
        }

        score = round(
            expense_score * 0.30 +
            savings_score * 0.25 +
            budget_score * 0.15 +
            investment_score * 0.15 +
            debt_score * 0.15
        )
        score = max(0, min(100, score))

    if score >= 85:
        tier = "Excellent"
    elif score >= 70:
        tier = "Good"
    elif score >= 50:
        tier = "Fair"
    else:
        tier = "Poor"

    return {
        "score": score, "tier": tier, "savings_rate": savings_rate,
        "investment_growth": investment_growth, "debt_to_income": debt_to_income,
        "breakdown": breakdown
    }


def get_spending_analysis(user_id):
    """The genuinely unique content that used to live on the standalone
    /analysis page (income burn rate %, full category breakdown, investment
    growth, top spending leak) - folded into the Intelligence Dashboard so
    there's one page for 'how am I doing' instead of two overlapping ones."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT SUM(amount) AS t FROM income WHERE user_id = ?", (user_id,))
    total_income = cursor.fetchone()["t"] or 0
    cursor.execute("SELECT SUM(amount) AS t FROM expenses WHERE user_id = ?", (user_id,))
    total_expense = cursor.fetchone()["t"] or 0

    cursor.execute("""
        SELECT category, SUM(amount) AS total FROM expenses
        WHERE user_id = ? GROUP BY category ORDER BY total DESC
    """, (user_id,))
    category_rows = cursor.fetchall()
    category_breakdown = []
    for row in category_rows:
        pct = round((row["total"] / total_expense * 100), 1) if total_expense > 0 else 0
        category_breakdown.append({"category": row["category"], "total": round(row["total"], 2), "percentage": pct})
    top_category = category_rows[0]["category"] if category_rows else "N/A"

    cursor.execute("SELECT SUM(invested_amount) AS inv, SUM(current_value) AS cur FROM investments WHERE user_id = ?", (user_id,))
    inv_row = cursor.fetchone()
    total_invested = inv_row["inv"] or 0
    total_current_value = inv_row["cur"] or 0
    investment_growth = round(((total_current_value - total_invested) / total_invested) * 100, 1) if total_invested > 0 else 0
    conn.close()

    expense_percentage = round((total_expense / total_income) * 100, 1) if total_income > 0 else 0

    return {
        "expense_percentage": expense_percentage,
        "category_breakdown": category_breakdown,
        "top_category": top_category,
        "investment_growth": investment_growth
    }


def get_debt_to_income_ratio(user_id):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT SUM(monthly_payment) AS t FROM debts WHERE user_id = ?", (user_id,))
    total_monthly_debt = cursor.fetchone()["t"] or 0
    cursor.execute("SELECT monthly_income FROM users WHERE id = ?", (user_id,))
    row = cursor.fetchone()
    monthly_income = (row["monthly_income"] or 0) if row else 0
    conn.close()
    return round((total_monthly_debt / monthly_income) * 100, 1) if monthly_income > 0 else 0


def get_budget_recommendations(user_id):
    """Personalized Budget Recommendations (Day 2): compares each budgeted
    category against actual spend and flags overspending, plus a savings
    nudge when the savings rate is low."""
    cur = get_currency_symbol(user_id)
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT SUM(amount) AS t FROM income WHERE user_id = ?", (user_id,))
    total_income = cursor.fetchone()["t"] or 0
    cursor.execute("SELECT SUM(amount) AS t FROM expenses WHERE user_id = ?", (user_id,))
    total_expense = cursor.fetchone()["t"] or 0
    savings_rate = round(((total_income - total_expense) / total_income * 100), 1) if total_income > 0 else 0

    cursor.execute("SELECT category, amount FROM budget WHERE user_id = ?", (user_id,))
    budget_rows = cursor.fetchall()
    recommendations = []
    for b in budget_rows:
        cursor.execute(
            "SELECT SUM(amount) AS spent FROM expenses WHERE user_id = ? AND category = ?",
            (user_id, b["category"])
        )
        spent = cursor.fetchone()["spent"] or 0
        if spent > b["amount"]:
            over = round(spent - b["amount"], 2)
            recommendations.append(f"Reduce {b['category']} spending by {cur}{over} to get back within budget.")

    if savings_rate < 20 and total_income > 0:
        recommendations.append(f"Increase monthly savings by {cur}{round(total_income * 0.05, 2)} to build a stronger safety net.")

    debt_ratio = get_debt_to_income_ratio(user_id)
    if debt_ratio > 40:
        recommendations.append(f"Debt-to-income ratio is {debt_ratio}% - prioritize paying down high-interest debt.")

    conn.close()
    if not recommendations:
        recommendations.append("Your spending is within budget across all categories. Great job!")
    return recommendations


def get_emergency_fund_months(user_id):
    """Approximates 'months of expenses covered' by dividing current savings
    by the average monthly expense seen in the user's actual data (no
    fabricated monthly figures - uses only months that really have records)."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT SUM(amount) AS t FROM income WHERE user_id = ?", (user_id,))
    total_income = cursor.fetchone()["t"] or 0
    cursor.execute("SELECT SUM(amount) AS t FROM expenses WHERE user_id = ?", (user_id,))
    total_expense = cursor.fetchone()["t"] or 0
    savings = total_income - total_expense

    cursor.execute("SELECT COUNT(DISTINCT substr(date, 1, 7)) AS m FROM expenses WHERE user_id = ?", (user_id,))
    months_with_data = cursor.fetchone()["m"] or 0
    conn.close()

    avg_monthly_expense = (total_expense / months_with_data) if months_with_data > 0 else total_expense
    if avg_monthly_expense > 0 and savings > 0:
        return round(savings / avg_monthly_expense, 1)
    return 0


def get_ai_insights(user_id):
    """AI-Based Financial Insights (Day 5): rule-based, human-readable
    observations about savings, investments, and emergency-fund coverage."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT SUM(amount) AS t FROM income WHERE user_id = ?", (user_id,))
    total_income = cursor.fetchone()["t"] or 0
    cursor.execute("SELECT SUM(amount) AS t FROM expenses WHERE user_id = ?", (user_id,))
    total_expense = cursor.fetchone()["t"] or 0
    savings_rate = round(((total_income - total_expense) / total_income * 100), 1) if total_income > 0 else 0

    cursor.execute("SELECT SUM(invested_amount) AS inv, SUM(current_value) AS cur FROM investments WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    invested = row["inv"] or 0
    current_value = row["cur"] or 0
    investment_growth = round(((current_value - invested) / invested * 100), 1) if invested > 0 else 0
    conn.close()

    emergency_months = get_emergency_fund_months(user_id)
    insights = []

    if total_income == 0:
        insights.append("Add your income to unlock personalized financial insights.")
        return insights

    if savings_rate >= 30:
        insights.append(f"Savings rate is healthy at {savings_rate}% of income.")
    elif savings_rate >= 0:
        insights.append(f"Savings rate is {savings_rate}% of income - aim for 20-30% for a stronger cushion.")
    else:
        insights.append(f"Expenses currently exceed income ({savings_rate}% savings rate) - review your spending.")

    if invested > 0:
        if investment_growth >= 0:
            insights.append(f"Investment portfolio gained {investment_growth}% overall.")
        else:
            insights.append(f"Investment portfolio is down {abs(investment_growth)}% overall - review your holdings.")
    else:
        insights.append("No investments recorded yet - consider starting an SIP to build long-term wealth.")

    if emergency_months >= 6:
        insights.append(f"Emergency fund is sufficient for {emergency_months} months of expenses.")
    elif emergency_months > 0:
        insights.append(f"Emergency fund covers {emergency_months} months of expenses - aim for at least 6.")
    else:
        insights.append("Consider building an emergency fund covering 6 months of expenses.")

    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT category, SUM(amount) AS t FROM expenses WHERE user_id = ? GROUP BY category ORDER BY t DESC LIMIT 1",
        (user_id,)
    )
    top_cat_row = cursor.fetchone()
    if top_cat_row and total_expense > 0:
        share = round(top_cat_row["t"] / total_expense * 100, 1)
        insights.append(f"{top_cat_row['category']} is your biggest expense category at {share}% of total spending.")

    today = datetime.date.today()
    week_out = today + datetime.timedelta(days=7)
    cursor.execute(
        "SELECT COUNT(*) AS c FROM bills WHERE user_id = ? AND (is_paid IS NULL OR is_paid = 0) AND due_date BETWEEN ? AND ?",
        (user_id, today.isoformat(), week_out.isoformat())
    )
    due_soon = cursor.fetchone()["c"]
    if due_soon > 0:
        insights.append(f"You have {due_soon} bill(s) due within the next 7 days - check the Bills page.")
    conn.close()

    return insights


def get_monthly_savings_trend(user_id, max_months=6):
    """Monthly Savings Trend (Day 6 dashboard widget): groups income and
    expenses by the month embedded in their date field, using only months
    that actually have data rather than fabricating a fixed timeline."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT substr(date, 1, 7) AS ym, SUM(amount) AS total FROM income WHERE user_id = ? GROUP BY ym",
        (user_id,)
    )
    income_by_month = {r["ym"]: r["total"] for r in cursor.fetchall() if r["ym"]}
    cursor.execute(
        "SELECT substr(date, 1, 7) AS ym, SUM(amount) AS total FROM expenses WHERE user_id = ? GROUP BY ym",
        (user_id,)
    )
    expense_by_month = {r["ym"]: r["total"] for r in cursor.fetchall() if r["ym"]}
    conn.close()

    months = sorted(set(income_by_month) | set(expense_by_month))[-max_months:]
    labels = months
    values = [round(income_by_month.get(m, 0) - expense_by_month.get(m, 0), 2) for m in months]
    return labels, values


def get_risk_level(investments):
    """Shared crypto-concentration risk check, reused by /portfolio and the
    Intelligence Dashboard so both pages agree instead of computing this
    independently."""
    total_current = sum(inv["current_value"] for inv in investments)
    if total_current <= 0:
        return "N/A"
    crypto_total = sum(inv["current_value"] for inv in investments if inv["asset"].lower() == "cryptocurrency")
    if (crypto_total / total_current) > 0.3:
        return "High"
    elif (crypto_total / total_current) == 0:
        return "Low"
    return "Moderate"


RISK_SCORE_MAP = {"Low": 25, "Moderate": 60, "High": 90, "N/A": 0}


def get_asset_breakdown(investments):
    """Groups raw investment rows by asset type with invested/current/ROI
    and a share-of-portfolio percentage - shared by /asset_allocation and
    the Intelligence Dashboard's allocation chart so the numbers agree."""
    total_current = sum(inv["current_value"] for inv in investments)
    grouped = {}
    for inv in investments:
        g = grouped.setdefault(inv["asset"], {"invested": 0, "current": 0})
        g["invested"] += inv["invested_amount"]
        g["current"] += inv["current_value"]

    breakdown = []
    for asset, vals in grouped.items():
        pct = round((vals["current"] / total_current) * 100, 1) if total_current > 0 else 0
        roi = round(((vals["current"] - vals["invested"]) / vals["invested"]) * 100, 2) if vals["invested"] > 0 else 0
        breakdown.append({
            "asset": asset,
            "invested": round(vals["invested"], 2),
            "current": round(vals["current"], 2),
            "roi": roi,
            "percentage": pct
        })
    breakdown.sort(key=lambda x: x["current"], reverse=True)
    return breakdown


def get_expense_heatmap(user_id):
    """Category-wise spend, tagged Low/Medium/High relative to the biggest
    category so the template can render the colored heatmap cards."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT category, SUM(amount) AS total FROM expenses WHERE user_id = ? GROUP BY category ORDER BY total DESC",
        (user_id,)
    )
    rows = cursor.fetchall()
    conn.close()

    if not rows:
        return []
    max_total = rows[0]["total"] or 1
    heatmap = []
    for r in rows:
        ratio = (r["total"] or 0) / max_total
        if ratio >= 0.66:
            level = "High"
        elif ratio >= 0.33:
            level = "Medium"
        else:
            level = "Low"
        heatmap.append({"category": r["category"], "total": round(r["total"], 2), "level": level})
    return heatmap


def get_goal_predictions(user_id):
    """Estimates months-to-completion for each active goal using the user's
    average monthly net savings (from get_monthly_savings_trend). Rough by
    nature - it's a projection, not a guarantee - but gives a genuinely
    useful 'at this rate, you'll hit it in N months' style estimate."""
    _, trend_values = get_monthly_savings_trend(user_id, max_months=6)
    avg_monthly_savings = (sum(trend_values) / len(trend_values)) if trend_values else 0

    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT goal_name, target_amount, saved_amount FROM goals WHERE user_id = ? ORDER BY id DESC",
        (user_id,)
    )
    rows = cursor.fetchall()
    conn.close()

    predictions = []
    for g in rows:
        remaining = g["target_amount"] - g["saved_amount"]
        progress = round((g["saved_amount"] / g["target_amount"] * 100), 1) if g["target_amount"] > 0 else 0
        if remaining <= 0:
            predictions.append({"name": g["goal_name"], "progress": 100, "message": "Goal already achieved!"})
            continue
        if avg_monthly_savings > 0:
            months_needed = math.ceil(remaining / avg_monthly_savings)
            plural = "s" if months_needed != 1 else ""
            predictions.append({
                "name": g["goal_name"],
                "progress": min(progress, 100),
                "message": f"At your current saving rate, you'll reach this goal in about {months_needed} month{plural}."
            })
        else:
            predictions.append({
                "name": g["goal_name"],
                "progress": min(progress, 100),
                "message": "Increase your monthly savings to start making progress toward this goal."
            })
    return predictions


def get_quick_stats(user_id):
    """Highest expense month, best saving month, highest-ROI investment, and
    total transaction count - small at-a-glance stat cards for the
    Intelligence Dashboard."""
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        "SELECT substr(date,1,7) AS ym, SUM(amount) AS total FROM expenses WHERE user_id = ? GROUP BY ym ORDER BY total DESC LIMIT 1",
        (user_id,)
    )
    top_expense_month = cursor.fetchone()

    cursor.execute(
        "SELECT substr(date,1,7) AS ym, SUM(amount) AS total FROM income WHERE user_id = ? GROUP BY ym",
        (user_id,)
    )
    income_by_month = {r["ym"]: r["total"] for r in cursor.fetchall() if r["ym"]}
    cursor.execute(
        "SELECT substr(date,1,7) AS ym, SUM(amount) AS total FROM expenses WHERE user_id = ? GROUP BY ym",
        (user_id,)
    )
    expense_by_month = {r["ym"]: r["total"] for r in cursor.fetchall() if r["ym"]}
    all_months = set(income_by_month) | set(expense_by_month)
    best_saving_month = None
    best_saving_value = None
    for m in all_months:
        net = income_by_month.get(m, 0) - expense_by_month.get(m, 0)
        if best_saving_value is None or net > best_saving_value:
            best_saving_value = net
            best_saving_month = m

    cursor.execute(
        "SELECT investment_name, invested_amount, current_value FROM investments WHERE user_id = ?",
        (user_id,)
    )
    best_roi_name, best_roi_val = None, None
    for inv in cursor.fetchall():
        if inv["invested_amount"] > 0:
            roi = round(((inv["current_value"] - inv["invested_amount"]) / inv["invested_amount"]) * 100, 1)
            if best_roi_val is None or roi > best_roi_val:
                best_roi_val = roi
                best_roi_name = inv["investment_name"]

    cursor.execute("SELECT COUNT(*) AS c FROM income WHERE user_id = ?", (user_id,))
    inc_count = cursor.fetchone()["c"]
    cursor.execute("SELECT COUNT(*) AS c FROM expenses WHERE user_id = ?", (user_id,))
    exp_count = cursor.fetchone()["c"]
    conn.close()

    def format_month(ym):
        if not ym:
            return "N/A"
        try:
            return datetime.datetime.strptime(ym + "-01", "%Y-%m-%d").strftime("%B %Y")
        except ValueError:
            return ym

    return {
        "highest_expense_month": format_month(top_expense_month["ym"]) if top_expense_month else "N/A",
        "highest_expense_month_amount": round(top_expense_month["total"], 2) if top_expense_month else 0,
        "best_saving_month": format_month(best_saving_month) if best_saving_month else "N/A",
        "best_saving_month_amount": round(best_saving_value, 2) if best_saving_value is not None else 0,
        "highest_roi_investment": best_roi_name or "N/A",
        "highest_roi_value": best_roi_val if best_roi_val is not None else 0,
        "total_transactions": inc_count + exp_count
    }


def get_recent_timeline(user_id, limit=12):
    """Merges income and expense entries (the only tables with a real date
    field) into a single Today / Yesterday / This Week / Earlier timeline,
    newest first."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT source AS label, amount, date FROM income WHERE user_id = ? ORDER BY date DESC LIMIT ?", (user_id, limit))
    incomes = [{"type": "income", "label": r["label"], "amount": r["amount"], "date": r["date"]} for r in cursor.fetchall()]
    cursor.execute("SELECT category AS label, amount, date FROM expenses WHERE user_id = ? ORDER BY date DESC LIMIT ?", (user_id, limit))
    expenses = [{"type": "expense", "label": r["label"], "amount": r["amount"], "date": r["date"]} for r in cursor.fetchall()]
    conn.close()

    events = incomes + expenses
    events.sort(key=lambda e: e["date"] or "", reverse=True)
    events = events[:limit]

    today = datetime.date.today()
    yesterday = today - datetime.timedelta(days=1)
    week_ago = today - datetime.timedelta(days=7)

    buckets = {"Today": [], "Yesterday": [], "This Week": [], "Earlier": []}
    for e in events:
        try:
            e_date = datetime.datetime.strptime(e["date"], "%Y-%m-%d").date()
        except (ValueError, TypeError):
            buckets["Earlier"].append(e)
            continue
        if e_date == today:
            buckets["Today"].append(e)
        elif e_date == yesterday:
            buckets["Yesterday"].append(e)
        elif e_date >= week_ago:
            buckets["This Week"].append(e)
        else:
            buckets["Earlier"].append(e)

    return {k: v for k, v in buckets.items() if v}


def get_income_expense_by_month(user_id, max_months=6):
    """Income vs expense side-by-side per month (last N months that actually
    have data) - powers the Month-over-Month comparison chart on the
    Analysis page. Distinct from get_monthly_savings_trend, which only
    returns the net savings line."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT substr(date, 1, 7) AS ym, SUM(amount) AS total FROM income WHERE user_id = ? GROUP BY ym",
        (user_id,)
    )
    income_by_month = {r["ym"]: r["total"] for r in cursor.fetchall() if r["ym"]}
    cursor.execute(
        "SELECT substr(date, 1, 7) AS ym, SUM(amount) AS total FROM expenses WHERE user_id = ? GROUP BY ym",
        (user_id,)
    )
    expense_by_month = {r["ym"]: r["total"] for r in cursor.fetchall() if r["ym"]}
    conn.close()

    months = sorted(set(income_by_month) | set(expense_by_month))[-max_months:]
    labels = months
    income_values = [round(income_by_month.get(m, 0), 2) for m in months]
    expense_values = [round(expense_by_month.get(m, 0), 2) for m in months]
    return labels, income_values, expense_values


def get_category_month_over_month(user_id):
    """Compares the most recent month of expense data against the month
    before it, category by category, so the Analysis page can show what
    went up or down rather than just a snapshot total."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT DISTINCT substr(date, 1, 7) AS ym FROM expenses WHERE user_id = ? AND ym IS NOT NULL ORDER BY ym DESC LIMIT 2",
        (user_id,)
    )
    months = [r["ym"] for r in cursor.fetchall() if r["ym"]]
    if len(months) < 2:
        conn.close()
        return None

    current_month, previous_month = months[0], months[1]
    cursor.execute(
        "SELECT category, SUM(amount) AS t FROM expenses WHERE user_id = ? AND substr(date,1,7) = ? GROUP BY category",
        (user_id, current_month)
    )
    current = {r["category"]: r["t"] for r in cursor.fetchall()}
    cursor.execute(
        "SELECT category, SUM(amount) AS t FROM expenses WHERE user_id = ? AND substr(date,1,7) = ? GROUP BY category",
        (user_id, previous_month)
    )
    previous = {r["category"]: r["t"] for r in cursor.fetchall()}
    conn.close()

    categories = sorted(set(current) | set(previous), key=lambda c: current.get(c, 0), reverse=True)
    comparisons = []
    for cat in categories:
        cur_val = round(current.get(cat, 0), 2)
        prev_val = round(previous.get(cat, 0), 2)
        delta = round(cur_val - prev_val, 2)
        pct = round((delta / prev_val) * 100, 1) if prev_val > 0 else (100.0 if cur_val > 0 else 0.0)
        comparisons.append({
            "category": cat, "current": cur_val, "previous": prev_val,
            "delta": delta, "pct": pct
        })
    comparisons.sort(key=lambda c: abs(c["delta"]), reverse=True)

    def label(ym):
        try:
            return datetime.datetime.strptime(ym + "-01", "%Y-%m-%d").strftime("%B %Y")
        except ValueError:
            return ym

    return {
        "current_label": label(current_month), "previous_label": label(previous_month),
        "categories": comparisons
    }


def get_spending_forecast(user_id, months_lookback=3):
    """Simple, transparent forecast (no black-box ML): projects next
    month's total spend, income, and per-category spend as the average of
    the last few months that actually have data, and flags the trend
    direction by comparing the oldest to the newest of those months."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT DISTINCT substr(date, 1, 7) AS ym FROM expenses WHERE user_id = ? AND ym IS NOT NULL ORDER BY ym DESC LIMIT ?",
        (user_id, months_lookback)
    )
    months = sorted([r["ym"] for r in cursor.fetchall() if r["ym"]])
    if not months:
        conn.close()
        return None

    placeholders = ",".join("?" for _ in months)
    cursor.execute(
        f"SELECT substr(date,1,7) AS ym, SUM(amount) AS t FROM expenses WHERE user_id = ? AND substr(date,1,7) IN ({placeholders}) GROUP BY ym",
        (user_id, *months)
    )
    totals_by_month = {r["ym"]: r["t"] for r in cursor.fetchall()}
    monthly_totals = [totals_by_month.get(m, 0) for m in months]
    predicted_total = round(sum(monthly_totals) / len(monthly_totals), 2)

    cursor.execute(
        f"SELECT SUM(amount) AS t FROM income WHERE user_id = ? AND substr(date,1,7) IN ({placeholders})",
        (user_id, *months)
    )
    total_income_lookback = cursor.fetchone()["t"] or 0
    predicted_income = round(total_income_lookback / len(months), 2)

    cursor.execute(
        f"SELECT category, SUM(amount) AS t FROM expenses WHERE user_id = ? AND substr(date,1,7) IN ({placeholders}) GROUP BY category ORDER BY t DESC",
        (user_id, *months)
    )
    predicted_by_category = [
        {"category": r["category"], "predicted": round(r["t"] / len(months), 2)}
        for r in cursor.fetchall()
    ]
    conn.close()

    if len(monthly_totals) >= 2 and monthly_totals[0] > 0:
        change_pct = round((monthly_totals[-1] - monthly_totals[0]) / monthly_totals[0] * 100, 1)
    else:
        change_pct = 0
    if change_pct > 5:
        trend = "rising"
    elif change_pct < -5:
        trend = "falling"
    else:
        trend = "steady"

    last_month = months[-1]
    next_month_dt = datetime.datetime.strptime(last_month + "-01", "%Y-%m-%d") + datetime.timedelta(days=32)
    next_month_label = next_month_dt.strftime("%B %Y")

    return {
        "based_on_months": len(months),
        "next_month_label": next_month_label,
        "predicted_total": predicted_total,
        "predicted_income": predicted_income,
        "predicted_savings": round(predicted_income - predicted_total, 2),
        "predicted_by_category": predicted_by_category[:6],
        "trend": trend,
        "change_pct": change_pct
    }


def get_anomaly_transactions(user_id, z_thresh=1.5, limit=8):
    """Flags individual expenses that are unusually large relative to their
    own category's history (mean + z_thresh standard deviations), so the
    AI Analytics page can surface 'this looks off' transactions instead of
    just aggregate totals. Categories need at least 3 entries to have a
    meaningful average - too few data points would flag everything."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, category, amount, date FROM expenses WHERE user_id = ? ORDER BY category, date",
        (user_id,)
    )
    rows = cursor.fetchall()
    conn.close()

    by_category = {}
    for r in rows:
        by_category.setdefault(r["category"], []).append(r)

    anomalies = []
    for category, entries in by_category.items():
        if len(entries) < 3:
            continue
        amounts = [e["amount"] for e in entries]
        mean = statistics.mean(amounts)
        try:
            stdev = statistics.stdev(amounts)
        except statistics.StatisticsError:
            stdev = 0
        if stdev == 0:
            continue
        for e in entries:
            z = (e["amount"] - mean) / stdev
            if z >= z_thresh:
                anomalies.append({
                    "category": category,
                    "amount": round(e["amount"], 2),
                    "date": e["date"],
                    "category_average": round(mean, 2),
                    "deviation_pct": round(((e["amount"] - mean) / mean) * 100, 1) if mean > 0 else 0
                })
    anomalies.sort(key=lambda a: a["deviation_pct"], reverse=True)
    return anomalies[:limit]


def get_smart_recommendations(user_id, forecast, anomalies):
    """Synthesizes signals across forecasting, anomaly detection, and
    existing budget/health data into forward-looking recommendations - the
    'so what should I actually do' layer that the AI Analytics page adds on
    top of the more descriptive AI Insights already used elsewhere."""
    cur = get_currency_symbol(user_id)
    recs = []

    if forecast:
        if forecast["predicted_savings"] < 0:
            recs.append(
                f"At this rate, next month's projected expenses ({cur}{forecast['predicted_total']}) "
                f"will exceed projected income ({cur}{forecast['predicted_income']}) - trim spending now to avoid a shortfall."
            )
        elif forecast["trend"] == "rising":
            recs.append(
                f"Spending has climbed {abs(forecast['change_pct'])}% over the last {forecast['based_on_months']} months - "
                f"keep an eye on {forecast['predicted_by_category'][0]['category'] if forecast['predicted_by_category'] else 'your top category'} to avoid it becoming a habit."
            )
        elif forecast["trend"] == "falling":
            recs.append(f"Spending has dropped {abs(forecast['change_pct'])}% recently - great momentum, keep it up.")

    if anomalies:
        top = anomalies[0]
        recs.append(
            f"A {cur}{top['amount']} {top['category']} expense on {top['date']} was {top['deviation_pct']}% above "
            f"your usual {top['category']} spend - worth double-checking it's expected."
        )

    debt_ratio = get_debt_to_income_ratio(user_id)
    if debt_ratio > 40:
        recs.append(f"Debt-to-income is {debt_ratio}% - prioritize paying down the highest-interest balance before it grows.")

    if not recs:
        recs.append("No red flags detected right now - your spending pattern looks consistent and on track.")

    return recs


@app.route("/intelligence")
def intelligence():
    """Intelligence Dashboard: a single page combining Financial Health (with
    a breakdown of what's driving the score), Notifications, Goal Progress +
    achievement predictions, Monthly Savings Trend, Investment Allocation,
    an Expense Heat Map, a Risk Meter, Quick Stats, and a Recent Activity
    timeline. AI Insights, Budget Recommendations, and the detailed category
    Spending Analysis live on their own pages (Analysis, Budget, AI
    Analytics) rather than being repeated here."""
    if "user_id" not in session:
        return redirect("/login")

    user_id = session["user_id"]

    financial_health = get_financial_health(user_id)
    notifications = get_notifications(user_id)
    trend_labels, trend_values = get_monthly_savings_trend(user_id)
    goal_predictions = get_goal_predictions(user_id)
    expense_heatmap = get_expense_heatmap(user_id)
    quick_stats = get_quick_stats(user_id)
    timeline = get_recent_timeline(user_id)
    debt_to_income_ratio = get_debt_to_income_ratio(user_id)

    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT asset, invested_amount, current_value FROM investments WHERE user_id = ?", (user_id,))
    investments = cursor.fetchall()
    conn.close()

    risk_level = get_risk_level(investments)
    risk_score = RISK_SCORE_MAP.get(risk_level, 0)
    asset_breakdown = get_asset_breakdown(investments)
    asset_labels = [a["asset"] for a in asset_breakdown]
    asset_values = [a["current"] for a in asset_breakdown]

    conn = get_connection()
    total_debt = conn.execute("SELECT SUM(outstanding_amount) AS t FROM debts WHERE user_id = ?", (user_id,)).fetchone()["t"] or 0
    conn.close()
    total_assets_value = sum(a["current"] for a in asset_breakdown)
    net_worth = round(total_assets_value - total_debt, 2)

    return render_template(
        "intelligence.html",
        financial_health=financial_health,
        notifications=notifications,
        trend_labels=trend_labels,
        trend_values=trend_values,
        goal_predictions=goal_predictions,
        expense_heatmap=expense_heatmap,
        quick_stats=quick_stats,
        timeline=timeline,
        debt_to_income_ratio=debt_to_income_ratio,
        risk_level=risk_level,
        risk_score=risk_score,
        asset_breakdown=asset_breakdown,
        asset_labels=asset_labels,
        asset_values=asset_values,
        net_worth=net_worth,
        total_debt=total_debt,
        now=datetime.datetime.now()
    )


@app.route("/log_debt_payment/<int:debt_id>", methods=["POST"])
def log_debt_payment(debt_id):
    """Advanced feature: record a payment against a debt instead of only
    being able to add/delete the debt itself - mirrors the goal
    'add saved amount' pattern already used elsewhere."""
    if "user_id" not in session:
        return redirect("/login")
    amount = float(request.form.get("payment_amount", 0))
    cur = get_currency_symbol(session["user_id"])
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT outstanding_amount FROM debts WHERE id = ? AND user_id = ?", (debt_id, session["user_id"]))
    row = cursor.fetchone()
    if row is not None:
        new_balance = max(0, row["outstanding_amount"] - amount)
        cursor.execute("UPDATE debts SET outstanding_amount = ? WHERE id = ? AND user_id = ?", (new_balance, debt_id, session["user_id"]))
        conn.commit()
        if new_balance == 0:
            flash("🎉 Debt fully paid off!", "success")
        else:
            flash(f"✓ Payment logged. {cur}{round(new_balance, 2)} remaining.", "success")
    conn.close()
    return redirect("/debts")


@app.route("/debts", methods=["GET", "POST"])
def debts():
    if "user_id" not in session:
        return redirect("/login")

    conn = get_connection()
    cursor = conn.cursor()
    user_id = session["user_id"]

    if request.method == "POST":
        debt_name = request.form["debt_name"]
        monthly_payment = float(request.form["monthly_payment"])
        outstanding_amount = float(request.form["outstanding_amount"])
        cursor.execute(
            "INSERT INTO debts(user_id, debt_name, monthly_payment, outstanding_amount) VALUES (?, ?, ?, ?)",
            (user_id, debt_name, monthly_payment, outstanding_amount)
        )
        conn.commit()
        flash("✓ Debt Added", "success")
        conn.close()
        return redirect("/debts")

    search_query = request.args.get("search", "")
    sql = "SELECT * FROM debts WHERE user_id = ?"
    params = [user_id]
    if search_query:
        sql += " AND debt_name LIKE ?"
        params.append(f"%{search_query}%")
    sql += " ORDER BY id DESC"
    cursor.execute(sql, params)
    debt_rows = cursor.fetchall()
    total_monthly_payment = sum(d["monthly_payment"] for d in debt_rows)
    total_outstanding = sum(d["outstanding_amount"] for d in debt_rows)
    debt_ratio = get_debt_to_income_ratio(user_id)
    conn.close()

    return render_template(
        "debts.html",
        debts=debt_rows,
        total_monthly_payment=round(total_monthly_payment, 2),
        total_outstanding=round(total_outstanding, 2),
        debt_ratio=debt_ratio,
        search_query=search_query
    )


@app.route("/edit_debt/<int:debt_id>", methods=["GET", "POST"])
def edit_debt(debt_id):
    if "user_id" not in session:
        return redirect("/login")

    conn = get_connection()
    cursor = conn.cursor()

    if request.method == "POST":
        debt_name = request.form["debt_name"]
        monthly_payment = float(request.form["monthly_payment"])
        outstanding_amount = float(request.form["outstanding_amount"])
        cursor.execute(
            "UPDATE debts SET debt_name = ?, monthly_payment = ?, outstanding_amount = ? WHERE id = ? AND user_id = ?",
            (debt_name, monthly_payment, outstanding_amount, debt_id, session["user_id"])
        )
        conn.commit()
        conn.close()
        flash("✓ Debt Updated", "success")
        return redirect("/debts")

    cursor.execute("SELECT * FROM debts WHERE id = ? AND user_id = ?", (debt_id, session["user_id"]))
    debt_item = cursor.fetchone()
    conn.close()
    return render_template("edit_debt.html", debt=debt_item)


@app.route("/delete_debt/<int:debt_id>", methods=["POST"])
def delete_debt(debt_id):
    if "user_id" not in session:
        return redirect("/login")
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM debts WHERE id = ? AND user_id = ?", (debt_id, session["user_id"]))
    conn.commit()
    conn.close()
    flash("Debt removed.", "info")
    return redirect("/debts")


@app.route("/bills", methods=["GET", "POST"])
def bills():
    if "user_id" not in session:
        return redirect("/login")

    conn = get_connection()
    cursor = conn.cursor()
    user_id = session["user_id"]

    if request.method == "POST":
        bill_name = request.form["bill_name"]
        amount = float(request.form["amount"])
        due_date = request.form["due_date"]
        is_recurring = 1 if request.form.get("is_recurring") else 0
        cursor.execute(
            "INSERT INTO bills(user_id, bill_name, amount, due_date, is_recurring) VALUES (?, ?, ?, ?, ?)",
            (user_id, bill_name, amount, due_date, is_recurring)
        )
        conn.commit()
        flash("✓ Bill Added", "success")
        conn.close()
        return redirect("/bills")

    search_query = request.args.get("search", "")
    sql = "SELECT * FROM bills WHERE user_id = ?"
    params = [user_id]
    if search_query:
        sql += " AND bill_name LIKE ?"
        params.append(f"%{search_query}%")
    sql += " ORDER BY due_date ASC"
    cursor.execute(sql, params)
    bill_rows = cursor.fetchall()
    today = datetime.date.today()
    processed_bills = []
    for b in bill_rows:
        b_dict = dict(b)
        try:
            d_date = datetime.datetime.strptime(b["due_date"], "%Y-%m-%d").date()
            days_left = (d_date - today).days
        except (ValueError, TypeError):
            days_left = None
        b_dict["days_left"] = days_left
        if days_left is None:
            b_dict["status"] = "Unknown"
        elif days_left < 0:
            b_dict["status"] = "Overdue"
        elif days_left <= 7:
            b_dict["status"] = "Due Soon"
        else:
            b_dict["status"] = "Upcoming"
        if b_dict.get("is_paid"):
            b_dict["status"] = "Paid"
        processed_bills.append(b_dict)
    conn.close()

    return render_template("bills.html", bills=processed_bills, search_query=search_query)


@app.route("/edit_bill/<int:bill_id>", methods=["GET", "POST"])
def edit_bill(bill_id):
    if "user_id" not in session:
        return redirect("/login")

    conn = get_connection()
    cursor = conn.cursor()

    if request.method == "POST":
        bill_name = request.form["bill_name"]
        amount = float(request.form["amount"])
        due_date = request.form["due_date"]
        is_recurring = 1 if request.form.get("is_recurring") else 0
        cursor.execute(
            "UPDATE bills SET bill_name = ?, amount = ?, due_date = ?, is_recurring = ? WHERE id = ? AND user_id = ?",
            (bill_name, amount, due_date, is_recurring, bill_id, session["user_id"])
        )
        conn.commit()
        conn.close()
        flash("✓ Bill Updated", "success")
        return redirect("/bills")

    cursor.execute("SELECT * FROM bills WHERE id = ? AND user_id = ?", (bill_id, session["user_id"]))
    bill_item = cursor.fetchone()
    conn.close()
    return render_template("edit_bill.html", bill=bill_item)


@app.route("/delete_bill/<int:bill_id>", methods=["POST"])
def delete_bill(bill_id):
    if "user_id" not in session:
        return redirect("/login")
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM bills WHERE id = ? AND user_id = ?", (bill_id, session["user_id"]))
    conn.commit()
    conn.close()
    flash("Bill removed.", "info")
    return redirect("/bills")


@app.route("/")
def index():
    return render_template("index.html")

@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        name = request.form["name"]
        email = request.form["email"]
        if request.form["password"] != request.form.get("confirm_password", ""):
            flash("Passwords do not match.", "danger")
            return render_template("register.html")
        # Passwords are hashed before storage - never store plaintext.
        password = generate_password_hash(request.form["password"])
        phone = request.form["phone"]
        occupation = request.form["occupation"]
        monthly_income = request.form["monthly_income"]
        member_since = datetime.date.today().strftime("%B %Y")

        conn = get_connection()
        cursor = conn.cursor()

        try:
            cursor.execute("""
            INSERT INTO users(name, email, password, phone, occupation, monthly_income, member_since)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (name, email, password, phone, occupation, monthly_income, member_since))

            conn.commit()
            conn.close()
            flash("Registration successful! Please login.", "success")
            return redirect("/login")
        except:
            conn.close()
            flash("Email already registered. Please try another email.", "danger")

    return render_template("register.html")

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form["email"]
        password = request.form["password"]

        conn = get_connection()
        cursor = conn.cursor()

        cursor.execute("SELECT * FROM users WHERE email = ?", (email,))
        user = cursor.fetchone()

        # --- Brute-force protection: lock out after 5 failed attempts for 15 min ---
        if user and user["locked_until"]:
            locked_until = datetime.datetime.strptime(user["locked_until"], "%Y-%m-%d %H:%M:%S")
            if datetime.datetime.now() < locked_until:
                wait_min = int((locked_until - datetime.datetime.now()).total_seconds() // 60) + 1
                conn.close()
                flash(f"Too many failed attempts. Try again in {wait_min} minute(s).", "danger")
                return render_template("login.html")

        valid = False
        if user:
            stored = user["password"]
            if stored.startswith("pbkdf2:") or stored.startswith("scrypt:"):
                valid = check_password_hash(stored, password)
            else:
                # Legacy plaintext password from before hashing was added.
                # Verify against it once, then transparently upgrade it to a
                # proper hash so it never has to be compared in plaintext again.
                valid = (stored == password)
                if valid:
                    cursor.execute(
                        "UPDATE users SET password = ? WHERE id = ?",
                        (generate_password_hash(password), user["id"])
                    )
                    conn.commit()

        if user and not valid:
            attempts = (user["failed_attempts"] or 0) + 1
            if attempts >= 5:
                lock_until = (datetime.datetime.now() + datetime.timedelta(minutes=15)).strftime("%Y-%m-%d %H:%M:%S")
                cursor.execute("UPDATE users SET failed_attempts = ?, locked_until = ? WHERE id = ?", (attempts, lock_until, user["id"]))
                conn.commit()
                conn.close()
                flash("Too many failed attempts. Account locked for 15 minutes.", "danger")
                return render_template("login.html")
            else:
                cursor.execute("UPDATE users SET failed_attempts = ? WHERE id = ?", (attempts, user["id"]))
                conn.commit()

        conn.close()

        if valid:
            conn = get_connection()
            conn.execute("UPDATE users SET failed_attempts = 0, locked_until = NULL WHERE id = ?", (user["id"],))
            conn.commit()
            conn.close()
            session["user_id"] = user["id"]
            session["user_name"] = user["name"]
            # Show the PREVIOUS login time on this login (not the one we're
            # about to write), then stamp now as the new last_login.
            session["previous_login"] = user["last_login"]
            conn = get_connection()
            conn.execute(
                "UPDATE users SET last_login = ? WHERE id = ?",
                (datetime.datetime.now().strftime("%Y-%m-%d %H:%M"), user["id"])
            )
            conn.commit()
            conn.close()
            flash("Welcome back!", "success")
            return redirect("/dashboard")
        else:
            flash("Invalid email or password", "danger")

    return render_template("login.html")

@app.route("/logout")
def logout():
    session.clear()
    flash("Logged out successfully.", "info")
    return redirect("/")

@app.route("/profile", methods=["GET", "POST"])
def profile():
    if "user_id" not in session:
        return redirect("/login")

    conn = get_connection()
    cursor = conn.cursor()

    if request.method == "POST":
        if "update_details" in request.form:
            name = request.form["name"]
            phone = request.form["phone"]
            occupation = request.form["occupation"]
            monthly_income = request.form["monthly_income"]
            cursor.execute("""
                UPDATE users SET name = ?, phone = ?, occupation = ?, monthly_income = ? 
                WHERE id = ?
            """, (name, phone, occupation, monthly_income, session["user_id"]))
            conn.commit()
            session["user_name"] = name
            flash("Profile updated successfully!", "success")
        elif "update_password" in request.form:
            cursor.execute("SELECT password FROM users WHERE id = ?", (session["user_id"],))
            current_hash = cursor.fetchone()["password"]
            if not check_password_hash(current_hash, request.form.get("current_password", "")):
                flash("Current password is incorrect.", "danger")
            else:
                new_pass = generate_password_hash(request.form["new_password"])
                cursor.execute("UPDATE users SET password = ? WHERE id = ?", (new_pass, session["user_id"]))
                conn.commit()
                flash("Password changed successfully!", "success")

    cursor.execute("SELECT * FROM users WHERE id = ?", (session["user_id"],))
    user = cursor.fetchone()

    if user is None:
        # Session refers to a user_id that no longer exists in this
        # database (e.g. an old session cookie left over from testing
        # against a different/reset finance.db). Fail safe instead of
        # crashing on a None lookup below.
        conn.close()
        session.clear()
        flash("Your session has expired. Please log in again.", "info")
        return redirect("/login")

    cursor.execute("SELECT COUNT(*) AS total FROM income WHERE user_id = ?", (session["user_id"],))
    inc_count = cursor.fetchone()["total"]
    cursor.execute("SELECT COUNT(*) AS total FROM expenses WHERE user_id = ?", (session["user_id"],))
    exp_count = cursor.fetchone()["total"]
    total_transactions = inc_count + exp_count

    conn.close()

    # Account completion: name + email always exist at registration, so the
    # optional fields (phone, occupation, monthly_income) decide the rest.
    optional_fields = [user["phone"], user["occupation"], user["monthly_income"]]
    filled = 2 + sum(1 for f in optional_fields if f)
    completion_pct = round(filled / 5 * 100)

    return render_template(
        "profile.html",
        user=user,
        total_transactions=total_transactions,
        completion_pct=completion_pct,
        previous_login=session.get("previous_login")
    )

@app.route("/add_income", methods=["GET", "POST"])
def add_income():
    if "user_id" not in session:
        return redirect("/login")

    if request.method == "POST":
        source = request.form["source"]
        amount = request.form["amount"]
        date = request.form["date"]
        is_recurring = 1 if request.form.get("is_recurring") else 0

        conn = get_connection()
        cursor = conn.cursor()

        cursor.execute("""
        INSERT INTO income(user_id, source, amount, date, is_recurring)
        VALUES (?, ?, ?, ?, ?)
        """, (session["user_id"], source, amount, date, is_recurring))

        conn.commit()
        conn.close()

        flash("✓ Income Added", "success")
        return redirect("/dashboard")

    return render_template("add_income.html")

@app.route("/edit_income/<int:inc_id>", methods=["GET", "POST"])
def edit_income(inc_id):
    if "user_id" not in session:
        return redirect("/login")

    conn = get_connection()
    cursor = conn.cursor()

    if request.method == "POST":
        source = request.form["source"]
        amount = request.form["amount"]
        date = request.form["date"]

        cursor.execute("""
        UPDATE income SET source = ?, amount = ?, date = ? 
        WHERE id = ? AND user_id = ?
        """, (source, amount, date, inc_id, session["user_id"]))
        conn.commit()
        conn.close()

        flash("Income updated successfully!", "success")
        return redirect("/transactions")

    cursor.execute("SELECT * FROM income WHERE id = ? AND user_id = ?", (inc_id, session["user_id"]))
    income = cursor.fetchone()
    conn.close()
    return render_template("edit_income.html", income=income)

@app.route("/delete_income/<int:income_id>", methods=["POST"])
def delete_income(income_id):
    if "user_id" not in session:
        return redirect("/login")

    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM income WHERE id = ? AND user_id = ?", (income_id, session["user_id"]))
    conn.commit()
    conn.close()

    flash("Income deleted.", "info")
    return redirect("/transactions")

@app.route("/add_expense", methods=["GET", "POST"])
def add_expense():
    if "user_id" not in session:
        return redirect("/login")

    if request.method == "POST":
        category = request.form["category"]
        amount = request.form["amount"]
        date = request.form["date"]
        note = request.form["note"]

        conn = get_connection()
        cursor = conn.cursor()

        cursor.execute("""
        INSERT INTO expenses(user_id, category, amount, date, note)
        VALUES (?, ?, ?, ?, ?)
        """, (session["user_id"], category, amount, date, note))

        conn.commit()
        conn.close()

        flash("✓ Expense Added", "success")
        return redirect("/dashboard")

    return render_template("add_expense.html")

@app.route("/edit_expense/<int:exp_id>", methods=["GET", "POST"])
def edit_expense(exp_id):
    if "user_id" not in session:
        return redirect("/login")

    conn = get_connection()
    cursor = conn.cursor()

    if request.method == "POST":
        category = request.form["category"]
        amount = request.form["amount"]
        date = request.form["date"]
        note = request.form["note"]

        cursor.execute("""
        UPDATE expenses SET category = ?, amount = ?, date = ?, note = ? 
        WHERE id = ? AND user_id = ?
        """, (category, amount, date, note, exp_id, session["user_id"]))
        conn.commit()
        conn.close()

        flash("Expense updated successfully!", "success")
        return redirect("/transactions")

    cursor.execute("SELECT * FROM expenses WHERE id = ? AND user_id = ?", (exp_id, session["user_id"]))
    expense = cursor.fetchone()
    conn.close()
    return render_template("edit_expense.html", expense=expense)

@app.route("/delete_expense/<int:expense_id>", methods=["POST"])
def delete_expense(expense_id):
    if "user_id" not in session:
        return redirect("/login")

    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM expenses WHERE id = ? AND user_id = ?", (expense_id, session["user_id"]))
    conn.commit()
    conn.close()

    flash("Expense deleted.", "info")
    return redirect("/transactions")

@app.route('/budget', methods=['GET', 'POST'])
def budget():
    if "user_id" not in session:
        return redirect(url_for("login"))

    conn = get_connection()
    cursor = conn.cursor()
    user_id = session["user_id"]

    if request.method == "POST":
        category = request.form["category"]
        # Cast to float so downstream arithmetic (remaining, percentage) is
        # always numeric instead of relying on SQLite's type affinity.
        amount = float(request.form["amount"])

        cursor.execute("SELECT * FROM budget WHERE user_id=? AND category=?", (user_id, category))
        existing = cursor.fetchone()

        if existing:
            cursor.execute("UPDATE budget SET amount=? WHERE user_id=? AND category=?", (amount, user_id, category))
            flash("✓ Budget Updated", "success")
        else:
            cursor.execute("INSERT INTO budget(user_id, category, amount) VALUES (?, ?, ?)", (user_id, category, amount))
            flash("✓ Budget Saved", "success")

        conn.commit()
        return redirect("/budget")

    cursor.execute("SELECT * FROM budget WHERE user_id = ?", (user_id,))
    budgets_raw = cursor.fetchall()
    
    budgets = []
    for b in budgets_raw:
        b_dict = dict(b)
        cursor.execute("SELECT SUM(amount) as spent FROM expenses WHERE user_id = ? AND category = ?", (user_id, b_dict['category']))
        spent_res = cursor.fetchone()
        spent = spent_res["spent"] or 0
        b_dict['spent'] = spent
        b_dict['remaining'] = b_dict['amount'] - spent
        b_dict['percentage'] = round((spent / b_dict['amount'] * 100) if b_dict['amount'] > 0 else 0, 1)
        budgets.append(b_dict)

    conn.close()
    budget_recommendations = get_budget_recommendations(user_id)
    return render_template("budget.html", budgets=budgets, budget_recommendations=budget_recommendations)

@app.route("/edit_budget/<int:b_id>", methods=["GET", "POST"])
def edit_budget(b_id):
    if "user_id" not in session:
        return redirect("/login")

    conn = get_connection()
    cursor = conn.cursor()

    if request.method == "POST":
        category = request.form["category"]
        amount = float(request.form["amount"])
        cursor.execute("UPDATE budget SET category = ?, amount = ? WHERE id = ? AND user_id = ?", (category, amount, b_id, session["user_id"]))
        conn.commit()
        conn.close()
        flash("✓ Budget Updated", "success")
        return redirect("/budget")

    cursor.execute("SELECT * FROM budget WHERE id = ? AND user_id = ?", (b_id, session["user_id"]))
    budget_item = cursor.fetchone()
    conn.close()
    return render_template("edit_budget.html", budget=budget_item)

@app.route("/delete_budget/<int:budget_id>", methods=["POST"])
def delete_budget(budget_id):
    if "user_id" not in session:
        return redirect("/login")

    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM budget WHERE id = ? AND user_id = ?", (budget_id, session["user_id"]))
    conn.commit()
    conn.close()

    flash("Budget removed.", "info")
    return redirect("/budget")

def generate_recurring_income(user_id):
    """Advanced feature: recurring income (e.g. salary) auto-repeats each
    month, mirroring the recurring-bills pattern. Runs once per dashboard
    load; idempotent - won't double up if this month's entry already exists."""
    conn = get_connection()
    cursor = conn.cursor()
    current_month = datetime.date.today().strftime("%Y-%m")
    cursor.execute("SELECT DISTINCT source, amount FROM income WHERE user_id = ? AND is_recurring = 1", (user_id,))
    recurring_sources = cursor.fetchall()
    for r in recurring_sources:
        cursor.execute(
            "SELECT COUNT(*) AS c FROM income WHERE user_id = ? AND source = ? AND substr(date,1,7) = ?",
            (user_id, r["source"], current_month)
        )
        if cursor.fetchone()["c"] == 0:
            today_str = datetime.date.today().strftime("%Y-%m-%d")
            cursor.execute(
                "INSERT INTO income(user_id, source, amount, date, is_recurring) VALUES (?, ?, ?, ?, 1)",
                (user_id, r["source"], r["amount"], today_str)
            )
    conn.commit()
    conn.close()


@app.route("/dashboard")
def dashboard():
    if "user_id" not in session:
        return redirect("/login")

    generate_recurring_income(session["user_id"])

    conn = get_connection()
    cursor = conn.cursor()
    user_id = session["user_id"]

    cursor.execute("SELECT SUM(amount) AS total_income FROM income WHERE user_id = ?", (user_id,))
    total_income = cursor.fetchone()["total_income"] or 0

    cursor.execute("SELECT SUM(amount) AS total_expense FROM expenses WHERE user_id = ?", (user_id,))
    total_expense = cursor.fetchone()["total_expense"] or 0

    cursor.execute("SELECT SUM(amount) AS total_budget FROM budget WHERE user_id = ?", (user_id,))
    total_budget = cursor.fetchone()["total_budget"] or 0

    savings = total_income - total_expense
    remaining_budget = total_budget - total_expense
    
    savings_rate = round((savings / total_income * 100), 1) if total_income > 0 else 0
    budget_used_rate = round((total_expense / total_budget * 100), 1) if total_budget > 0 else 0

    cursor.execute("SELECT category, SUM(amount) as total FROM expenses WHERE user_id = ? GROUP BY category ORDER BY total DESC LIMIT 1", (user_id,))
    highest_expense = cursor.fetchone()
    
    cursor.execute("SELECT source, SUM(amount) as total FROM income WHERE user_id = ? GROUP BY source ORDER BY total DESC LIMIT 1", (user_id,))
    highest_income = cursor.fetchone()

    cursor.execute("SELECT * FROM income WHERE user_id = ? ORDER BY id DESC LIMIT 3", (user_id,))
    recent_incomes = cursor.fetchall()
    cursor.execute("SELECT * FROM expenses WHERE user_id = ? ORDER BY id DESC LIMIT 3", (user_id,))
    recent_expenses = cursor.fetchall()
    
    cursor.execute("SELECT category, SUM(amount) as total FROM expenses WHERE user_id = ? GROUP BY category", (user_id,))
    breakdown_result = cursor.fetchall()
    
    pie_labels = [row["category"] for row in breakdown_result]
    pie_values = [row["total"] for row in breakdown_result]

    cursor.execute("SELECT SUM(current_value) AS t FROM investments WHERE user_id = ?", (user_id,))
    total_investment_value = cursor.fetchone()["t"] or 0
    cursor.execute("SELECT SUM(outstanding_amount) AS t FROM debts WHERE user_id = ?", (user_id,))
    total_debt = cursor.fetchone()["t"] or 0
    net_worth = round(savings + total_investment_value - total_debt, 2)

    conn.close()

    # --- Financial Health tier only, for the header badge. The full score,
    #     gauge, AI insights, budget recommendations, goal predictions, risk
    #     meter, and savings trend now live on the dedicated /intelligence
    #     page instead of being computed twice on every dashboard load. ---
    financial_health = get_financial_health(user_id)

    # Dynamic Time Greeting & date, computed from the actual current date
    now = datetime.datetime.now()
    hour = now.hour
    if hour < 12:
        greeting = "Good Morning"
    elif hour < 17:
        greeting = "Good Afternoon"
    else:
        greeting = "Good Evening"

    today_str = now.strftime("%A\n%d %B %Y")

    return render_template(
        "dashboard.html",
        total_income=total_income,
        total_expense=total_expense,
        total_budget=total_budget,
        savings=savings,
        remaining_budget=remaining_budget,
        savings_rate=savings_rate,
        budget_used_rate=budget_used_rate,
        net_worth=net_worth,
        financial_health=financial_health,
        highest_expense=highest_expense,
        highest_income=highest_income,
        recent_incomes=recent_incomes,
        recent_expenses=recent_expenses,
        greeting=greeting,
        today_str=today_str,
        pie_labels=pie_labels,  
        pie_values=pie_values,
        today=datetime.date.today()
    )

@app.route("/transactions")
def transactions():
    if "user_id" not in session:
        return redirect("/login")

    conn = get_connection()
    cursor = conn.cursor()
    user_id = session["user_id"]

    search_query = request.args.get("search", "")
    month_filter = request.args.get("month", "")

    income_sql = "SELECT * FROM income WHERE user_id = ?"
    expense_sql = "SELECT * FROM expenses WHERE user_id = ?"
    
    inc_params = [user_id]
    exp_params = [user_id]

    if search_query:
        income_sql += " AND source LIKE ?"
        expense_sql += " AND (category LIKE ? OR note LIKE ?)"
        inc_params.append(f"%{search_query}%")
        exp_params.extend([f"%{search_query}%", f"%{search_query}%"])

    if month_filter:
        income_sql += " AND date LIKE ?"
        expense_sql += " AND date LIKE ?"
        inc_params.append(f"{month_filter}%")
        exp_params.append(f"{month_filter}%")

    cursor.execute(income_sql + " ORDER BY date DESC", inc_params)
    all_incomes = cursor.fetchall()

    cursor.execute(expense_sql + " ORDER BY date DESC", exp_params)
    all_expenses = cursor.fetchall()

    total_records = len(all_incomes) + len(all_expenses)

    # --- Pagination: 15 rows per page, independent pages for each table ---
    PAGE_SIZE = 15
    inc_page = max(1, request.args.get("inc_page", 1, type=int))
    exp_page = max(1, request.args.get("exp_page", 1, type=int))
    inc_total_pages = max(1, -(-len(all_incomes) // PAGE_SIZE))
    exp_total_pages = max(1, -(-len(all_expenses) // PAGE_SIZE))
    inc_page = min(inc_page, inc_total_pages)
    exp_page = min(exp_page, exp_total_pages)
    incomes = all_incomes[(inc_page - 1) * PAGE_SIZE: inc_page * PAGE_SIZE]
    expenses = all_expenses[(exp_page - 1) * PAGE_SIZE: exp_page * PAGE_SIZE]

    conn.close()

    return render_template(
        "transactions.html",
        incomes=incomes,
        expenses=expenses,
        total_records=total_records,
        search_query=search_query,
        month_filter=month_filter,
        inc_page=inc_page,
        inc_total_pages=inc_total_pages,
        exp_page=exp_page,
        exp_total_pages=exp_total_pages
    )

@app.route("/analysis")
def analysis():
    if "user_id" not in session:
        return redirect("/login")
    user_id = session["user_id"]
    spending_analysis = get_spending_analysis(user_id)
    financial_health = get_financial_health(user_id)
    ai_insights = get_ai_insights(user_id)
    debt_to_income_ratio = get_debt_to_income_ratio(user_id)
    mom_labels, mom_income, mom_expense = get_income_expense_by_month(user_id)
    category_mom = get_category_month_over_month(user_id)
    return render_template(
        "analysis.html",
        spending_analysis=spending_analysis,
        financial_health=financial_health,
        ai_insights=ai_insights,
        debt_to_income_ratio=debt_to_income_ratio,
        mom_labels=mom_labels,
        mom_income=mom_income,
        mom_expense=mom_expense,
        category_mom=category_mom
    )


@app.route("/ai_analytics")
def ai_analytics():
    """AI Analytics: forecasting, anomaly detection, and a synthesized
    recommendations engine - the predictive layer on top of the more
    descriptive Analysis and Budget pages (which it links to rather than
    repeating their content)."""
    if "user_id" not in session:
        return redirect("/login")
    user_id = session["user_id"]
    forecast = get_spending_forecast(user_id)
    anomalies = get_anomaly_transactions(user_id)
    smart_recommendations = get_smart_recommendations(user_id, forecast, anomalies)
    financial_health = get_financial_health(user_id)
    return render_template(
        "ai_analytics.html",
        forecast=forecast,
        anomalies=anomalies,
        smart_recommendations=smart_recommendations,
        financial_health=financial_health
    )


@app.route("/toggle_bill_paid/<int:bill_id>", methods=["POST"])
def toggle_bill_paid(bill_id):
    """Advanced feature: bills can now be marked paid/unpaid. Paid bills
    stop generating due-date notifications. Recurring bills auto-create
    next month's bill when marked paid."""
    if "user_id" not in session:
        return redirect("/login")
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM bills WHERE id = ? AND user_id = ?", (bill_id, session["user_id"]))
    row = cursor.fetchone()
    if row is not None:
        new_state = 0 if row["is_paid"] else 1
        cursor.execute("UPDATE bills SET is_paid = ? WHERE id = ? AND user_id = ?", (new_state, bill_id, session["user_id"]))
        if new_state == 1 and row["is_recurring"]:
            try:
                next_due = datetime.datetime.strptime(row["due_date"], "%Y-%m-%d").date() + datetime.timedelta(days=30)
            except (ValueError, TypeError):
                next_due = datetime.date.today() + datetime.timedelta(days=30)
            cursor.execute(
                "INSERT INTO bills(user_id, bill_name, amount, due_date, is_recurring, is_paid) VALUES (?, ?, ?, ?, 1, 0)",
                (session["user_id"], row["bill_name"], row["amount"], next_due.strftime("%Y-%m-%d"))
            )
            flash(f"✓ Marked paid - next bill auto-created for {next_due.strftime('%d-%b-%Y')}", "success")
        conn.commit()
    conn.close()
    return redirect("/bills")

@app.route("/investments")
def investments():
    if "user_id" not in session:
        return redirect("/login")

    conn = get_connection()
    cursor = conn.cursor()
    user_id = session["user_id"]

    search_query = request.args.get("search", "").lower()
    sort_by = request.args.get("sort", "id")

    sql = "SELECT * FROM investments WHERE user_id = ?"
    params = [user_id]

    if search_query:
        sql += " AND (investment_name LIKE ? OR asset LIKE ?)"
        params.extend([f"%{search_query}%", f"%{search_query}%"])

    sort_mapping = {
        "roi": "((current_value - invested_amount) / invested_amount) DESC",
        "name": "investment_name ASC",
        "asset": "asset ASC",
        "current_value": "current_value DESC",
        "id": "id DESC"
    }
    sql += f" ORDER BY {sort_mapping.get(sort_by, 'id DESC')}"

    cursor.execute(sql, params)
    user_investments = cursor.fetchall()
    
    cursor.execute("SELECT COUNT(*) AS total_assets FROM investments WHERE user_id = ?", (user_id,))
    total_assets = cursor.fetchone()["total_assets"] or 0

    processed_investments = []
    total_roi_sum = 0
    best_inv = None
    worst_inv = None
    max_roi = -99999
    min_roi = 99999

    for inv in user_investments:
        inv_dict = dict(inv) 
        profit_loss = inv_dict['current_value'] - inv_dict['invested_amount']
        roi = round((profit_loss / inv_dict['invested_amount']) * 100, 2) if inv_dict['invested_amount'] > 0 else 0
            
        inv_dict['profit_loss'] = profit_loss
        inv_dict['roi'] = roi
        
        if roi > 20:
            inv_dict['status'] = "Excellent"
            inv_dict['status_color'] = "success"
        elif roi >= 10:
            inv_dict['status'] = "Good"
            inv_dict['status_color'] = "primary"
        elif roi >= 0:
            inv_dict['status'] = "Average"
            inv_dict['status_color'] = "warning"
        else:
            inv_dict['status'] = "Poor"
            inv_dict['status_color'] = "danger"

        total_roi_sum += roi

        if roi > max_roi:
            max_roi = roi
            best_inv = inv_dict['investment_name']
        if roi < min_roi:
            min_roi = roi
            worst_inv = inv_dict['investment_name']

        processed_investments.append(inv_dict)

    avg_roi = round(total_roi_sum / total_assets, 1) if total_assets > 0 else 0

    conn.close()
    return render_template(
        "investments.html", 
        investments=processed_investments,
        total_assets=total_assets,
        best_inv=best_inv or "N/A",
        worst_inv=worst_inv or "N/A",
        avg_roi=avg_roi,
        search_query=search_query,
        sort_by=sort_by
    )

@app.route("/add_investment", methods=["POST"])
def add_investment():
    if "user_id" not in session:
        return redirect("/login")

    asset = request.form["asset"]
    name = request.form["name"]
    invested = float(request.form["invested"])
    current = float(request.form["current"])

    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
    INSERT INTO investments(user_id, asset, investment_name, invested_amount, current_value)
    VALUES (?, ?, ?, ?, ?)
    """, (session["user_id"], asset, name, invested, current))

    conn.commit()
    conn.close()

    flash("✓ Investment Added Successfully", "success")
    return redirect("/investments")

@app.route("/edit_investment/<int:inv_id>", methods=["GET", "POST"])
def edit_investment(inv_id):
    if "user_id" not in session:
        return redirect("/login")
    
    conn = get_connection()
    cursor = conn.cursor()

    if request.method == "POST":
        asset = request.form["asset"]
        name = request.form["name"]
        invested = float(request.form["invested"])
        current = float(request.form["current"])

        cursor.execute("""
        UPDATE investments SET asset = ?, investment_name = ?, invested_amount = ?, current_value = ? 
        WHERE id = ? AND user_id = ?
        """, (asset, name, invested, current, inv_id, session["user_id"]))
        conn.commit()
        conn.close()

        flash("✓ Investment Updated Successfully", "success")
        return redirect("/investments")

    cursor.execute("SELECT * FROM investments WHERE id = ? AND user_id = ?", (inv_id, session["user_id"]))
    inv = cursor.fetchone()
    conn.close()
    return render_template("edit_investment.html", investment=inv)

@app.route("/delete_investment/<int:inv_id>", methods=["POST"])
def delete_investment(inv_id):
    if "user_id" not in session:
        return redirect("/login")

    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM investments WHERE id = ? AND user_id = ?", (inv_id, session["user_id"]))
    conn.commit()
    conn.close()

    flash("Investment removed.", "info")
    return redirect("/investments")

@app.route("/goals")
def goals():
    if "user_id" not in session:
        return redirect("/login")

    conn = get_connection()
    cursor = conn.cursor()
    
    search_query = request.args.get("search", "")
    sql = "SELECT * FROM goals WHERE user_id = ?"
    params = [session["user_id"]]
    if search_query:
        sql += " AND goal_name LIKE ?"
        params.append(f"%{search_query}%")
    sql += " ORDER BY id DESC"
    cursor.execute(sql, params)
    user_goals = cursor.fetchall()
    conn.close()
    
    processed_goals = []
    today = datetime.date.today()
    for goal in user_goals:
        goal_dict = dict(goal)
        target = goal_dict['target_amount']
        saved = goal_dict['saved_amount']
        
        progress = 0
        if target > 0:
            progress = (saved / target) * 100
            
        if progress > 100:
            progress = 100
            
        goal_dict['progress'] = round(progress, 1)
        goal_dict['remaining'] = max(0, target - saved)
        
        if goal_dict['target_date']:
            try:
                d_date = datetime.datetime.strptime(goal_dict['target_date'], "%Y-%m-%d").date()
                delta = (d_date - today).days
                goal_dict['days_remaining'] = delta if delta >= 0 else 0
            except:
                goal_dict['days_remaining'] = 0
        else:
            goal_dict['days_remaining'] = 0

        if progress >= 100:
            goal_dict['status'] = "Completed"
        elif goal_dict['days_remaining'] == 0:
            goal_dict['status'] = "Pending"
        else:
            goal_dict['status'] = "In Progress"

        processed_goals.append(goal_dict)

    # --- Goal Analytics summary (Milestone 2, Module 3: Goal Completion
    #     Percentage / Remaining Amount, surfaced across the whole list) ---
    total_goals = len(processed_goals)
    goals_achieved = len([g for g in processed_goals if g['status'] == 'Completed'])
    overall_completion_pct = round(sum(g['progress'] for g in processed_goals) / total_goals, 1) if total_goals > 0 else 0
    remaining_savings_required = round(sum(g['remaining'] for g in processed_goals), 2)

    return render_template(
        "goals.html",
        goals=processed_goals,
        total_goals=total_goals,
        goals_achieved=goals_achieved,
        overall_completion_pct=overall_completion_pct,
        remaining_savings_required=remaining_savings_required,
        search_query=search_query
    )

@app.route("/add_goal", methods=["POST"])
def add_goal():
    if "user_id" not in session:
        return redirect("/login")

    name = request.form["name"]
    target = float(request.form["target"])
    saved = float(request.form["saved"])
    date = request.form["date"]

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
    INSERT INTO goals (user_id, goal_name, target_amount, saved_amount, target_date)
    VALUES (?, ?, ?, ?, ?)
    """, (session["user_id"], name, target, saved, date))

    conn.commit()
    conn.close()

    flash("✓ Goal Created Successfully", "success")
    return redirect("/goals")

@app.route("/update_goal/<int:goal_id>", methods=["POST"])
def update_goal(goal_id):
    if "user_id" not in session:
        return redirect("/login")

    added_amount = float(request.form["added_amount"])

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
    UPDATE goals 
    SET saved_amount = saved_amount + ? 
    WHERE id = ? AND user_id = ?
    """, (added_amount, goal_id, session["user_id"]))

    conn.commit()
    conn.close()

    flash("✓ Goal Progress Updated", "success")
    return redirect("/goals")

@app.route("/edit_goal/<int:goal_id>", methods=["GET", "POST"])
def edit_goal(goal_id):
    if "user_id" not in session:
        return redirect("/login")

    conn = get_connection()
    cursor = conn.cursor()

    if request.method == "POST":
        name = request.form["name"]
        target = float(request.form["target"])
        date = request.form["date"]

        cursor.execute("""
        UPDATE goals SET goal_name = ?, target_amount = ?, target_date = ? 
        WHERE id = ? AND user_id = ?
        """, (name, target, date, goal_id, session["user_id"]))
        conn.commit()
        conn.close()

        flash("✓ Goal Updated Successfully", "success")
        return redirect("/goals")

    cursor.execute("SELECT * FROM goals WHERE id = ? AND user_id = ?", (goal_id, session["user_id"]))
    goal = cursor.fetchone()
    conn.close()
    return render_template("edit_goal.html", goal=goal)

@app.route("/delete_goal/<int:goal_id>", methods=["POST"])
def delete_goal(goal_id):
    if "user_id" not in session:
        return redirect("/login")

    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM goals WHERE id = ? AND user_id = ?", (goal_id, session["user_id"]))
    conn.commit()
    conn.close()

    flash("Goal removed.", "info")
    return redirect("/goals")

@app.route('/export_goals')
def export_goals():
    if "user_id" not in session:
        return redirect("/login")
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT goal_name, target_amount, saved_amount, target_date FROM goals WHERE user_id = ?", (session["user_id"],))
    rows = cursor.fetchall()
    conn.close()
    si = StringIO()
    cw = csv.writer(si)
    cw.writerow(['Goal', 'Target (₹)', 'Saved (₹)', 'Target Date'])
    for g in rows:
        cw.writerow([g['goal_name'], g['target_amount'], g['saved_amount'], g['target_date']])
    output = make_response(si.getvalue())
    output.headers["Content-Disposition"] = "attachment; filename=Goals.csv"
    output.headers["Content-type"] = "text/csv"
    return output


@app.route('/export_all_data')
def export_all_data():
    """Single-click full account export - every table the user owns,
    combined into one CSV with a section header per table. Complements
    the existing per-page exports (Debts, Goals, etc.) for people who
    want everything at once instead of table-by-table."""
    if "user_id" not in session:
        return redirect("/login")
    user_id = session["user_id"]
    conn = get_connection()
    cursor = conn.cursor()

    si = StringIO()
    cw = csv.writer(si)

    sections = [
        ("INCOME", "SELECT source, amount, date FROM income WHERE user_id = ? ORDER BY date DESC",
         ["Source", "Amount", "Date"]),
        ("EXPENSES", "SELECT category, amount, date FROM expenses WHERE user_id = ? ORDER BY date DESC",
         ["Category", "Amount", "Date"]),
        ("BUDGET", "SELECT category, amount FROM budget WHERE user_id = ?",
         ["Category", "Monthly Limit"]),
        ("BILLS", "SELECT bill_name, amount, due_date, is_paid FROM bills WHERE user_id = ? ORDER BY due_date",
         ["Bill", "Amount", "Due Date", "Paid"]),
        ("DEBTS", "SELECT debt_name, monthly_payment, outstanding_amount FROM debts WHERE user_id = ?",
         ["Debt", "Monthly Payment", "Outstanding"]),
        ("INVESTMENTS", "SELECT asset, invested_amount, current_value FROM investments WHERE user_id = ?",
         ["Asset", "Invested Amount", "Current Value"]),
        ("GOALS", "SELECT goal_name, target_amount, saved_amount, target_date FROM goals WHERE user_id = ?",
         ["Goal", "Target Amount", "Saved Amount", "Target Date"]),
    ]

    for title, query, headers in sections:
        cw.writerow([f"--- {title} ---"])
        cw.writerow(headers)
        cursor.execute(query, (user_id,))
        for row in cursor.fetchall():
            cw.writerow(list(row))
        cw.writerow([])

    conn.close()
    output = make_response(si.getvalue())
    output.headers["Content-Disposition"] = "attachment; filename=Smart_Finance_Full_Export.csv"
    output.headers["Content-type"] = "text/csv"
    return output


@app.route('/export_debts')
def export_debts():
    if "user_id" not in session:
        return redirect("/login")
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT debt_name, monthly_payment, outstanding_amount FROM debts WHERE user_id = ?", (session["user_id"],))
    rows = cursor.fetchall()
    conn.close()
    si = StringIO()
    cw = csv.writer(si)
    cw.writerow(['Debt', 'Monthly Payment (₹)', 'Outstanding (₹)'])
    for d in rows:
        cw.writerow([d['debt_name'], d['monthly_payment'], d['outstanding_amount']])
    output = make_response(si.getvalue())
    output.headers["Content-Disposition"] = "attachment; filename=Debts.csv"
    output.headers["Content-type"] = "text/csv"
    return output


@app.route('/export_goals_pdf')
def export_goals_pdf():
    if "user_id" not in session:
        return redirect("/login")
    from reportlab.lib.pagesizes import A4
    from reportlab.lib import colors
    from reportlab.lib.units import mm
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
    from reportlab.lib.styles import getSampleStyleSheet

    conn = get_connection()
    rows = conn.execute("SELECT goal_name, target_amount, saved_amount, target_date FROM goals WHERE user_id = ?", (session["user_id"],)).fetchall()
    conn.close()

    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4, topMargin=20*mm, bottomMargin=20*mm)
    styles = getSampleStyleSheet()
    elements = [Paragraph("Goals Report", styles["Title"]), Spacer(1, 12)]
    data = [["Goal", "Target (Rs.)", "Saved (Rs.)", "Target Date"]]
    for g in rows:
        data.append([g["goal_name"], f"{g['target_amount']:,.2f}", f"{g['saved_amount']:,.2f}", g["target_date"] or "-"])
    table = Table(data, repeatRows=1)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0d6efd")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.whitesmoke]),
        ("PADDING", (0, 0), (-1, -1), 5),
    ]))
    elements.append(table)
    doc.build(elements)
    buffer.seek(0)
    output = make_response(buffer.getvalue())
    output.headers["Content-Disposition"] = "attachment; filename=Goals.pdf"
    output.headers["Content-type"] = "application/pdf"
    return output


@app.route('/export_debts_pdf')
def export_debts_pdf():
    if "user_id" not in session:
        return redirect("/login")
    from reportlab.lib.pagesizes import A4
    from reportlab.lib import colors
    from reportlab.lib.units import mm
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
    from reportlab.lib.styles import getSampleStyleSheet

    conn = get_connection()
    rows = conn.execute("SELECT debt_name, monthly_payment, outstanding_amount FROM debts WHERE user_id = ?", (session["user_id"],)).fetchall()
    conn.close()

    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4, topMargin=20*mm, bottomMargin=20*mm)
    styles = getSampleStyleSheet()
    elements = [Paragraph("Debts Report", styles["Title"]), Spacer(1, 12)]
    data = [["Debt", "Monthly Payment (Rs.)", "Outstanding (Rs.)"]]
    for d in rows:
        data.append([d["debt_name"], f"{d['monthly_payment']:,.2f}", f"{d['outstanding_amount']:,.2f}"])
    table = Table(data, repeatRows=1)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0d6efd")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.whitesmoke]),
        ("PADDING", (0, 0), (-1, -1), 5),
    ]))
    elements.append(table)
    doc.build(elements)
    buffer.seek(0)
    output = make_response(buffer.getvalue())
    output.headers["Content-Disposition"] = "attachment; filename=Debts.pdf"
    output.headers["Content-type"] = "application/pdf"
    return output


@app.route('/export_report')
def export_report():
    if "user_id" not in session:
        return redirect("/login")

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
    SELECT date, category, amount, note 
    FROM expenses 
    WHERE user_id = ? 
    ORDER BY date DESC
    """, (session["user_id"],))
    
    expenses = cursor.fetchall()
    conn.close()

    si = StringIO()
    cw = csv.writer(si)
    cw.writerow(['Date', 'Category', 'Amount (₹)', 'Note'])
    for expense in expenses:
        cw.writerow([expense['date'], expense['category'], expense['amount'], expense['note']])
    
    output = make_response(si.getvalue())
    output.headers["Content-Disposition"] = "attachment; filename=Financial_Report.csv"
    output.headers["Content-type"] = "text/csv"
    return output

@app.route('/export_investments')
def export_investments():
    if "user_id" not in session:
        return redirect("/login")

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
    SELECT asset, investment_name, invested_amount, current_value 
    FROM investments 
    WHERE user_id = ? 
    ORDER BY id DESC
    """, (session["user_id"],))
    
    investments = cursor.fetchall()
    conn.close()

    si = StringIO()
    cw = csv.writer(si)
    cw.writerow(['Asset Type', 'Investment Name', 'Invested Amount (₹)', 'Current Value (₹)', 'Profit/Loss (₹)', 'ROI (%)'])
    for inv in investments:
        profit_loss = inv['current_value'] - inv['invested_amount']
        roi = round((profit_loss / inv['invested_amount']) * 100, 2) if inv['invested_amount'] > 0 else 0
        cw.writerow([inv['asset'], inv['investment_name'], inv['invested_amount'], inv['current_value'], profit_loss, roi])
    
    output = make_response(si.getvalue())
    output.headers["Content-Disposition"] = "attachment; filename=Investment_Portfolio.csv"
    output.headers["Content-type"] = "text/csv"
    return output

@app.route('/export_investments_pdf')
def export_investments_pdf():
    """Generate a formatted PDF snapshot of the user's investment portfolio."""
    if "user_id" not in session:
        return redirect("/login")

    from reportlab.lib.pagesizes import A4
    from reportlab.lib import colors
    from reportlab.lib.units import mm
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
    from reportlab.lib.styles import getSampleStyleSheet

    conn = get_connection()
    cursor = conn.cursor()
    user_id = session["user_id"]

    cursor.execute("""
    SELECT asset, investment_name, invested_amount, current_value 
    FROM investments 
    WHERE user_id = ? 
    ORDER BY id DESC
    """, (user_id,))
    investments = cursor.fetchall()
    conn.close()

    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4, topMargin=20*mm, bottomMargin=20*mm)
    styles = getSampleStyleSheet()
    elements = []

    elements.append(Paragraph("Investment Portfolio Report", styles["Title"]))
    elements.append(Paragraph(datetime.date.today().strftime("Generated on %d %B %Y"), styles["Normal"]))
    elements.append(Spacer(1, 12))

    total_invested = sum(inv['invested_amount'] for inv in investments)
    total_current = sum(inv['current_value'] for inv in investments)
    total_profit = total_current - total_invested
    overall_roi = round((total_profit / total_invested * 100), 2) if total_invested > 0 else 0

    summary_data = [
        ["Total Invested", f"Rs. {total_invested:,.2f}"],
        ["Current Value", f"Rs. {total_current:,.2f}"],
        ["Profit / Loss", f"Rs. {total_profit:,.2f}"],
        ["Overall ROI", f"{overall_roi}%"],
    ]
    summary_table = Table(summary_data, colWidths=[100*mm, 60*mm])
    summary_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.whitesmoke),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
        ("PADDING", (0, 0), (-1, -1), 6),
    ]))
    elements.append(summary_table)
    elements.append(Spacer(1, 18))

    elements.append(Paragraph("Holdings", styles["Heading2"]))
    table_data = [["Asset", "Investment", "Invested (Rs.)", "Current (Rs.)", "P/L (Rs.)", "ROI (%)"]]
    for inv in investments:
        profit_loss = inv['current_value'] - inv['invested_amount']
        roi = round((profit_loss / inv['invested_amount']) * 100, 2) if inv['invested_amount'] > 0 else 0
        table_data.append([
            inv['asset'],
            inv['investment_name'],
            f"{inv['invested_amount']:,.2f}",
            f"{inv['current_value']:,.2f}",
            f"{profit_loss:,.2f}",
            f"{roi}%"
        ])

    holdings_table = Table(table_data, repeatRows=1)
    holdings_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0d6efd")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.whitesmoke]),
        ("PADDING", (0, 0), (-1, -1), 5),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
    ]))
    elements.append(holdings_table)

    doc.build(elements)
    buffer.seek(0)

    output = make_response(buffer.getvalue())
    output.headers["Content-Disposition"] = "attachment; filename=Investment_Portfolio.pdf"
    output.headers["Content-type"] = "application/pdf"
    return output

@app.route("/update_investment/<int:inv_id>", methods=["POST"])
def update_investment(inv_id):
    if "user_id" not in session:
        return redirect("/login")

    new_current_value = float(request.form["new_current_value"])
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE investments SET current_value = ? WHERE id = ? AND user_id = ?", (new_current_value, inv_id, session["user_id"]))
    conn.commit()
    conn.close()

    flash("Investment updated!", "success")
    return redirect("/investments")

# ==========================================
# ADVANCED FEATURES: CALCULATOR & MONTHLY REPORT
# ==========================================
@app.route("/calculator", methods=["GET", "POST"])
def calculator():
    if "user_id" not in session:
        return redirect("/login")
    
    emi_result = None
    if request.method == "POST":
        p = float(request.form.get("principal", 0))
        r = float(request.form.get("rate", 0)) / 12 / 100
        n = float(request.form.get("months", 1))
        if r > 0:
            emi_result = round((p * r * (1 + r)**n) / ((1 + r)**n - 1), 2)
        else:
            emi_result = round(p / n, 2)
            
    return render_template("calculator.html", emi_result=emi_result)

@app.route("/monthly_report")
def monthly_report():
    if "user_id" not in session:
        return redirect("/login")
        
    conn = get_connection()
    cursor = conn.cursor()
    user_id = session["user_id"]
    
    cursor.execute("SELECT SUM(amount) as total FROM income WHERE user_id = ?", (user_id,))
    total_income = cursor.fetchone()["total"] or 0
    
    cursor.execute("SELECT SUM(amount) as total FROM expenses WHERE user_id = ?", (user_id,))
    total_expense = cursor.fetchone()["total"] or 0
    
    conn.close()
    net_savings = total_income - total_expense
    
    return render_template("monthly_report.html", income=total_income, expenses=total_expense, savings=net_savings)

# ==========================================
# MODULE 2: ASSET ALLOCATION ROUTE
# ==========================================
@app.route("/asset_allocation")
def asset_allocation():
    if "user_id" not in session: 
        return redirect("/login")

    conn = get_connection()
    cursor = conn.cursor()
    user_id = session.get("user_id", 1) 

    cursor.execute("SELECT * FROM investments WHERE user_id = ?", (user_id,))
    investments = cursor.fetchall()
    
    total_invested = sum(inv['invested_amount'] for inv in investments)
    total_current = sum(inv['current_value'] for inv in investments)
    total_profit = total_current - total_invested
    
    overall_roi = round((total_profit / total_invested * 100), 2) if total_invested > 0 else 0

    cursor.execute("""
        SELECT asset, SUM(invested_amount) as invested, SUM(current_value) as total 
        FROM investments WHERE user_id = ? GROUP BY asset
    """, (user_id,))
    asset_data = cursor.fetchall()
    
    asset_labels = [row['asset'] for row in asset_data]
    asset_values = [row['total'] for row in asset_data]
    
    asset_breakdown = []
    for row in asset_data:
        percentage = round((row['total'] / total_current) * 100, 1) if total_current > 0 else 0
        roi = round(((row['total'] - row['invested']) / row['invested']) * 100, 2) if row['invested'] > 0 else 0
        asset_breakdown.append({
            "asset": row['asset'],
            "invested": round(row['invested'], 2),
            "current": round(row['total'], 2),
            "roi": roi,
            "percentage": percentage
        })

    # --- Largest / smallest holding by current value ---
    largest_holding = "N/A"
    smallest_holding = "N/A"
    if asset_breakdown:
        largest_holding = max(asset_breakdown, key=lambda x: x['current'])['asset']
        smallest_holding = min(asset_breakdown, key=lambda x: x['current'])['asset']

    total_assets_count = len(asset_breakdown)

    # --- Risk level: same crypto-concentration logic used on /portfolio, kept
    #     consistent across pages instead of re-deriving it differently here ---
    risk_level = "Moderate"
    crypto_total = sum(
        inv['current_value'] for inv in investments
        if inv['asset'].lower() == 'cryptocurrency'
    )
    if total_current > 0:
        if (crypto_total / total_current) > 0.3:
            risk_level = "High"
        elif (crypto_total / total_current) == 0:
            risk_level = "Low"
    else:
        risk_level = "N/A"

    # Map risk to a diversification label for the "Diversification" stat card
    # (kept separate from the badge below, since that one already speaks to
    # concentration risk directly and doesn't need a duplicate label)
    diversification_label = {
        "Low": "Excellent",
        "Moderate": "Good",
        "High": "Needs Attention",
        "N/A": "N/A"
    }[risk_level]

    # --- Health score: blends diversification (asset count), concentration
    #     risk, and ROI into a single 0-100 figure, replacing the old
    #     hardcoded default(91) ---
    health_score = 0
    if total_invested > 0:
        diversification_component = min(100, total_assets_count * 20)  # 5+ asset classes = full marks
        risk_component = {"Low": 100, "Moderate": 65, "High": 30, "N/A": 0}[risk_level]
        roi_component = max(0, min(100, 50 + overall_roi))  # 0% ROI -> 50, scales from there
        health_score = round(
            diversification_component * 0.35 + risk_component * 0.35 + roi_component * 0.30
        )
        health_score = max(0, min(100, health_score))

    investments_list = [dict(row) for row in investments]
    conn.close()

    return render_template("asset_allocation.html", 
                           total_invested=round(total_invested, 2),
                           total_current=round(total_current, 2),
                           total_profit=round(total_profit, 2),
                           overall_roi=overall_roi,
                           asset_labels=asset_labels,
                           asset_values=asset_values,
                           asset_breakdown=asset_breakdown,
                           raw_investments=investments_list,
                           largest_holding=largest_holding,
                           smallest_holding=smallest_holding,
                           total_assets_count=total_assets_count,
                           risk_level=diversification_label,
                           health_score=health_score)
    
# ==========================================
# MODULE 4: PORTFOLIO MASTER DASHBOARD ROUTE
# ==========================================
@app.route("/portfolio")
def portfolio():
    if "user_id" not in session:
        return redirect("/login")

    conn = get_connection()
    cursor = conn.cursor()
    user_id = session.get("user_id", 1)

    cursor.execute("SELECT * FROM investments WHERE user_id = ? ORDER BY id ASC", (user_id,))
    investments = cursor.fetchall()
    
    cursor.execute("SELECT goal_name, target_amount, saved_amount FROM goals WHERE user_id = ?", (user_id,))
    goals = [{"name": g[0], "target": g[1], "saved": g[2]} for g in cursor.fetchall()]
    
    total_invested = sum(inv['invested_amount'] for inv in investments)
    total_current = sum(inv['current_value'] for inv in investments)
    total_profit = total_current - total_invested
    overall_roi = round((total_profit / total_invested * 100), 2) if total_invested > 0 else 0

    processed_investments = []
    categories = {}
    highest_investment = "None"
    smallest_investment = "None"
    max_val = -1
    min_val = float("inf")
    highest_roi_name = "None"
    highest_roi_val = -999
    lowest_roi_name = "None"
    lowest_roi_val = 999

    total_investments_count = len(investments)
    winning_investments_count = 0
    losing_investments_count = 0
    total_roi_sum = 0
    roi_counted = 0

    for inv in investments:
        cat = inv['asset']
        categories[cat] = categories.get(cat, 0) + inv['current_value']

        # Largest / smallest holding by invested amount
        if inv['invested_amount'] > max_val:
            max_val = inv['invested_amount']
            highest_investment = inv['investment_name']
        if inv['invested_amount'] < min_val:
            min_val = inv['invested_amount']
            smallest_investment = inv['investment_name']

        if inv['current_value'] > inv['invested_amount']:
            winning_investments_count += 1
        elif inv['current_value'] < inv['invested_amount']:
            losing_investments_count += 1

        if inv['invested_amount'] > 0:
            roi = round(((inv['current_value'] - inv['invested_amount']) / inv['invested_amount']) * 100, 2)
            processed_investments.append({"name": inv['investment_name'], "roi": roi, "asset": inv['asset']})
            total_roi_sum += roi
            roi_counted += 1
            if roi > highest_roi_val:
                highest_roi_val = roi
                highest_roi_name = inv['investment_name']
            if roi < lowest_roi_val:
                lowest_roi_val = roi
                lowest_roi_name = inv['investment_name']

    # Most recently added investment (table is ordered ASC by id, so last row is newest)
    recent_investment = investments[-1]['investment_name'] if investments else "N/A"

    average_roi = round(total_roi_sum / roi_counted, 2) if roi_counted > 0 else 0

    processed_investments.sort(key=lambda x: x['roi'], reverse=True)
    top_assets = processed_investments[:2] if processed_investments else []
    worst_assets = processed_investments[-2:] if len(processed_investments) >= 2 else []
    worst_assets.reverse() 

    growth_labels = []
    growth_values = []
    if total_invested > 0:
        today = datetime.date.today()
        for i in range(5, -1, -1):
            month_date = today - datetime.timedelta(days=30 * i)
            growth_labels.append(month_date.strftime("%b %Y"))
        multipliers = [0.20, 0.40, 0.55, 0.75, 0.90, 1.0]
        growth_values = [round(total_invested * m, 2) for m in multipliers]

    highest_value = max(growth_values) if growth_values else (total_current if total_current else 0)
    lowest_value = min(growth_values) if growth_values else (total_invested if total_invested else 0)

    risk_level = "Moderate"
    risk_score = 65
    crypto_total = sum(inv['current_value'] for inv in investments if inv['asset'].lower() == 'cryptocurrency')
    if total_current > 0:
        if (crypto_total / total_current) > 0.3:
            risk_level = "High"
            risk_score = 85
        elif (crypto_total / total_current) == 0:
            risk_level = "Low"
            risk_score = 30

    # Diversification & concentration metrics
    num_asset_types = len(categories)
    if num_asset_types >= 4:
        diversification_status = "Well Diversified"
    elif num_asset_types >= 2:
        diversification_status = "Moderately Diversified"
    elif num_asset_types == 1:
        diversification_status = "Concentrated"
    else:
        diversification_status = "N/A"

    largest_holding = "N/A"
    largest_holding_pct = 0
    if categories and total_current > 0:
        largest_holding = max(categories, key=categories.get)
        largest_holding_pct = round((categories[largest_holding] / total_current) * 100, 1)

    conn.close()

    return render_template("portfolio.html", 
                           goals=goals,
                           total_invested=total_invested,
                           total_current=total_current,
                           total_profit=total_profit,
                           overall_roi=overall_roi,
                           top_assets=top_assets,
                           worst_assets=worst_assets,
                           growth_labels=growth_labels,
                           growth_values=growth_values,
                           risk_level=risk_level,
                           risk_score=risk_score,
                           categories=categories,
                           highest_investment=highest_investment,
                           highest_roi_name=highest_roi_name,
                           highest_roi_val=highest_roi_val,
                           lowest_roi_name=lowest_roi_name,
                           lowest_roi_val=lowest_roi_val,
                           # --- newly supplied fields that portfolio.html expects ---
                           best_asset=highest_roi_name,
                           worst_asset=lowest_roi_name,
                           largest_investment=highest_investment,
                           smallest_investment=smallest_investment,
                           average_roi=average_roi,
                           recent_investment=recent_investment,
                           total_investments_count=total_investments_count,
                           winning_investments_count=winning_investments_count,
                           losing_investments_count=losing_investments_count,
                           highest_value=highest_value,
                           lowest_value=lowest_value,
                           diversification_status=diversification_status,
                           largest_holding=largest_holding,
                           largest_holding_pct=largest_holding_pct,
                           now=datetime.datetime.now())
    
if __name__ == "__main__":
    # Defaults to debug OFF - safer if this ever gets deployed as-is. Set
    # FLASK_DEBUG=1 in the environment for local development to get the
    # auto-reloader and interactive debugger back.
    app.run(debug=os.environ.get("FLASK_DEBUG", "0") == "1")