import sqlite3
import os
from contextlib import closing
from typing import List, Optional

DB_PATH = os.getenv("DB_PATH", "storage/sqlite.db")


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with closing(sqlite3.connect(DB_PATH)) as con:
        cur = con.cursor()
        cur.executescript("""
        PRAGMA journal_mode=WAL;

        CREATE TABLE IF NOT EXISTS users(
          user_id INTEGER PRIMARY KEY,
          wallet TEXT
        );

        CREATE TABLE IF NOT EXISTS boards(
          board_id INTEGER PRIMARY KEY AUTOINCREMENT,
          user_id INTEGER NOT NULL,
          token_id TEXT,
          has_free_center BOOLEAN DEFAULT 1,
          created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS board_numbers(
          board_id INTEGER, r INTEGER, c INTEGER, val INTEGER,
          PRIMARY KEY(board_id, r, c)
        );

        CREATE TABLE IF NOT EXISTS sessions(
          session_id INTEGER PRIMARY KEY AUTOINCREMENT,
          chat_id INTEGER NOT NULL,
          host_user_id INTEGER NOT NULL,
          pattern TEXT DEFAULT 'standard',
          status TEXT CHECK(status IN('live','ended')) DEFAULT 'live',
          created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS session_players(
          session_id INTEGER, user_id INTEGER,
          PRIMARY KEY(session_id, user_id)
        );

        CREATE TABLE IF NOT EXISTS session_boards(
          session_id INTEGER, board_id INTEGER,
          PRIMARY KEY(session_id, board_id)
        );

        CREATE TABLE IF NOT EXISTS draws(
          session_id INTEGER, idx INTEGER, number INTEGER,
          drawn_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
          PRIMARY KEY(session_id, idx)
        );

        CREATE TABLE IF NOT EXISTS claims(
          session_id INTEGER, board_id INTEGER, user_id INTEGER, pattern TEXT,
          claimed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
          PRIMARY KEY(session_id, board_id)
        );

        CREATE TABLE IF NOT EXISTS user_stats(
          user_id INTEGER PRIMARY KEY,
          total_sessions INTEGER DEFAULT 0,
          total_boards_joined INTEGER DEFAULT 0,
          total_bingos INTEGER DEFAULT 0,
          last_played TIMESTAMP
        );

        CREATE INDEX IF NOT EXISTS idx_draws_session ON draws(session_id);
        CREATE INDEX IF NOT EXISTS idx_bn_board    ON board_numbers(board_id);
        """)
        con.commit()


def conn():
    return sqlite3.connect(DB_PATH)


# --- Users / Wallets ----------------------------------------------------

def set_user_wallet(user_id: int, wallet: str):
    """
    Speichert oder aktualisiert die Wallet-Adresse eines Users.
    Wird z.B. beim ersten Board-Upload gesetzt und bei Änderung überschrieben.
    """
    with conn() as con:
        cur = con.cursor()
        # SQLite ON CONFLICT upsert auf user_id
        cur.execute(
            """
            INSERT INTO users(user_id, wallet)
            VALUES(?, ?)
            ON CONFLICT(user_id) DO UPDATE SET wallet=excluded.wallet
            """,
            (user_id, wallet)
        )


def get_user_wallet(user_id: int) -> Optional[str]:
    """
    Gibt die gespeicherte Wallet-Adresse eines Users zurück oder None.
    """
    with conn() as con:
        cur = con.cursor()
        cur.execute("SELECT wallet FROM users WHERE user_id=?", (user_id,))
        row = cur.fetchone()
        return row[0] if row else None


# --- Boards -------------------------------------------------------------

def create_board(
    user_id: int,
    token_id: Optional[str] = None,
    has_free_center: bool = True
) -> int:
    """
    Legt ein neues Board an.

    token_id = Card Number / Karten-ID, die im Bot unter 'My Boards'
    angezeigt wird (Board #{id} (Card XYZ)).
    """
    with conn() as con:
        cur = con.cursor()
        cur.execute(
            "INSERT INTO boards(user_id, token_id, has_free_center) VALUES(?,?,?)",
            (user_id, token_id, 1 if has_free_center else 0)
        )
        return cur.lastrowid


def save_board_numbers(board_id: int, grid):
    """Speichert ein 5x5-Grid in board_numbers."""
    with conn() as con:
        cur = con.cursor()
        for r in range(5):
            for c in range(5):
                val = grid[r][c]
                cur.execute(
                    "INSERT OR REPLACE INTO board_numbers(board_id,r,c,val) VALUES(?,?,?,?)",
                    (board_id, r, c, None if val is None else int(val))
                )


def get_user_board_ids(user_id: int) -> List[int]:
    with conn() as con:
        cur = con.cursor()
        cur.execute("SELECT board_id FROM boards WHERE user_id=?", (user_id,))
        return [row[0] for row in cur.fetchall()]


def load_board(board_id: int):
    """Lädt ein 5x5-Board als Liste von Listen (None oder int)."""
    with conn() as con:
        cur = con.cursor()
        cur.execute("SELECT r,c,val FROM board_numbers WHERE board_id=?", (board_id,))
        vals = cur.fetchall()
        grid = [[None] * 5 for _ in range(5)]
        for r, c, v in vals:
            grid[r][c] = v
        return grid


def get_board_owner(board_id: int) -> Optional[int]:
    with conn() as con:
        cur = con.cursor()
        cur.execute("SELECT user_id FROM boards WHERE board_id=?", (board_id,))
        row = cur.fetchone()
        return row[0] if row else None


def get_board_token(board_id: int) -> Optional[str]:
    """
    Gibt die gespeicherte Card Number (token_id) für ein Board zurück,
    oder None, falls keine hinterlegt ist.
    """
    with conn() as con:
        cur = con.cursor()
        cur.execute("SELECT token_id FROM boards WHERE board_id=?", (board_id,))
        row = cur.fetchone()
        return row[0] if row else None


def set_board_token(board_id: int, token_id: Optional[str]):
    """Speichert z.B. die Card Number im Feld token_id."""
    with conn() as con:
        con.execute(
            "UPDATE boards SET token_id=? WHERE board_id=?",
            (token_id, board_id)
        )


def delete_board(board_id: int, user_id: int) -> bool:
    """
    Löscht genau EIN Board eines Users (inkl. Zahlen und Session-Verknüpfungen).
    Gibt True zurück, falls wirklich gelöscht wurde.
    """
    with conn() as con:
        cur = con.cursor()
        # nur löschen, wenn das Board dem User gehört
        cur.execute("DELETE FROM boards WHERE board_id=? AND user_id=?", (board_id, user_id))
        deleted = cur.rowcount
        if deleted:
            # zugehörige Zahlen entfernen
            cur.execute("DELETE FROM board_numbers WHERE board_id=?", (board_id,))
            # aus laufenden Sessions entfernen
            cur.execute("DELETE FROM session_boards WHERE board_id=?", (board_id,))
        return bool(deleted)


def delete_all_boards(user_id: int) -> int:
    """
    Löscht ALLE Boards eines Users (inkl. Zahlen & Session-Verknüpfungen).
    Gibt die Anzahl der gelöschten Boards zurück.
    """
    with conn() as con:
        cur = con.cursor()
        cur.execute("SELECT board_id FROM boards WHERE user_id=?", (user_id,))
        ids = [r[0] for r in cur.fetchall()]
        if not ids:
            return 0

        # Boards löschen
        cur.execute("DELETE FROM boards WHERE user_id=?", (user_id,))
        # Zahlen & Session-Links löschen
        cur.executemany("DELETE FROM board_numbers WHERE board_id=?", [(bid,) for bid in ids])
        cur.executemany("DELETE FROM session_boards WHERE board_id=?", [(bid,) for bid in ids])
        return len(ids)


# --- Sessions -----------------------------------------------------------

def create_session(chat_id: int, host_user_id: int, pattern: str = 'standard') -> int:
    with conn() as con:
        cur = con.cursor()
        cur.execute(
            "INSERT INTO sessions(chat_id,host_user_id,pattern,status) VALUES(?,?,?, 'live')",
            (chat_id, host_user_id, pattern)
        )
        return cur.lastrowid


def get_live_session(chat_id: int) -> Optional[int]:
    with conn() as con:
        cur = con.cursor()
        cur.execute(
            "SELECT session_id FROM sessions WHERE chat_id=? AND status='live' "
            "ORDER BY session_id DESC LIMIT 1",
            (chat_id,)
        )
        row = cur.fetchone()
        return row[0] if row else None


def get_session_host(session_id: int) -> Optional[int]:
    with conn() as con:
        cur = con.cursor()
        cur.execute("SELECT host_user_id FROM sessions WHERE session_id=?", (session_id,))
        row = cur.fetchone()
        return row[0] if row else None


def add_player(session_id: int, user_id: int):
    with conn() as con:
        con.execute(
            "INSERT OR IGNORE INTO session_players(session_id,user_id) VALUES(?,?)",
            (session_id, user_id)
        )


def add_session_board(session_id: int, board_id: int):
    with conn() as con:
        con.execute(
            "INSERT OR IGNORE INTO session_boards(session_id,board_id) VALUES(?,?)",
            (session_id, board_id)
        )


def get_session_board_ids(session_id: int) -> List[int]:
    with conn() as con:
        cur = con.cursor()
        cur.execute("SELECT board_id FROM session_boards WHERE session_id=?", (session_id,))
        return [r[0] for r in cur.fetchall()]


def count_players(session_id: int) -> int:
    with conn() as con:
        return con.execute(
            "SELECT COUNT(*) FROM session_players WHERE session_id=?",
            (session_id,)
        ).fetchone()[0]


def count_session_boards(session_id: int) -> int:
    with conn() as con:
        return con.execute(
            "SELECT COUNT(*) FROM session_boards WHERE session_id=?",
            (session_id,)
        ).fetchone()[0]


def end_session(session_id: int):
    with conn() as con:
        con.execute("UPDATE sessions SET status='ended' WHERE session_id=?", (session_id,))


def get_pattern(session_id: int) -> str:
    with conn() as con:
        return con.execute(
            "SELECT pattern FROM sessions WHERE session_id=?",
            (session_id,)
        ).fetchone()[0]


# --- Draws & Claims -----------------------------------------------------

def draw_exists(session_id: int, number: int) -> bool:
    with conn() as con:
        row = con.execute(
            "SELECT 1 FROM draws WHERE session_id=? AND number=?",
            (session_id, number)
        ).fetchone()
        return bool(row)


def next_draw_index(session_id: int) -> int:
    with conn() as con:
        row = con.execute(
            "SELECT COALESCE(MAX(idx),0)+1 FROM draws WHERE session_id=?",
            (session_id,)
        ).fetchone()
        return row[0]


def insert_draw(session_id: int, number: int, idx: int):
    with conn() as con:
        con.execute(
            "INSERT INTO draws(session_id,idx,number) VALUES(?,?,?)",
            (session_id, idx, number)
        )


def get_drawn_numbers(session_id: int) -> List[int]:
    with conn() as con:
        cur = con.cursor()
        cur.execute("SELECT number FROM draws WHERE session_id=? ORDER BY idx", (session_id,))
        return [r[0] for r in cur.fetchall()]


def get_last_draw(session_id: int):
    with conn() as con:
        cur = con.cursor()
        cur.execute(
            "SELECT idx, number FROM draws WHERE session_id=? ORDER BY idx DESC LIMIT 1",
            (session_id,)
        )
        row = cur.fetchone()
        return None if not row else type("LastDraw", (object,), {"idx": row[0], "number": row[1]})


def delete_draw(session_id: int, idx: int):
    with conn() as con:
        con.execute("DELETE FROM draws WHERE session_id=? AND idx=?", (session_id, idx))


def claim_exists(session_id: int, board_id: int) -> bool:
    with conn() as con:
        row = con.execute(
            "SELECT 1 FROM claims WHERE session_id=? AND board_id=?",
            (session_id, board_id)
        ).fetchone()
        return bool(row)


def insert_claim(session_id: int, board_id: int, user_id: int, pattern: str):
    with conn() as con:
        con.execute(
            "INSERT OR IGNORE INTO claims(session_id,board_id,user_id,pattern) VALUES(?,?,?,?)",
            (session_id, board_id, user_id, pattern)
        )


# --- Stats / Leaderboard ------------------------------------------------

def ensure_user_stats(user_id: int):
    with conn() as con:
        con.execute(
            "INSERT OR IGNORE INTO user_stats(user_id) VALUES(?)",
            (user_id,)
        )


def bump_participation(session_id: int, user_id: int, boards_joined: int):
    ensure_user_stats(user_id)
    with conn() as con:
        con.execute(
            """
            UPDATE user_stats
               SET total_sessions = total_sessions + 1,
                   total_boards_joined = total_boards_joined + ?,
                   last_played = CURRENT_TIMESTAMP
             WHERE user_id = ?
            """,
            (boards_joined, user_id)
        )


def bump_bingo(user_id: int):
    ensure_user_stats(user_id)
    with conn() as con:
        con.execute(
            """
            UPDATE user_stats
               SET total_bingos = total_bingos + 1,
                   last_played = CURRENT_TIMESTAMP
             WHERE user_id = ?
            """,
            (user_id,)
        )


def get_leaderboard(order_by: str = "total_bingos", limit: int = 10):
    if order_by not in ("total_bingos", "total_boards_joined", "total_sessions"):
        order_by = "total_bingos"
    with conn() as con:
        cur = con.cursor()
        cur.execute(
            f"""
            SELECT user_id, total_bingos, total_boards_joined, total_sessions,
                   COALESCE(strftime('%Y-%m-%d %H:%M','last_played'),'—')
              FROM user_stats
             ORDER BY {order_by} DESC, total_bingos DESC
             LIMIT ?
            """,
            (limit,)
        )
        return cur.fetchall()


def get_user_stats_row(user_id: int):
    with conn() as con:
        cur = con.cursor()
        cur.execute(
            """
            SELECT user_id, total_bingos, total_boards_joined, total_sessions,
                   COALESCE(strftime('%Y-%m-%d %H:%M','last_played'),'—')
             FROM user_stats
             WHERE user_id = ?
            """,
            (user_id,)
        )
        return cur.fetchone()


# --- Reset für Tests ----------------------------------------------------

def reset_all():
    """
    Löscht alle spielrelevanten Daten – für Testzwecke.
    Setzt außerdem die Auto-Inkrement-Zähler für Boards und Sessions zurück
    und entfernt alle gespeicherten Wallets.
    """
    with conn() as con:
        cur = con.cursor()
        cur.executescript("""
        DELETE FROM claims;
        DELETE FROM draws;
        DELETE FROM session_boards;
        DELETE FROM session_players;
        DELETE FROM sessions;
        DELETE FROM board_numbers;
        DELETE FROM boards;
        DELETE FROM user_stats;
        DELETE FROM users;
        """)
        # AutoIncrement-Zähler zurücksetzen (nur SQLite, falls sqlite_sequence existiert)
        try:
            cur.executescript("""
            DELETE FROM sqlite_sequence WHERE name IN ('boards','sessions');
            """)
        except sqlite3.OperationalError:
            # Falls es die Tabelle nicht gibt, einfach ignorieren
            pass

        con.commit()
