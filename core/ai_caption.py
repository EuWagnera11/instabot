import base64
import logging
import tempfile
from pathlib import Path

import requests

from config import Config

logger = logging.getLogger(__name__)

TONE_PROMPTS = {
    "profissional": "Tom profissional e corporativo. Direto, confiante, focado em resultados.",
    "descontraido": "Tom descontraido e informal. Leve, com emojis, linguagem do dia-a-dia.",
    "engajamento": "Tom focado em engajamento. Use perguntas, CTAs, emojis, gere interacao.",
    "informativo": "Tom informativo e educativo. Compartilhe conhecimento de forma clara.",
}

SYSTEM_PROMPT = """Voce e um especialista em social media para Instagram.
Sua tarefa e criar legendas (copys) para posts no Instagram em portugues brasileiro.

Regras:
- Escreva a legenda pronta para postar (sem aspas, sem "Legenda:")
- Inclua 3-8 hashtags relevantes no final
- Use quebras de linha para separar paragrafos
- Maximo 2200 caracteres
- Adapte ao tom solicitado
- Analise a imagem/video para entender o contexto do post
"""


class AICaptionGenerator:
    def __init__(self):
        self.api_url = Config.AI_API_URL.rstrip("/")
        self.api_key = Config.AI_API_KEY
        self.model = Config.AI_MODEL

    def generate_caption(self, image_path: str, tone: str = "descontraido", context: str = "") -> str:
        image_b64 = self._encode_image(image_path)
        if not image_b64:
            return self._generate_text_only(tone, context)

        tone_desc = TONE_PROMPTS.get(tone, TONE_PROMPTS["descontraido"])
        user_msg = f"Crie uma legenda para este post do Instagram.\nTom: {tone_desc}"
        if context:
            user_msg += f"\nContexto adicional: {context}"

        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": user_msg},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"},
                    },
                ],
            },
        ]

        return self._call_api(messages)

    def generate_caption_from_video(self, video_path: str, tone: str = "descontraido", context: str = "") -> str:
        frame_path = self._extract_frame(video_path)
        if frame_path:
            result = self.generate_caption(frame_path, tone, context)
            Path(frame_path).unlink(missing_ok=True)
            return result
        return self._generate_text_only(tone, context or "Video para Instagram Reels")

    def _generate_text_only(self, tone: str, context: str) -> str:
        tone_desc = TONE_PROMPTS.get(tone, TONE_PROMPTS["descontraido"])
        user_msg = f"Crie uma legenda para um post do Instagram.\nTom: {tone_desc}\nContexto: {context or 'Post generico'}"
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_msg},
        ]
        return self._call_api(messages)

    def _call_api(self, messages: list) -> str:
        url = f"{self.api_url}/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.model,
            "messages": messages,
            "max_tokens": 1000,
            "temperature": 0.8,
        }

        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=60)
            resp.raise_for_status()
            data = resp.json()
            caption = data["choices"][0]["message"]["content"].strip()
            logger.info("Caption gerada com sucesso (%d chars)", len(caption))
            return caption
        except Exception as exc:
            logger.error("Erro ao gerar caption com IA: %s", exc)
            raise RuntimeError(f"Erro na API de IA: {exc}") from exc

    @staticmethod
    def _encode_image(image_path: str) -> str | None:
        try:
            path = Path(image_path)
            if not path.exists():
                return None
            data = path.read_bytes()
            return base64.b64encode(data).decode("utf-8")
        except Exception:
            return None

    @staticmethod
    def _extract_frame(video_path: str) -> str | None:
        try:
            import cv2
            cap = cv2.VideoCapture(video_path)
            ret, frame = cap.read()
            cap.release()
            if ret:
                tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
                cv2.imwrite(tmp.name, frame)
                return tmp.name
        except ImportError:
            pass
        return None
