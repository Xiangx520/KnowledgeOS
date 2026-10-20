"""Qdrant dense search + BM25, reciprocal rank fusion and optional reranking."""
from __future__ import annotations

import hashlib
import math
import re
import threading
import uuid
from contextlib import nullcontext
from functools import lru_cache

from qdrant_client import QdrantClient, models
from rank_bm25 import BM25Okapi

from app.config import get_settings
from app.services.embeddings import dimension, embed, tokenize

_lock = threading.RLock()


@lru_cache(maxsize=1)
def client() -> QdrantClient:
    s = get_settings()
    if s.qdrant_url:
        return QdrantClient(url=s.qdrant_url, api_key=s.qdrant_api_key or None, timeout=60)
    path = s.data_dir / "qdrant"
    path.mkdir(parents=True, exist_ok=True)
    return QdrantClient(path=str(path))


def collection() -> str:
    s = get_settings()
    identity = f"{s.embedding_provider}:{s.embedding_model}:{getattr(s, 'embedding_base_url', '')}:{dimension()}:jieba-v1"
    return s.qdrant_collection_prefix + "_" + hashlib.sha256(identity.encode()).hexdigest()[:12]


def point_id(chunk_id: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, chunk_id))


def index_chunks(chunks: list[dict], write_fence=None, progress=None) -> None:
    if not chunks:
        return
    with _lock if not get_settings().qdrant_url else nullcontext():
        store, name = client(), collection()
        if not store.collection_exists(name):
            store.create_collection(name, vectors_config=models.VectorParams(size=dimension(), distance=models.Distance.COSINE))
            if get_settings().qdrant_url:
                for field in ("knowledge_base_id", "document_id", "chunk_id"):
                    store.create_payload_index(name, field, field_schema=models.PayloadSchemaType.KEYWORD)
        for offset in range(0, len(chunks), 64):
            if progress:
                progress(offset, len(chunks))
            batch = chunks[offset:offset + 64]
            vectors = embed([c["text"] for c in batch])
            points = [models.PointStruct(id=point_id(c["id"]), vector=v, payload={
                "chunk_id": c["id"], "document_id": c["document_id"], "knowledge_base_id": c["knowledge_base_id"],
                "revision_id": c.get("revision_id"),
            }) for c, v in zip(batch, vectors, strict=True)]
            # Only vector writes are fenced; expensive embedding holds no SQL lock.
            with write_fence() if write_fence else nullcontext():
                store.upsert(name, points, wait=True)
            if progress:
                progress(min(offset + 64, len(chunks)), len(chunks))


def delete_document(document_id: str) -> None:
    delete_revision(document_id)


def delete_revision(document_id: str, revision_id: str | None = None) -> None:
    with _lock:
        store = client()
        # Clean old model collections too, so switching embeddings never resurrects deleted content.
        for item in store.get_collections().collections:
            if item.name.startswith(get_settings().qdrant_collection_prefix + "_"):
                conditions = [models.FieldCondition(key="document_id", match=models.MatchValue(value=document_id))]
                if revision_id:
                    version = models.FieldCondition(key="revision_id", match=models.MatchValue(value=revision_id))
                    legacy = str(uuid.uuid5(uuid.NAMESPACE_URL, "legacy-revision:" + document_id))
                    empty = models.IsEmptyCondition(is_empty=models.PayloadField(key="revision_id"))
                    conditions.append(models.Filter(should=[version, empty]) if revision_id == legacy else version)
                store.delete(item.name, models.FilterSelector(filter=models.Filter(must=conditions)), wait=True)


@lru_cache(maxsize=1)
def reranker(name: str):
    from sentence_transformers import CrossEncoder

    return CrossEncoder(name, trust_remote_code=False)


def rerank_scores(question: str, documents: list[str]) -> list[float]:
    s = get_settings()
    if s.reranker_provider == "local":
        return [float(value) for value in reranker(s.reranker_model).predict(
            [(question, text) for text in documents])]
    import httpx

    # Use the native DashScope rerank endpoint, not chat/completions.
    response = httpx.post(s.reranker_api_url,
        headers={"Authorization": "Bearer " + (s.reranker_api_key or s.llm_api_key)},
        json={"model": s.reranker_model, "input": {"query": question, "documents": documents},
              "parameters": {"top_n": len(documents)}}, timeout=s.model_timeout)
    if not response.is_success:
        # Do not expose provider response bodies, which can contain input text.
        raise ValueError(f"精排 API 请求失败（HTTP {response.status_code}），请检查地址、密钥、模型和额度")
    payload = response.json()
    results = payload.get("output", payload).get("results", [])
    scores = {}
    for item in results:
        index, value = item.get("index"), item.get("relevance_score")
        if (type(index) is not int or not 0 <= index < len(documents) or index in scores
                or isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)):
            raise ValueError("精排 API 返回了无效的文档索引或分数")
        scores[index] = float(value)
    if len(scores) != len(documents):
        raise ValueError("精排 API 未返回全部候选文档的分数")
    return [scores[index] for index in range(len(documents))]


def rerank_candidates(question: str, candidates: list[dict], top_k: int, use_model: bool = True) -> list[dict]:
    from app.services.evidence import clean_pdf_text, clean_text, coverage, evidence_score, focus, quantity_subject
    candidates = [{**item, "text": clean_text(item["text"])} for item in candidates]
    candidates = [{**item, "text": clean_pdf_text(item["text"])} if item.get("document_name", "").lower().endswith(".pdf")
                  else item for item in candidates]
    candidates = [item for item in candidates if item["text"]]
    if quantity_subject(question):
        # A value lookup needs evidence about the requested metric, not just a
        # document title or shared words such as "average" and "time".
        candidates = [item for item in candidates if coverage(question, item["text"]) >= .75]
    if focus(question) != question or re.search(r"什么是|是什么", question):
        candidates = [item for item in candidates if coverage(question, item["text"] + " " +
                      item.get("document_name", "") + " " + item.get("knowledge_base_name", "")) >= .75]
    model = get_settings().reranker_model if use_model else ""
    if model and candidates:
        values = rerank_scores(question, [c["text"] for c in candidates])
        for item, value in zip(candidates, values, strict=True):
            item["rerank_score"] = float(value)
        candidates.sort(key=lambda c: c["rerank_score"], reverse=True)
    else:
        candidates.sort(key=lambda c: (evidence_score(question, c), c.get("retrieval_score", 0)), reverse=True)
    return candidates[:top_k]


def retrieve(question: str, chunks: list[dict], top_k: int = 5, strategy: str = "hybrid", rerank_results: bool = True) -> list[dict]:
    if not chunks:
        return []
    if strategy not in {"hybrid", "dense", "bm25"}:
        raise ValueError("strategy must be hybrid, dense or bm25")
    top_k = max(1, min(top_k, 20))
    from app.services.corpus import CorpusScope
    lazy = isinstance(chunks, CorpusScope)
    by_id = {} if lazy else {c["id"]: c for c in chunks}
    limit = min(len(chunks), max(60, top_k * 6))
    dense, sparse = [], []
    dense_scores, sparse_scores = {}, {}
    if strategy != "bm25":
        vector = embed([question])[0]
        with _lock if not get_settings().qdrant_url else nullcontext():
            store, name = client(), collection()
            if not store.collection_exists(name):
                raise ValueError("当前 embedding 配置尚无索引，请重新索引文档（失败/就绪文档均可重试）")
            # Authorization is applied BEFORE vector ranking, down to the exact allowed chunk IDs.
            scope = chunks.dense_filter() if lazy else models.Filter(must=[models.HasIdCondition(has_id=[point_id(i) for i in by_id])])
            hits = store.query_points(name, query=vector, query_filter=scope, limit=min(limit * 4, 320) if lazy else limit).points
        if lazy:
            # SQL is authoritative for active revision and expiry even during a cleanup outage.
            by_id.update(chunks.hydrate([(h.payload or {}).get("chunk_id") for h in hits]))
        for hit in hits:
            cid = (hit.payload or {}).get("chunk_id")
            if cid in by_id and hit.score > 0:
                dense.append(cid)
                dense_scores[cid] = float(hit.score)
    if strategy != "dense":
        if lazy:
            sparse, sparse_scores = chunks.sparse(question, limit)
            by_id.update(chunks.hydrate(sparse))
        else:
            sparse, sparse_scores = _small_corpus_sparse(question, chunks, limit)
    dense = dense[:limit]
    fused = {}
    for ranking in (dense, sparse):
        for rank, cid in enumerate(ranking, 1):
            fused[cid] = fused.get(cid, 0.0) + 1 / (60 + rank)
    ids = sorted(fused, key=lambda cid: fused[cid], reverse=True)[:limit]
    result = [{**by_id[cid], "chunk_id": cid, "score": fused[cid], "retrieval_score": fused[cid],
               "dense_score": dense_scores.get(cid), "bm25_score": sparse_scores.get(cid)} for cid in ids]
    return rerank_candidates(question, result, top_k, use_model=rerank_results) if strategy == "hybrid" else result[:top_k]


def _small_corpus_sparse(question, chunks, limit):
    """Explicit in-memory fixture/demo adapter; production uses SQL posting lists."""
    sparse, sparse_scores = [], {}
    if chunks:
        corpus = [tokenize(c["text"]) or ["__empty__"] for c in chunks]
        # BM25 can return negative IDF for tiny corpora. Positive floor keeps useful common terms.
        bm25 = BM25Okapi(corpus)
        for word, value in bm25.idf.items():
            if value <= 0:
                bm25.idf[word] = 0.1
        scores = bm25.get_scores(tokenize(question))
        ordered = sorted(zip(chunks, scores, strict=True), key=lambda x: float(x[1]), reverse=True)
        for c, score in ordered[:limit]:
            if score > 0:
                sparse.append(c["id"])
                sparse_scores[c["id"]] = float(score)
    return sparse, sparse_scores
