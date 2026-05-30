import os
from pathlib import Path


class Config:
    # --- Chrome ---
    CHROME_EXECUTABLE: str = os.getenv(
        'INSTABOT_CHROME_PATH',
        r'C:\Program Files\Google\Chrome\Application\chrome.exe'
    )

    # --- Pastas ---
    MEDIA_FOLDER: str = os.getenv(
        'INSTABOT_MEDIA_FOLDER',
        r'C:\Users\Wagner\Instagram\posts'
    )
    PROFILES_DIR: str = os.getenv('INSTABOT_PROFILES_DIR', 'data/profiles')
    UPLOADS_DIR: str = os.getenv('INSTABOT_UPLOADS_DIR', 'data/uploads')
    DB_PATH: str = os.getenv('INSTABOT_DB_PATH', 'data/instabot.db')

    # --- Agendamento ---
    SCHEDULER_TIMEZONE: str = os.getenv('INSTABOT_TIMEZONE', 'America/Sao_Paulo')

    # --- Instagram ---
    IG_APP_ID: str = '936619743392459'

    # --- IA (API compatível com OpenAI) ---
    AI_API_URL: str = os.getenv('INSTABOT_AI_URL', 'https://api.aibee.cloud')
    AI_API_KEY: str = os.getenv('INSTABOT_AI_KEY', 'sk-kpa-9418aa3ea4fef7e450d32cd23f7fd78b0465e1b24e49aba1577b92d37deef258')
    AI_MODEL: str = os.getenv('INSTABOT_AI_MODEL', 'claude-sonnet-4-6')

    # --- Limites ---
    MIN_POST_DELAY: int = int(os.getenv('INSTABOT_MIN_POST_DELAY', '300'))
    MAX_UPLOAD_SIZE: int = 50 * 1024 * 1024

    # --- Flask ---
    SECRET_KEY: str = os.getenv('FLASK_SECRET_KEY', 'instabot-dev-key-change-me')
    DEBUG: bool = os.getenv('FLASK_DEBUG', 'true').lower() in ('true', '1', 'yes')
    HOST: str = os.getenv('FLASK_HOST', '0.0.0.0')
    PORT: int = int(os.getenv('FLASK_PORT', '5000'))

    @classmethod
    def ensure_dirs(cls) -> None:
        Path(cls.DB_PATH).parent.mkdir(parents=True, exist_ok=True)
        Path(cls.MEDIA_FOLDER).mkdir(parents=True, exist_ok=True)
        Path(cls.PROFILES_DIR).mkdir(parents=True, exist_ok=True)
        Path(cls.UPLOADS_DIR).mkdir(parents=True, exist_ok=True)
