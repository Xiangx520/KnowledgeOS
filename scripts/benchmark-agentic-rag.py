"""Offline bounded-agent control-flow benchmark on synthetic fixtures.

This runs standard, legacy agent and the new bounded loop against the same
multi-document fixtures using hash embeddings and no model.  It measures labeled
evidence-chain/action/budget behavior, not real-model answer quality.  Set no
production endpoints; everything runs in a temporary database and local Qdrant.
"""
import argparse
import json
import os
import re
import sys
import tempfile
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
FIXTURES = ROOT / "backend/tests/fixtures/agentic_rag"


def _policy(scenario):
    kind = scenario["kind"]
    first_query = scenario.get("first_hop_query", "采购合同 审批责任人")
    role = next(iter(scenario.get("first_hop_must_contain") or ["区域负责人"]))

    if kind in {"dependency_two_hop", "dependency_two_hop_variant"}:
        def dependency(state, budget, validate):
            joined = "\n".join(item.text for item in state.evidence)
            table = next((item for item in state.evidence if "单笔审批上限" in item.text), None)
            match = re.search("审批责任人为" + re.escape(role) + "。", joined)
            if not state.evidence:
                return {"action": "search", "task_id": "t1", "query": first_query,
                        "top_k": 1, "reason_code": "collect_evidence"}
            if match and table is None:
                source = next(item for item in state.evidence if role in item.text)
                return {"action": "search", "task_id": "t2", "query": f"{role} 单笔采购合同 审批上限",
                        "top_k": 2, "reason_code": "resolve_dependency",
                        "derive_task": {"id": "t2", "goal": f"查询{role}审批上限", "depends_on": ["t1"],
                                        "bindings": [{"name": "审批角色", "value": role,
                                                      "evidence_id": source.evidence_id,
                                                      "excerpt": match.group(0)}]}}
            return {"action": "answer", "task_ids": [task.id for task in state.tasks],
                    "evidence_ids": [item.evidence_id for item in state.evidence],
                    "reason_code": "sufficient_evidence"}
        return dependency

    if kind in {"comparison", "comparison_missing_side", "conflict"}:
        def task_search(state, budget, validate):
            for task in state.tasks:
                if task.origin == "user" and not task.evidence_ids:
                    return {"action": "search", "task_id": task.id, "query": task.query,
                            "top_k": 5, "reason_code": "compare_sides"}
            if state.evidence:
                return {"action": "answer", "task_ids": [task.id for task in state.tasks],
                        "evidence_ids": [item.evidence_id for item in state.evidence],
                        "reason_code": "sufficient_evidence"}
            return {"action": "abstain", "missing": "没有检索到证据", "reason_code": "knowledge_missing"}
        return task_search

    if kind == "no_evidence":
        def no_evidence(state, budget, validate):
            if not state.evidence and not state.steps:
                return {"action": "search", "task_id": "t1", "query": "量子计算机 低温制冷机 冷却温度",
                        "top_k": 5, "reason_code": "collect_evidence"}
            return {"action": "abstain", "missing": "知识库没有相关证据", "reason_code": "knowledge_missing"}
        return no_evidence

    if kind == "clarify":
        def clarify(state, budget, validate):
            return {"action": "clarify", "question": "请明确要查询的对象。",
                    "reason_code": "user_condition_ambiguous"}
        return clarify
    return None


def _wait_job(client, headers, job_id):
    for _ in range(300):
        result = client.get("/api/jobs/" + job_id, headers=headers).json()
        if result["status"] in {"completed", "failed"}:
            if result["status"] != "completed":
                raise RuntimeError(result.get("error") or "ingestion failed")
            return
        time.sleep(0.05)
    raise RuntimeError("ingestion timeout")


def _prepare(client, headers, scenario):
    kb = client.post("/api/knowledge-bases", headers=headers,
                     json={"name": "bench-" + scenario["id"]}).json()["id"]
    for key in scenario["documents"]:
        meta = json.loads((FIXTURES / "manifest.json").read_text(encoding="utf-8"))["documents"][key]
        content = (FIXTURES / meta["file"]).read_bytes()
        response = client.post("/api/documents/upload", headers=headers, data={"knowledge_base_id": kb},
                               files={"file": (meta["file"], content, "text/markdown")})
        _wait_job(client, headers, response.json()["job_id"])
    return kb


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--scenario", action="append", default=[],
                        help="limit to a scenario id (repeatable)")
    args = parser.parse_args()
    manifest = json.loads((FIXTURES / "manifest.json").read_text(encoding="utf-8"))
    scenarios = [item for item in manifest["scenarios"]
                 if not args.scenario or item["id"] in args.scenario]
    with tempfile.TemporaryDirectory(prefix="agentic-rag-") as temporary:
        values = {"DATA_DIR": temporary, "DATABASE_URL": "sqlite:///" + str(Path(temporary) / "bench.db"),
                  "EMBEDDING_PROVIDER": "hash", "LLM_MODEL": "", "LLM_API_KEY": "", "REDIS_URL": "",
                  "QDRANT_URL": "", "QDRANT_COLLECTION_PREFIX": "agentbench_" + uuid.uuid4().hex[:10],
                  "RERANKER_MODEL": "", "RERANKER_PROVIDER": "local", "ENVIRONMENT": "development",
                  "AGENT_LOOP_ENABLED": "false", "CITATION_VERIFIER": "numeric",
                  "WORKER_POLL_SECONDS": "0.03", "ALLOW_REGISTRATION": "true",
                  "PLATFORM_ADMIN_EMAILS": "[]", "AUTO_MIGRATE": "true"}
        for key, value in values.items():
            os.environ["RAG_" + key] = value
        from app.config import get_settings
        from app.main import create_app
        from app.services.agent_evaluation import compare_variants
        from app.services.retrieval import client as vector_client
        from fastapi.testclient import TestClient

        get_settings.cache_clear()
        vector_client.cache_clear()
        with TestClient(create_app()) as client:
            auth = client.post("/api/auth/register", json={
                "email": "bench@example.com", "password": "correct horse 47", "name": "bench"})
            headers = {"Authorization": "Bearer " + auth.json()["access_token"]}
            case_rows, runs = [], {"baseline": [], "agent_loop": []}
            latencies = {"baseline": [], "agent_loop": []}
            for scenario in scenarios:
                kb = _prepare(client, headers, scenario)
                body = {"question": scenario["question"], "knowledge_base_ids": [kb], "mode": "agent"}
                started = time.perf_counter()
                baseline = client.post("/api/chat", headers=headers, json=body).json()
                latencies["baseline"].append((time.perf_counter() - started) * 1000)
                policy = _policy(scenario)
                client.app.state.settings.agent_loop_enabled = True
                client.app.state.agent_policy = policy
                started = time.perf_counter()
                response = client.post("/api/chat", headers=headers, json=body)
                latencies["agent_loop"].append((time.perf_counter() - started) * 1000)
                client.app.state.settings.agent_loop_enabled = False
                client.app.state.agent_policy = None
                if response.status_code != 200:
                    raise RuntimeError(f"{scenario['id']}: HTTP {response.status_code} {response.text}")
                case_rows.append(scenario)
                runs["baseline"].append(baseline)
                runs["agent_loop"].append(response.json())
            report = compare_variants(case_rows, runs, latencies)
            report.update({
                "generated_at": datetime.now(UTC).isoformat(),
                "fixture_version": manifest["version"],
                "real_model": False,
                "llm_configured": False,
                "embedding_provider": "hash",
                "policy": "injected scripted policy (deterministic control-flow test)",
                "note": ("Offline synthetic-fixture benchmark. It proves action selection, dependency binding, "
                         "evidence-chain and budget behavior; it is not real-model quality evidence and its "
                         "quality numbers must not be presented as production results."),
            })
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps({"output": str(args.output.resolve()), "real_model": False,
                              "paired": report["paired"]}, ensure_ascii=False))
        vector_client.cache_clear()
        get_settings.cache_clear()


if __name__ == "__main__":
    main()
