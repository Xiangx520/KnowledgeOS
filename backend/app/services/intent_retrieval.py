"""Execute independent intent tasks without relaxing user constraints."""
from app.services.evidence import coverage


def constraint_match(task, candidate):
    text = candidate["text"] + " " + candidate.get("document_name", "")
    # No publication/department metadata exists yet. These are evidence checks,
    # never a claim that upload timestamps implement publication-date filtering.
    return all((slot.value not in text) if slot.operator == "exclude" else (slot.value in text) for slot in task.constraints)


def retrieve_plan(plan, chunks, top_k, mode, retrieve_fn, rerank_fn, cancelled=None, emit=None):
    from app.config import get_settings

    selected, reports = {}, []
    limit = min(20, max(top_k * 4, 10))
    for index, task in enumerate(plan.tasks):
        if cancelled and cancelled():
            raise ValueError("请求已取消")
        candidates = retrieve_fn(task.expression, chunks, top_k=limit, rerank_results=False)
        candidates = [c for c in candidates if constraint_match(task, c)]
        calls = 1
        # Retry the full independent question once, keeping every constraint.
        if (mode == "agent" and get_settings().agent_max_rewrites > 0
                and task.expression != task.question
                and (not candidates or coverage(task.question, " ".join(c["text"] for c in candidates)) < .65)):
            if cancelled and cancelled():
                raise ValueError("请求已取消")
            extra = retrieve_fn(task.question, chunks, top_k=limit, rerank_results=False)
            by_id = {c["id"]: c for c in candidates}
            by_id.update({c["id"]: c for c in extra if constraint_match(task, c)})
            candidates = list(by_id.values())
            calls += 1
        ranked = rerank_fn(task.question, candidates, top_k)
        ranked = [c for c in ranked if coverage(task.question, c["text"]) >= get_settings().evidence_min_coverage]
        report = {"task": index, "question": task.question, "expression": task.expression,
                  "constraints": [s.model_dump() for s in task.constraints], "retrieval_calls": calls,
                  "status": "evidence_found" if ranked else "insufficient_evidence",
                  "chunk_ids": [c["id"] for c in ranked]}
        reports.append(report)
        if emit:
            emit("trace", {"step": "intent_retrieve", "detail": report})
        for candidate in ranked:
            item = selected.setdefault(candidate["id"], {**candidate, "intent_tasks": []})
            item["intent_tasks"].append(index)
    # Every task gets its own top-k allocation. Never let one task crowd out another.
    return list(selected.values()), reports
