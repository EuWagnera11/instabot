import asyncio
import logging
from pathlib import Path
from typing import Any

from config import Config

logger = logging.getLogger(__name__)


class BrowserManagerError(Exception):
    pass


class BrowserManager:
    def __init__(self, profile_id: int, headless: bool = True) -> None:
        self.profile_id = profile_id
        self.headless = headless
        self._playwright: Any = None
        self._context: Any = None
        self._page: Any = None
        self._profile_dir = Path(Config.PROFILES_DIR) / str(profile_id)
        self._profile_dir.mkdir(parents=True, exist_ok=True)

    async def start(self) -> None:
        if self._context is not None:
            return

        from playwright.async_api import async_playwright

        self._playwright = await async_playwright().start()
        self._context = await self._playwright.chromium.launch_persistent_context(
            user_data_dir=str(self._profile_dir.resolve()),
            executable_path=Config.CHROME_EXECUTABLE,
            headless=self.headless,
            viewport={"width": 430, "height": 932},
            user_agent=(
                "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                "AppleWebKit/605.1.15 (KHTML, like Gecko) "
                "Version/17.0 Mobile/15E148 Safari/604.1"
            ),
            locale="pt-BR",
            timezone_id="America/Sao_Paulo",
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
            ],
            ignore_default_args=["--enable-automation"],
        )

        pages = self._context.pages
        self._page = pages[0] if pages else await self._context.new_page()
        logger.info("Chrome iniciado (profile_id=%d, headless=%s)", self.profile_id, self.headless)

    async def stop(self) -> None:
        if self._context:
            await self._context.close()
            self._context = None
            self._page = None
        if self._playwright:
            await self._playwright.stop()
            self._playwright = None

    async def get_page(self) -> Any:
        if self._page is None:
            raise BrowserManagerError("Navegador não iniciado. Chame start() primeiro.")
        return self._page

    async def is_logged_in(self) -> bool:
        if self._page is None:
            return False
        try:
            await self._page.goto(
                "https://www.instagram.com/",
                wait_until="domcontentloaded",
                timeout=20000,
            )
            await self._page.wait_for_timeout(3000)
            current_url = self._page.url
            if "accounts/login" in current_url or "accounts/signup" in current_url:
                return False
            return bool(await self._page.evaluate(
                "() => document.cookie.includes('ds_user_id')"
            ))
        except Exception as exc:
            logger.warning("Erro ao verificar login: %s", exc)
            return False

    async def open_login_page(self) -> None:
        if self._page is None:
            raise BrowserManagerError("Navegador não iniciado.")
        await self._page.goto(
            "https://www.instagram.com/accounts/login/",
            wait_until="domcontentloaded",
            timeout=30000,
        )

    async def wait_for_login(self, timeout_seconds: int = 300) -> bool:
        if self._page is None:
            return False
        elapsed = 0
        while elapsed < timeout_seconds:
            await self._page.wait_for_timeout(3000)
            elapsed += 3
            try:
                current_url = self._page.url
                if (
                    "instagram.com" in current_url
                    and "accounts/login" not in current_url
                    and "accounts/signup" not in current_url
                    and "challenge" not in current_url
                ):
                    if await self._page.evaluate("() => document.cookie.includes('ds_user_id')"):
                        logger.info("Login detectado (profile_id=%d)", self.profile_id)
                        return True
            except Exception:
                pass
        return False

    def has_saved_profile(self) -> bool:
        cookies_file = self._profile_dir / "Default" / "Cookies"
        local_storage = self._profile_dir / "Default" / "Local Storage"
        return cookies_file.exists() or local_storage.exists()

    async def clear_profile(self) -> None:
        await self.stop()
        import shutil
        if self._profile_dir.exists():
            shutil.rmtree(self._profile_dir, ignore_errors=True)
            self._profile_dir.mkdir(parents=True, exist_ok=True)


def run_login_flow(profile_id: int) -> bool:
    """Abre Chrome visível para o usuário fazer login manualmente.

    Após login bem-sucedido, extrai cookies do navegador e salva em
    session.json para que IGAuth possa usar a sessão autenticada.
    """
    import json
    import time

    async def _login():
        bm = BrowserManager(profile_id=profile_id, headless=False)
        await bm.start()
        is_logged = await bm.is_logged_in()
        if is_logged:
            logger.info("Já está logado (profile_id=%d)", profile_id)
            await _save_browser_session(bm, profile_id)
            await bm.stop()
            return True
        await bm.open_login_page()
        success = await bm.wait_for_login(timeout_seconds=300)
        if success:
            await _save_browser_session(bm, profile_id)
        await bm.stop()
        return success

    async def _save_browser_session(bm: BrowserManager, pid: int):
        """Extrai cookies do navegador e salva em session.json."""
        try:
            page = await bm.get_page()
            context = page.context
            cookies = await context.cookies("https://www.instagram.com")

            cookie_dict = {}
            csrf_token = ""
            user_id = ""
            username = ""

            for c in cookies:
                cookie_dict[c["name"]] = c["value"]
                if c["name"] == "csrftoken":
                    csrf_token = c["value"]
                if c["name"] == "ds_user_id":
                    user_id = c["value"]

            # Tenta extrair username da página
            try:
                username = await page.evaluate(
                    """() => {
                        try {
                            const el = document.querySelector('img[data-testid="user-avatar"]');
                            if (el) return el.alt || '';
                            // Fallback: tenta extrair do cookie ou URL
                            return '';
                        } catch { return ''; }
                    }"""
                )
                if not username:
                    # Tenta via cookie sessionid ou via navegação
                    username = cookie_dict.get("ds_user", "")
            except Exception:
                pass

            session_file = Path(Config.PROFILES_DIR) / str(pid) / "session.json"
            session_file.parent.mkdir(parents=True, exist_ok=True)

            session_data = {
                "username": username or f"user_{user_id}",
                "user_id": user_id,
                "csrf_token": csrf_token,
                "cookies": cookie_dict,
                "logged_at": time.time(),
                "login_method": "browser",
            }

            session_file.write_text(json.dumps(session_data, indent=2))
            logger.info(
                "Sessão do navegador salva para profile_id=%d (user_id=%s)",
                pid, user_id,
            )
        except Exception as exc:
            logger.error("Erro ao salvar sessão do navegador: %s", exc)

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(_login())
    finally:
        loop.close()
