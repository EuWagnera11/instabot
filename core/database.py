import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from config import Config

logger = logging.getLogger(__name__)


class Database:
    def __init__(self, db_path: str | None = None) -> None:
        self.db_path = db_path or Config.DB_PATH
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn: sqlite3.Connection | None = None

    def __enter__(self) -> "Database":
        self._conn = self._connect()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = self._connect()
        return self._conn

    def init_db(self) -> None:
        logger.info("Inicializando banco de dados em: %s", self.db_path)

        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS profiles (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                name                TEXT    NOT NULL,
                instagram_username  TEXT    DEFAULT '',
                login_status        TEXT    DEFAULT 'unknown',
                last_login_check    TIMESTAMP,
                created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS scheduled_posts (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                profile_id    INTEGER   NOT NULL,
                media_path    TEXT      NOT NULL,
                post_type     TEXT      NOT NULL CHECK(post_type IN ('photo','reel','carousel','story')),
                caption       TEXT      DEFAULT '',
                scheduled_at  TIMESTAMP NOT NULL,
                status        TEXT      NOT NULL DEFAULT 'pending'
                                        CHECK(status IN ('pending','publishing','published','failed')),
                published_at  TIMESTAMP,
                error         TEXT,
                created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (profile_id) REFERENCES profiles(id)
            );

            CREATE TABLE IF NOT EXISTS publish_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                post_id     INTEGER NOT NULL,
                action      TEXT    NOT NULL,
                details     TEXT,
                created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (post_id) REFERENCES scheduled_posts(id)
            );
        """)
        self._migrate()
        self.conn.commit()
        logger.info("Banco de dados inicializado com sucesso.")

    def _migrate(self) -> None:
        columns = {
            row[1] for row in
            self.conn.execute("PRAGMA table_info(profiles)").fetchall()
        }
        if "dolphin_profile_id" in columns and "login_status" not in columns:
            self.conn.execute("ALTER TABLE profiles ADD COLUMN login_status TEXT DEFAULT 'unknown'")
            self.conn.execute("ALTER TABLE profiles ADD COLUMN last_login_check TIMESTAMP")
            logger.info("Migração: adicionadas colunas login_status e last_login_check.")

    # ------------------------------------------------------------------
    # Perfis
    # ------------------------------------------------------------------

    def add_profile(self, name: str, instagram_username: str = "") -> int:
        cursor = self.conn.execute(
            "INSERT INTO profiles (name, instagram_username) VALUES (?, ?)",
            (name, instagram_username),
        )
        self.conn.commit()
        profile_id = cursor.lastrowid or 0
        logger.info("Perfil adicionado: id=%d, nome=%s", profile_id, name)
        return profile_id

    def get_profile(self, profile_id: int) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM profiles WHERE id = ?", (profile_id,)
        ).fetchone()
        return dict(row) if row else None

    def get_profiles(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM profiles ORDER BY name"
        ).fetchall()
        return [dict(row) for row in rows]

    def update_profile_login_status(self, profile_id: int, status: str) -> None:
        self.conn.execute(
            "UPDATE profiles SET login_status = ?, last_login_check = ? WHERE id = ?",
            (status, datetime.utcnow().isoformat(), profile_id),
        )
        self.conn.commit()

    def delete_profile(self, profile_id: int) -> bool:
        cursor = self.conn.execute(
            "DELETE FROM profiles WHERE id = ?", (profile_id,)
        )
        self.conn.commit()
        return (cursor.rowcount or 0) > 0

    # ------------------------------------------------------------------
    # Postagens agendadas
    # ------------------------------------------------------------------

    def add_post(
        self,
        profile_id: int,
        media_path: str,
        post_type: str,
        caption: str,
        scheduled_at: str,
    ) -> int:
        cursor = self.conn.execute(
            """
            INSERT INTO scheduled_posts (profile_id, media_path, post_type, caption, scheduled_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (profile_id, media_path, post_type, caption, scheduled_at),
        )
        self.conn.commit()
        return cursor.lastrowid or 0

    def get_post(self, post_id: int) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM scheduled_posts WHERE id = ?", (post_id,)
        ).fetchone()
        return dict(row) if row else None

    def get_all_posts(self) -> list[dict]:
        rows = self.conn.execute(
            """
            SELECT sp.*, p.name AS profile_name, p.instagram_username
            FROM scheduled_posts sp
            JOIN profiles p ON sp.profile_id = p.id
            ORDER BY sp.scheduled_at DESC
            """
        ).fetchall()
        return [dict(row) for row in rows]

    def get_pending_posts(self) -> list[dict]:
        now = datetime.utcnow().isoformat()
        rows = self.conn.execute(
            """
            SELECT sp.*, p.name AS profile_name
            FROM scheduled_posts sp
            JOIN profiles p ON sp.profile_id = p.id
            WHERE sp.status = 'pending' AND sp.scheduled_at <= ?
            ORDER BY sp.scheduled_at ASC
            """,
            (now,),
        ).fetchall()
        return [dict(row) for row in rows]

    def get_all_pending_posts(self) -> list[dict]:
        rows = self.conn.execute(
            """
            SELECT sp.*, p.name AS profile_name
            FROM scheduled_posts sp
            JOIN profiles p ON sp.profile_id = p.id
            WHERE sp.status = 'pending'
            ORDER BY sp.scheduled_at ASC
            """
        ).fetchall()
        return [dict(row) for row in rows]

    def update_post_status(
        self,
        post_id: int,
        status: str,
        error: str | None = None,
    ) -> None:
        published_at = (
            datetime.utcnow().isoformat() if status == "published" else None
        )
        self.conn.execute(
            """
            UPDATE scheduled_posts
            SET status = ?, published_at = ?, error = ?
            WHERE id = ?
            """,
            (status, published_at, error, post_id),
        )
        self.conn.commit()

    def update_post(self, post_id: int, **kwargs) -> bool:
        allowed = {"caption", "scheduled_at", "media_path", "post_type"}
        fields = {k: v for k, v in kwargs.items() if k in allowed}
        if not fields:
            return False
        set_clause = ", ".join(f"{k} = ?" for k in fields)
        values = list(fields.values()) + [post_id]
        cursor = self.conn.execute(
            f"UPDATE scheduled_posts SET {set_clause} WHERE id = ? AND status = 'pending'",
            values,
        )
        self.conn.commit()
        return (cursor.rowcount or 0) > 0

    def delete_post(self, post_id: int) -> bool:
        cursor = self.conn.execute(
            "DELETE FROM scheduled_posts WHERE id = ? AND status = 'pending'",
            (post_id,),
        )
        self.conn.commit()
        return (cursor.rowcount or 0) > 0

    def delete_posts_by_profile(self, profile_id: int) -> int:
        cursor = self.conn.execute(
            "DELETE FROM scheduled_posts WHERE profile_id = ? AND status = 'pending'",
            (profile_id,),
        )
        self.conn.commit()
        return cursor.rowcount or 0

    # ------------------------------------------------------------------
    # Logs
    # ------------------------------------------------------------------

    def add_log(self, post_id: int, action: str, details: str = "") -> int:
        cursor = self.conn.execute(
            "INSERT INTO publish_log (post_id, action, details) VALUES (?, ?, ?)",
            (post_id, action, details),
        )
        self.conn.commit()
        return cursor.lastrowid or 0

    def get_logs(self, post_id: int) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM publish_log WHERE post_id = ? ORDER BY created_at ASC",
            (post_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def get_recent_logs(self, limit: int = 20) -> list[dict]:
        rows = self.conn.execute(
            """
            SELECT pl.*, sp.post_type, sp.media_path, p.name AS profile_name
            FROM publish_log pl
            JOIN scheduled_posts sp ON pl.post_id = sp.id
            JOIN profiles p ON sp.profile_id = p.id
            ORDER BY pl.created_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------
    # Estatísticas
    # ------------------------------------------------------------------

    def get_stats(self) -> dict:
        row = self.conn.execute(
            """
            SELECT
                COUNT(*)                                        AS total,
                SUM(CASE WHEN status = 'published' THEN 1 ELSE 0 END)  AS published,
                SUM(CASE WHEN status = 'pending'   THEN 1 ELSE 0 END)  AS pending,
                SUM(CASE WHEN status = 'failed'    THEN 1 ELSE 0 END)  AS failed
            FROM scheduled_posts
            """
        ).fetchone()
        return dict(row) if row else {
            "total": 0, "published": 0, "pending": 0, "failed": 0
        }
