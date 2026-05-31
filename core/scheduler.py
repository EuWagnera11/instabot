import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

from config import Config
from core.database import Database

logger = logging.getLogger(__name__)


class PostScheduler:
    def __init__(
        self,
        db: Database,
        instagram_poster_factory: Callable[..., Any],
    ) -> None:
        self.db = db
        self.poster_factory = instagram_poster_factory
        self._last_post_time: dict[int, datetime] = {}
        self._tz = ZoneInfo(Config.SCHEDULER_TIMEZONE)

        self._scheduler = BackgroundScheduler(
            timezone=Config.SCHEDULER_TIMEZONE,
            job_defaults={
                "coalesce": True,
                "max_instances": 3,
                "misfire_grace_time": 600,
            },
        )
        self._running = False

    def start(self) -> None:
        if self._running:
            return
        self._scheduler.add_job(
            self._check_pending_posts,
            CronTrigger(second=0),
            id="check_pending",
            replace_existing=True,
        )
        self._scheduler.start()
        self._running = True
        logger.info("Scheduler iniciado (timezone=%s).", Config.SCHEDULER_TIMEZONE)

    def stop(self) -> None:
        if not self._running:
            return
        self._scheduler.shutdown(wait=False)
        self._running = False
        logger.info("Scheduler parado.")

    @property
    def is_running(self) -> bool:
        return self._running

    def _now(self) -> datetime:
        """Retorna datetime atual no timezone configurado."""
        return datetime.now(self._tz)

    def schedule_post(self, post_id: int, scheduled_time: datetime | str) -> str:
        if isinstance(scheduled_time, str):
            scheduled_time = datetime.fromisoformat(scheduled_time)

        # Garante que o horário tem timezone
        if scheduled_time.tzinfo is None:
            scheduled_time = scheduled_time.replace(tzinfo=self._tz)

        job_id = f"post_{post_id}"

        # Se o horário já passou, executa imediatamente
        now = self._now()
        if scheduled_time <= now:
            self._scheduler.add_job(
                self._execute_post,
                "date",
                id=job_id,
                replace_existing=True,
                args=[post_id],
            )
        else:
            self._scheduler.add_job(
                self._execute_post,
                DateTrigger(run_date=scheduled_time),
                id=job_id,
                replace_existing=True,
                args=[post_id],
            )
        return job_id

    def cancel_post(self, post_id: int) -> bool:
        job_id = f"post_{post_id}"
        try:
            self._scheduler.remove_job(job_id)
            return True
        except Exception:
            return False

    def _execute_post(self, post_id: int) -> None:
        post = self.db.get_post(post_id)
        if not post:
            return
        if post["status"] != "pending":
            return

        profile_id = post["profile_id"]

        # Respeita delay mínimo entre posts da mesma conta
        last = self._last_post_time.get(profile_id)
        if last:
            elapsed = (self._now() - last).total_seconds()
            if elapsed < Config.MIN_POST_DELAY:
                wait = Config.MIN_POST_DELAY - int(elapsed)
                logger.info("Aguardando %ds para perfil %d (delay mínimo)", wait, profile_id)
                import time
                time.sleep(wait)

        self.db.update_post_status(post_id, "publishing")
        self.db.add_log(post_id, "inicio", "Iniciando publicação")

        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                result = loop.run_until_complete(self._async_publish(post))
                self.db.add_log(post_id, "publicado", f"Concluído: {result}")
                self.db.update_post_status(post_id, "published")
                self._last_post_time[profile_id] = self._now()
                self._emit("post_published", {"post_id": post_id, "profile_id": profile_id})
            finally:
                loop.close()
        except Exception as exc:
            error_msg = str(exc)
            logger.error("Erro ao publicar post %d: %s", post_id, error_msg)
            self.db.add_log(post_id, "erro", error_msg)
            self.db.update_post_status(post_id, "failed", error=error_msg)
            self._emit("post_failed", {"post_id": post_id, "error": error_msg})

    async def _async_publish(self, post: dict) -> dict:
        from core.ig_auth import IGAuth, IGAuthError
        from core.instagram import InstagramPoster
        from core.media import IMAGE_EXTENSIONS, ALL_MEDIA_EXTENSIONS
        from pathlib import Path

        profile_id = post["profile_id"]
        auth = IGAuth(profile_id)

        if not auth.is_logged_in():
            raise RuntimeError(
                f"Perfil {profile_id} não está logado no Instagram."
            )

        session = auth.get_session()
        csrf_token = auth.get_csrf_token()

        if not session or not csrf_token:
            raise RuntimeError(
                f"Sessão inválida para perfil {profile_id}. Faça login novamente."
            )

        poster = InstagramPoster(session=session, csrf_token=csrf_token)
        post_type = post["post_type"]
        media_path = post["media_path"]
        caption = post.get("caption", "")

        if post_type == "photo":
            return await poster.post_photo(media_path, caption)

        elif post_type == "reel":
            video_path = Path(media_path)
            cover_path: str | None = None
            for ext in IMAGE_EXTENSIONS:
                candidate = video_path.parent / f"{video_path.stem}_cover{ext}"
                if candidate.exists():
                    cover_path = str(candidate)
                    break
            return await poster.post_reel(media_path, caption, cover_path=cover_path)

        elif post_type == "carousel":
            # media_path pode ser JSON array ou diretório
            try:
                file_paths = json.loads(media_path)
            except (json.JSONDecodeError, TypeError):
                folder = Path(media_path)
                if folder.is_dir():
                    file_paths = [
                        str(f) for f in sorted(folder.iterdir())
                        if f.suffix.lower() in ALL_MEDIA_EXTENSIONS
                    ]
                else:
                    file_paths = [media_path]
            return await poster.post_carousel(file_paths, caption)

        elif post_type == "story":
            return await poster.post_story(media_path)

        else:
            raise ValueError(f"Tipo de post desconhecido: {post_type}")

    def _check_pending_posts(self) -> None:
        try:
            pending = self.db.get_pending_posts()
            if not pending:
                return

            for post in pending:
                job_id = f"post_{post['id']}"
                if self._scheduler.get_job(job_id):
                    continue
                self._scheduler.add_job(
                    self._execute_post,
                    "date",
                    id=job_id,
                    replace_existing=True,
                    args=[post["id"]],
                )
        except Exception as exc:
            logger.error("Erro ao verificar posts pendentes: %s", exc)

    def _emit(self, event_type: str, data: dict) -> None:
        try:
            from app import emit_event
            emit_event(event_type, data)
        except Exception:
            pass
