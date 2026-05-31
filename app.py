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
from core.ig_auth import IGAuth, IGAuthError
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
    instagram_poster_factory=None,
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
    profiles = db.get_profiles()
    upcoming_posts = db.get_all_pending_posts()[:5]
    recent_logs = db.get_recent_logs(10)
    return render_template("dashboard.html", stats=stats, profiles=profiles, upcoming_posts=upcoming_posts, recent_logs=recent_logs)


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


@app.route("/calendar")
def calendar_page():
    return render_template("calendar.html")


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
        scheduled_at = datetime.now(_tz).strftime("%Y-%m-%dT%H:%M:%S")
    elif scheduled_time:
        dt = datetime.fromisoformat(scheduled_time)
        scheduled_at = dt.strftime("%Y-%m-%dT%H:%M:%S")
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
        dt = datetime.fromisoformat(data["scheduled_at"])
        data["scheduled_at"] = dt.strftime("%Y-%m-%dT%H:%M:%S")
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
    now = datetime.now(_tz).strftime("%Y-%m-%dT%H:%M:%S")
    scheduler.cancel_post(post_id)
    scheduler.schedule_post(post_id, now)
    db.add_log(post_id, "publicacao_imediata", "Publicação imediata solicitada")
    return jsonify({"success": True, "message": "Publicação imediata iniciada."})


@app.route("/api/posts/<int:post_id>", methods=["GET"])
def api_get_post(post_id: int):
    post = db.get_post(post_id)
    if not post:
        return jsonify({"error": "Post não encontrado."}), 404
    logs = db.get_logs(post_id)
    profile = db.get_profile(post["profile_id"])
    return jsonify({
        "success": True,
        "post": {
            **post,
            "profile_name": profile["name"] if profile else "—",
            "instagram_username": profile.get("instagram_username", "") if profile else "",
        },
        "logs": logs,
    })


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
    """Login via username/senha (API, sem navegador)."""
    profile = db.get_profile(profile_id)
    if not profile:
        return jsonify({"error": "Perfil não encontrado."}), 404

    data = request.get_json(silent=True) or {}
    username = data.get("username", "").strip()
    password = data.get("password", "").strip()

    if not username or not password:
        return jsonify({"error": "Username e senha são obrigatórios."}), 400

    auth = IGAuth(profile_id)
    try:
        result = auth.login(username, password)
        db.update_profile_login_status(profile_id, "logged_in")
        emit_event("login_status", {"profile_id": profile_id, "status": "logged_in"})
        return jsonify({"success": True, "username": result.get("username")})
    except IGAuthError as exc:
        msg = str(exc)
        if msg == "2FA_REQUIRED":
            db.update_profile_login_status(profile_id, "2fa_pending")
            return jsonify({"success": False, "requires_2fa": True, "message": "Digite o código 2FA."})
        elif msg.startswith("CHECKPOINT:"):
            db.update_profile_login_status(profile_id, "checkpoint")
            return jsonify({"success": False, "checkpoint": True, "message": "Instagram pediu verificação. Abra o app e confirme."}), 403
        else:
            db.update_profile_login_status(profile_id, "failed")
            return jsonify({"error": msg}), 401


@app.route("/api/profiles/<int:profile_id>/login/2fa", methods=["POST"])
def api_profile_login_2fa(profile_id: int):
    """Verifica código 2FA."""
    profile = db.get_profile(profile_id)
    if not profile:
        return jsonify({"error": "Perfil não encontrado."}), 404

    data = request.get_json(silent=True) or {}
    code = data.get("code", "").strip()

    if not code:
        return jsonify({"error": "Código 2FA é obrigatório."}), 400

    auth = IGAuth(profile_id)
    try:
        result = auth.verify_2fa(code)
        db.update_profile_login_status(profile_id, "logged_in")
        emit_event("login_status", {"profile_id": profile_id, "status": "logged_in"})
        return jsonify({"success": True, "username": result.get("username")})
    except IGAuthError as exc:
        return jsonify({"error": str(exc)}), 401


@app.route("/api/profiles/<int:profile_id>/login/status", methods=["GET"])
def api_profile_login_status(profile_id: int):
    profile = db.get_profile(profile_id)
    if not profile:
        return jsonify({"error": "Perfil não encontrado."}), 404
    auth = IGAuth(profile_id)
    return jsonify({
        "success": True,
        "login_status": profile.get("login_status", "unknown"),
        "logged_in": auth.is_logged_in(),
        "user_info": auth.get_user_info(),
    })


@app.route("/api/profiles/<int:profile_id>/login/check", methods=["POST"])
def api_check_profile_login(profile_id: int):
    """Verifica se a sessão ainda é válida."""
    auth = IGAuth(profile_id)
    if auth.is_logged_in():
        db.update_profile_login_status(profile_id, "logged_in")
        return jsonify({"success": True, "logged_in": True})
    else:
        db.update_profile_login_status(profile_id, "expired")
        return jsonify({"success": True, "logged_in": False})


@app.route("/api/profiles/<int:profile_id>/logout", methods=["POST"])
def api_profile_logout(profile_id: int):
    """Remove sessão salva."""
    auth = IGAuth(profile_id)
    auth.logout()
    db.update_profile_login_status(profile_id, "logged_out")
    return jsonify({"success": True})


# ---------------------------------------------------------------------------
# Login via navegador Chrome (abre aba para login manual)
# ---------------------------------------------------------------------------

_browser_login_sessions: dict[int, dict] = {}
_browser_login_lock = threading.Lock()


@app.route("/api/profiles/<int:profile_id>/login/browser", methods=["POST"])
def api_profile_login_browser(profile_id: int):
    """Inicia login via navegador Chrome visível.

    Abre o Chrome com a página de login do Instagram para que o
    usuário faça login manualmente (suporta 2FA, checkpoint, etc.).
    """
    profile = db.get_profile(profile_id)
    if not profile:
        return jsonify({"error": "Perfil não encontrado."}), 404

    with _browser_login_lock:
        session_info = _browser_login_sessions.get(profile_id)
        if session_info and session_info.get("status") == "waiting":
            return jsonify({
                "success": True,
                "status": "waiting",
                "message": "Navegador já está aberto. Faça login na janela do Chrome.",
            })

    def _run_browser_login():
        with _browser_login_lock:
            _browser_login_sessions[profile_id] = {
                "status": "waiting",
                "message": "Abrindo Chrome...",
            }

        db.update_profile_login_status(profile_id, "browser_login")
        emit_event("login_status", {"profile_id": profile_id, "status": "browser_login"})

        try:
            success = run_login_flow(profile_id)
            if success:
                with _browser_login_lock:
                    _browser_login_sessions[profile_id] = {
                        "status": "success",
                        "message": "Login realizado com sucesso!",
                    }
                db.update_profile_login_status(profile_id, "logged_in")
                emit_event("login_status", {"profile_id": profile_id, "status": "logged_in"})
                logger.info("Login via browser OK (profile_id=%d)", profile_id)
            else:
                with _browser_login_lock:
                    _browser_login_sessions[profile_id] = {
                        "status": "failed",
                        "message": "Login expirou ou foi cancelado.",
                    }
                db.update_profile_login_status(profile_id, "failed")
                emit_event("login_status", {"profile_id": profile_id, "status": "failed"})
        except Exception as exc:
            logger.error("Erro no login via browser: %s", exc)
            with _browser_login_lock:
                _browser_login_sessions[profile_id] = {
                    "status": "error",
                    "message": f"Erro: {exc}",
                }
            db.update_profile_login_status(profile_id, "failed")

    thread = threading.Thread(target=_run_browser_login, daemon=True)
    thread.start()

    return jsonify({
        "success": True,
        "status": "waiting",
        "message": "Chrome aberto! Faça login na janela que apareceu.",
    })


@app.route("/api/profiles/<int:profile_id>/login/browser/status", methods=["GET"])
def api_profile_login_browser_status(profile_id: int):
    """Retorna o status do login via browser."""
    with _browser_login_lock:
        session_info = _browser_login_sessions.get(profile_id)

    if not session_info:
        return jsonify({"status": "idle", "message": "Nenhum login em andamento."})

    return jsonify({
        "status": session_info["status"],
        "message": session_info["message"],
    })


# ===========================================================================
# API — Templates de legenda
# ===========================================================================


@app.route("/api/templates", methods=["GET"])
def api_get_templates():
    templates = db.get_templates()
    return jsonify({"success": True, "templates": templates})


@app.route("/api/templates", methods=["POST"])
def api_create_template():
    data = request.get_json(silent=True) or {}
    name = data.get("name", "").strip()
    content = data.get("content", "").strip()
    tone = data.get("tone", "descontraido")
    if not name or not content:
        return jsonify({"error": "Nome e conteúdo são obrigatórios."}), 400
    tid = db.add_template(name, content, tone)
    return jsonify({"success": True, "id": tid})


@app.route("/api/templates/<int:template_id>", methods=["PUT"])
def api_update_template(template_id: int):
    data = request.get_json(silent=True) or {}
    updated = db.update_template(template_id, **data)
    if not updated:
        return jsonify({"error": "Template não encontrado."}), 404
    return jsonify({"success": True})


@app.route("/api/templates/<int:template_id>", methods=["DELETE"])
def api_delete_template(template_id: int):
    deleted = db.delete_template(template_id)
    if not deleted:
        return jsonify({"error": "Template não encontrado."}), 404
    return jsonify({"success": True})


# ===========================================================================
# API — Grupos de hashtags
# ===========================================================================


@app.route("/api/hashtag-groups", methods=["GET"])
def api_get_hashtag_groups():
    groups = db.get_hashtag_groups()
    return jsonify({"success": True, "groups": groups})


@app.route("/api/hashtag-groups", methods=["POST"])
def api_create_hashtag_group():
    data = request.get_json(silent=True) or {}
    name = data.get("name", "").strip()
    hashtags = data.get("hashtags", "").strip()
    if not name or not hashtags:
        return jsonify({"error": "Nome e hashtags são obrigatórios."}), 400
    gid = db.add_hashtag_group(name, hashtags)
    return jsonify({"success": True, "id": gid})


@app.route("/api/hashtag-groups/<int:group_id>", methods=["PUT"])
def api_update_hashtag_group(group_id: int):
    data = request.get_json(silent=True) or {}
    updated = db.update_hashtag_group(group_id, **data)
    if not updated:
        return jsonify({"error": "Grupo não encontrado."}), 404
    return jsonify({"success": True})


@app.route("/api/hashtag-groups/<int:group_id>", methods=["DELETE"])
def api_delete_hashtag_group(group_id: int):
    deleted = db.delete_hashtag_group(group_id)
    if not deleted:
        return jsonify({"error": "Grupo não encontrado."}), 404
    return jsonify({"success": True})


# ===========================================================================
# API — Smart Schedule (horários inteligentes)
# ===========================================================================


@app.route("/api/profiles/<int:profile_id>/smart-schedule", methods=["GET"])
def api_smart_schedule(profile_id: int):
    suggestions = db.get_smart_schedule(profile_id)
    day_names = ["Seg", "Ter", "Qua", "Qui", "Sex", "Sáb", "Dom"]
    result = []
    for s in suggestions:
        result.append({
            "day_of_week": s["day_of_week"],
            "day_name": day_names[s["day_of_week"]],
            "hour": s["hour"],
            "time": f"{s['hour']:02d}:00",
            "score": s["score"],
        })
    return jsonify({"success": True, "suggestions": result})


# ===========================================================================
# API — Métricas
# ===========================================================================


@app.route("/api/profiles/<int:profile_id>/metrics", methods=["GET"])
def api_profile_metrics(profile_id: int):
    metrics = db.get_profile_metrics(profile_id)
    return jsonify({"success": True, "metrics": metrics})


# ===========================================================================
# API — Reorder posts
# ===========================================================================


@app.route("/api/posts/reorder", methods=["POST"])
def api_reorder_posts():
    data = request.get_json(silent=True) or {}
    post_ids = data.get("post_ids", [])
    if not post_ids or not isinstance(post_ids, list):
        return jsonify({"error": "post_ids é obrigatório (lista de IDs)."}), 400

    # Pega o horário do primeiro post como base
    first_post = db.get_post(post_ids[0])
    if not first_post:
        return jsonify({"error": "Post não encontrado."}), 404

    base_time = first_post["scheduled_at"]

    # Calcula intervalo médio entre posts existentes
    if len(post_ids) > 1:
        last_post = db.get_post(post_ids[-1])
        if last_post:
            from datetime import datetime as dt
            t1 = dt.fromisoformat(first_post["scheduled_at"])
            t2 = dt.fromisoformat(last_post["scheduled_at"])
            total_minutes = int((t2 - t1).total_seconds() / 60)
            interval = max(total_minutes // (len(post_ids) - 1), 60)
        else:
            interval = 5760  # 4 dias default
    else:
        interval = 5760

    db.reorder_posts(post_ids, base_time, interval)

    # Reagendar no scheduler
    for pid in post_ids:
        scheduler.cancel_post(pid)
        post = db.get_post(pid)
        if post and post["status"] == "pending":
            scheduler.schedule_post(pid, post["scheduled_at"])

    return jsonify({"success": True, "message": f"Posts reordenados ({len(post_ids)} posts)."})


# ===========================================================================
# API — Calendário
# ===========================================================================


@app.route("/api/calendar", methods=["GET"])
def api_calendar():
    month_str = request.args.get("month", "")
    if not month_str:
        from datetime import datetime as dt
        now = dt.now()
        year, month = now.year, now.month
    else:
        parts = month_str.split("-")
        year, month = int(parts[0]), int(parts[1])

    posts = db.get_posts_by_month(year, month)

    # Agrupa por dia
    days = {}
    for p in posts:
        day = p["scheduled_at"][8:10] if p["scheduled_at"] else "00"
        day_int = int(day)
        if day_int not in days:
            days[day_int] = []
        days[day_int].append({
            "id": p["id"],
            "post_type": p["post_type"],
            "status": p["status"],
            "time": p["scheduled_at"][11:16] if p["scheduled_at"] and len(p["scheduled_at"]) > 11 else "",
            "caption": (p["caption"] or "")[:50],
            "profile_name": p.get("profile_name", ""),
        })

    return jsonify({
        "success": True,
        "year": year,
        "month": month,
        "days": days,
        "total_posts": len(posts),
    })


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
        scheduled_at = scheduled_dt.strftime("%Y-%m-%dT%H:%M:%S")

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
