"""API integration tests against real SQLite, local Qdrant and hash retrieval."""
import time
from datetime import timedelta

import pytest
from app.config import Settings
from app.models import Document, IngestionJob, utcnow


def register(client, email="owner@example.com"):
    response = client.post("/api/auth/register", json={"email": email, "password": "correct horse 47", "name": "测试用户"})
    assert response.status_code == 201, response.text
    return {"Authorization": "Bearer " + response.json()["access_token"]}


def test_history_replay_scopes_saved_message_and_reuses_answer(client):
    owner, other = register(client), register(client, "stranger@example.com")
    kb = create_kb(client, owner)
    uploaded = upload(client, owner, kb)
    assert wait_job(client, owner, uploaded["job_id"])["status"] == "completed"
    chat = client.post("/api/chat", headers=owner, json={"question": "年假有几天", "knowledge_base_ids": [kb]}).json()
    case = {"question": "前端文字不能覆盖存档问题", "reference": "十五天", "message_id": chat["message_id"]}
    forbidden = client.post("/api/evaluations", headers=other, json={"name": "越权复评", "cases": [case]})
    assert forbidden.status_code == 404
    replay = client.post("/api/evaluations", headers=owner, json={"name": "历史复评", "cases": [case]})
    assert replay.status_code == 202, replay.text
    row = wait_evaluation(client, owner, replay.json()["id"])["results"][0]
    assert row["answer"] == chat["answer"] and row["question"] == "年假有几天"
    assert row["from_history"] and row["retrieval_basis"] == "recorded"


def wait_evaluation(client, headers, run_id):
    for _ in range(200):
        response = client.get('/api/evaluations/' + run_id, headers=headers)
        assert response.status_code == 200, response.text
        result = response.json()
        if result['status'] in {'completed', 'failed'}:
            return result
        time.sleep(0.05)
    pytest.fail('Evaluation did not finish')


def create_kb(client, headers, name="员工制度"):
    response = client.post("/api/knowledge-bases", headers=headers, json={"name": name, "description": "真实测试语料"})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def upload(client, headers, kb_id, content=None, filename="leave.txt"):
    content = content if content is not None else "员工每年享有十五天带薪年假。请假需要提前三天提交申请。".encode()
    response = client.post("/api/documents/upload", headers=headers, data={"knowledge_base_id": kb_id},
                           files={"file": (filename, content, "text/plain")})
    assert response.status_code == 201, response.text
    return response.json()


def wait_job(client, headers, job_id):
    for _ in range(200):
        response = client.get("/api/jobs/" + job_id, headers=headers)
        assert response.status_code == 200, response.text
        result = response.json()
        if result["status"] in {"completed", "failed"}:
            return result
        time.sleep(0.05)
    pytest.fail("Ingestion did not finish within 10 seconds")


def test_authentication_tokens_and_ownership(client):
    assert client.get("/api/health").json()["status"] == "ok"
    assert client.get("/api/knowledge-bases").status_code == 401
    owner = register(client)
    other = register(client, "other@example.com")
    assert client.get("/api/auth/me", headers=owner).json()["email"] == "owner@example.com"
    assert client.post("/api/auth/login", json={"email": "OWNER@example.com", "password": "correct horse 47"}).status_code == 200
    assert client.post("/api/auth/login", json={"email": "owner@example.com", "password": "incorrect password"}).status_code == 401
    assert client.get("/api/auth/me", headers={"Authorization": owner["Authorization"] + "tamper"}).status_code == 401
    kb = create_kb(client, owner)
    assert client.get("/api/knowledge-bases", headers=other).json() == []
    assert client.get("/api/documents", params={"knowledge_base_id": kb}, headers=other).status_code == 404
    assert client.delete("/api/knowledge-bases/" + kb, headers=other).status_code == 404
    assert client.post("/api/chat", headers=other, json={"question": "年假", "knowledge_base_ids": [kb]}).status_code == 404


def test_upload_ingestion_deduplication_scope_and_cleanup(client):
    owner, other = register(client), register(client, "other@example.com")
    kb, other_kb = create_kb(client, owner), create_kb(client, other)
    result = upload(client, owner, kb, filename="../../leave.txt")
    document_id, job_id = result["document"]["id"], result["job_id"]
    assert result["document"]["name"] == "leave.txt"
    assert wait_job(client, owner, job_id)["status"] == "completed"
    document = client.get("/api/documents", headers=owner).json()[0]
    assert document["status"] == "ready" and document["chunk_count"] > 0
    duplicate = upload(client, owner, kb, filename="renamed.txt")
    assert duplicate["duplicate"] and duplicate["document"]["id"] == document_id
    same_other = upload(client, other, other_kb)
    assert same_other["document"]["id"] != document_id
    assert wait_job(client, other, same_other["job_id"])["status"] == "completed"
    for path in [f"/api/jobs/{job_id}", f"/api/documents/{document_id}/download", f"/api/documents/{document_id}/chunks"]:
        assert client.get(path, headers=other).status_code == 404
    assert client.delete(f"/api/documents/{document_id}", headers=other).status_code == 404
    assert client.post(f"/api/documents/{document_id}/retry", headers=other).status_code == 404
    assert len(client.get(f"/api/documents/{document_id}/chunks", headers=owner).json()) > 0
    assert "十五天" in client.get(f"/api/documents/{document_id}/download", headers=owner).text
    with client.app.state.database.session() as db:
        from pathlib import Path
        path = Path(db.get(Document, document_id).stored_path)
    assert path.parent.name == "uploads" and path.name != "leave.txt"
    assert client.delete(f"/api/documents/{document_id}", headers=owner).status_code == 204
    for _ in range(100):
        if not path.exists():
            break
        time.sleep(0.05)
    assert not path.exists()
    assert client.get(f"/api/jobs/{job_id}", headers=owner).status_code == 404
    assert client.get("/api/documents", headers=owner).json() == []
    assert client.delete("/api/knowledge-bases/" + other_kb, headers=other).status_code == 204


def test_mixed_structure_upload_preview_and_retrieval(client):
    owner = register(client)
    kb = create_kb(client, owner)
    markdown = ("# 报销制度\n员工出差住宿每晚报销上限500元。\n\n"
                "| 项目 | 额度 |\n| --- | --- |\n| 住宿 | 500元 |\n\n"
                "## 示例\n```python\n    limit = 500\n    print(limit)\n```\n")
    uploaded = upload(client, owner, kb, content=markdown.encode(), filename="mixed.md")
    assert wait_job(client, owner, uploaded["job_id"])["status"] == "completed"
    doc_id = uploaded["document"]["id"]
    response = client.get(f"/api/documents/{doc_id}/chunks", headers=owner)
    assert response.status_code == 200
    chunks = response.json()
    assert [c["ordinal"] for c in chunks] == list(range(len(chunks)))
    assert any(c["text"].startswith("| 项目 | 额度 |") for c in chunks)
    assert any(c["text"].startswith("```python\n    limit = 500") for c in chunks)
    assert all(c["page"] == 1 and len(c["text"]) <= 800 for c in chunks)
    answer = client.post("/api/chat", headers=owner, json={
        "question": "出差住宿报销上限是多少", "knowledge_base_ids": [kb]}).json()
    assert answer["citations"] and "500" in answer["answer"]
    assert all(c["document_id"] == doc_id for c in answer["citations"])


def test_upload_validation_failed_job_retry_and_durable_recovery(client):
    headers = register(client)
    kb = create_kb(client, headers)
    assert client.post("/api/documents/upload", headers=headers, data={"knowledge_base_id": kb},
                       files={"file": ("bad.exe", b"x")}).status_code == 415
    assert client.post("/api/documents/upload", headers=headers, data={"knowledge_base_id": kb},
                       files={"file": ("empty.txt", b"")}).status_code == 400
    result = upload(client, headers, kb, content=b"not a PDF", filename="broken.pdf")
    job = wait_job(client, headers, result["job_id"])
    assert job["status"] == "failed" and job["error"]
    retry = client.post(f"/api/documents/{result['document']['id']}/retry", headers=headers)
    assert retry.status_code == 200
    assert wait_job(client, headers, retry.json()["job_id"])["status"] == "failed"
    ready = upload(client, headers, kb)
    assert wait_job(client, headers, ready["job_id"])["status"] == "completed"
    # Simulate a worker crash with an expired lease. The running worker must
    # recover the SQL record without a Redis notification or a new upload.
    with client.app.state.database.session() as db:
        old = db.get(IngestionJob, ready["job_id"])
        old.status, old.lease_token = "processing", "dead-worker"
        old.lease_expires_at = utcnow() - timedelta(seconds=1)
        db.commit()
    assert wait_job(client, headers, ready["job_id"])["status"] == "completed"
    with client.app.state.database.session() as db:
        assert db.get(IngestionJob, ready["job_id"]).attempts == 2


def test_chat_history_citations_evaluation_and_cross_user_isolation(client):
    owner, other = register(client), register(client, "other@example.com")
    kb = create_kb(client, owner)
    result = upload(client, owner, kb)
    doc_id = result["document"]["id"]
    assert wait_job(client, owner, result["job_id"])["status"] == "completed"
    body = {"question": "员工每年享有几天带薪年假？", "knowledge_base_ids": [kb], "mode": "agent"}
    response = client.post("/api/chat", headers=owner, json=body)
    assert response.status_code == 200, response.text
    answer = response.json()
    assert answer["provider"] == "extractive" and "十五天" in answer["answer"]
    assert answer["citations"] and all(c["document_id"] == doc_id for c in answer["citations"])
    conversation_id = answer["conversation_id"]
    history = client.get("/api/conversations/" + conversation_id, headers=owner).json()
    assert [m["role"] for m in history["messages"]] == ["user", "assistant"]
    assert history["messages"][1]["citations"] == answer["citations"]
    assert client.get("/api/conversations/" + conversation_id, headers=other).status_code == 404
    assert client.post("/api/chat", headers=other, json={**body, "knowledge_base_ids": [], "conversation_id": conversation_id}).status_code == 404
    private_answer = client.post("/api/chat", headers=other, json={"question": "年假是多少天"}).json()
    assert private_answer["citations"] == []
    cases = [{"question": "年假是多少天", "reference": "十五天", "expected_document_ids": [doc_id]}]
    evaluation = client.post("/api/evaluations", headers=owner,
                             json={"name": "真实标注测试", "cases": cases, "knowledge_base_ids": [kb]})
    assert evaluation.status_code == 202, evaluation.text
    run = wait_evaluation(client, owner, evaluation.json()["id"])
    assert run["status"] == "completed" and run["metrics"]["hit_at_5"] == 1
    assert run["metrics"]["ragas_enabled"] is False and "faithfulness" not in run["metrics"]
    assert client.get("/api/evaluations/" + run["id"], headers=other).status_code == 404
    assert client.get("/api/evaluations", headers=other).json() == []
    comparison = client.post("/api/evaluations/compare", headers=owner,
                             json={"cases": cases, "knowledge_base_ids": [kb]})
    assert comparison.status_code == 200, comparison.text
    assert comparison.json()["hybrid"]["metrics"]["hit_at_5"] == 1
    assert client.post("/api/evaluations/compare", headers=other,
                       json={"cases": cases, "knowledge_base_ids": []}).status_code == 422
    assert client.delete("/api/conversations/" + conversation_id, headers=owner).status_code == 204
    assert client.get("/api/conversations/" + conversation_id, headers=owner).status_code == 404


def test_upload_limit_stream_cleanup(client, monkeypatch):
    headers = register(client)
    kb = create_kb(client, headers)
    monkeypatch.setattr(client.app.state.settings, "max_upload_mb", 1)
    response = client.post("/api/documents/upload", headers=headers, data={"knowledge_base_id": kb},
                           files={"file": ("too-big.txt", b"a" * (1024 * 1024 + 1))})
    assert response.status_code == 413
    oversized_envelope = client.post("/api/documents/upload", headers={**headers, "Content-Length": str(3 * 1024 * 1024)},
                                     data={"knowledge_base_id": kb}, files={"file": ("tiny.txt", b"text")})
    assert oversized_envelope.status_code == 413
    assert client.get("/api/documents", headers=headers).json() == []
    assert list((client.app.state.settings.data_dir / "uploads").iterdir()) == []


def test_expired_tokens_and_production_configuration():
    import base64
    import hashlib
    import hmac
    import json

    from app.security import token_subject
    settings = Settings(_env_file=None)
    payload = base64.urlsafe_b64encode(json.dumps({"sub": "0" * 36, "exp": 1}).encode()).decode().rstrip("=")
    signature = base64.urlsafe_b64encode(hmac.new(settings.secret_key.encode(), payload.encode(), hashlib.sha256).digest()).decode().rstrip("=")
    assert token_subject(payload + "." + signature, settings) is None
    with pytest.raises(ValueError, match="RAG_SECRET_KEY"):
        Settings(environment="production", _env_file=None)
