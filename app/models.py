"""
Mock core-banking DB layer. Raw sqlite3 (no ORM) deliberately -
closer to how a real legacy internal app would be wired, and keeps
markup/behavior fully in our control for the automation targets.
"""
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "bank.db"


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_conn()
    conn.executescript("""
        DROP TABLE IF EXISTS members;
        DROP TABLE IF EXISTS subaccounts;
        DROP TABLE IF EXISTS transactions;

        CREATE TABLE members (
            member_id TEXT PRIMARY KEY,
            first_name TEXT NOT NULL,
            last_name TEXT NOT NULL,
            savings_balance_cents INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'active'  -- active | locked
        );

        CREATE TABLE subaccounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            member_id TEXT NOT NULL,
            account_type TEXT NOT NULL,
            nickname TEXT,
            opening_deposit_cents INTEGER NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            member_id TEXT NOT NULL,
            txn_date TEXT NOT NULL,
            description TEXT NOT NULL,
            amount_cents INTEGER NOT NULL,  -- negative = debit, positive = credit
            status TEXT NOT NULL DEFAULT 'posted',  -- posted | disputed
            dispute_reason TEXT
        );
    """)
    conn.commit()
    conn.close()


def seed():
    conn = get_conn()
    members = [
        ("12345", "Dana", "Whitfield", 184230, "active"),
        ("23456", "Marcus", "Oyelaran", 502, "active"),
        ("34567", "Priya", "Ramaswamy", 990100, "active"),
        ("45678", "Wei", "Chen", 0, "active"),
        ("56789", "Sofia", "Alvarez", 12750, "active"),
        ("99999", "Restricted", "Account", 0, "locked"),  # permission-denied test case
        # NOTE: member_id "88888" intentionally NOT seeded -> not-found test case
    ]
    conn.executemany(
        "INSERT INTO members (member_id, first_name, last_name, savings_balance_cents, status) "
        "VALUES (?, ?, ?, ?, ?)",
        members,
    )

    # ordered newest-first per member, so "the latest transaction" is always row 1
    transactions = [
        ("12345", "2026-08-15", "Grocery Store Purchase", -4523),
        ("12345", "2026-08-10", "Payroll Deposit", 250000),
        ("12345", "2026-08-05", "ATM Withdrawal", -10000),
        ("12345", "2026-07-29", "Coffee Shop", -650),
        ("23456", "2026-08-12", "Online Transfer Out", -5000),
        ("23456", "2026-08-01", "Payroll Deposit", 80000),
    ]
    conn.executemany(
        "INSERT INTO transactions (member_id, txn_date, description, amount_cents) "
        "VALUES (?, ?, ?, ?)",
        transactions,
    )
    conn.commit()
    conn.close()


def list_transactions(member_id: str):
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM transactions WHERE member_id = ? ORDER BY txn_date DESC, id DESC",
        (member_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_transaction(transaction_id: int):
    conn = get_conn()
    row = conn.execute("SELECT * FROM transactions WHERE id = ?", (transaction_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def dispute_transaction(transaction_id: int, reason: str):
    conn = get_conn()
    conn.execute(
        "UPDATE transactions SET status = 'disputed', dispute_reason = ? WHERE id = ?",
        (reason, transaction_id),
    )
    conn.commit()
    conn.close()


def find_member(member_id: str):
    conn = get_conn()
    row = conn.execute("SELECT * FROM members WHERE member_id = ?", (member_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def search_members(query: str):
    conn = get_conn()
    like = f"%{query}%"
    rows = conn.execute(
        "SELECT * FROM members WHERE member_id LIKE ? OR first_name LIKE ? OR last_name LIKE ?",
        (like, like, like),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def create_subaccount(member_id: str, account_type: str, nickname: str, opening_deposit_cents: int):
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO subaccounts (member_id, account_type, nickname, opening_deposit_cents) "
        "VALUES (?, ?, ?, ?)",
        (member_id, account_type, nickname, opening_deposit_cents),
    )
    conn.commit()
    new_id = cur.lastrowid
    conn.close()
    return new_id
