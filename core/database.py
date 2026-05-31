import logging
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
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

            CREATE TABLE IF NOT EXISTS caption_templates (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                name       TEXT NOT NULL,
                content    TEXT NOT NULL,
                tone       TEXT DEFAULT 'descontraido',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS hashtag_groups (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                name       TEXT NOT NULL,
                hashtags   TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS post_metrics (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                post_id    INTEGER NOT NULL,
                likes      INTEGER DEFAULT 0,
                comments   INTEGER DEFAULT 0,
                reach      INTEGER DEFAULT 0,
                fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (post_id) REFERENCES scheduled_posts(id)
            );

            CREATE TABLE IF NOT EXISTS smart_schedule (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                profile_id  INTEGER NOT NULL,
                day_of_week INTEGER NOT NULL,
                hour        INTEGER NOT NULL,
                score       REAL DEFAULT 0,
                updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (profile_id) REFERENCES profiles(id)
            );
        """)
        self._migrate()
        self.conn.commit()
        logger.info("Banco de dados inicializado com sucesso.")

    def _migrate(self) -> None:
        # Migration: profiles
        columns = {
            row[1] for row in
            self.conn.execute("PRAGMA table_info(profiles)").fetchall()
        }
        if "dolphin_profile_id" in columns and "login_status" not in columns:
            self.conn.execute("ALTER TABLE profiles ADD COLUMN login_status TEXT DEFAULT 'unknown'")
            self.conn.execute("ALTER TABLE profiles ADD COLUMN last_login_check TIMESTAMP")
            logger.info("Migração: adicionadas colunas login_status e last_login_check.")

        # Migration: scheduled_posts.retry_count
        post_columns = {
            row[1] for row in
            self.conn.execute("PRAGMA table_info(scheduled_posts)").fetchall()
        }
        if "retry_count" not in post_columns:
            self.conn.execute("ALTER TABLE scheduled_posts ADD COLUMN retry_count INTEGER DEFAULT 0")
            logger.info("Migração: adicionada coluna retry_count.")

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
            (status, datetime.now(ZoneInfo(Config.SCHEDULER_TIMEZONE)).isoformat(), profile_id),
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
            ORDER BY
                CASE sp.status
                    WHEN 'pending' THEN 0
                    WHEN 'publishing' THEN 1
                    WHEN 'failed' THEN 2
                    WHEN 'published' THEN 3
                    ELSE 4
                END,
                sp.scheduled_at ASC
            """
        ).fetchall()
        return [dict(row) for row in rows]

    def get_pending_posts(self) -> list[dict]:
        now = datetime.now(ZoneInfo(Config.SCHEDULER_TIMEZONE)).strftime("%Y-%m-%dT%H:%M:%S")
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
            datetime.now(ZoneInfo(Config.SCHEDULER_TIMEZONE)).strftime("%Y-%m-%dT%H:%M:%S") if status == "published" else None
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

    def get_stats_by_profile(self, profile_id: int) -> dict:
        row = self.conn.execute(
            """
            SELECT
                COUNT(*)                                        AS total,
                SUM(CASE WHEN status = 'published' THEN 1 ELSE 0 END)  AS published,
                SUM(CASE WHEN status = 'pending'   THEN 1 ELSE 0 END)  AS pending,
                SUM(CASE WHEN status = 'failed'    THEN 1 ELSE 0 END)  AS failed
            FROM scheduled_posts WHERE profile_id = ?
            """,
            (profile_id,),
        ).fetchone()
        return dict(row) if row else {
            "total": 0, "published": 0, "pending": 0, "failed": 0
        }

    # ------------------------------------------------------------------
    # Templates de legenda
    # ------------------------------------------------------------------

    def add_template(self, name: str, content: str, tone: str = "descontraido") -> int:
        cursor = self.conn.execute(
            "INSERT INTO caption_templates (name, content, tone) VALUES (?, ?, ?)",
            (name, content, tone),
        )
        self.conn.commit()
        return cursor.lastrowid or 0

    def get_templates(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM caption_templates ORDER BY name"
        ).fetchall()
        return [dict(row) for row in rows]

    def get_template(self, template_id: int) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM caption_templates WHERE id = ?", (template_id,)
        ).fetchone()
        return dict(row) if row else None

    def update_template(self, template_id: int, **kwargs) -> bool:
        allowed = {"name", "content", "tone"}
        fields = {k: v for k, v in kwargs.items() if k in allowed}
        if not fields:
            return False
        set_clause = ", ".join(f"{k} = ?" for k in fields)
        values = list(fields.values()) + [template_id]
        cursor = self.conn.execute(
            f"UPDATE caption_templates SET {set_clause} WHERE id = ?", values
        )
        self.conn.commit()
        return (cursor.rowcount or 0) > 0

    def delete_template(self, template_id: int) -> bool:
        cursor = self.conn.execute(
            "DELETE FROM caption_templates WHERE id = ?", (template_id,)
        )
        self.conn.commit()
        return (cursor.rowcount or 0) > 0

    # ------------------------------------------------------------------
    # Grupos de hashtags
    # ------------------------------------------------------------------

    def add_hashtag_group(self, name: str, hashtags: str) -> int:
        cursor = self.conn.execute(
            "INSERT INTO hashtag_groups (name, hashtags) VALUES (?, ?)",
            (name, hashtags),
        )
        self.conn.commit()
        return cursor.lastrowid or 0

    def get_hashtag_groups(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM hashtag_groups ORDER BY name"
        ).fetchall()
        return [dict(row) for row in rows]

    def get_hashtag_group(self, group_id: int) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM hashtag_groups WHERE id = ?", (group_id,)
        ).fetchone()
        return dict(row) if row else None

    def update_hashtag_group(self, group_id: int, **kwargs) -> bool:
        allowed = {"name", "hashtags"}
        fields = {k: v for k, v in kwargs.items() if k in allowed}
        if not fields:
            return False
        set_clause = ", ".join(f"{k} = ?" for k in fields)
        values = list(fields.values()) + [group_id]
        cursor = self.conn.execute(
            f"UPDATE hashtag_groups SET {set_clause} WHERE id = ?", values
        )
        self.conn.commit()
        return (cursor.rowcount or 0) > 0

    def delete_hashtag_group(self, group_id: int) -> bool:
        cursor = self.conn.execute(
            "DELETE FROM hashtag_groups WHERE id = ?", (group_id,)
        )
        self.conn.commit()
        return (cursor.rowcount or 0) > 0

    # ------------------------------------------------------------------
    # Métricas de posts
    # ------------------------------------------------------------------

    def add_metrics(self, post_id: int, likes: int = 0, comments: int = 0, reach: int = 0) -> int:
        cursor = self.conn.execute(
            "INSERT INTO post_metrics (post_id, likes, comments, reach) VALUES (?, ?, ?, ?)",
            (post_id, likes, comments, reach),
        )
        self.conn.commit()
        return cursor.lastrowid or 0

    def get_metrics(self, post_id: int) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM post_metrics WHERE post_id = ? ORDER BY fetched_at DESC LIMIT 1",
            (post_id,),
        ).fetchone()
        return dict(row) if row else None

    def get_profile_metrics(self, profile_id: int) -> list[dict]:
        rows = self.conn.execute(
            """
            SELECT pm.*, sp.caption, sp.media_path, sp.published_at
            FROM post_metrics pm
            JOIN scheduled_posts sp ON pm.post_id = sp.id
            WHERE sp.profile_id = ?
            ORDER BY pm.fetched_at DESC
            """,
            (profile_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------
    # Smart Schedule (horários inteligentes)
    # ------------------------------------------------------------------

    def update_smart_schedule(self, profile_id: int, day_of_week: int, hour: int, score: float) -> None:
        existing = self.conn.execute(
            "SELECT id FROM smart_schedule WHERE profile_id = ? AND day_of_week = ? AND hour = ?",
            (profile_id, day_of_week, hour),
        ).fetchone()
        now = datetime.now(ZoneInfo(Config.SCHEDULER_TIMEZONE)).isoformat()
        if existing:
            self.conn.execute(
                "UPDATE smart_schedule SET score = ?, updated_at = ? WHERE id = ?",
                (score, now, existing["id"]),
            )
        else:
            self.conn.execute(
                "INSERT INTO smart_schedule (profile_id, day_of_week, hour, score, updated_at) VALUES (?, ?, ?, ?, ?)",
                (profile_id, day_of_week, hour, score, now),
            )
        self.conn.commit()

    def get_smart_schedule(self, profile_id: int, limit: int = 5) -> list[dict]:
        rows = self.conn.execute(
            """
            SELECT day_of_week, hour, score
            FROM smart_schedule
            WHERE profile_id = ?
            ORDER BY score DESC
            LIMIT ?
            """,
            (profile_id, limit),
        ).fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------
    # Retry
    # ------------------------------------------------------------------

    def increment_retry(self, post_id: int) -> int:
        self.conn.execute(
            "UPDATE scheduled_posts SET retry_count = COALESCE(retry_count, 0) + 1 WHERE id = ?",
            (post_id,),
        )
        self.conn.commit()
        row = self.conn.execute(
            "SELECT retry_count FROM scheduled_posts WHERE id = ?", (post_id,)
        ).fetchone()
        return row["retry_count"] if row else 0

    # ------------------------------------------------------------------
    # Calendar
    # ------------------------------------------------------------------

    def get_posts_by_month(self, year: int, month: int) -> list[dict]:
        start = f"{year:04d}-{month:02d}-01"
        if month == 12:
            end = f"{year + 1:04d}-01-01"
        else:
            end = f"{year:04d}-{month + 1:02d}-01"
        rows = self.conn.execute(
            """
            SELECT sp.*, p.name AS profile_name, p.instagram_username
            FROM scheduled_posts sp
            JOIN profiles p ON sp.profile_id = p.id
            WHERE sp.scheduled_at >= ? AND sp.scheduled_at < ?
            ORDER BY sp.scheduled_at ASC
            """,
            (start, end),
        ).fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------
    # Reorder
    # ------------------------------------------------------------------

    def reorder_posts(self, post_ids: list[int], base_time: str, interval_minutes: int) -> None:
        """Reordena posts pendentes atribuindo novos horários."""
        dt = datetime.fromisoformat(base_time)
        for i, pid in enumerate(post_ids):
            new_time = dt + timedelta(minutes=interval_minutes * i)
            self.conn.execute(
                "UPDATE scheduled_posts SET scheduled_at = ? WHERE id = ? AND status = 'pending'",
                (new_time.strftime("%Y-%m-%dT%H:%M:%S"), pid),
            )
        self.conn.commit()
