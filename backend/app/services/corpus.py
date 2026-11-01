"""Lazy authorized corpus. SQL loads only ranked candidates, never all source text."""
import hashlib
import json
from collections import Counter

from sqlalchemy import and_, func, or_, select

from app.models import Chunk, ChunkTerm, Document, KnowledgeBase, utcnow
from app.services.access import allowed_kbs
from app.services.embeddings import tokenize


def term_rows(chunk_id, text):
    tokens = [t for t in tokenize(text) if len(t) <= 128]
    return len(tokens), [ChunkTerm(chunk_id=chunk_id, term=t, frequency=f) for t, f in Counter(tokens).items()]


class CorpusScope:
    def __init__(self, db, user, selected):
        from fastapi import HTTPException
        self.db = db
        self.user_id = user.id
        query = select(KnowledgeBase.id, KnowledgeBase.generation).where(allowed_kbs(user.id))
        if selected:
            query = query.where(KnowledgeBase.id.in_(selected))
        self.bases = db.execute(query.order_by(KnowledgeBase.id)).all()
        self.kb_ids = [row.id for row in self.bases]
        if selected and set(selected) != set(self.kb_ids):
            raise HTTPException(404, "一个或多个知识库不存在")
        self.conditions = [Document.knowledge_base_id.in_(self.kb_ids), Document.knowledge_base_id.in_(
            select(KnowledgeBase.id).where(allowed_kbs(user.id))),
            Document.active_revision_id.is_not(None), Chunk.revision_id == Document.active_revision_id,
            Document.status != "deleting", or_(Document.expires_at.is_(None), Document.expires_at > utcnow())]

    def __len__(self):
        return self.db.scalar(select(func.count()).select_from(Chunk).join(Document).where(*self.conditions)) or 0

    def fingerprint(self):
        current = self.db.execute(select(KnowledgeBase.id, KnowledgeBase.generation).where(
            KnowledgeBase.id.in_(self.kb_ids), allowed_kbs(self.user_id)).order_by(KnowledgeBase.id)).all()
        return hashlib.sha256(json.dumps([[(r.id, r.generation) for r in current], len(self)]).encode()).hexdigest()

    def hydrate(self, ids):
        if not ids:
            return {}
        rows = self.db.execute(select(Chunk, Document, KnowledgeBase.name).select_from(Chunk)
            .join(Document, Document.id == Chunk.document_id).join(KnowledgeBase, KnowledgeBase.id == Document.knowledge_base_id).where(
            *self.conditions, Chunk.id.in_(ids))).all()
        return {c.id: {"id": c.id, "document_id": d.id, "document_name": d.name,
            "knowledge_base_id": d.knowledge_base_id, "text": c.text, "page": c.page,
            "ordinal": c.ordinal, "revision_id": c.revision_id, "source": d.source,
            "context_summary": c.context_summary, "source_metadata": c.source_metadata,
            "knowledge_base_name": name} for c, d, name in rows}

    def sparse(self, question, limit):
        from app.services.evidence import terms
        words = sorted(t for t in terms(question) if len(t) <= 128)[:64]
        if not words:
            return [], {}
        statistics = self.db.execute(select(func.count(Chunk.id), func.avg(Chunk.token_count))
            .join(Document).where(*self.conditions)).one()
        count, average = statistics[0], max(statistics[1] or 1, 1)
        # Posting lists and document frequencies are restricted to active authorized revisions.
        frequencies = select(ChunkTerm.term.label("term"), func.count().label("df")).join(
            Chunk, Chunk.id == ChunkTerm.chunk_id).join(Document).where(
                *self.conditions, ChunkTerm.term.in_(words)).group_by(ChunkTerm.term).subquery()
        tf = ChunkTerm.frequency
        idf = func.ln(1 + (count - frequencies.c.df + 0.5) / (frequencies.c.df + 0.5))
        bm25 = idf * tf * 2.2 / (tf + 1.2 * (0.25 + 0.75 * Chunk.token_count / average))
        query = select(ChunkTerm.chunk_id, func.sum(bm25).label("score")).join(
            Chunk, Chunk.id == ChunkTerm.chunk_id).join(Document).join(
            frequencies, frequencies.c.term == ChunkTerm.term).where(*self.conditions,
            ChunkTerm.term.in_(words)).group_by(ChunkTerm.chunk_id).order_by(
            func.sum(bm25).desc(), ChunkTerm.chunk_id).limit(limit)
        hits = self.db.execute(query).all()
        return [r[0] for r in hits], {r[0]: float(r[1]) for r in hits}

    def expand_neighbors(self, candidates):
        """Bounded same-page context avoids severing definitions from adjacent code."""
        if not candidates:
            return candidates
        windows = [and_(Chunk.document_id == c["document_id"], Chunk.revision_id == c["revision_id"],
                        Chunk.ordinal.between(c["ordinal"] - 1, c["ordinal"] + 1),
                        Chunk.page == c.get("page")) for c in candidates]
        rows = self.db.scalars(select(Chunk).join(Document).where(*self.conditions, or_(*windows))
                               .order_by(Chunk.ordinal)).all()
        from app.services.evidence import clean_pdf_text, clean_text
        expanded = []
        for candidate in candidates:
            group = [row for row in rows if row.document_id == candidate["document_id"]
                     and row.page == candidate.get("page") and abs(row.ordinal-candidate["ordinal"]) <= 1]
            text = ""
            for row in group:
                part = clean_text(row.text)
                if candidate["document_name"].lower().endswith(".pdf"):
                    part = clean_pdf_text(part)
                overlap = next((n for n in range(min(len(text),len(part),200), 19, -1) if text.endswith(part[:n])), 0)
                text += ("\n\n" if text and not overlap else "") + part[overlap:]
            expanded.append({**candidate, "text": text or candidate["text"], "context_chunk_ids": [row.id for row in group]})
        return expanded

    def dense_filter(self):
        from qdrant_client import models
        current_ids = self.db.scalars(select(KnowledgeBase.id).where(
            KnowledgeBase.id.in_(self.kb_ids), allowed_kbs(self.user_id))).all()
        return models.Filter(must=[models.FieldCondition(key="knowledge_base_id",
            match=models.MatchAny(any=list(current_ids)))])


def active_chunks_condition():
    return and_(Chunk.revision_id == Document.active_revision_id,
                or_(Document.expires_at.is_(None), Document.expires_at > utcnow()))
