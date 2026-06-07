import logging
from typing import Optional

import aiohttp
import chromadb

from config import settings

logger = logging.getLogger(__name__)

EMBED_DIM = 4096


class EmbeddingService:
    """Сервис эмбеддингов (Ollama) и векторного хранилища (ChromaDB).

    Хранит ленивый синглтон HTTP-клиента ChromaDB в self._client и
    предоставляет методы получения эмбеддингов через Ollama, а также
    сохранения, поиска, дедупликации и фильтрации векторов в ChromaDB.
    """

    def __init__(self) -> None:
        self._client: chromadb.HttpClient | None = None

    def get_chroma_client(self) -> chromadb.HttpClient:
        """Назначение: лениво создать и вернуть HTTP-клиент ChromaDB.

        Возвращает:
            chromadb.HttpClient: единственный на экземпляр клиент.
        """
        if self._client is None:
            # Парсим host и port из URL
            url = settings.CHROMADB_URL.rstrip("/")
            # Убираем http:// или https://
            stripped = url.split("://", 1)[-1]
            host, port_str = stripped.rsplit(":", 1) if ":" in stripped else (stripped, "8000")
            self._client = chromadb.HttpClient(host=host, port=int(port_str))
            logger.info("ChromaDB клиент подключён к %s", settings.CHROMADB_URL)
        return self._client

    def _get_channel_collection(self):
        """Коллекция для векторов каналов."""
        client = self.get_chroma_client()
        return client.get_or_create_collection(
            name="tg_channels",
            metadata={"hnsw:space": "cosine"},
        )

    def _get_post_collection(self):
        """Коллекция для векторов постов."""
        client = self.get_chroma_client()
        return client.get_or_create_collection(
            name="tg_posts",
            metadata={"hnsw:space": "cosine"},
        )

    # ─── Ollama: получение эмбеддингов ──────────────────────

    async def get_embedding(self, text: str) -> list[float]:
        """Назначение: получить эмбеддинг текста через Ollama /v1/embeddings.

        Параметры:
            text (str): входной текст.

        Возвращает:
            list[float]: вектор эмбеддинга.

        Исключения:
            RuntimeError: при ненулевом HTTP-статусе ответа Ollama.
        """
        url = f"{settings.EMBED_BASE_URL}/v1/embeddings"
        headers = {
            "Authorization": f"Bearer {settings.EMBED_API_KEY}",
            "Content-Type": "application/json",
        }
        logger.info("Запрос эмбеддинга к %s (модель: %s)", settings.EMBED_BASE_URL, settings.EMBED_MODEL)
        timeout = aiohttp.ClientTimeout(total=120)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                url,
                json={"model": settings.EMBED_MODEL, "input": text},
                headers=headers,
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    raise RuntimeError(f"Ollama embeddings ошибка {resp.status}: {body}")
                data = await resp.json()
                return data["data"][0]["embedding"]

    async def get_embeddings_batch(self, texts: list[str], chunk_size: int = 20) -> list[list[float]]:
        """Назначение: получить эмбеддинги для списка текстов чанками.

        Параметры:
            texts (list[str]): тексты для векторизации.
            chunk_size (int): размер чанка на один HTTP-запрос.

        Возвращает:
            list[list[float]]: векторы в порядке входных текстов.

        Исключения:
            RuntimeError: при ненулевом HTTP-статусе ответа Ollama.
        """
        url = f"{settings.EMBED_BASE_URL}/v1/embeddings"
        headers = {
            "Authorization": f"Bearer {settings.EMBED_API_KEY}",
            "Content-Type": "application/json",
        }
        result: list[list[float]] = []
        logger.info("Batch-эмбеддинги для %d текстов через %s", len(texts), settings.EMBED_BASE_URL)
        timeout = aiohttp.ClientTimeout(total=120)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            for i in range(0, len(texts), chunk_size):
                chunk = texts[i : i + chunk_size]
                async with session.post(
                    url,
                    json={"model": settings.EMBED_MODEL, "input": chunk},
                    headers=headers,
                ) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        raise RuntimeError(f"Embedding service ошибка {resp.status}: {body}")
                    data = await resp.json()
                    items = sorted(data["data"], key=lambda x: x["index"])
                    result.extend(item["embedding"] for item in items)
        return result

    # ─── ChromaDB: хранение и поиск ─────────────────────────

    def upsert_channel_embedding(
        self,
        channel_id: int,
        embedding: list[float],
        metadata: dict | None = None,
    ) -> None:
        """Назначение: сохранить вектор канала в ChromaDB.

        Параметры:
            channel_id (int): идентификатор канала.
            embedding (list[float]): вектор канала.
            metadata (dict | None): дополнительные метаданные.
        """
        collection = self._get_channel_collection()
        meta = metadata or {}
        meta["channel_id"] = channel_id
        collection.upsert(
            ids=[f"channel_{channel_id}"],
            embeddings=[embedding],
            metadatas=[meta],
        )
        logger.info("Вектор канала %d сохранён в ChromaDB", channel_id)

    def upsert_post_embedding(
        self,
        post_id: int,
        embedding: list[float],
        metadata: dict | None = None,
    ) -> None:
        """Назначение: сохранить вектор поста в ChromaDB.

        Параметры:
            post_id (int): идентификатор поста.
            embedding (list[float]): вектор поста.
            metadata (dict | None): дополнительные метаданные.
        """
        collection = self._get_post_collection()
        meta = metadata or {}
        meta["post_id"] = post_id
        collection.upsert(
            ids=[f"post_{post_id}"],
            embeddings=[embedding],
            metadatas=[meta],
        )

    def upsert_post_embeddings_batch(
        self,
        post_ids: list[int],
        embeddings: list[list[float]],
        metadatas: list[dict] | None = None,
    ) -> None:
        """Назначение: пакетно сохранить векторы постов в ChromaDB.

        Параметры:
            post_ids (list[int]): идентификаторы постов.
            embeddings (list[list[float]]): соответствующие векторы.
            metadatas (list[dict] | None): метаданные по каждому посту.
        """
        if not post_ids:
            return
        collection = self._get_post_collection()
        ids = [f"post_{pid}" for pid in post_ids]
        metas = metadatas or [{"post_id": pid} for pid in post_ids]
        collection.upsert(ids=ids, embeddings=embeddings, metadatas=metas)
        logger.info("Сохранено %d векторов постов в ChromaDB", len(post_ids))

    def search_similar_posts(
        self,
        query_embedding: list[float],
        top_k: int = 10,
        channel_ids: list[int] | None = None,
    ) -> list[tuple[int, float]]:
        """Назначение: найти похожие посты по вектору запроса.

        Параметры:
            query_embedding (list[float]): вектор запроса.
            top_k (int): максимум результатов.
            channel_ids (list[int] | None): ограничение по каналам.

        Возвращает:
            list[tuple[int, float]]: [(post_id, distance), ...].
        """
        collection = self._get_post_collection()
        where = None
        if channel_ids:
            if len(channel_ids) == 1:
                where = {"channel_id": channel_ids[0]}
            else:
                where = {"channel_id": {"$in": channel_ids}}

        results = collection.query(
            query_embeddings=[query_embedding],
            n_results=top_k,
            where=where,
            include=["distances", "metadatas"],
        )

        pairs = []
        if results["ids"] and results["ids"][0]:
            for i, doc_id in enumerate(results["ids"][0]):
                post_id = results["metadatas"][0][i]["post_id"]
                distance = results["distances"][0][i] if results["distances"] else 0.0
                pairs.append((post_id, distance))
        return pairs

    def deduplicate_by_embeddings(
        self,
        post_ids: list[int],
        threshold: float | None = None,
    ) -> list[int]:
        """Назначение: дедупликация постов по близости векторов в ChromaDB.

        Все эмбеддинги загружаются одним запросом, попарные сходства считаются
        через numpy; из группы дубликатов остаётся первый по порядку post_id.

        Параметры:
            post_ids (list[int]): идентификаторы постов-кандидатов.
            threshold (float | None): минимальная cosine similarity для
                признания дубликатом (по умолчанию settings.DEDUP_THRESHOLD).

        Возвращает:
            list[int]: post_id без дубликатов.
        """
        import numpy as np

        if threshold is None:
            threshold = settings.DEDUP_THRESHOLD

        if not post_ids or len(post_ids) <= 1:
            return post_ids

        collection = self._get_post_collection()
        chroma_ids = [f"post_{pid}" for pid in post_ids]
        try:
            data = collection.get(ids=chroma_ids, include=["embeddings"])
        except Exception:
            return post_ids

        if data["embeddings"] is None or len(data["embeddings"]) == 0:
            return post_ids

        id_to_emb: dict[int, list[float]] = {}
        for i, cid in enumerate(data["ids"]):
            pid = int(cid.replace("post_", ""))
            id_to_emb[pid] = data["embeddings"][i]

        ordered_ids = [pid for pid in post_ids if pid in id_to_emb]
        n = len(ordered_ids)
        if n <= 1:
            return [pid for pid in post_ids if pid not in set()]

        # Матрица эмбеддингов → нормализуем строки → попарные cosine similarities за O(n²d)
        emb_matrix = np.array([id_to_emb[pid] for pid in ordered_ids], dtype=np.float32)
        norms = np.linalg.norm(emb_matrix, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        normed = emb_matrix / norms
        sim_matrix = normed @ normed.T  # shape (n, n), значения в [-1, 1]

        is_duplicate: set[int] = set()
        for i, pid in enumerate(ordered_ids):
            if pid in is_duplicate:
                continue
            for j in range(i + 1, n):
                pid2 = ordered_ids[j]
                if pid2 in is_duplicate:
                    continue
                if float(sim_matrix[i, j]) >= threshold:
                    is_duplicate.add(pid2)

        return [pid for pid in post_ids if pid not in is_duplicate]

    def filter_by_keyword_embedding(
        self,
        keyword_embedding: list[float],
        channel_ids: list[int] | None = None,
        threshold: float = 0.5,
        top_k: int = 100,
    ) -> list[int]:
        """Назначение: отфильтровать посты по близости к ключевому слову.

        Параметры:
            keyword_embedding (list[float]): вектор ключевого слова.
            channel_ids (list[int] | None): ограничение по каналам.
            threshold (float): минимальная близость (similarity).
            top_k (int): максимум кандидатов из ChromaDB.

        Возвращает:
            list[int]: post_id подходящих постов.
        """
        max_distance = 1.0 - threshold
        collection = self._get_post_collection()

        where = None
        if channel_ids:
            if len(channel_ids) == 1:
                where = {"channel_id": channel_ids[0]}
            else:
                where = {"channel_id": {"$in": channel_ids}}

        results = collection.query(
            query_embeddings=[keyword_embedding],
            n_results=top_k,
            where=where,
            include=["distances", "metadatas"],
        )

        matched = []
        if results["ids"] and results["ids"][0]:
            for i, doc_id in enumerate(results["ids"][0]):
                dist = results["distances"][0][i]
                if dist <= max_distance:
                    matched.append(results["metadatas"][0][i]["post_id"])
        return matched


# Синглтон-экземпляр сервиса
embedding_service = EmbeddingService()
