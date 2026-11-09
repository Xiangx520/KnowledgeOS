# Agentic RAG 实施说明

日期：2026-11-08  
配套计划：[agentic-rag-migration-plan.md](agentic-rag-migration-plan.md)  
状态：P0—P4 已实现并通过离线验收；P5 已完成离线控制流评测入口与一次真实模型冒烟，真实模型配对质量评测未完成。

## 1. 实现了什么

`mode=agent` 在 `RAG_AGENT_LOOP_ENABLED=true` 且模型可用时走新的有界单 Agent 循环；否则保持原有有界流程。`mode=standard` 不进入新循环，现有响应字段、缓存、SSE 事件和前端展示保持兼容。

新循环每一轮由结构化策略选择一个动作：

- `search`：在当前授权、活动版本的 `CorpusScope` 内检索；可附带 `derive_task`，用已取得证据中的原文实体/引用创建依赖子任务。
- `read_context`：读取已授权证据的原文，可按同页 ±1 邻块扩展，并保存完整 `context_chunk_ids`。
- `lookup_reference`：根据证据中的文档名/条款引用，在当前 scope 中查找关联文档；多个文档接近时返回 `ambiguous`。
- `answer`：进入共享生成、核验、修订和安全摘录路径。
- `clarify` / `abstain`：只在关键条件歧义或知识库缺资料时使用。

状态机根据实际观察选择下一步，而不是固定重试：第二跳的查询和绑定来自第一跳证据；核验发现 `missing_evidence` 时可创建有界补检索子任务；重复动作、无进展、预算、时限、核验器异常和授权/版本变化都有明确停止路径。

## 2. 模块

| 文件 | 职责 |
| --- | --- |
| `backend/app/services/agent_state.py` | 冻结目标、任务 DAG、证据账本、绑定、缺口、动作判别联合、预算与动作校验 |
| `backend/app/services/agent_tools.py` | 受限只读工具；`CorpusScope` 注入、参数/输出边界、邻块溯源、引用查找 |
| `backend/app/services/agent_policy.py` | 结构化模型决策、JSON 解析、ID/约束校验、一次受预算约束的纠正 |
| `backend/app/services/agent_runner.py` | 有界状态机、预算预留/消费、观察更新、停止原因、补证据、共享生成/核验调用 |
| `backend/app/services/agent_evaluation.py` | 证据链、动作轨迹、停止原因、预算与延迟评分 |
| `backend/app/services/answering.py` | 抽取共享生成/核验/最终化函数；agent 安全摘录 |
| `backend/app/services/verification.py` | 结构化缺口、数字/引用检查、语义失败关闭 |
| `backend/app/services/chat.py` | 入口集成、有效配置、权限/版本探针、幂等、邻块最终复验、元数据持久化 |
| `frontend/src/api.ts` / `App.vue` | `agent` 摘要、来源/轨迹展示；done 最终答案替换草稿 |

## 3. 工具与证据约束

- 模型只能提供查询、任务 ID、证据 ID、策略和受限窗口；不能指定用户、知识库范围、路径、URL 或 SQL。执行器注入 `CorpusScope`。
- 任务 ID 和证据 ID 必须存在于当前执行状态；未知 ID、未知字段和越界参数被拒绝。
- `derive_task` 的每个绑定必须包含 `evidence_id`、`value`、`excerpt`；`value` 和 `excerpt` 必须是该证据原文的连续子串，查询必须包含已验证的绑定值。
- 任务 DAG 检查重复 ID、缺失依赖和环；动态任务受 `AGENT_MAX_DYNAMIC_TASKS` 限制。
- 证据账本保存 `chunk_id`、`document_id`、`revision_id`、知识库、页码、定位、任务和步骤；邻块合并保存完整 `context_chunk_ids` 与定位。最终发布前对引用块和所有邻块再次执行授权/活动版本复验。
- 文档内容是不受信数据；策略提示明确忽略其中的指令，且工具白名单、授权、预算和用户条件不能由文档改变。

## 4. 预算与停止条件

| 配置 | 默认 | 说明 |
| --- | ---: | --- |
| `RAG_AGENT_LOOP_ENABLED` | false | 关闭时 agent 走原有有界流程 |
| `RAG_AGENT_MAX_STEPS` | 6 | 已接受并调度的动作，含 answer |
| `RAG_AGENT_MAX_TOOL_CALLS` | 8 | 所有读工具调用 |
| `RAG_AGENT_MAX_MODEL_CALLS` | 12 | 意图、决策、生成、核验、修订及失败尝试 |
| `RAG_AGENT_MAX_TOTAL_TOKENS` | 24000 | 输入+输出；缺 usage 时保守估算并标记 `estimated_tokens` |
| `RAG_AGENT_DEADLINE_SECONDS` | 120 | 总时限；单次模型调用不超过剩余时限 |
| `RAG_AGENT_MAX_EVIDENCE_CHUNKS` | 24 | 证据账本片段上限 |
| `RAG_AGENT_MAX_DYNAMIC_TASKS` | 8 | 初始任务外的子任务上限 |
| `RAG_AGENT_MAX_RECOVERY_ROUNDS` | 1 | 核验缺口补检索轮次 |
| `RAG_AGENT_MIN_SEARCH_COVERAGE` | 0.25 | agent 搜索候选进入账本的最低词项覆盖 |

每次模型调用和工具调用前预留预算；失败尝试也计数；生成和核验保留模型调用/ token 份额。停止原因包括 `completed`、`insufficient_evidence`、`conflict_unresolved`、`budget_exhausted`、`deadline_exceeded`、`no_progress`、`policy_error`、`verifier_unavailable`、`abstained`、`clarification`。撤权返回 401/403，版本变化返回 409，不通过降级路径绕过。

## 5. 核验与降级

- `RAG_CITATION_VERIFIER=llm` 时，核验器可返回结构化 `gaps`；`missing_evidence` 且带补检索 query 的缺口会创建有界子任务。
- 无关内容删除；数字错误先核对原文；`verifier_unavailable` 失败关闭，不当作普通缺口反复搜索。
- agent 模式发布综合事实答案要求语义核验成功。使用 `numeric` 或核验不可用时，agent 返回明确标注的原文摘录，`agent.degraded_reason=semantic_verifier_unavailable`，不宣称语义核验通过。
- standard 的既有 numeric 默认行为未改变。

## 6. 兼容与运行保障

- `ChatRequest` 的 `standard|agent` 枚举、旧响应字段、`trace/sources/draft/done/error` SSE 事件保留。
- 新增版本化 `agent` 摘要：`effective_mode`、`status`、`stop_reason`、`degraded_reason`、`steps`、`budget`、`task_summary`、`recovery_rounds`、`semantic_checked`、`gaps`、`bindings`。
- `effective_settings` 显式传入新执行链；深层生成/核验使用同一份配置。每次模型尝试（含失败和策略纠正）通过 `consume` 计入用户每日模型请求预算；请求入口原有的 1 次请求计量保留，因此一次 agent 请求的配额消耗为 1 加实际模型尝试数。配额不足时有界结束，不发起超额调用。
- 会话租约、请求 run TTL、并发 capacity TTL 和心跳覆盖 agent 总时限；`ttl = max(model_timeout*10+180, agent_deadline+300)`。
- 新 agent 循环不读写答案缓存；相同 `request_id` 的幂等重放仍检查引用授权。standard 缓存行为不变。
- trace 只记录动作、查询、结果 ID、预算、错误类型和停止原因；默认不保存模型隐式思维链、密钥或无限量原文。

## 7. 实际验证

执行日期：2026-11-08。命令从 `rag-knowledge-assistant` 根目录执行。

```powershell
python -m ruff check backend scripts
Push-Location backend; python -m pytest -q; Pop-Location
Push-Location frontend; npm run build; Pop-Location
python scripts/benchmark-agentic-rag.py --output docs/artifacts/agentic-rag-offline-2026-11-08.json
python scripts/smoke-agentic-rag.py --output docs/artifacts/agentic-rag-real-smoke-2026-11-08.json
```

实际结果：

- `ruff`：All checks passed。
- `pytest`：155 passed（包含新增 agent 状态、工具、循环、核验补证据、冲突、预算、撤权/版本、SSE、幂等和评测评分测试）。
- 前端 `npm run build`：`vue-tsc --noEmit && vite build` 成功。
- 离线 benchmark（7 个合成 fixture 场景，hash embedding，无模型，注入脚本策略）：旧 agent 证据链成功率 0.600，新循环 1.000；2 个多跳用例改善、0 个回退；预算遵守率 1.0；旧 agent p50 147.31 ms / p95 257.02 ms，新循环 p50 202.96 ms / p95 354.75 ms；新循环平均工具调用 1.43、模型调用 0、token 0。离线 fixture 延迟受本地 Qdrant/Jieba 初始化影响，不作为生产性能承诺。结果文件：[artifacts/agentic-rag-offline-2026-11-08.json](artifacts/agentic-rag-offline-2026-11-08.json)。
- 真实模型冒烟（`qwen3.8-flash`，OpenAI 兼容 provider，hash embedding，reranker 关闭，`citation_verifier=llm`）：
  - 两跳 fixture：`completed`，语义核验通过，4 次模型调用，8290 token，59.3 s；答案同时引用审批制度和授权表，并给出“八十万元”。
  - 无证据 fixture：两次搜索后 `abstained`，2 次模型调用，5598 token，13.1 s，无引用。
  - 结果文件：[artifacts/agentic-rag-real-smoke-2026-11-08.json](artifacts/agentic-rag-real-smoke-2026-11-08.json)。

注意：真实模型冒烟中第一次检索同时返回了两份文档，因此该次运行没有强制产生第二跳；第二跳依赖由注入策略的离线测试验证。真实模型的完整配对质量评测（标准/旧 agent/新 agent、固定验收集、三次重复、逐事实人工抽检）未执行，不能把上述离线数字当作生产质量结论。

## 8. 当前限制

- 只有单 agent；没有多 agent、公网搜索、外部写操作或任意代码/SQL 工具。
- 无模型时 agent 显式回退旧流程，并记录 `effective_mode=legacy_degraded`。
- 语义核验未配置/不可用时，agent 只能安全摘录或拒答；不会声称综合事实答案已核验。
- 年份/部门仍是正文/文件名文本约束，不是可靠的结构化元数据过滤；上传时间不当作发布日期。
- `lookup_reference` 使用文档名/引用文本和接近分数判断歧义；邻块只扩展到同页 ±1。
- 核验缺口补检索只处理带 query 的 `missing_evidence`；无 query 的缺口进入安全结束。
- 证据账本和输出有字符/token 上限；超长原文会截断并标记。
- 离线 benchmark 使用合成 fixture 和 hash embedding，不能代表真实语料召回质量。
- 真实模型配对评测、成本和延迟分布、逐事实支持率尚未完成。

## 9. 下一步

1. 在固定验收集上运行 standard / 旧 agent / 新 agent 的真实模型配对评测，至少三次重复，记录模型、参数、成本、p50/p95 和逐事实抽检结果。
2. 根据真实失败案例校准 `RAG_AGENT_MIN_SEARCH_COVERAGE`、步骤/工具/模型预算和核验缺口策略。
3. 增加多跳/比较的独立验收集和人工标注；不要把开发集调参结果当作验收集结论。
4. 评估是否需要对 agent 答案缓存做独立键设计；当前首期保持绕开缓存。
