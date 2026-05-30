"""
Módulo de publicação no Instagram — o coração do InstaBot.

Baseado na engenharia reversa da extensão INSSIST para Chrome.
Realiza upload e publicação de fotos, reels, carrosséis e stories
usando as APIs internas do Instagram via sessão autenticada do navegador.

A abordagem utilizada:
1. Conecta-se ao Chrome com perfil persistente já logado no Instagram.
2. Extrai cookies e CSRF token da sessão do navegador.
3. Usa `requests.Session` com esses cookies para fazer chamadas HTTP
   diretamente do Python (mais simples que page.evaluate para binários).
"""

import asyncio
import base64
import json
import logging
import re
import time
from pathlib import Path
from typing import Any

import requests
from PIL import Image

from config import Config

logger = logging.getLogger(__name__)

# URLs base da API interna do Instagram
_IG_API = "https://www.instagram.com/api/v1"
_IG_RUPLOAD_PHOTO = "https://i.instagram.com/rupload_igphoto"
_IG_RUPLOAD_VIDEO = "https://i.instagram.com/rupload_igvideo"


class InstagramPosterError(Exception):
    """Exceção genérica para erros de publicação no Instagram."""


class InstagramPoster:
    """Publicador de conteudo no Instagram via APIs internas.

    Aceita uma requests.Session ja autenticada (via IGAuth) ou
    uma page do Playwright (modo legado).

    Args:
        session: requests.Session autenticada (preferencial).
        csrf_token: Token CSRF da sessao.
        page: Objeto Page do Playwright (modo legado, opcional).
    """

    def __init__(self, session: requests.Session | None = None, csrf_token: str | None = None, page: Any = None) -> None:
        self.page = page
        self._csrf_token: str | None = csrf_token
        self._session: requests.Session | None = session
        self._ig_app_id: str = Config.IG_APP_ID

    # ==================================================================
    # Sessão e Autenticação
    # ==================================================================

    async def _get_session(self) -> requests.Session:
        """Retorna a sessao requests autenticada.

        Se ja foi fornecida no construtor (via IGAuth), retorna diretamente.
        Caso contrario, extrai cookies do Playwright (modo legado).
        """
        if self._session is not None:
            return self._session

        if self.page is None:
            raise InstagramPosterError(
                "Nenhuma sessao autenticada disponivel. Faca login primeiro."
            )

        context = self.page.context
        cookies = await context.cookies("https://www.instagram.com")

        session = requests.Session()
        for cookie in cookies:
            session.cookies.set(
                cookie["name"],
                cookie["value"],
                domain=cookie.get("domain", ".instagram.com"),
                path=cookie.get("path", "/"),
            )

        session.headers.update({
            "User-Agent": await self.page.evaluate("() => navigator.userAgent"),
            "Accept": "*/*",
            "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
            "Origin": "https://www.instagram.com",
            "Referer": "https://www.instagram.com/",
            "X-IG-App-ID": self._ig_app_id,
        })

        self._session = session
        logger.info("Sessão HTTP criada com %d cookies.", len(cookies))
        return session

    async def get_csrf_token(self) -> str:
        """Obtem o token CSRF da sessao do Instagram.

        Se fornecido no construtor, retorna diretamente.
        Caso contrario, tenta extrair dos cookies da sessao ou do Playwright.
        """
        if self._csrf_token:
            return self._csrf_token

        # Tenta extrair dos cookies da sessao
        if self._session:
            token = self._session.cookies.get("csrftoken")
            if token:
                self._csrf_token = token
                return token

        if self.page is None:
            raise InstagramPosterError(
                "Nenhum CSRF token disponivel. Faca login primeiro."
            )

        try:
            # Garante que estamos no Instagram
            current_url = self.page.url
            if "instagram.com" not in current_url:
                await self.page.goto(
                    "https://www.instagram.com/",
                    wait_until="domcontentloaded",
                    timeout=30000,
                )
                await self.page.wait_for_timeout(2000)

            # Tenta extrair do conteúdo da página
            page_content = await self.page.content()
            match = re.search(r'csrf_token"?\s*:?\s*"([^"]+)"', page_content)

            if match:
                self._csrf_token = match.group(1)
                logger.info("CSRF token obtido com sucesso.")
                return self._csrf_token

            # Fallback: tenta extrair dos cookies
            cookies = await self.page.context.cookies("https://www.instagram.com")
            for cookie in cookies:
                if cookie["name"] == "csrftoken":
                    self._csrf_token = cookie["value"]
                    logger.info("CSRF token obtido via cookie.")
                    return self._csrf_token

            raise InstagramPosterError(
                "Não foi possível extrair o CSRF token. "
                "Verifique se o perfil está logado no Instagram."
            )

        except InstagramPosterError:
            raise
        except Exception as exc:
            raise InstagramPosterError(
                f"Erro ao obter CSRF token: {exc}"
            ) from exc

    # ==================================================================
    # Geradores de ID
    # ==================================================================

    @staticmethod
    def _generate_upload_id() -> str:
        """Gera um ID de upload baseado em timestamp.

        Returns:
            ID de upload como string de milissegundos.
        """
        return str(int(time.time() * 1000))

    @staticmethod
    def _generate_upload_name(prefix: str = "fb_uploader") -> str:
        """Gera um nome único para o upload.

        Args:
            prefix: Prefixo do nome (padrão: 'fb_uploader').

        Returns:
            Nome de upload no formato '{prefix}_{timestamp}'.
        """
        upload_id = str(int(time.time() * 1000))
        return f"{prefix}_{upload_id}"

    # ==================================================================
    # Upload de Foto
    # ==================================================================

    async def upload_photo(
        self,
        file_path: str,
        upload_name: str | None = None,
        is_video_cover: bool = False,
    ) -> str:
        """Faz upload de uma foto para o Instagram.

        Args:
            file_path: Caminho local da imagem.
            upload_name: Nome do upload. Se None, gera automaticamente.
            is_video_cover: Se True, marca como capa de vídeo (media_type=2).

        Returns:
            Nome do upload (upload_name) para uso na publicação.

        Raises:
            InstagramPosterError: Se o upload falhar.
        """
        file_path_obj = Path(file_path)
        if not file_path_obj.exists():
            raise InstagramPosterError(f"Arquivo não encontrado: {file_path}")

        upload_name = upload_name or self._generate_upload_name()
        upload_id = upload_name.split("_")[-1]

        # Obtém dimensões da imagem
        try:
            with Image.open(file_path) as img:
                width, height = img.size
        except Exception:
            width, height = 1080, 1080

        file_data = file_path_obj.read_bytes()
        file_size = len(file_data)

        media_type = 2 if is_video_cover else 1
        rupload_params = json.dumps({
            "upload_id": upload_id,
            "media_type": media_type,
            "upload_media_width": width,
            "upload_media_height": height,
        })

        session = await self._get_session()
        csrf_token = await self.get_csrf_token()

        url = f"{_IG_RUPLOAD_PHOTO}/{upload_name}"
        headers = {
            "Accept": "*/*",
            "Offset": "0",
            "X-Entity-Name": upload_name,
            "X-Entity-Length": str(file_size),
            "X-IG-App-ID": self._ig_app_id,
            "X-Instagram-Rupload-Params": rupload_params,
            "X-CSRFToken": csrf_token,
            "Content-Type": "image/jpeg",
            "Content-Length": str(file_size),
        }

        try:
            resp = session.post(url, data=file_data, headers=headers, timeout=120)
            resp.raise_for_status()
            result = resp.json()

            if result.get("status") != "ok":
                raise InstagramPosterError(
                    f"Upload de foto falhou: {result}"
                )

            logger.info(
                "Foto enviada com sucesso: %s (%dx%d, %d bytes)",
                upload_name, width, height, file_size,
            )
            return upload_name

        except requests.RequestException as exc:
            raise InstagramPosterError(
                f"Erro no upload da foto: {exc}"
            ) from exc

    # ==================================================================
    # Upload de Vídeo
    # ==================================================================

    async def upload_video(
        self,
        file_path: str,
        upload_name: str | None = None,
        video_type: str = "reel",
    ) -> str:
        """Faz upload de um vídeo para o Instagram.

        Args:
            file_path: Caminho local do vídeo.
            upload_name: Nome do upload. Se None, gera automaticamente.
            video_type: Tipo de vídeo — 'reel', 'carousel', 'story'.

        Returns:
            Nome do upload para uso na publicação.

        Raises:
            InstagramPosterError: Se o upload falhar.
        """
        file_path_obj = Path(file_path)
        if not file_path_obj.exists():
            raise InstagramPosterError(f"Arquivo não encontrado: {file_path}")

        upload_name = upload_name or self._generate_upload_name("fb_uploader")
        upload_id = upload_name.split("_")[-1]

        file_data = file_path_obj.read_bytes()
        file_size = len(file_data)

        # Parâmetros específicos por tipo de vídeo
        rupload_params: dict[str, Any] = {
            "upload_id": upload_id,
            "media_type": 2,
            "client-passthrough": 1,
        }

        if video_type == "reel":
            rupload_params["is_clips_video"] = "1"
            rupload_params["extract_cover_frame"] = "1"
            rupload_params["content_tags"] = ""
        elif video_type == "carousel":
            rupload_params["is_sidecar"] = "1"
        elif video_type == "story":
            rupload_params["for_album"] = True

        session = await self._get_session()
        csrf_token = await self.get_csrf_token()

        url = f"{_IG_RUPLOAD_VIDEO}/{upload_name}"
        headers = {
            "Accept": "*/*",
            "Offset": "0",
            "X-Entity-Name": upload_name,
            "X-Entity-Length": str(file_size),
            "X-IG-App-ID": self._ig_app_id,
            "X-Instagram-Rupload-Params": json.dumps(rupload_params),
            "X-CSRFToken": csrf_token,
            "Content-Type": "video/mp4",
            "Content-Length": str(file_size),
        }

        try:
            resp = session.post(url, data=file_data, headers=headers, timeout=300)
            resp.raise_for_status()
            result = resp.json()

            if result.get("status") != "ok":
                raise InstagramPosterError(
                    f"Upload de vídeo falhou: {result}"
                )

            logger.info(
                "Vídeo enviado com sucesso: %s (%d bytes, tipo=%s)",
                upload_name, file_size, video_type,
            )
            return upload_name

        except requests.RequestException as exc:
            raise InstagramPosterError(
                f"Erro no upload do vídeo: {exc}"
            ) from exc

    # ==================================================================
    # Publicação de Foto
    # ==================================================================

    async def publish_photo(
        self,
        upload_name: str,
        caption: str = "",
        location: dict | None = None,
    ) -> dict:
        """Configura e publica uma foto no feed.

        Args:
            upload_name: Nome do upload retornado por upload_photo().
            caption: Legenda da postagem.
            location: Dados de localização (opcional).

        Returns:
            Resposta da API do Instagram.

        Raises:
            InstagramPosterError: Se a publicação falhar.
        """
        upload_id = upload_name.split("_")[-1]
        session = await self._get_session()
        csrf_token = await self.get_csrf_token()

        url = f"{_IG_API}/media/configure/"
        headers = {
            "X-CSRFToken": csrf_token,
            "X-IG-App-ID": self._ig_app_id,
            "Content-Type": "application/x-www-form-urlencoded",
        }

        data: dict[str, Any] = {
            "upload_id": upload_id,
            "caption": caption,
            "disable_comments": "0",
            "like_and_view_counts_disabled": "0",
            "source_type": "library",
        }

        if location:
            data["location"] = json.dumps(location)
            data["geotag_enabled"] = "1"

        try:
            resp = session.post(url, data=data, headers=headers, timeout=60)
            resp.raise_for_status()
            result = resp.json()

            if result.get("status") != "ok":
                raise InstagramPosterError(
                    f"Publicação de foto falhou: {result}"
                )

            media_id = result.get("media", {}).get("pk", "desconhecido")
            logger.info("Foto publicada com sucesso! Media ID: %s", media_id)
            return result

        except requests.RequestException as exc:
            raise InstagramPosterError(
                f"Erro ao publicar foto: {exc}"
            ) from exc

    # ==================================================================
    # Publicação de Reel
    # ==================================================================

    async def publish_reel(
        self,
        upload_name: str,
        cover_upload_name: str | None = None,
        caption: str = "",
        share_to_feed: bool = True,
    ) -> dict:
        upload_id = upload_name.split("_")[-1]
        session = await self._get_session()
        csrf_token = await self.get_csrf_token()

        # Aguarda processamento do vídeo (15s fixo — mais confiável que polling)
        logger.info("Aguardando processamento do vídeo (15s)...")
        await asyncio.sleep(15)

        url = f"{_IG_API}/media/configure_to_clips/"
        headers = {
            "X-CSRFToken": csrf_token,
            "X-IG-App-ID": self._ig_app_id,
            "Content-Type": "application/x-www-form-urlencoded",
        }

        data: dict[str, Any] = {
            "upload_id": upload_id,
            "is_unified_video": "1",
            "clips_share_preview_to_feed": "1" if share_to_feed else "0",
            "caption": caption,
            "disable_comments": "0",
            "like_and_view_counts_disabled": "0",
            "source_type": "library",
        }

        if cover_upload_name:
            cover_upload_id = cover_upload_name.split("_")[-1]
            data["cover_frame_timestamp_ms"] = "0"
            data["cover"] = json.dumps({
                "upload_id": cover_upload_id,
                "media_type": 2,
            })

        # Tenta publicar com retry
        last_error = ""
        for attempt in range(3):
            try:
                resp = session.post(url, data=data, headers=headers, timeout=120)
                resp.raise_for_status()
                result = resp.json()

                if result.get("status") == "ok" and result.get("message") not in (
                    "media_needs_reupload", "transcode_timeout", "upload_error"
                ):
                    media_id = result.get("media", {}).get("pk", "desconhecido")
                    logger.info("Reel publicado com sucesso! Media ID: %s", media_id)
                    return result

                last_error = result.get("message", str(result))
                logger.warning(
                    "Tentativa %d falhou: %s. Aguardando antes de retry...",
                    attempt + 1, last_error
                )
                await asyncio.sleep(10 * (attempt + 1))

            except requests.RequestException as exc:
                last_error = str(exc)
                logger.warning("Tentativa %d erro HTTP: %s", attempt + 1, last_error)
                await asyncio.sleep(10)

        raise InstagramPosterError(f"Publicação de reel falhou após 3 tentativas: {last_error}")

    async def _wait_for_video_processing(
        self, session: requests.Session, csrf_token: str, upload_id: str,
        max_attempts: int = 20, interval: int = 3,
    ) -> None:
        """Aguarda o Instagram processar o vídeo antes de publicar."""
        url = f"{_IG_API}/media/uploaded_media_info/"
        headers = {
            "X-CSRFToken": csrf_token,
            "X-IG-App-ID": self._ig_app_id,
            "Content-Type": "application/x-www-form-urlencoded",
        }

        for attempt in range(max_attempts):
            await asyncio.sleep(interval)
            try:
                resp = session.post(
                    url, data={"upload_id": upload_id}, headers=headers, timeout=30
                )
                if resp.status_code == 200:
                    result = resp.json()
                    status = result.get("upload_status")
                    if status == "completed" or result.get("status") == "ok":
                        logger.info("Vídeo processado (tentativa %d)", attempt + 1)
                        return
                    logger.info("Processando vídeo... tentativa %d, status=%s", attempt + 1, status)
            except Exception:
                pass

        logger.warning("Timeout aguardando processamento, tentando publicar mesmo assim...")

    # ==================================================================
    # Publicação de Carrossel
    # ==================================================================

    async def publish_carousel(
        self,
        items: list[dict[str, str]],
        caption: str = "",
    ) -> dict:
        """Configura e publica um carrossel (sidecar).

        Args:
            items: Lista de dicionários com 'upload_name' e 'type' ('photo'/'video')
                   para cada item do carrossel.
            caption: Legenda do carrossel.

        Returns:
            Resposta da API do Instagram.

        Raises:
            InstagramPosterError: Se a publicação falhar.
        """
        if len(items) < 2:
            raise InstagramPosterError(
                "Um carrossel precisa de pelo menos 2 itens."
            )

        session = await self._get_session()
        csrf_token = await self.get_csrf_token()

        sidecar_id = self._generate_upload_id()

        # Monta metadados dos filhos
        children_metadata: list[dict[str, Any]] = []
        for item in items:
            upload_id = item["upload_name"].split("_")[-1]
            child: dict[str, Any] = {"upload_id": upload_id}

            if item.get("type") == "video":
                child["video_result"] = "deprecated"
                child["clips_share_preview_to_feed"] = "1"

            children_metadata.append(child)

        url = f"{_IG_API}/media/configure_sidecar/"
        headers = {
            "X-CSRFToken": csrf_token,
            "X-IG-App-ID": self._ig_app_id,
            "Content-Type": "application/json",
        }

        payload = {
            "client_sidecar_id": sidecar_id,
            "children_metadata": children_metadata,
            "caption": caption,
            "disable_comments": "0",
            "like_and_view_counts_disabled": "0",
            "source_type": "library",
        }

        try:
            resp = session.post(
                url, json=payload, headers=headers, timeout=120,
            )
            resp.raise_for_status()
            result = resp.json()

            if result.get("status") != "ok":
                raise InstagramPosterError(
                    f"Publicação de carrossel falhou: {result}"
                )

            media_id = result.get("media", {}).get("pk", "desconhecido")
            logger.info(
                "Carrossel publicado com sucesso! Media ID: %s (%d itens)",
                media_id, len(items),
            )
            return result

        except requests.RequestException as exc:
            raise InstagramPosterError(
                f"Erro ao publicar carrossel: {exc}"
            ) from exc

    # ==================================================================
    # Publicação de Story
    # ==================================================================

    async def publish_story(
        self,
        upload_name: str,
        is_video: bool = False,
    ) -> dict:
        """Configura e publica um story.

        Args:
            upload_name: Nome do upload da mídia.
            is_video: Se True, a mídia é um vídeo.

        Returns:
            Resposta da API do Instagram.

        Raises:
            InstagramPosterError: Se a publicação falhar.
        """
        upload_id = upload_name.split("_")[-1]
        session = await self._get_session()
        csrf_token = await self.get_csrf_token()

        url = f"{_IG_API}/web/create/configure_to_story/"
        headers = {
            "X-CSRFToken": csrf_token,
            "X-IG-App-ID": self._ig_app_id,
            "Content-Type": "application/x-www-form-urlencoded",
        }

        data: dict[str, Any] = {
            "upload_id": upload_id,
            "source_type": "library",
        }

        try:
            if is_video:
                logger.info("Aguardando processamento do vídeo para story...")
                await asyncio.sleep(5)

            resp = session.post(url, data=data, headers=headers, timeout=60)
            resp.raise_for_status()
            result = resp.json()

            if result.get("status") != "ok":
                raise InstagramPosterError(
                    f"Publicação de story falhou: {result}"
                )

            logger.info("Story publicado com sucesso!")
            return result

        except requests.RequestException as exc:
            raise InstagramPosterError(
                f"Erro ao publicar story: {exc}"
            ) from exc

    # ==================================================================
    # Método de conveniência: upload + publicação completa
    # ==================================================================

    async def post_photo(self, file_path: str, caption: str = "") -> dict:
        """Faz upload e publica uma foto em um único passo.

        Args:
            file_path: Caminho da imagem.
            caption: Legenda.

        Returns:
            Resposta da publicação.
        """
        upload_name = await self.upload_photo(file_path)
        return await self.publish_photo(upload_name, caption)

    async def post_reel(
        self,
        file_path: str,
        caption: str = "",
        cover_path: str | None = None,
        share_to_feed: bool = True,
    ) -> dict:
        """Faz upload e publica um reel em um único passo.

        Args:
            file_path: Caminho do vídeo.
            caption: Legenda.
            cover_path: Caminho da imagem de capa (opcional).
            share_to_feed: Se True, compartilha no feed.

        Returns:
            Resposta da publicação.
        """
        upload_name = await self.upload_video(file_path, video_type="reel")

        cover_upload_name: str | None = None
        if cover_path:
            cover_upload_name = await self.upload_photo(
                cover_path, is_video_cover=True,
            )
        else:
            # Extrai primeiro frame do vídeo como capa
            cover_path_auto = self._extract_video_frame(file_path)
            if cover_path_auto:
                cover_upload_name = await self.upload_photo(
                    cover_path_auto, is_video_cover=True,
                )

        return await self.publish_reel(
            upload_name, cover_upload_name, caption, share_to_feed,
        )

    @staticmethod
    def _extract_video_frame(video_path: str) -> str | None:
        """Extrai o primeiro frame de um vídeo como imagem JPEG para usar como capa."""
        try:
            import subprocess
            import tempfile
            output = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
            output.close()
            # Tenta usar ffmpeg se disponível
            result = subprocess.run(
                [
                    "ffmpeg", "-y", "-i", video_path,
                    "-vframes", "1", "-q:v", "2",
                    output.name,
                ],
                capture_output=True, timeout=15,
            )
            if result.returncode == 0:
                return output.name
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass

        # Fallback: usa Pillow se for possível (não funciona com vídeo puro)
        # Fallback 2: usa cv2 se disponível
        try:
            import cv2
            cap = cv2.VideoCapture(video_path)
            ret, frame = cap.read()
            cap.release()
            if ret:
                import tempfile
                output_path = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False).name
                cv2.imwrite(output_path, frame)
                return output_path
        except ImportError:
            pass

        logger.warning("Não foi possível extrair frame do vídeo para capa.")
        return None

    async def post_carousel(
        self,
        file_paths: list[str],
        caption: str = "",
    ) -> dict:
        """Faz upload e publica um carrossel em um único passo.

        Args:
            file_paths: Lista de caminhos dos arquivos de mídia.
            caption: Legenda.

        Returns:
            Resposta da publicação.
        """
        items: list[dict[str, str]] = []

        for path in file_paths:
            ext = Path(path).suffix.lower()
            if ext in {".mp4", ".mov", ".avi", ".mkv"}:
                upload_name = await self.upload_video(
                    path, video_type="carousel",
                )
                items.append({"upload_name": upload_name, "type": "video"})
            else:
                upload_name = await self.upload_photo(path)
                items.append({"upload_name": upload_name, "type": "photo"})

        return await self.publish_carousel(items, caption)

    async def post_story(
        self,
        file_path: str,
    ) -> dict:
        """Faz upload e publica um story em um único passo.

        Args:
            file_path: Caminho da mídia.

        Returns:
            Resposta da publicação.
        """
        ext = Path(file_path).suffix.lower()
        is_video = ext in {".mp4", ".mov", ".avi", ".mkv"}

        if is_video:
            upload_name = await self.upload_video(file_path, video_type="story")
        else:
            upload_name = await self.upload_photo(file_path)

        return await self.publish_story(upload_name, is_video=is_video)
