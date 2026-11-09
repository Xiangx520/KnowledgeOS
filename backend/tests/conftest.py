import os
import threading
import uuid

import pytest
from app.config import get_settings
from app.main import create_app
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    infrastructure_url = os.getenv("RAG_TEST_DATABASE_URL")
    schema = "rag_test_" + uuid.uuid4().hex
    admin = None
    if infrastructure_url:
        from sqlalchemy import create_engine, text
        from sqlalchemy.engine import make_url
        admin = create_engine(infrastructure_url)
        with admin.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        infrastructure_url = make_url(infrastructure_url).update_query_dict({"options": "-csearch_path=" + schema}).render_as_string(hide_password=False)
    monkeypatch.setenv("RAG_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("RAG_DATABASE_URL", infrastructure_url or "sqlite:///" + (tmp_path / "test.db").as_posix())
    monkeypatch.setenv("RAG_WORKER_POLL_SECONDS", "0.03")
    monkeypatch.setenv("RAG_EMBEDDING_PROVIDER", "hash")
    monkeypatch.setenv("RAG_LLM_MODEL", "")
    monkeypatch.setenv("RAG_LLM_API_KEY", "")
    monkeypatch.setenv("RAG_REDIS_URL", os.getenv("RAG_TEST_REDIS_URL", ""))
    monkeypatch.setenv("RAG_QDRANT_URL", os.getenv("RAG_TEST_QDRANT_URL", ""))
    monkeypatch.setenv("RAG_QDRANT_COLLECTION_PREFIX", schema)
    monkeypatch.setenv("RAG_ENVIRONMENT", "development")
    monkeypatch.setenv("RAG_RERANKER_MODEL", "")
    monkeypatch.setenv("RAG_RERANKER_PROVIDER", "local")
    monkeypatch.setenv("RAG_CITATION_VERIFIER", "numeric")
    monkeypatch.setenv("RAG_AGENT_LOOP_ENABLED", "false")
    monkeypatch.setenv("RAG_AUTO_MIGRATE", "true")
    monkeypatch.setenv("RAG_ALLOW_REGISTRATION", "true")
    monkeypatch.setenv("RAG_WORKER_ROLE", "all")
    monkeypatch.setenv("RAG_PLATFORM_ADMIN_EMAILS", "[]")
    get_settings.cache_clear()
    from app.services.retrieval import client as vector_client
    vector_client.cache_clear()
    stop, worker = threading.Event(), None
    with TestClient(create_app()) as test_client:
        if get_settings().redis_url:
            from app.worker import run_worker
            worker = threading.Thread(target=run_worker,
                args=(test_client.app.state.database, get_settings(), stop), daemon=True)
            test_client.app.state.worker_stop, test_client.app.state.worker_thread = stop, worker
            worker.start()
        try:
            yield test_client
        finally:
            stop.set()
            if worker:
                worker.join(timeout=10)
    if vector_client.cache_info().currsize:
        if os.getenv("RAG_TEST_QDRANT_URL"):
            for collection in vector_client().get_collections().collections:
                if collection.name.startswith(schema + "_"):
                    vector_client().delete_collection(collection.name)
        vector_client().close()
        vector_client.cache_clear()
    get_settings.cache_clear()
    if admin is not None:
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()
