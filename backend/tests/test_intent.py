import json
from types import SimpleNamespace

import pytest
from app.services import answering
from app.services.intent import IntentPlan, IntentState, plan_intent, rule_plan, validate_plan
from app.services.intent_retrieval import retrieve_plan
from app.services.query_evaluation import compare_queries


@pytest.fixture(autouse=True)
def no_model(monkeypatch):
    monkeypatch.setattr(answering, "llm_ready", lambda: False)


def test_far_antecedent_and_constraints_are_state_not_message_window():
    plan = rule_plan("财务部2024年住宿报销上限是多少", IntentState(), "m1")
    assert plan.state.topics[0].subject == "住宿报销上限"
    for index in range(12):
        plan = rule_plan("继续说明", plan.state, f"m{index + 2}")
    followup = rule_plan("它的额度是多少", plan.state, "current")
    task = followup.tasks[0]
    assert task.subject == "住宿报销上限"
    assert "2024年" in task.expression and "财务部" in task.expression
    assert "它" not in task.expression
    assert followup.state.topics[0].origin.message_id == "m1"


def test_replace_remove_conditions_and_topic_switch():
    first = rule_plan("财务部2024年住宿报销上限是多少", IntentState(), "m1")
    changed = rule_plan("改成2023年的", first.state, "m2")
    assert "2024" not in changed.tasks[0].question and "2023年" in changed.tasks[0].question
    removed = rule_plan("不限部门了", changed.state, "m3")
    assert all(s.kind != "department" for s in removed.tasks[0].constraints)
    switched = rule_plan("什么是Redis", removed.state, "m4")
    assert switched.tasks[0].subject == "Redis" and switched.tasks[0].constraints == []
    assert rule_plan("它的作用呢", switched.state, "m5").clarification


def test_missing_reference_relative_year_and_empty_state_clarify_without_retrieval(monkeypatch):
    monkeypatch.setattr(answering, "retrieve", lambda *a, **k: pytest.fail("clarification searched"))
    result = answering.answer_question("它的额度是多少", [])
    assert result["needs_clarification"] and not result["citations"]
    assert result["provider"] == "clarification"
    assert rule_plan("查去年住宿标准", IntentState(), "m").clarification


def test_multiple_requests_keep_independent_subjects_and_constraints():
    plan = rule_plan("2024年住宿报销上限是多少；2023年交通报销上限是多少", IntentState(), "m1")
    assert len(plan.tasks) == 2
    assert plan.tasks[0].subject == "住宿报销上限"
    assert plan.tasks[1].subject == "交通报销上限"
    assert [s.value for s in plan.tasks[1].constraints] == ["2023年"]
    assert "2024年" not in plan.tasks[1].question


def test_model_structured_plan_removes_chatter_and_preserves_conditions(monkeypatch):
    question = "昨天和同事聊了很多事情，天气也不错。请问财务部2024年住宿报销上限是多少？" + "无关背景。" * 30
    baseline = rule_plan("财务部2024年住宿报销上限是多少", IntentState(), "current")
    plan = baseline.model_dump()
    plan["tasks"][0]["request"]["quote"] = "住宿报销上限是多少"
    plan["tasks"][0]["question"] = "财务部2024年住宿报销上限是多少？"
    plan["tasks"][0]["expression"] = "财务部 2024年 住宿报销上限"
    monkeypatch.setattr(answering, "llm_ready", lambda: True)
    monkeypatch.setattr(answering, "configured_llm", lambda: SimpleNamespace(
        invoke=lambda _: SimpleNamespace(content=json.dumps(plan, ensure_ascii=False))))
    result = plan_intent(question)
    assert result.planner == "model" and result.model_calls == 1
    assert "天气" not in result.tasks[0].expression
    assert "财务部" in result.complete_question and "2024年" in result.complete_question


@pytest.mark.parametrize("change", ["invent", "drop", "origin", "number"])
def test_invalid_model_proposals_are_rejected(change):
    raw = "财务部2024年住宿报销上限是多少"
    plan = rule_plan(raw, IntentState(), "current")
    if change == "invent":
        plan.state.topics[0].subject = "秘密项目"
    elif change == "drop":
        plan.tasks[0].expression = "住宿报销上限"
    elif change == "origin":
        plan.tasks[0].request.message_id = "another-conversation"
    else:
        plan.tasks[0].expression += " 9999元"
    with pytest.raises(ValueError):
        validate_plan(plan, raw, IntentState(), "current")


def test_invalid_model_json_falls_back_without_mutating_previous(monkeypatch):
    state = rule_plan("什么是Redis", IntentState(), "first").state.model_dump()
    monkeypatch.setattr(answering, "llm_ready", lambda: True)
    monkeypatch.setattr(answering, "configured_llm", lambda: SimpleNamespace(
        invoke=lambda _: SimpleNamespace(content="not JSON")))
    result = plan_intent("它的用途是什么", state)
    assert result.planner == "fallback" and result.model_calls == 1
    assert result.tasks[0].subject == "Redis"
    assert state["topics"][0]["origin"]["message_id"] == "first"


def test_task_budgets_conditions_and_retry_are_bounded(monkeypatch):
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "agent_max_rewrites", 2)
    plan = rule_plan("2024年住宿上限是多少；2023年交通上限是多少", IntentState(), "m")
    for task in plan.tasks:
        task.expression = task.subject + " " + task.constraints[0].value
    calls = []
    def search(query, *args, **kwargs):
        calls.append(query)
        return [{"id": "bad", "chunk_id": "bad", "text": "2022年其他规定", "document_name": "wrong.txt"}]
    selected, reports = retrieve_plan(plan, [], 5, "agent", search, lambda q, c, k: c)
    assert not selected and len(calls) == 4
    assert all(r["status"] == "insufficient_evidence" and r["retrieval_calls"] == 2 for r in reports)
    assert all("2024年" in q for q in calls[:2]) and all("2023年" in q for q in calls[2:])


def test_state_is_saved_atomically_and_old_conversations_bootstrap(client):
    from app.models import Conversation, Message, User
    from sqlalchemy import select
    from test_api import register

    owner = register(client)
    with client.app.state.database.session() as db:
        user = db.scalar(select(User).where(User.email == "owner@example.com"))
        conversation = Conversation(user_id=user.id, title="old conversation")
        db.add(conversation)
        db.flush()
        cid = conversation.id
        db.add(Message(conversation_id=cid, role="user", content="什么是Redis"))
        db.commit()
    for _ in range(4):
        response = client.post("/api/chat", headers=owner, json={"question": "继续说明", "conversation_id": cid})
        assert response.status_code == 200, response.text
    body = {"question": "它的用途是什么", "conversation_id": cid, "request_id": "intent-idempotent-1"}
    result = client.post("/api/chat", headers=owner, json=body).json()
    assert result["intent"]["tasks"][0]["subject"] == "Redis"
    assert client.post("/api/chat", headers=owner, json=body).json()["idempotent_replay"]
    detail = client.get("/api/conversations/" + cid, headers=owner).json()
    users = [m for m in detail["messages"] if m["role"] == "user"]
    assert users[-1]["run_metadata"]["intent_state"]["topics"][0]["subject"] == "Redis"
    assert detail["messages"][-1]["run_metadata"]["intent"]["tasks"][0]["subject"] == "Redis"
    stranger = register(client, "other@example.com")
    assert client.post("/api/chat", headers=stranger, json={"question": "它是什么", "conversation_id": cid}).status_code == 404


def test_paired_comparison_reports_gain_and_clarification_without_label_inflation(monkeypatch):
    from app.services import query_evaluation
    evidence = {"id": "c1", "chunk_id": "c1", "document_id": "d1", "document_name": "规则",
                "text": "住宿报销上限为500元", "score": .1}
    monkeypatch.setattr(query_evaluation, "retrieve", lambda q, *a, **k: [evidence] if "住宿报销" in q else [])
    monkeypatch.setattr(query_evaluation, "rerank_candidates", lambda q, c, k: c)
    history = [{"role": "user", "content": "住宿报销上限是多少"}] + [{"role": "user", "content": "继续说明"}] * 8
    result = compare_queries([
        {"question": "它的额度是多少", "history": history, "expected_document_ids": ["d1"], "expected_subject": "住宿报销上限"},
        {"question": "它是什么", "expected_action": "clarify"},
        {"question": "未标注的问题"}], [evidence])
    assert result["paired"]["labeled_cases"] == 2
    assert result["paired"]["fixed"] == 2 and result["paired"]["regressed"] == 0
    assert result["structured"]["state_exact_match"] == 1
    assert result["results"][1]["structured"]["recall_at_5"] is None
    assert result["results"][2]["structured"]["retrieval_task_success"] is None


def test_comparison_api_preserves_old_mode_and_validates_evidence_scope(client):
    from test_api import register
    owner = register(client)
    response = client.post("/api/evaluations/compare", headers=owner, json={
        "kind": "query_policy", "cases": [{"question": "它是什么", "expected_action": "clarify"}]})
    assert response.status_code == 200, response.text
    assert response.json()["paired"]["fixed"] == 1
    forbidden = client.post("/api/evaluations/compare", headers=owner, json={
        "kind": "query_policy", "cases": [{"question": "q", "expected_chunk_ids": ["foreign-chunk"]}]})
    assert forbidden.status_code == 422


def test_plan_schema_is_strict():
    with pytest.raises(ValueError):
        IntentPlan.model_validate({"state": {}, "sql": "select *"})


def test_exclusion_is_not_converted_to_an_inclusion():
    from app.services.intent_retrieval import constraint_match
    plan = rule_plan("查询住宿标准，不包括2023年", IntentState(), "m")
    task = plan.tasks[0]
    assert [(s.value, s.operator) for s in task.constraints] == [("2023年", "exclude")]
    assert "排除：2023年" in task.expression
    assert not constraint_match(task, {"text": "2023年住宿标准", "document_name": "规定"})
    assert constraint_match(task, {"text": "2024年住宿标准", "document_name": "规定"})


def test_cache_preserves_current_message_provenance(monkeypatch):
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "cache_ttl", 60)
    first = answering.answer_question("什么是Redis", [], message_id="conversation-a")
    second = answering.answer_question("什么是Redis", [], message_id="conversation-b")
    assert first["intent"]["state"]["topics"][0]["origin"]["message_id"] == "conversation-a"
    assert second["intent"]["state"]["topics"][0]["origin"]["message_id"] == "conversation-b"


def test_missing_subtask_cannot_borrow_another_tasks_evidence(monkeypatch):
    candidate = {"id": "c", "chunk_id": "c", "document_id": "d", "document_name": "2024年住宿规定",
        "knowledge_base_id": "k", "text": "2024年住宿报销上限为500元。", "score": .1}
    monkeypatch.setattr(answering, "retrieve", lambda q, *a, **k: [candidate] if "2024" in q else [])
    monkeypatch.setattr(answering, "rerank_candidates", lambda q, c, k: c)
    result = answering.answer_question("2024年住宿报销上限是多少；2023年住宿报销上限是多少", [])
    assert result["task_results"][1]["status"] == "insufficient_evidence"
    second = result["answer"].split("2023年住宿报销上限是多少", 1)[1]
    assert "500" not in second and answering.INSUFFICIENT_EVIDENCE in second


def test_long_request_never_silently_truncates_constraints():
    raw = "住宿报销上限是多少？" + "无关背景。" * 400 + "仅限国内出差"
    plan = rule_plan(raw, IntentState(), "m")
    assert not plan.tasks and plan.clarification
