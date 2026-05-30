"""
Login no Instagram via API privada (sem navegador).

Faz login com username/senha, salva a sessão (cookies) em disco,
e fornece uma requests.Session pronta para o InstagramPoster.
"""

import hashlib
import json
import logging
import time
import uuid
from pathlib import Path
from typing import Optional

import requests

from config import Config

logger = logging.getLogger(__name__)

_IG_WEB = "https://www.instagram.com"

_WEB_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/17.0 Mobile/15E148 Safari/604.1"
)


class IGAuthError(Exception):
    pass


class IGAuth:
    """Gerencia autenticacao no Instagram sem navegador."""

    def __init__(self, profile_id: int):
        self.profile_id = profile_id
        self._session_file = Path(Config.PROFILES_DIR) / str(profile_id) / "session.json"
        self._session_file.parent.mkdir(parents=True, exist_ok=True)

    def login(self, username: str, password: str) -> dict:
        """Faz login no Instagram via API web."""
        session = requests.Session()
        session.headers.update({
            "User-Agent": _WEB_UA,
            "Accept": "*/*",
            "Accept-Language": "pt-BR,pt;q=0.9",
            "Origin": _IG_WEB,
            "Referer": f"{_IG_WEB}/",
            "X-IG-App-ID": Config.IG_APP_ID,
        })

        # 1. Pegar CSRF token
        try:
            resp = session.get(f"{_IG_WEB}/accounts/login/", timeout=30)
            csrf_token = resp.cookies.get("csrftoken", "")
            if not csrf_token:
                csrf_token = session.cookies.get("csrftoken", "missing")
        except Exception as exc:
            raise IGAuthError(f"Erro ao acessar Instagram: {exc}") from exc

        # 2. Fazer login
        login_data = {
            "username": username,
            "enc_password": f"#PWD_INSTAGRAM_BROWSER:0:{int(time.time())}:{password}",
            "queryParams": "{}",
            "optIntoOneTap": "false",
        }

        headers = {
            "X-CSRFToken": csrf_token,
            "Content-Type": "application/x-www-form-urlencoded",
            "X-Requested-With": "XMLHttpRequest",
        }

        try:
            resp = session.post(
                f"{_IG_WEB}/accounts/login/ajax/",
                data=login_data,
                headers=headers,
                timeout=30,
            )
            result = resp.json()
        except Exception as exc:
            raise IGAuthError(f"Erro na requisicao de login: {exc}") from exc

        if result.get("authenticated"):
            user_id = result.get("userId", "")
            csrf_token = session.cookies.get("csrftoken", csrf_token)

            session_data = {
                "username": username,
                "user_id": user_id,
                "csrf_token": csrf_token,
                "cookies": {c.name: c.value for c in session.cookies},
                "logged_at": time.time(),
            }

            self._session_file.write_text(json.dumps(session_data, indent=2))
            logger.info("Login OK para @%s (user_id=%s)", username, user_id)
            return session_data

        elif result.get("two_factor_required"):
            two_factor_info = result.get("two_factor_info", {})
            partial_data = {
                "username": username,
                "two_factor_identifier": two_factor_info.get("two_factor_identifier"),
                "csrf_token": session.cookies.get("csrftoken", csrf_token),
                "cookies": {c.name: c.value for c in session.cookies},
                "pending_2fa": True,
            }
            self._session_file.write_text(json.dumps(partial_data, indent=2))
            raise IGAuthError("2FA_REQUIRED")

        elif result.get("checkpoint_url"):
            raise IGAuthError(f"CHECKPOINT:{result['checkpoint_url']}")

        else:
            msg = result.get("message", "Login falhou")
            raise IGAuthError(f"Login falhou: {msg}")

    def verify_2fa(self, code: str) -> dict:
        """Verifica codigo 2FA."""
        if not self._session_file.exists():
            raise IGAuthError("Nenhum login pendente de 2FA.")

        data = json.loads(self._session_file.read_text())
        if not data.get("pending_2fa"):
            raise IGAuthError("Nenhum login pendente de 2FA.")

        session = requests.Session()
        session.headers.update({
            "User-Agent": _WEB_UA,
            "Accept": "*/*",
            "Origin": _IG_WEB,
            "Referer": f"{_IG_WEB}/",
            "X-IG-App-ID": Config.IG_APP_ID,
        })

        for name, value in data.get("cookies", {}).items():
            session.cookies.set(name, value)

        headers = {
            "X-CSRFToken": data["csrf_token"],
            "Content-Type": "application/x-www-form-urlencoded",
            "X-Requested-With": "XMLHttpRequest",
        }

        verify_data = {
            "username": data["username"],
            "verificationCode": code,
            "identifier": data["two_factor_identifier"],
        }

        try:
            resp = session.post(
                f"{_IG_WEB}/accounts/login/ajax/two_factor/",
                data=verify_data,
                headers=headers,
                timeout=30,
            )
            result = resp.json()
        except Exception as exc:
            raise IGAuthError(f"Erro ao verificar 2FA: {exc}") from exc

        if result.get("authenticated"):
            user_id = result.get("userId", "")
            csrf_token = session.cookies.get("csrftoken", data["csrf_token"])

            session_data = {
                "username": data["username"],
                "user_id": user_id,
                "csrf_token": csrf_token,
                "cookies": {c.name: c.value for c in session.cookies},
                "logged_at": time.time(),
            }

            self._session_file.write_text(json.dumps(session_data, indent=2))
            logger.info("2FA verificado para @%s", data["username"])
            return session_data

        raise IGAuthError(f"Codigo 2FA invalido: {result.get('message', '')}")

    def get_session(self) -> Optional[requests.Session]:
        """Retorna uma requests.Session autenticada."""
        if not self._session_file.exists():
            return None

        data = json.loads(self._session_file.read_text())
        if data.get("pending_2fa"):
            return None

        session = requests.Session()
        for name, value in data.get("cookies", {}).items():
            session.cookies.set(name, value, domain=".instagram.com", path="/")

        session.headers.update({
            "User-Agent": _WEB_UA,
            "Accept": "*/*",
            "Accept-Language": "pt-BR,pt;q=0.9",
            "Origin": _IG_WEB,
            "Referer": f"{_IG_WEB}/",
            "X-IG-App-ID": Config.IG_APP_ID,
        })

        return session

    def get_csrf_token(self) -> Optional[str]:
        """Retorna o CSRF token salvo."""
        if not self._session_file.exists():
            return None
        data = json.loads(self._session_file.read_text())
        return data.get("csrf_token")

    def get_user_info(self) -> Optional[dict]:
        """Retorna info basica do usuario logado."""
        if not self._session_file.exists():
            return None
        data = json.loads(self._session_file.read_text())
        if data.get("pending_2fa"):
            return None
        return {
            "username": data.get("username"),
            "user_id": data.get("user_id"),
            "logged_at": data.get("logged_at"),
        }

    def is_logged_in(self) -> bool:
        """Verifica se ha sessao valida salva."""
        if not self._session_file.exists():
            return False
        data = json.loads(self._session_file.read_text())
        if data.get("pending_2fa"):
            return False
        logged_at = data.get("logged_at", 0)
        if time.time() - logged_at > 86400 * 80:
            return False
        return True

    def logout(self) -> None:
        """Remove sessao salva."""
        if self._session_file.exists():
            self._session_file.unlink()
            logger.info("Sessao removida para profile_id=%d", self.profile_id)
