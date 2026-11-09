"""Bounded real-model smoke test for the single-agent retrieval loop.

Uses the configured RAG_LLM_* provider from the project .env (never printed),
hash embeddings, no reranker and a temporary SQLite/Qdrant store.  It runs one
multi-hop case and one no-evidence case, capped by AGENT_* budgets.  Results are
reported as a smoke test, not a quality benchmark.
"""
import argparse
import json
import os
import tempfile
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "backend/tests/fixtures/agentic_rag"


def _wait_job(client, headers, job_id):
    for _ in range(400):
        result = client.get("/api/jobs/" + job_id, headers=headers).json()
        if result["status"] in {"completed", "failed"}:
            return result
        time.sleep(0.05)
    return {"status": "timeout"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    manifest = json.loads((FIXTURES / "manifest.json").read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory(prefix="agentic-real-") as temporary:
        overrides = {
            "DATA_DIR": temporary,
            "DATABASE_URL": "sqlite:///" + str(Path(temporary) / "smoke.db"),
            "EMBEDDING_PROVIDER": "hash",
            "RERANKER_MODEL": "",
            "QDRANT_COLLECTION_PREFIX": "agentsmoke_" + uuid.uuid4().hex[:10],
            "REDIS_URL": "",
            "QDRANT_URL": "",
            "ENVIRONMENT": "development",
            "AGENT_LOOP_ENABLED": "true",
            "CITATION_VERIFIER": "llm",
            "VERIFIER_MODEL": "",
            "WORKER_POLL_SECONDS": "0.03",
            "ALLOW_REGISTRATION": "true",
            "PLATFORM_ADMIN_EMAILS": "[]",
            "AUTO_MIGRATE": "true",
        }
        for key, value in overrides.items():
            os.environ["RAG_" + key] = value
        from app.config import get_settings
        from app.main import create_app
        from app.services.answering import llm_ready
        from app.services.retrieval import client as vector_client
        from fastapi.testclient import TestClient

        get_settings.cache_clear()
        vector_client.cache_clear()
        settings = get_settings()
        report = {"generated_at": datetime.now(UTC).isoformat(), "real_model": llm_ready(settings),
                  "model": settings.llm_model, "provider": settings.llm_provider,
                  "embedding": settings.embedding_provider, "verifier": settings.citation_verifier,
                  "reranker": settings.reranker_model or "disabled", "cases": []}
        with TestClient(create_app()) as client:
            auth = client.post("/api/auth/register", json={
                "email": "smoke@example.com", "password": "correct horse 47", "name": "smoke"})
            headers = {"Authorization": "Bearer " + auth.json()["access_token"]}
            kb = client.post("/api/knowledge-bases", headers=headers,
                             json={"name": "real-agent-smoke"}).json()["id"]
            for key in ("approval_policy", "authorization_table"):
                meta = manifest["documents"][key]
                content = (FIXTURES / meta["file"]).read_bytes()
                uploaded = client.post("/api/documents/upload", headers=headers,
                                       data={"knowledge_base_id": kb},
                                       files={"file": (meta["file"], content, "text/markdown")}).json()
                report["cases"].append({"ingestion": key, "status": _wait_job(client, headers, uploaded["job_id"])["status"]})
            cases = [
                {"id": "two_hop_role_dependency",
                 "question": "采购合同由谁审批？该角色单笔采购合同审批上限是多少？"},
                {"id": "no_evidence", "question": "量子计算机低温制冷机的冷却温度是多少？"},
            ]
            for case in cases:
                started = time.perf_counter()
                response = client.post("/api/chat", headers=headers, json={
                    "question": case["question"], "knowledge_base_ids": [kb], "mode": "agent"})
                latency = round((time.perf_counter() - started) * 1000, 2)
                payload = response.json()
                agent = payload.get("agent") or {}
                report["cases"].append({
                    "id": case["id"], "status_code": response.status_code,
                    "latency_ms": latency,
                    "answer": (payload.get("answer") or "")[:1200],
                    "citations": [citation.get("document_name") for citation in payload.get("citations") or []],
                    "provider": payload.get("provider"),
                    "verification": payload.get("verification"),
                    "agent": {key: agent.get(key) for key in
                              ("effective_mode", "status", "stop_reason", "degraded_reason",
                               "budget", "task_summary", "recovery_rounds", "semantic_checked",
                               "steps", "bindings", "gaps")},
                })
        report["note"] = ("Bounded real-model smoke test on synthetic fixtures. It can demonstrate that the "
                          "model chooses actions and that verification runs; it is not a paired quality evaluation.")
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"output": str(args.output.resolve()), "real_model": report["real_model"],
                          "model": report["model"]}, ensure_ascii=False))
        vector_client.cache_clear()
        get_settings.cache_clear()


if __name__ == "__main__":
    main()
