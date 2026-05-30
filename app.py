import asyncio
import json
import logging
import os
import queue
import sys
import threading
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from flask import (
    Flask,
    Response,
    jsonify,
    redirect,
    render_template,
    request,
    send_from_directory,
    url_for,
)
from werkzeug.utils import secure_filename

from config import Config
from core.database import Database
from core.browser import BrowserManager, run_login_flow
from core.instagram import InstagramPoster
from core.media import MediaManager, IMAGE_EXTENSIONS, VIDEO_EXTENSIONS, ALL_MEDIA_EXTENSIONS, resize_image
from core.scheduler import PostScheduler
from core.ai_caption import AICaptionGenerator

# ---------------------------------------------------------------------------
Path("data").mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("data/instabot.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("instabot")

# ---------------------------------------------------------------------------
Config.ensure_dirs()

app = Flask(__name__, template_folder="templates", static_folder="static")
app.secret_key = Config.SECRET_KEY
app.config["MAX_CONTENT_LENGTH"] = Config.MAX_UPLOAD_SIZE

db = Database()
db.init_db()

media_manager = MediaManager()
ai_generator = AICaptionGenerator()

scheduler = PostScheduler(
    db=db,
    instagram_poster_factory=lambda page: InstagramPoster(page),
)

# SSE event bus
_sse_clients: list[queue.Queue] = []


def emit_event(event_type: str, data: dict) -> None:
    msg = f"event: {event_type}\ndata: {json.dumps(data)}\n\n"
    dead = []
    for q in _sse_clients:
        try:
            q.put_nowait(msg)
        except queue.Full:
            dead.append(q)
    for q in dead:
        _sse_clients.remove(q)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_tz = ZoneInfo(Config.SCHEDULER_TIMEZONE)


def _local_to_utc(local_str: str) -> str:
    dt = datetime.fromisoformat(local_str)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_tz)
    return dt.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%S")


def _save_upload(file) -> str:
    filename = secure_filename(file.filename or "media")
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest_name = f"{timestamp}_{filename}"
    dest_path = Path(Config.UPLOADS_DIR) / dest_name
    dest_path = dest_path.resolve()
    file.save(str(dest_path))
    return str(dest_path)


# ===========================================================================
# Rotas — Páginas HTML
# ===========================================================================


@app.route("/")
def index():
    stats = db.get_stats()
    return render_template("dashboard.html", stats=stats)


@app.route("/posts")
def posts_page():
    all_posts = db.get_all_posts()
    return render_template("posts.html", posts=all_posts)


@app.route("/schedule")
def schedule_page():
    profiles = db.get_profiles()
    media_items = media_manager.scan_folder()
    return render_template(
        "schedule.html",
        profiles=profiles,
        media_items=[item.to_dict() for item in media_items],
    )


@app.route("/settings")
def settings_page():
    return render_template("settings.html")


# ===========================================================================
# API — Upload de Mídia
# ===========================================================================


@app.route("/api/upload", methods=["POST"])
def api_upload():
    if "media" not in request.files:
        return jsonify({"error": "Nenhum arquivo enviado."}), 400

    files = request.files.getlist("media")
    saved = []
    for f in files:
        if not f.filename:
            continue
        ext = Path(f.filename).suffix.lower()
        if ext not in ALL_MEDIA_EXTENSIONS:
            return jsonify({"error": f"Extensão não suportada: {ext}"}), 400
        saved.append(_save_upload(f))

    if not saved:
        return jsonify({"error": "Nenhum arquivo válido."}), 400

    return jsonify({"success": True, "files": saved}), 201


@app.route("/uploads/<path:filename>")
def serve_upload(filename):
    return send_from_directory(Config.UPLOADS_DIR, filename)


# ===========================================================================
# API — Posts (criar, listar, editar, deletar, publicar)
# ===========================================================================


@app.route("/api/posts", methods=["POST"])
def api_create_post():
    # Suporta tanto FormData (com arquivos) quanto JSON
    if request.content_type and "multipart" in request.content_type:
        profile_id = request.form.get("profile_id")
        post_type = request.form.get("post_type")
        caption = request.form.get("caption", "")
        scheduled_time = request.form.get("scheduled_time", "")
        publish_now = request.form.get("publish_now") in ("true", "1", "on")
        aspect_ratio = request.form.get("aspect_ratio", "original")

        files = request.files.getlist("media")
        if not files or not files[0].filename:
            return jsonify({"error": "Arquivo de mídia obrigatório."}), 400

        if post_type == "carousel":
            paths = [_save_upload(f) for f in files if f.filename]
            # Aplica resize em cada imagem do carrossel
            for i, p in enumerate(paths):
                if Path(p).suffix.lower() in IMAGE_EXTENSIONS:
                    paths[i] = resize_image(p, aspect_ratio)
            media_path = json.dumps(paths)
        else:
            media_path = _save_upload(files[0])
            # Aplica resize se for imagem
            if Path(media_path).suffix.lower() in IMAGE_EXTENSIONS:
                media_path = resize_image(media_path, aspect_ratio)
    else:
        data = request.get_json(silent=True) or {}
        profile_id = data.get("profile_id")
        post_type = data.get("post_type")
        caption = data.get("caption", "")
        scheduled_time = data.get("scheduled_time", data.get("scheduled_at", ""))
        publish_now = data.get("publish_now", False)
        media_path = data.get("media_path", "")

    if not profile_id or not post_type:
        return jsonify({"error": "profile_id e post_type são obrigatórios."}), 400
    if not media_path:
        return jsonify({"error": "Mídia obrigatória."}), 400

    valid_types = ("photo", "reel", "carousel", "story")
    if post_type not in valid_types:
        return jsonify({"error": f"post_type inválido. Aceitos: {valid_types}"}), 400

    if publish_now:
        scheduled_at = datetime.utcnow().isoformat()
    elif scheduled_time:
        scheduled_at = _local_to_utc(scheduled_time)
    else:
        return jsonify({"error": "Informe scheduled_time ou publish_now."}), 400

    post_id = db.add_post(
        profile_id=int(profile_id),
        media_path=media_path,
        post_type=post_type,
        caption=caption,
        scheduled_at=scheduled_at,
    )
    scheduler.schedule_post(post_id, scheduled_at)
    db.add_log(post_id, "agendado", f"Agendado para {scheduled_at}")

    return jsonify({"success": True, "post_id": post_id}), 201


@app.route("/api/posts", methods=["GET"])
def api_get_posts():
    posts = db.get_all_posts()
    return jsonify({"success": True, "posts": posts})


@app.route("/api/posts/<int:post_id>", methods=["PUT"])
def api_update_post(post_id: int):
    data = request.get_json(silent=True) or {}
    if "scheduled_at" in data:
        data["scheduled_at"] = _local_to_utc(data["scheduled_at"])
        scheduler.cancel_post(post_id)
        scheduler.schedule_post(post_id, data["scheduled_at"])
    updated = db.update_post(**data, post_id=post_id)
    if not updated:
        return jsonify({"error": "Post não encontrado ou não está pendente."}), 404
    return jsonify({"success": True})


@app.route("/api/posts/<int:post_id>", methods=["DELETE"])
def api_delete_post(post_id: int):
    scheduler.cancel_post(post_id)
    removed = db.delete_post(post_id)
    if removed:
        return jsonify({"success": True, "message": f"Post {post_id} removido."})
    return jsonify({"error": "Post não encontrado ou não está pendente."}), 404


@app.route("/api/posts/<int:post_id>/publish", methods=["POST"])
def api_publish_now(post_id: int):
    post = db.get_post(post_id)
    if not post or post["status"] != "pending":
        return jsonify({"error": "Post não encontrado ou não está pendente."}), 404
    now = datetime.utcnow().isoformat()
    scheduler.cancel_post(post_id)
    scheduler.schedule_post(post_id, now)
    db.add_log(post_id, "publicacao_imediata", "Publicação imediata solicitada")
    return jsonify({"success": True, "message": "Publicação imediata iniciada."})


@app.route("/api/posts/<int:post_id>/logs", methods=["GET"])
def api_get_post_logs(post_id: int):
    logs = db.get_logs(post_id)
    return jsonify({"success": True, "logs": logs})


# ===========================================================================
# API — Perfis (contas Instagram)
# ===========================================================================


@app.route("/api/profiles", methods=["GET"])
def api_profiles():
    profiles = db.get_profiles()
    return jsonify({"success": True, "profiles": profiles})


@app.route("/api/profiles", methods=["POST"])
def api_add_profile():
    data = request.get_json(silent=True)
    if not data or "name" not in data:
        return jsonify({"error": "Nome é obrigatório."}), 400
    profile_id = db.add_profile(
        name=data["name"],
        instagram_username=data.get("instagram_username", ""),
    )
    # Cria diretório do perfil Chrome
    (Path(Config.PROFILES_DIR) / str(profile_id)).mkdir(parents=True, exist_ok=True)
    return jsonify({"success": True, "profile_id": profile_id}), 201


@app.route("/api/profiles/<int:profile_id>", methods=["GET"])
def api_get_profile(profile_id: int):
    profile = db.get_profile(profile_id)
    if not profile:
        return jsonify({"error": "Perfil não encontrado."}), 404
    return jsonify({"success": True, "profile": profile})


@app.route("/api/profiles/<int:profile_id>", methods=["DELETE"])
def api_delete_profile(profile_id: int):
    import shutil
    db.delete_posts_by_profile(profile_id)
    removed = db.delete_profile(profile_id)
    if not removed:
        return jsonify({"error": "Perfil não encontrado."}), 404
    profile_dir = Path(Config.PROFILES_DIR) / str(profile_id)
    if profile_dir.exists():
        shutil.rmtree(profile_dir, ignore_errors=True)
    return jsonify({"success": True, "message": "Perfil removido."})


# ===========================================================================
# API — Login por conta
# ===========================================================================


@app.route("/api/profiles/<int:profile_id>/login", methods=["POST"])
def api_profile_login(profile_id: int):
    profile = db.get_profile(profile_id)
    if not profile:
        return jsonify({"error": "Perfil não encontrado."}), 404

    def _do_login():
        try:
            success = run_login_flow(profile_id)
            status = "logged_in" if success else "failed"
            db.update_profile_login_status(profile_id, status)
            emit_event("login_status", {"profile_id": profile_id, "status": status})
        except Exception as exc:
            logger.error("Erro no login do perfil %d: %s", profile_id, exc)
            db.update_profile_login_status(profile_id, "failed")

    thread = threading.Thread(target=_do_login, daemon=True)
    thread.start()
    return jsonify({"success": True, "message": "Chrome aberto. Faça login no Instagram."})


@app.route("/api/profiles/<int:profile_id>/login/status", methods=["GET"])
def api_profile_login_status(profile_id: int):
    profile = db.get_profile(profile_id)
    if not profile:
        return jsonify({"error": "Perfil não encontrado."}), 404
    bm = BrowserManager(profile_id=profile_id, headless=True)
    return jsonify({
        "success": True,
        "login_status": profile.get("login_status", "unknown"),
        "has_saved_profile": bm.has_saved_profile(),
    })


@app.route("/api/profiles/<int:profile_id>/login/check", methods=["POST"])
def api_check_profile_login(profile_id: int):
    async def _check():
        bm = BrowserManager(profile_id=profile_id, headless=True)
        await bm.start()
        try:
            result = await bm.is_logged_in()
            status = "logged_in" if result else "expired"
            db.update_profile_login_status(profile_id, status)
            return result
        finally:
            await bm.stop()

    loop = asyncio.new_event_loop()
    try:
        result = loop.run_until_complete(_check())
        return jsonify({"success": True, "logged_in": result})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
    finally:
        loop.close()


# ===========================================================================
# API — Mídia (scan de pasta local)
# ===========================================================================


@app.route("/api/media", methods=["GET"])
def api_media():
    items = media_manager.scan_folder()
    return jsonify({
        "success": True,
        "media_folder": str(media_manager.folder_path),
        "items": [item.to_dict() for item in items],
    })


# ===========================================================================
# API — Configurações
# ===========================================================================


@app.route("/api/settings", methods=["GET"])
def api_get_settings():
    return jsonify({
        "success": True,
        "settings": {
            "media_folder": Config.MEDIA_FOLDER,
            "scheduler_timezone": Config.SCHEDULER_TIMEZONE,
            "min_post_delay": Config.MIN_POST_DELAY,
            "scheduler_running": scheduler.is_running,
        },
    })


@app.route("/api/settings", methods=["POST"])
def api_settings():
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "Corpo JSON inválido."}), 400

    if "media_folder" in data:
        new_folder = data["media_folder"]
        if Path(new_folder).exists():
            Config.MEDIA_FOLDER = new_folder
            media_manager.folder_path = Path(new_folder)
        else:
            return jsonify({"error": f"Pasta não encontrada: {new_folder}"}), 400

    return jsonify({"success": True, "message": "Configurações atualizadas."})


# ===========================================================================
# API — IA (geração de copy)
# ===========================================================================


@app.route("/api/ai/caption", methods=["POST"])
def api_ai_caption():
    tone = "descontraido"
    context = ""

    if request.content_type and "multipart" in request.content_type:
        tone = request.form.get("tone", "descontraido")
        context = request.form.get("context", "")
        file = request.files.get("media")
        if file and file.filename:
            path = _save_upload(file)
        else:
            media_path = request.form.get("media_path", "")
            if not media_path:
                return jsonify({"error": "Envie uma imagem ou informe media_path."}), 400
            path = media_path
    else:
        data = request.get_json(silent=True) or {}
        tone = data.get("tone", "descontraido")
        context = data.get("context", "")
        path = data.get("media_path", "")
        if not path:
            return jsonify({"error": "media_path é obrigatório."}), 400

    try:
        ext = Path(path).suffix.lower()
        if ext in VIDEO_EXTENSIONS:
            caption = ai_generator.generate_caption_from_video(path, tone, context)
        else:
            caption = ai_generator.generate_caption(path, tone, context)
        return jsonify({"success": True, "caption": caption})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


# ===========================================================================
# API — Post em Massa (bulk)
# ===========================================================================


@app.route("/api/posts/bulk", methods=["POST"])
def api_posts_bulk():
    if request.content_type and "multipart" in request.content_type:
        profile_id = request.form.get("profile_id")
        start_date = request.form.get("start_date")
        interval_days = int(request.form.get("interval_days", "1"))
        hour = request.form.get("hour", "10:00")
        tone = request.form.get("tone", "descontraido")
        post_type = request.form.get("post_type", "photo")
        aspect_ratio = request.form.get("aspect_ratio", "original")
        generate_captions = request.form.get("generate_captions", "true") == "true"

        # Ler captions pré-geradas do frontend
        captions_json = request.form.get("captions_json", "")
        if captions_json:
            try:
                captions = json.loads(captions_json)
            except (json.JSONDecodeError, TypeError):
                captions = []
        else:
            captions = []

        files = request.files.getlist("media")
        if not files or not files[0].filename:
            return jsonify({"error": "Envie pelo menos um arquivo."}), 400

        media_paths = [_save_upload(f) for f in files if f.filename]
        # Aplica resize nas imagens
        for i, p in enumerate(media_paths):
            if Path(p).suffix.lower() in IMAGE_EXTENSIONS:
                media_paths[i] = resize_image(p, aspect_ratio)
    else:
        data = request.get_json(silent=True) or {}
        profile_id = data.get("profile_id")
        start_date = data.get("start_date")
        interval_days = int(data.get("interval_days", 1))
        hour = data.get("hour", "10:00")
        tone = data.get("tone", "descontraido")
        post_type = data.get("post_type", "photo")
        generate_captions = data.get("generate_captions", True)
        media_paths = data.get("media_paths", [])
        captions = data.get("captions", [])

    if not profile_id or not start_date:
        return jsonify({"error": "profile_id e start_date são obrigatórios."}), 400
    if not media_paths:
        return jsonify({"error": "Nenhuma mídia fornecida."}), 400

    # Gerar captions com IA se solicitado e não há captions pré-definidas
    if generate_captions and (not captions or len(captions) < len(media_paths)):
        captions = []
        for path in media_paths:
            try:
                ext = Path(path).suffix.lower()
                if ext in VIDEO_EXTENSIONS:
                    cap = ai_generator.generate_caption_from_video(path, tone)
                else:
                    cap = ai_generator.generate_caption(path, tone)
                captions.append(cap)
            except Exception as exc:
                logger.warning("Erro ao gerar caption para %s: %s", path, exc)
                captions.append("")

    # Pad captions se necessário
    while len(captions) < len(media_paths):
        captions.append("")

    # Calcular datas
    from datetime import timedelta
    base_date = datetime.fromisoformat(start_date)
    h, m = hour.split(":")
    base_date = base_date.replace(hour=int(h), minute=int(m), second=0)

    created_posts = []
    for i, (path, caption) in enumerate(zip(media_paths, captions)):
        scheduled_dt = base_date + timedelta(days=i * interval_days)
        scheduled_at = _local_to_utc(scheduled_dt.isoformat())

        post_id = db.add_post(
            profile_id=int(profile_id),
            media_path=path,
            post_type=post_type,
            caption=caption,
            scheduled_at=scheduled_at,
        )
        scheduler.schedule_post(post_id, scheduled_at)
        db.add_log(post_id, "agendado_bulk", f"Bulk: post {i+1}/{len(media_paths)} para {scheduled_dt.strftime('%d/%m/%Y %H:%M')}")
        created_posts.append({
            "post_id": post_id,
            "scheduled_at": scheduled_dt.isoformat(),
            "caption": caption[:100],
        })

    return jsonify({
        "success": True,
        "message": f"{len(created_posts)} posts agendados com sucesso.",
        "posts": created_posts,
    }), 201


@app.route("/bulk")
def bulk_page():
    profiles = db.get_profiles()
    return render_template("bulk.html", profiles=profiles)


# ===========================================================================
# API — Status geral + SSE
# ===========================================================================


@app.route("/api/status", methods=["GET"])
def api_status():
    profiles = db.get_profiles()
    logged_in_count = sum(1 for p in profiles if p.get("login_status") == "logged_in")
    pending = db.get_all_pending_posts()
    next_post = pending[0]["scheduled_at"] if pending else None
    return jsonify({
        "success": True,
        "scheduler_running": scheduler.is_running,
        "profiles_count": len(profiles),
        "profiles_logged_in": logged_in_count,
        "pending_posts": len(pending),
        "next_post_at": next_post,
    })


@app.route("/api/events")
def api_events():
    def stream():
        q: queue.Queue = queue.Queue(maxsize=50)
        _sse_clients.append(q)
        try:
            yield "event: connected\ndata: {}\n\n"
            while True:
                try:
                    msg = q.get(timeout=30)
                    yield msg
                except queue.Empty:
                    yield ": keepalive\n\n"
        except GeneratorExit:
            pass
        finally:
            if q in _sse_clients:
                _sse_clients.remove(q)

    return Response(stream(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ===========================================================================
# Tratamento de Erros
# ===========================================================================


@app.errorhandler(404)
def not_found(error):
    if request.path.startswith("/api/"):
        return jsonify({"error": "Recurso não encontrado."}), 404
    return render_template("404.html"), 404


@app.errorhandler(500)
def internal_error(error):
    logger.error("Erro interno: %s", error)
    if request.path.startswith("/api/"):
        return jsonify({"error": "Erro interno do servidor."}), 500
    return render_template("500.html"), 500


@app.route("/health")
def health():
    return jsonify({
        "status": "ok",
        "scheduler": scheduler.is_running,
        "timestamp": datetime.utcnow().isoformat(),
    })


# ===========================================================================
# Ponto de entrada
# ===========================================================================

if __name__ == "__main__":
    scheduler.start()

    # Re-agenda posts pendentes após restart
    for post in db.get_all_pending_posts():
        scheduler.schedule_post(post["id"], post["scheduled_at"])

    logger.info("InstaBot iniciando em http://%s:%d", Config.HOST, Config.PORT)

    try:
        app.run(
            host=Config.HOST,
            port=Config.PORT,
            debug=Config.DEBUG,
            use_reloader=False,
        )
    finally:
        scheduler.stop()
