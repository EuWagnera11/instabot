"""
Gerenciador de mídia — escaneia pastas e classifica arquivos para postagem.

Detecta automaticamente o tipo de postagem (foto, reel, carrossel, story)
com base em extensões, prefixos de nome e estrutura de subpastas.
"""

import logging
from dataclasses import dataclass, field
from pathlib import Path

from config import Config

logger = logging.getLogger(__name__)

# Extensões suportadas
IMAGE_EXTENSIONS: set[str] = {".jpg", ".jpeg", ".png", ".webp"}
VIDEO_EXTENSIONS: set[str] = {".mp4", ".mov", ".avi", ".mkv"}
ALL_MEDIA_EXTENSIONS: set[str] = IMAGE_EXTENSIONS | VIDEO_EXTENSIONS


@dataclass
class MediaItem:
    """Representa um item de mídia detectado na pasta.

    Attributes:
        path: Caminho absoluto do arquivo principal.
        type: Tipo de arquivo ('photo' ou 'video').
        post_type: Tipo de postagem ('photo', 'reel', 'carousel', 'story').
        caption: Legenda lida do arquivo .txt correspondente.
        cover_path: Caminho da capa (usado para reels).
        children: Lista de caminhos filhos (usado para carrosséis).
    """

    path: str
    type: str  # 'photo' | 'video'
    post_type: str  # 'photo' | 'reel' | 'carousel' | 'story'
    caption: str = ""
    cover_path: str | None = None
    children: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        """Converte o item para dicionário serializável."""
        return {
            "path": self.path,
            "type": self.type,
            "post_type": self.post_type,
            "caption": self.caption,
            "cover_path": self.cover_path,
            "children": self.children,
        }


class MediaManager:
    """Gerenciador de mídia — escaneia a pasta configurada e detecta itens.

    Args:
        folder_path: Caminho da pasta de mídia. Se None, usa Config.MEDIA_FOLDER.
    """

    def __init__(self, folder_path: str | None = None) -> None:
        """Inicializa o gerenciador de mídia.

        Args:
            folder_path: Pasta raiz contendo os arquivos de mídia.
        """
        self.folder_path = Path(folder_path or Config.MEDIA_FOLDER)

    # ------------------------------------------------------------------
    # Escaneamento principal
    # ------------------------------------------------------------------

    def scan_folder(self) -> list[MediaItem]:
        """Escaneia a pasta de mídia e retorna itens classificados.

        A lógica de detecção segue esta ordem de prioridade:
        1. **Subpastas** → carrossel (os arquivos dentro viram os itens do carrossel).
        2. **Prefixo 'story_'** → story.
        3. **Prefixo 'reel_'** ou vídeo solto → reel.
        4. **Imagem solta** → foto.

        Returns:
            Lista de MediaItem prontos para agendamento.
        """
        if not self.folder_path.exists():
            logger.warning("Pasta de mídia não encontrada: %s", self.folder_path)
            return []

        items: list[MediaItem] = []

        for entry in sorted(self.folder_path.iterdir()):
            try:
                if entry.is_dir():
                    carousel = self._scan_carousel(entry)
                    if carousel:
                        items.append(carousel)
                elif self._is_media(entry):
                    item = self._classify_file(entry)
                    if item:
                        items.append(item)
            except Exception as exc:
                logger.error("Erro ao processar %s: %s", entry, exc)

        logger.info("Escaneamento concluído: %d itens encontrados.", len(items))
        return items

    # ------------------------------------------------------------------
    # Classificação de arquivo
    # ------------------------------------------------------------------

    def _classify_file(self, file_path: Path) -> MediaItem | None:
        """Classifica um arquivo individual de mídia.

        Args:
            file_path: Caminho do arquivo.

        Returns:
            MediaItem classificado ou None se não for mídia válida.
        """
        name_lower = file_path.stem.lower()
        ext = file_path.suffix.lower()
        media_type = "video" if ext in VIDEO_EXTENSIONS else "photo"
        caption = self._read_caption(file_path)

        # Story
        if name_lower.startswith("story_"):
            return MediaItem(
                path=str(file_path),
                type=media_type,
                post_type="story",
                caption=caption,
            )

        # Reel (por prefixo ou vídeo solto)
        if name_lower.startswith("reel_") or media_type == "video":
            cover_path = self._find_cover(file_path)
            return MediaItem(
                path=str(file_path),
                type="video",
                post_type="reel",
                caption=caption,
                cover_path=cover_path,
            )

        # Foto comum
        return MediaItem(
            path=str(file_path),
            type="photo",
            post_type="photo",
            caption=caption,
        )

    # ------------------------------------------------------------------
    # Carrossel (subpasta)
    # ------------------------------------------------------------------

    def _scan_carousel(self, folder: Path) -> MediaItem | None:
        """Escaneia uma subpasta e cria um item do tipo carrossel.

        Args:
            folder: Pasta contendo os arquivos do carrossel.

        Returns:
            MediaItem do tipo carousel ou None se a pasta estiver vazia.
        """
        children: list[str] = []

        for f in sorted(folder.iterdir()):
            if self._is_media(f):
                children.append(str(f))

        if not children:
            logger.debug("Subpasta %s não contém mídia válida.", folder)
            return None

        caption = self._read_caption(folder)
        first_ext = Path(children[0]).suffix.lower()
        first_type = "video" if first_ext in VIDEO_EXTENSIONS else "photo"

        return MediaItem(
            path=str(folder),
            type=first_type,
            post_type="carousel",
            caption=caption,
            children=children,
        )

    # ------------------------------------------------------------------
    # Utilitários
    # ------------------------------------------------------------------

    @staticmethod
    def _is_media(path: Path) -> bool:
        """Verifica se o arquivo é de mídia suportada.

        Args:
            path: Caminho a verificar.

        Returns:
            True se for uma extensão de mídia conhecida.
        """
        return path.is_file() and path.suffix.lower() in ALL_MEDIA_EXTENSIONS

    @staticmethod
    def _read_caption(media_path: Path) -> str:
        """Lê a legenda de um arquivo .txt com o mesmo nome.

        Procura por um arquivo com extensão .txt ao lado do arquivo de mídia
        ou dentro da pasta do carrossel.

        Args:
            media_path: Caminho do arquivo de mídia ou pasta do carrossel.

        Returns:
            Conteúdo da legenda ou string vazia.
        """
        if media_path.is_dir():
            txt_path = media_path / "caption.txt"
            if not txt_path.exists():
                # Tenta nome da pasta + .txt no diretório pai
                txt_path = media_path.parent / f"{media_path.name}.txt"
        else:
            txt_path = media_path.with_suffix(".txt")

        if txt_path.exists():
            try:
                return txt_path.read_text(encoding="utf-8").strip()
            except Exception as exc:
                logger.warning("Erro ao ler legenda %s: %s", txt_path, exc)

        return ""

    @staticmethod
    def _find_cover(video_path: Path) -> str | None:
        """Procura uma imagem de capa para um vídeo (reel).

        Busca por um arquivo de imagem com o mesmo nome do vídeo
        ou com sufixo '_cover'.

        Args:
            video_path: Caminho do arquivo de vídeo.

        Returns:
            Caminho da capa ou None.
        """
        stem = video_path.stem

        for ext in IMAGE_EXTENSIONS:
            # nome_cover.jpg
            cover = video_path.parent / f"{stem}_cover{ext}"
            if cover.exists():
                return str(cover)

            # mesmo nome com extensão de imagem
            same_name = video_path.parent / f"{stem}{ext}"
            if same_name.exists():
                return str(same_name)

        return None


# ======================================================================
# Redimensionamento de mídia por aspect ratio
# ======================================================================

ASPECT_RATIOS = {
    "1:1": (1, 1),
    "4:5": (4, 5),
    "9:16": (9, 16),
    "16:9": (16, 9),
}


def resize_image(image_path: str, ratio: str) -> str:
    """Redimensiona/cropa uma imagem para o aspect ratio desejado.

    Faz crop centralizado para encaixar no ratio sem distorcer.
    Salva por cima do arquivo original.

    Args:
        image_path: Caminho da imagem.
        ratio: Ratio desejado ('1:1', '4:5', '9:16', '16:9', 'original').

    Returns:
        Caminho da imagem (mesmo arquivo, redimensionado).
    """
    if ratio == "original" or ratio not in ASPECT_RATIOS:
        return image_path

    try:
        from PIL import Image

        target_w, target_h = ASPECT_RATIOS[ratio]

        with Image.open(image_path) as img:
            orig_w, orig_h = img.size

            # Calcula crop box centralizado
            target_aspect = target_w / target_h
            orig_aspect = orig_w / orig_h

            if orig_aspect > target_aspect:
                new_w = int(orig_h * target_aspect)
                new_h = orig_h
            else:
                new_w = orig_w
                new_h = int(orig_w / target_aspect)

            left = (orig_w - new_w) // 2
            top = (orig_h - new_h) // 2
            right = left + new_w
            bottom = top + new_h

            cropped = img.crop((left, top, right, bottom))

            max_dim = 1440
            if cropped.width > max_dim or cropped.height > max_dim:
                cropped.thumbnail((max_dim, max_dim), Image.LANCZOS)

            cropped.save(image_path, quality=95)
            logger.info("Imagem redimensionada para %s: %dx%d", ratio, cropped.width, cropped.height)

    except Exception as exc:
        logger.warning("Erro ao redimensionar imagem: %s", exc)

    return image_path
