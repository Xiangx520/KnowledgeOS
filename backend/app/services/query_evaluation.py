"""Paired old/new query-policy evaluation; never grades prose similarity as success."""
from __future__ import annotations

import random
import re
import statistics
import time

from app.config import get_settings
from app.services.embeddings import tokenize
from app.services.intent import IntentState, plan_intent
from app.services.intent_retrieval import retrieve_plan
from app.services.retrieval import rerank_candidates, retrieve


def legacy_retrieve(question, history, chunks, mode="standard", top_k=5):
    """Frozen pre-intent policy, kept only as the evaluation baseline."""
    from app.services.answering import _coverage

    query = question
    if re.search(r"它|这个|那[么个]?|上述|继续|前面|再说|该(?:系统|方案|技术)", question):
        previous = next((m["content"][:1600] for m in reversed(history[-6:]) if m["role"] == "user"), "")
        if previous:
            query = previous[-1000:] + "\n追问：" + question
    limit = min(20, max(top_k * 4, 10))
    candidates = retrieve(query, chunks, top_k=limit, rerank_results=False)
    calls = 1
    seen = {query}
    if mode == "agent":
        for attempt in range(min(get_settings().agent_max_rewrites, 3)):
            if _coverage(query, candidates) >= .65 or not chunks:
                break
            words = [w for w in tokenize(query) if len(w.strip()) > 1]
            rewrite = " ".join(words[attempt::2] or words).strip()
            if not rewrite or rewrite in seen:
                break
            seen.add(rewrite)
            extra = retrieve(rewrite, chunks, top_k=limit, rerank_results=False)
            calls += 1
            scores, by_id = {}, {}
            for ranking in (candidates, extra):
                for rank, item in enumerate(ranking, 1):
                    cid = item["chunk_id"]
                    by_id[cid] = item
                    scores[cid] = scores.get(cid, 0) + 1 / (60 + rank)
            candidates = [by_id[cid] for cid in sorted(scores, key=scores.get, reverse=True)[:limit]]
    candidates = rerank_candidates(query, candidates, top_k)
    candidates = [c for c in candidates if _coverage(query, [c]) >= get_settings().evidence_min_coverage]
    return candidates, calls


def score(case, candidates, action):
    expected = set(case.get("expected_chunk_ids") or case.get("expected_document_ids") or [])
    key = "chunk_id" if case.get("expected_chunk_ids") else "document_id"
    found = [c.get(key, c["id"]) for c in candidates[:5]]
    rank = next((i + 1 for i, item in enumerate(found) if item in expected), None)
    gold_action = case.get("expected_action", "retrieve")
    action_correct = action == gold_action
    recall = len(set(found) & expected) / len(expected) if expected and gold_action == "retrieve" else None
    constraints = case.get("expected_constraints") or []
    violations = [not all(value in c["text"] + " " + c.get("document_name", "") for value in constraints) for c in candidates]
    return {"action_correct": float(action_correct), "recall_at_5": recall,
        "hit_at_5": float(rank is not None) if recall is not None else None,
        "mrr_at_5": (1 / rank if rank else 0.0) if recall is not None else None,
        "constraint_violation_rate": statistics.mean(violations) if violations and constraints else None,
        "retrieval_task_success": float(action_correct and (recall == 1 if expected else True) and not any(violations))
            if gold_action == "clarify" or expected else None,
        "retrieved_ids": found}


def average(rows, key):
    values = [row[key] for row in rows if row.get(key) is not None]
    return statistics.mean(values) if values else None


def compare_queries(cases, chunks, use_model=False, mode="standard"):
    rows = []
    for index, case in enumerate(cases):
        history = case.get("history") or []
        # Sequential, past-only state replay. Assistant-generated facts never seed state.
        state, history_calls = IntentState(), 0
        for turn_index, turn in enumerate(history):
            if turn["role"] == "user":
                prior = plan_intent(turn["content"], state.model_dump(), f"history-{turn_index}", use_model)
                state = prior.state
                history_calls += prior.model_calls
        started = time.perf_counter()
        old, old_calls = legacy_retrieve(case["question"], history, chunks, mode)
        old_row = {**score(case, old, "retrieve"), "latency_ms": (time.perf_counter() - started) * 1000,
                   "retrieval_calls": old_calls, "model_calls": 0}
        started = time.perf_counter()
        plan = plan_intent(case["question"], state.model_dump(), f"case-{index}", use_model)
        new, reports = retrieve_plan(plan, chunks, 5, mode, retrieve, rerank_candidates)
        new_row = {**score(case, new, "retrieve" if plan.tasks else "clarify"),
            "latency_ms": (time.perf_counter() - started) * 1000,
            "retrieval_calls": sum(r["retrieval_calls"] for r in reports),
            "model_calls": plan.model_calls, "history_model_calls": history_calls,
            "intent": plan.model_dump(), "task_results": reports}
        expected_subject = case.get("expected_subject")
        gold_slots = set(case.get("expected_constraints") or [])
        actual_slots = {s.value for t in plan.tasks for s in t.constraints}
        if expected_subject is not None or gold_slots:
            new_row["state_exact_match"] = float((expected_subject is None or
                any(t.subject == expected_subject for t in plan.tasks)) and actual_slots == gold_slots)
        new_row["constraint_recall"] = len(gold_slots & actual_slots) / len(gold_slots) if gold_slots else None
        new_row["constraint_precision"] = len(gold_slots & actual_slots) / len(actual_slots) if actual_slots and gold_slots else None
        expected_count = case.get("expected_task_count")
        new_row["task_count_accuracy"] = float(len(plan.tasks) == expected_count) if expected_count is not None else None
        rows.append({"case": index, "conversation_key": case.get("conversation_key") or str(index),
                     "question": case["question"], "baseline": old_row, "structured": new_row})
    names = ("action_correct", "hit_at_5", "recall_at_5", "mrr_at_5", "retrieval_task_success",
             "constraint_violation_rate", "latency_ms", "retrieval_calls", "model_calls",
             "state_exact_match", "constraint_recall", "constraint_precision", "task_count_accuracy")
    summaries = {}
    for variant in ("baseline", "structured"):
        values = [r[variant] for r in rows]
        latencies = sorted(r["latency_ms"] for r in values)
        summaries[variant] = {name: average(values, name) for name in names}
        summaries[variant]["latency_p95_ms"] = latencies[max(0, int(len(latencies) * .95 + .999) - 1)] if latencies else None
    pairs = [r for r in rows if r["baseline"]["retrieval_task_success"] is not None]
    fixed = sum(r["baseline"]["retrieval_task_success"] == 0 and r["structured"]["retrieval_task_success"] == 1 for r in pairs)
    regressed = sum(r["baseline"]["retrieval_task_success"] == 1 and r["structured"]["retrieval_task_success"] == 0 for r in pairs)
    groups = {}
    for row in pairs:
        groups.setdefault(row["conversation_key"], []).append(row["structured"]["retrieval_task_success"] - row["baseline"]["retrieval_task_success"])
    rng, samples = random.Random(42), []
    keys = list(groups)
    if keys:
        for _ in range(1000):
            sample = [delta for key in rng.choices(keys, k=len(keys)) for delta in groups[key]]
            samples.append(statistics.mean(sample))
        samples.sort()
    return {**summaries, "paired": {"labeled_cases": len(pairs), "fixed": fixed, "regressed": regressed,
        "net_gain": fixed - regressed, "success_delta_ci95": [samples[25], samples[974]] if samples else None},
        "results": rows, "note": "配对检索策略评测；成功仅表示动作正确、标注证据全部命中且没有已标注条件违规，不等于最终答案正确。"
        "Recall/MRR 按前五项计算；多任务也共用此评测窗口。条件检查基于正文/文件名，不是元数据过滤。"
        "历史顺序回放耗费单列，延迟是目标轮延迟。hash 仅适合功能测试；上线结论需要真实语料、语义模型和人工答案评分。"}
