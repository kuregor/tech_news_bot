import asyncio
import json
import logging
from typing import Any

from cerebras.cloud.sdk import Cerebras

from config import settings

logger = logging.getLogger(__name__)

MAX_RETRIES = 3


def _parse_json(text: str):
    """Извлечь JSON из ответа AI. Если обрезан — восстанавливает частичный массив."""
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        lines = [l for l in lines if not l.strip().startswith("```")]
        text = "\n".join(lines)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Если это массив — пробуем вернуть всё до последнего полного объекта
        stripped = text.strip()
        if stripped.startswith("["):
            last = stripped.rfind("},")
            if last == -1:
                last = stripped.rfind("}")
            if last > 0:
                try:
                    return json.loads(stripped[: last + 1] + "]")
                except json.JSONDecodeError:
                    pass
        raise


def _merge_topics(r1: dict, r2: dict, n1: int, n2: int) -> dict:
    """Объединить результаты двух вызовов analyze_topics."""
    total = n1 + n2
    w1, w2 = n1 / total, n2 / total

    merged: dict[str, dict] = {}
    for t in r1.get("top_topics", []):
        merged[t["label"]] = {**t, "percentage": t["percentage"] * w1}
    for t in r2.get("top_topics", []):
        if t["label"] in merged:
            merged[t["label"]]["percentage"] += t["percentage"] * w2
        else:
            merged[t["label"]] = {**t, "percentage": t["percentage"] * w2}

    top_topics = sorted(merged.values(), key=lambda x: x["percentage"], reverse=True)
    kws = list(dict.fromkeys(r1.get("trending_keywords", []) + r2.get("trending_keywords", [])))[:10]
    tags = list(dict.fromkeys(r1.get("hashtags", []) + r2.get("hashtags", [])))[:10]
    return {"top_topics": top_topics, "trending_keywords": kws, "hashtags": tags}


class AIClient:
    """Сервис анализа и классификации текстов через LLM.

    Хранит ленивый синглтон клиента LLM в self._client и предоставляет
    высокоуровневые методы для анализа тем, описания, суммаризации и
    классификации постов Telegram-каналов.
    """

    def __init__(self) -> None:
        self._client: Cerebras | None = None

    def _get_client(self) -> Cerebras:
        """Назначение: лениво создать и вернуть клиент LLM.

        Возвращает:
            Cerebras: единственный на экземпляр клиент SDK.
        """
        if self._client is None:
            self._client = Cerebras(
                api_key=settings.LLM_API_KEY,
                base_url=settings.LLM_BASE_URL,
            )
        return self._client

    async def _call_llm(
        self,
        system_prompt: str,
        user_prompt: str,
        retries: int = MAX_RETRIES,
        max_tokens: int = 2048,
    ) -> str:
        """Назначение: вызвать LLM API с retry и exponential backoff.

        Параметры:
            system_prompt (str): системный промпт.
            user_prompt (str): пользовательский промпт.
            retries (int): число попыток при ошибке.
            max_tokens (int): лимит токенов ответа.

        Возвращает:
            str: текст ответа модели.

        Исключения:
            Exception: пробрасывает context_length_exceeded без ретраев.
            RuntimeError: если все попытки исчерпаны.
        """
        client = self._get_client()

        def _sync_call():
            return client.chat.completions.create(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                model=settings.LLM_MODEL,
                max_completion_tokens=max_tokens,
                temperature=0.3,
                top_p=1,
                stream=False,
            )

        loop = asyncio.get_running_loop()

        for attempt in range(1, retries + 1):
            try:
                response = await loop.run_in_executor(None, _sync_call)
                return response.choices[0].message.content

            except Exception as e:
                if "context_length_exceeded" in str(e):
                    raise  # ретраить тот же промпт бессмысленно
                logger.warning(
                    "LLM ошибка (попытка %d/%d): %s",
                    attempt, retries, e,
                )
                if attempt < retries:
                    await asyncio.sleep(2 ** attempt)
                    continue
                raise

        raise RuntimeError("LLM: все попытки исчерпаны")

    async def analyze_topics(self, posts_texts: list[str]) -> dict[str, Any]:
        """Назначение: анализ тем канала через LLM (промпт А).

        Параметры:
            posts_texts (list[str]): тексты постов канала.

        Возвращает:
            dict: {top_topics, trending_keywords, hashtags}.

        Исключения:
            RuntimeError: если все попытки LLM исчерпаны.
        """
        system_prompt = (
            "Ты аналитик Telegram-каналов. Отвечай ТОЛЬКО валидным JSON без пояснений.\n"
            "Формат ответа:\n"
            "{\n"
            '  "top_topics": [{"label": "...", "emoji": "...", "percentage": 0.15}],\n'
            '  "trending_keywords": ["слово1", "слово2"],\n'
            '  "hashtags": ["#тег1", "#тег2"]\n'
            "}\n"
            "top_topics — основные темы канала (5-8 штук), percentage — доля постов на эту тему (сумма = 1.0).\n"
            "trending_keywords — 5-10 актуальных ключевых слов.\n"
            "hashtags — 5-10 подходящих хэштегов."
        )
        truncated = [t[:200] for t in posts_texts if t]
        user_prompt = "Проанализируй посты Telegram-канала и определи темы:\n\n" + "\n---\n".join(truncated)

        try:
            raw = await self._call_llm(system_prompt, user_prompt)
            return _parse_json(raw)
        except Exception as e:
            if "context_length_exceeded" in str(e) and len(posts_texts) > 1:
                logger.warning(
                    "LLM context_length_exceeded в analyze_topics: делим %d постов пополам",
                    len(posts_texts),
                )
                mid = len(posts_texts) // 2
                r1, r2 = await asyncio.gather(
                    self.analyze_topics(posts_texts[:mid]),
                    self.analyze_topics(posts_texts[mid:]),
                )
                return _merge_topics(r1, r2, mid, len(posts_texts) - mid)
            raise

    async def analyze_description(
        self, description: str, top_posts_texts: list[str]
    ) -> dict[str, Any]:
        """Назначение: сформировать описание канала через LLM (промпт Б).

        Параметры:
            description (str): исходное описание канала.
            top_posts_texts (list[str]): тексты топ-постов по охвату.

        Возвращает:
            dict: {tagline, about, audience, style}.

        Исключения:
            RuntimeError: если все попытки LLM исчерпаны.
        """
        system_prompt = (
            "Ты аналитик Telegram-каналов. Отвечай ТОЛЬКО валидным JSON без пояснений.\n"
            "Формат ответа:\n"
            "{\n"
            '  "tagline": "краткий слоган канала в одну строку",\n'
            '  "about": "описание канала в 2-3 предложения",\n'
            '  "audience": "кто читает канал, в 1-2 предложения",\n'
            '  "style": "стиль подачи материала, в 1-2 предложения"\n'
            "}"
        )
        texts = top_posts_texts
        while True:
            posts_block = "\n---\n".join(t[:400] for t in texts[:20])
            user_prompt = (
                f"Описание канала: {description or 'не указано'}\n\n"
                f"Топ-20 постов по охвату:\n{posts_block}\n\n"
                "Сформируй описание канала."
            )
            try:
                raw = await self._call_llm(system_prompt, user_prompt)
                return _parse_json(raw)
            except Exception as e:
                if "context_length_exceeded" in str(e) and len(texts) > 1:
                    logger.warning(
                        "LLM context_length_exceeded в analyze_description: "
                        "сокращаем с %d до %d постов",
                        len(texts), len(texts) // 2,
                    )
                    texts = texts[: len(texts) // 2]
                    continue
                raise

    async def summarize_post(self, text: str) -> str:
        """Назначение: краткое резюме одного поста для дайджеста.

        Параметры:
            text (str): текст поста.

        Возвращает:
            str: резюме в 1-2 предложения.
        """
        system_prompt = (
            "Ты редактор новостного дайджеста. "
            "Напиши краткое резюме поста в 1-2 предложения на русском. "
            "Отвечай только текстом резюме, без кавычек и пояснений."
        )
        return await self._call_llm(system_prompt, text[:1500])

    async def classify_posts_by_topic(
        self, posts_data: list[dict], max_tokens: int = 2048
    ) -> list[dict]:
        """Назначение: классифицировать посты по темам одним вызовом LLM.

        Параметры:
            posts_data (list[dict]): [{"id": post_id, "text": "..."}, ...].
            max_tokens (int): лимит токенов ответа.

        Возвращает:
            list[dict]: [{"id": post_id, "topic": "...", "emoji": "..."}, ...].
        """
        system_prompt = (
            "Ты редактор новостного дайджеста. Отвечай ТОЛЬКО валидным JSON без пояснений.\n"
            "Тебе даны посты с id. Определи для каждого поста одну доминирующую тему.\n"
            "Объединяй похожие темы под одним названием (например, все про AI/ML → одна тема).\n"
            "Старайся использовать 3-6 тем на весь список.\n"
            "Формат ответа — JSON-массив:\n"
            '[{"id": 123, "topic": "ИИ и нейросети", "emoji": "🤖"}, ...]\n'
            "Каждый элемент содержит id поста, короткое название темы на русском и подходящий emoji."
        )
        posts_block = "\n".join(
            f"[id={p['id']}] {p['text']}" for p in posts_data
        )
        user_prompt = f"Классифицируй эти посты по темам:\n\n{posts_block}"

        try:
            raw = await self._call_llm(system_prompt, user_prompt, max_tokens=max_tokens)
        except Exception as e:
            if "context_length_exceeded" in str(e) and len(posts_data) > 1:
                logger.warning(
                    "LLM context_length_exceeded: делим батч %d постов пополам",
                    len(posts_data),
                )
                mid = len(posts_data) // 2
                part1, part2 = await asyncio.gather(
                    self.classify_posts_by_topic(posts_data[:mid], max_tokens),
                    self.classify_posts_by_topic(posts_data[mid:], max_tokens),
                )
                return part1 + part2
            raise

        result = _parse_json(raw)
        if isinstance(result, dict) and "posts" in result:
            result = result["posts"]
        return result

    async def classify_posts_for_trends(self, posts: list) -> dict[int, str]:
        """Назначение: классифицировать посты по темам для /trends.

        Лимит LLM — 8192 токена на весь контекст (вход + выход).
        Батч 30 постов × ~70 токенов + 2048 выход ≈ 4100 токенов — безопасно.

        Параметры:
            posts (list): объекты постов с атрибутами id и text.

        Возвращает:
            dict[int, str]: {post_id: topic}.
        """
        BATCH = 30
        chunks = [posts[i:i + BATCH] for i in range(0, len(posts), BATCH)]

        def _sanitize(t: str) -> str:
            return t.replace("\n", " ").replace('"', "'")[:80]

        async def _classify_chunk(chunk):
            data = [{"id": p.id, "text": _sanitize(p.text or "")} for p in chunk]
            results = await self.classify_posts_by_topic(data)
            return {r["id"]: r.get("topic", "") for r in results if isinstance(r, dict)}

        mappings = await asyncio.gather(*[_classify_chunk(c) for c in chunks])
        merged: dict[int, str] = {}
        for m in mappings:
            merged.update(m)
        return merged

    async def compare_styles(
        self,
        ch1_name: str, ch1_posts: list[str],
        ch2_name: str, ch2_posts: list[str],
    ) -> dict[str, str]:
        """Назначение: сравнить стили подачи двух каналов через LLM.

        Параметры:
            ch1_name (str): username первого канала.
            ch1_posts (list[str]): тексты постов первого канала.
            ch2_name (str): username второго канала.
            ch2_posts (list[str]): тексты постов второго канала.

        Возвращает:
            dict[str, str]: {style_ch1, style_ch2}.
        """
        system_prompt = (
            "Ты аналитик Telegram-каналов. Отвечай ТОЛЬКО валидным JSON без пояснений.\n"
            "Формат ответа:\n"
            "{\n"
            '  "style_ch1": "описание стиля первого канала в одном предложении",\n'
            '  "style_ch2": "описание стиля второго канала в одном предложении"\n'
            "}"
        )
        ch1_block = "\n---\n".join(ch1_posts[:10])
        ch2_block = "\n---\n".join(ch2_posts[:10])
        user_prompt = (
            f"Канал 1 ({ch1_name}):\n{ch1_block}\n\n"
            f"Канал 2 ({ch2_name}):\n{ch2_block}\n\n"
            "Сравни стили подачи этих двух каналов."
        )
        raw = await self._call_llm(system_prompt, user_prompt)
        return _parse_json(raw)


# Синглтон-экземпляр сервиса
ai_client = AIClient()
