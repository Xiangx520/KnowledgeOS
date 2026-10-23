"""Reproducible retrieval metrics and opt-in model-judged Ragas evaluation."""
from __future__ import annotations

import copy
import hashlib
import json
import math
import statistics
import time

from app.config import get_settings
from app.services.answering import _read_cache, _write_cache, answer_question, configured_llm, llm_ready
from app.services.embeddings import tokenize
from app.services.retrieval import retrieve


def _retrieval_metrics(cases: list[dict], chunks: list[dict], strategy: str) -> dict:
    rows = []
    start = time.perf_counter()
    for case in cases:
        expected = set(case.get("expected_document_ids") or [])
        hits = case["recorded_citations"] if "recorded_citations" in case else retrieve(case["question"], chunks, top_k=5, strategy=strategy)
        found = [c["document_id"] for c in hits[:5]]
        # Reference IDs define relevance at the document level, not individual chunks.
        rank = next((i + 1 for i, doc in enumerate(found) if doc in expected), None)
        rows.append({"question": case["question"], "expected_document_ids": sorted(expected),
            "retrieved_document_ids": found, "hit_at_5": float(rank is not None) if expected else None,
            "retrieval_basis": "recorded" if "recorded_citations" in case else "live",
            "recall_at_5": len(set(found) & expected) / len(expected) if expected else None,
            "reciprocal_rank": 1 / rank if rank else (0.0 if expected else None)})
    labeled = [r for r in rows if r["hit_at_5"] is not None]
    return {"strategy": strategy, "metrics": {
        "hit_at_5": statistics.mean(r["hit_at_5"] for r in labeled) if labeled else None,
        "recall_at_5": statistics.mean(r["recall_at_5"] for r in labeled) if labeled else None,
        "mrr_at_5": statistics.mean(r["reciprocal_rank"] for r in labeled) if labeled else None,
        "labeled_cases": len(labeled), "total_cases": len(rows), "latency_ms": round((time.perf_counter() - start) * 1000, 2),
    }, "results": rows}


def compare_retrieval(cases: list[dict], chunks: list[dict]) -> dict:
    dense = _retrieval_metrics(cases, chunks, "dense")
    hybrid = _retrieval_metrics(cases, chunks, "hybrid")
    before, after = dense["metrics"]["hit_at_5"], hybrid["metrics"]["hit_at_5"]
    change = (after - before) / before if before is not None and before > 0 and after is not None else None
    return {"dense": dense, "hybrid": hybrid, "relative_hit_rate_change": change,
        "note": "相同授权语料/标注集比较；无标注或基线为零时相对提升不定义。hash 模式不能代表语义模型性能。"}


def evaluate_cases(cases: list[dict], chunks: list[dict], use_ragas: bool = False) -> dict:
    # Fail before retrieval/answer generation: an invalid evaluation must not spend model calls.
    if use_ragas:
        if not llm_ready():
            raise ValueError("Ragas 需要真实的生成模型；请配置 Ollama 或 OpenAI 兼容服务")
        if any(not case.get("reference", "").strip() for case in cases):
            raise ValueError("Ragas context_recall/context_precision 评测要求每个案例包含 reference")
        if get_settings().embedding_provider == "hash":
            raise ValueError("Ragas answer_relevancy 需要语义 embedding，请使用 ollama、sentence-transformers 或 openai")
        try:
            from datasets import Dataset
            from langchain_core.embeddings import Embeddings
            from langchain_core.prompt_values import StringPromptValue
            from ragas import evaluate
            from ragas.embeddings import LangchainEmbeddingsWrapper
            from ragas.llms import LangchainLLMWrapper
            from ragas.metrics import answer_relevancy, context_precision, context_recall, faithfulness
            from ragas.run_config import RunConfig

            from app.services.embeddings import embed
        except ImportError as exc:
            raise ValueError("请先安装可选依赖：pip install -e '.[evaluation]'；Ragas 未安装") from exc
    rows = []
    for case in cases:
        start = time.perf_counter()
        recorded = "recorded_answer" in case
        response = {"answer": case["recorded_answer"], "citations": case["recorded_citations"],
            "provider": "recorded", "cached": True} if recorded else answer_question(case["question"], chunks, history=case.get("history"))
        row = _retrieval_metrics([{**case, "recorded_citations": response["citations"]}], chunks, "hybrid")["results"][0]
        row["retrieval_basis"] = "recorded" if recorded else "live_intent"
        ref_tokens, answer_tokens = set(tokenize(case.get("reference", ""))), set(tokenize(response["answer"]))
        # Explicit lexical overlap, never named answer_relevancy or faithfulness.
        overlap = len(ref_tokens & answer_tokens) / len(ref_tokens) if ref_tokens else None
        rows.append({**row, "reference": case.get("reference", ""), "answer": response["answer"],
            "contexts": [c["text"] for c in response["citations"]], "citations": response["citations"],
            "reference_token_recall": overlap, "provider": response["provider"], "cached": response["cached"],
            "from_history": recorded, "source_message_id": case.get("message_id"),
            "intent": response.get("intent"), "task_results": response.get("task_results", []),
            "latency_ms": round((time.perf_counter() - start) * 1000, 2)})
    labeled = [r for r in rows if r["hit_at_5"] is not None]
    metrics = {"hit_at_5": statistics.mean(r["hit_at_5"] for r in labeled) if labeled else None,
        "recall_at_5": statistics.mean(r["recall_at_5"] for r in labeled) if labeled else None,
        "mrr_at_5": statistics.mean(r["reciprocal_rank"] for r in labeled) if labeled else None,
        "labeled_cases": len(labeled), "total_cases": len(rows),
        "ragas_enabled": use_ragas, "history_replayed_cases": sum(r["from_history"] for r in rows),
        "reference_token_recall": statistics.mean(r["reference_token_recall"] for r in rows if r["reference_token_recall"] is not None)
            if any(r["reference_token_recall"] is not None for r in rows) else None}
    if use_ragas:
        class ConfiguredEmbeddings(Embeddings):
            def embed_documents(self, texts):
                return embed(texts)
            def embed_query(self, text):
                return embed([text])[0]

        s = get_settings()
        pending = []
        for row in rows:
            identity = [row["question"], row["answer"], row["contexts"], row["reference"],
                s.llm_model, s.llm_base_url, s.llm_provider, s.embedding_model, s.embedding_base_url,
                s.embedding_provider, "ragas-0.3.9-four-metrics-json-instance-v2-strictness1"]
            row["grade_cache_key"] = "rag:grade:" + hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()
            saved = _read_cache(row["grade_cache_key"]) if s.cache_ttl > 0 else None
            if saved is not None:
                row["ragas"], row["judge_cached"] = saved, True
            else:
                row["judge_cached"] = False
                pending.append(row)
        names = ("faithfulness", "answer_relevancy", "context_precision", "context_recall")
        if pending:
            data = Dataset.from_list([{"question": r["question"], "answer": r["answer"], "contexts": r["contexts"],
                "ground_truth": r["reference"]} for r in pending])
            judge = configured_llm()
            if s.llm_provider == "ollama":
                judge.format = "json"
            wrapped = LangchainLLMWrapper(judge)
            generate_sync, generate_async = wrapped.generate_text, wrapped.agenerate_text

            def instance_prompt(prompt):
                return StringPromptValue(text="Evaluate the supplied input using the requested rubric. "
                    "Return a JSON data INSTANCE with filled values matching the output schema. "
                    "Do not return JSON Schema, $defs, properties or required declarations. "
                    "根据输入和示例完成评判，输出实际评估数据，不要复制结构定义。\n\n" + prompt.to_string())

            def generate(prompt, *args, **kwargs):
                return generate_sync(instance_prompt(prompt), *args, **kwargs)

            async def agenerate(prompt, *args, **kwargs):
                return await generate_async(instance_prompt(prompt), *args, **kwargs)

            wrapped.generate_text, wrapped.agenerate_text = generate, agenerate
            judges = copy.deepcopy([faithfulness, answer_relevancy, context_precision, context_recall])
            judges[1].strictness = 1
            scores = evaluate(data, metrics=judges,
                llm=wrapped, embeddings=LangchainEmbeddingsWrapper(ConfiguredEmbeddings()),
                run_config=RunConfig(timeout=s.model_timeout, max_workers=1, max_retries=1),
                raise_exceptions=True, show_progress=False)
            frame = scores.to_pandas()
            for index, row in enumerate(pending):
                row["ragas"] = {name: float(frame[name].iloc[index]) if math.isfinite(float(frame[name].iloc[index])) else None for name in names}
                if all(value is not None for value in row["ragas"].values()):
                    _write_cache(row["grade_cache_key"], row["ragas"])
        for name in names:
            values = [r["ragas"][name] for r in rows]
            metrics[name] = statistics.mean(values) if values and all(v is not None for v in values) else None
        metrics["judge_cached_cases"] = sum(r["judge_cached"] for r in rows)
        metrics["judge_scored_cases"] = len(pending)
        for row in rows:
            row.pop("grade_cache_key", None)
    return {"metrics": metrics, "results": rows}
