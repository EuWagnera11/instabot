import base64
import logging
import tempfile
import re
from pathlib import Path

import requests

from config import Config

logger = logging.getLogger(__name__)

TONE_PROMPTS = {
    "profissional": (
        "Tom profissional, autoritativo e consultivo. "
        "Posicione-se como especialista no assunto. Use dados e fatos para dar credibilidade. "
        "Linguagem culta mas acessível, transmitindo confiança e seriedade."
    ),
    "descontraido": (
        "Tom descontraído, próximo e autêntico. "
        "Fale como se estivesse conversando com um amigo. Use emojis com moderação, "
        "linguagem do dia-a-dia, gírias leves e humor sutil quando adequado."
    ),
    "engajamento": (
        "Tom provocativo e interativo, focado em engajamento máximo. "
        "Comece com uma pergunta impactante ou dado surpreendente. "
        "Use CTAs diretos (comente, compartilhe, salve), crie curiosidade e urgência."
    ),
    "informativo": (
        "Tom educativo e didático. Ensine algo valioso de forma clara e estruturada. "
        "Use listas, dados e exemplos práticos. Posicione-se como referência no tema."
    ),
}

SYSTEM_PROMPT = """Você é um copywriter profissional especializado em Instagram com anos de experiência em marketing digital.
Sua tarefa é criar legendas (copys) de alta qualidade para posts no Instagram em português brasileiro.

## ESTRUTURA OBRIGATÓRIA DA LEGENDA:

1. **GANCHO (Hook)**: Comece com uma pergunta impactante, dado surpreendente ou afirmação ousada que prenda a atenção imediatamente. Use emojis estratégicos.
2. **DESENVOLVIMENTO**: Aprofunde o tema com informações valiosas, dores do público ou benefícios. Use parágrafos curtos com quebras de linha entre eles.
3. **CTA (Call to Action)**: Finalize com uma chamada para ação clara — pode ser interação (comente, salve, compartilhe), direcionamento (link na bio, entre em contato) ou reflexão.
4. **HASHTAGS**: Inclua 5-10 hashtags relevantes e estratégicas no final, misturando hashtags populares com hashtags de nicho.

## REGRAS DE QUALIDADE:

- Escreva a legenda PRONTA para postar (sem aspas, sem "Legenda:", sem explicações)
- Use quebras de linha entre parágrafos para facilitar a leitura
- Use emojis de forma estratégica (não exagerada) para destacar pontos importantes
- O texto deve ter entre 500 e 2000 caracteres (legendas mais longas performam melhor)
- Adapte ao tom solicitado
- Se uma imagem for fornecida, analise-a e crie a legenda com base no conteúdo visual
- NÃO inclua pensamentos, raciocínio, explicações ou meta-texto. APENAS a legenda final.

## EXEMPLO DE LEGENDA DE ALTA QUALIDADE:

Você sabia que a falta de planejamento pode custar uma parte significativa do que você levou uma vida inteira para construir? 🏛️

O processo de inventário costuma ser longo, burocrático e, principalmente, oneroso. Quando somamos o ITCMD, custas processuais, taxas de cartório e honorários, a despesa total pode facilmente abocanhar até 20% de todos os bens deixados.

Mas como evitar que isso aconteça com a sua família?

A resposta está no Planejamento Sucessório. Estratégias legais e seguras garantem não apenas uma enorme economia em impostos, mas também evitam conflitos entre herdeiros.

Proteger o futuro de quem você ama é uma decisão que se toma hoje.

📲 Clique no link da bio e agende uma consultoria.

#PlanejamentoSucessorio #DireitoDeFamilia #Advocacia #ProtecaoPatrimonial #Heranca

---
Use este exemplo como referência de estrutura e qualidade, mas NÃO copie o conteúdo. Crie algo original e relevante ao contexto fornecido.
"""


class AICaptionGenerator:
    def __init__(self):
        self.api_url = Config.AI_API_URL.rstrip("/")
        self.api_key = Config.AI_API_KEY
        self.model = Config.AI_MODEL

    def generate_caption(self, image_path: str, tone: str = "descontraido", context: str = "") -> str:
        """Gera legenda para uma imagem. Envia a imagem para análise visual."""
        tone_desc = TONE_PROMPTS.get(tone, TONE_PROMPTS["descontraido"])

        user_msg = f"Crie uma legenda profissional e envolvente para este post do Instagram.\n\n"
        user_msg += f"Tom desejado: {tone_desc}\n"
        if context:
            user_msg += f"Contexto adicional: {context}\n"
        user_msg += "\nSiga a estrutura: GANCHO → DESENVOLVIMENTO → CTA → HASHTAGS"
        user_msg += "\nResponda APENAS com a legenda pronta para postar. Nada mais."

        # Envia a imagem para análise visual (GPT-4o-mini suporta visão)
        image_b64 = self._encode_image(image_path)
        if image_b64:
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
        else:
            # Fallback: texto puro com contexto do nome do arquivo
            filename = Path(image_path).stem if image_path else "post"
            name_parts = filename.replace("-", "_").split("_")
            clean_name = " ".join(p for p in name_parts if not p.isdigit() and len(p) > 2)
            if clean_name:
                user_msg += f"\nTema/referência do conteúdo: {clean_name}"
            messages = [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_msg},
            ]

        return self._call_api(messages)

    def generate_caption_from_video(self, video_path: str, tone: str = "descontraido", context: str = "") -> str:
        """Gera legenda para um vídeo/reel. Extrai frame se possível."""
        # Tenta extrair um frame do vídeo para análise visual
        frame_path = self._extract_frame(video_path)
        if frame_path:
            try:
                result = self.generate_caption(frame_path, tone, context or "Vídeo/Reel para Instagram")
                return result
            finally:
                Path(frame_path).unlink(missing_ok=True)

        # Fallback: texto puro
        tone_desc = TONE_PROMPTS.get(tone, TONE_PROMPTS["descontraido"])
        filename = Path(video_path).stem if video_path else "reel"
        name_parts = filename.split("_")
        clean_name = " ".join(p for p in name_parts if not p.isdigit() and len(p) > 2)

        user_msg = f"Crie uma legenda criativa para um Reel/vídeo do Instagram.\n"
        user_msg += f"Tom: {tone_desc}\n"
        if clean_name:
            user_msg += f"Referência do conteúdo: {clean_name}\n"
        if context:
            user_msg += f"Contexto adicional: {context}\n"
        user_msg += "\nResponda APENAS com a legenda pronta. Sem explicações."

        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_msg},
        ]
        return self._call_api(messages)

    def _call_api(self, messages: list, _retries: int = 3) -> str:
        import time as _time
        url = f"{self.api_url}/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.model,
            "messages": messages,
            "max_tokens": 2000,
            "temperature": 0.7,
        }

        last_error = None
        for attempt in range(_retries + 1):
            try:
                logger.info("Chamando API de IA: %s (model=%s, tentativa=%d)", url, self.model, attempt + 1)
                resp = requests.post(url, json=payload, headers=headers, timeout=60)

                # Retry automático para rate limit (429)
                if resp.status_code == 429 and attempt < _retries:
                    wait = min(2 ** (attempt + 1), 10)  # 2s, 4s, 8s
                    logger.warning("Rate limit (429), aguardando %ds antes de tentar novamente...", wait)
                    _time.sleep(wait)
                    continue

                if not resp.ok:
                    logger.error("API IA erro HTTP %d: %s", resp.status_code, resp.text[:500])
                    raise RuntimeError(f"API retornou HTTP {resp.status_code}: {resp.text[:200]}")

                data = resp.json()

                # Extrai conteúdo com proteção contra null
                choices = data.get("choices", [])
                if not choices:
                    logger.warning("API retornou choices vazio: %s", str(data)[:300])
                    raise RuntimeError("A IA não retornou resposta. Tente novamente.")

                content = choices[0].get("message", {}).get("content")
                if not content:
                    # Se enviou imagem, tenta de novo sem imagem (fallback)
                    has_image = any(
                        isinstance(m.get("content"), list) for m in messages
                        if isinstance(m, dict)
                    )
                    if has_image:
                        logger.warning("Resposta vazia com imagem, tentando sem imagem...")
                        text_messages = []
                        for m in messages:
                            if isinstance(m.get("content"), list):
                                text_parts = [
                                    p["text"] for p in m["content"]
                                    if isinstance(p, dict) and p.get("type") == "text"
                                ]
                                text_messages.append({"role": m["role"], "content": " ".join(text_parts)})
                            else:
                                text_messages.append(m)
                        payload["messages"] = text_messages
                        resp2 = requests.post(url, json=payload, headers=headers, timeout=60)
                        if resp2.ok:
                            data2 = resp2.json()
                            choices2 = data2.get("choices", [])
                            if choices2:
                                content = choices2[0].get("message", {}).get("content")

                    if not content:
                        raise RuntimeError("A IA retornou resposta vazia. Tente novamente.")

                caption = content.strip()

                # Remove blocos de raciocínio <think>...</think> se presentes
                caption = re.sub(r'<think>.*?</think>', '', caption, flags=re.DOTALL).strip()

                # Remove prefixos comuns que o modelo pode adicionar
                for prefix in ["Legenda:", "Caption:", "```", "Aqui está", "Claro!"]:
                    if caption.lower().startswith(prefix.lower()):
                        caption = caption[len(prefix):].strip()

                if not caption:
                    raise RuntimeError("A IA gerou uma legenda vazia. Tente novamente.")

                logger.info("Caption gerada com sucesso (%d chars)", len(caption))
                return caption

            except RuntimeError:
                raise
            except Exception as exc:
                last_error = exc
                logger.error("Erro ao gerar caption com IA: %s", exc)
                if attempt < _retries:
                    _time.sleep(2)
                    continue
                raise RuntimeError(f"Erro na API de IA: {exc}") from exc

        raise RuntimeError(f"Falha após {_retries + 1} tentativas: {last_error}")

    @staticmethod
    def _encode_image(image_path: str) -> str | None:
        """Codifica uma imagem em base64 para envio à API."""
        try:
            path = Path(image_path)
            if not path.exists():
                return None
            if path.stat().st_size > 10 * 1024 * 1024:  # > 10MB, pula
                logger.warning("Imagem muito grande para envio: %s", image_path)
                return None
            data = path.read_bytes()
            return base64.b64encode(data).decode("utf-8")
        except Exception:
            return None

    @staticmethod
    def _extract_frame(video_path: str) -> str | None:
        """Extrai primeiro frame do vídeo para análise visual."""
        try:
            import subprocess
            tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
            tmp.close()
            result = subprocess.run(
                [
                    "ffmpeg", "-y", "-i", video_path,
                    "-vframes", "1", "-q:v", "2",
                    tmp.name,
                ],
                capture_output=True, timeout=15,
            )
            if result.returncode == 0 and Path(tmp.name).stat().st_size > 0:
                return tmp.name
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass

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
