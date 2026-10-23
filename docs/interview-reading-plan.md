## 先建立正确的项目定位

这是一个带有有界检索编排的 RAG 知识助手。普通模式执行混合召回和精排；Agent 模式在此基础上检查词项覆盖，在不足时进行确定性的关键词改写和再次检索，合并候选后统一精排。

当前实现的查询改写不额外调用 LLM，也没有通用工具选择、开放式自主规划或多 Agent 协作。`trace` 是程序记录的执行步骤，不是模型的思维过程。词项覆盖率是启发式指标，不代表答案正确率。

## 第 1 天：建立地图，跑通最小闭环

阅读顺序：

1. `README.md`：项目简介、架构、快速开始、模型接入。
2. `docs/architecture.md`：组件与数据流。
3. `backend/app/main.py`：`create_app`，理解服务如何组装。
4. `backend/app/config.py`：关注数据目录、模型、分块、队列、检索配置。
5. `backend/app/models.py`：先只看 KnowledgeBase、Document、DocumentRevision、Chunk、IngestionJob、Conversation、Message。

当天补概念：HTTP 请求/响应、JSON、Python 模块与函数、数据库表与主外键、Embedding、RAG 的入库与查询两阶段。

操作：按 README 启动开发环境，使用 `examples/company-handbook.md` 完成建立知识库 → 上传 → 查看分块 → 提问 → 查看来源。初次运行可使用 `.env.example` 的 hash embedding / 原文摘录路径；它只用于理解流程，不能作为真实语义模型效果证明。单独记录目前有没有启用真实生成、语义向量和重排模型。

环境排障最多先投入 60 分钟；若仍未跑通，先读 `backend/tests/test_api.py` 中 `test_upload_ingestion_deduplication_scope_and_cleanup`，用请求和断言理解闭环，记录阻塞后继续学习。

- [ ] 产出一张架构草图，标注 API、Worker、SQL、Qdrant、Redis 各自保存或处理什么。
- [ ] 能回答：为什么有 SQL 还要 Qdrant？Redis 在这里承担什么职责？
- [ ] 能区分：文档、版本、分块、向量、引用。

## 第 2 天：追踪一份文档如何变成知识

阅读顺序：

1. `backend/app/api/routes.py`：`upload_document`，先忽略无关接口。
2. `backend/app/worker.py`：`notify_job` → `_claim` → `process_job`。
3. `backend/app/services/parsing.py`：`parse_document` → `chunk_pages`。
4. `backend/app/services/embeddings.py`：`tokenize`、`embed`。
5. `backend/app/services/retrieval.py`：`index_chunks`。

当天补概念：列表/字典、循环、异常、事务、异步任务；遇到 SQLAlchemy 时先读懂查询条件与提交位置。

重点：默认 800 字符分块、120 字符重叠，不能当成 800/120 token；来源页码如何保留；SHA256 去重；上传返回与索引完成为什么分开；SQL 保存任务状态、Redis 负责通知的意义。

实验：重复上传同一个文件，观察是否去重；查看相邻分块的重叠和页码。通过现有测试理解失败重试，不必直接破坏运行环境。

- [ ] 产出“上传 → 任务 → 解析 → 切块 → 向量 → SQL/Qdrant”的调用图。
- [ ] 写清一份 chunk 的主要字段、每个字段在哪里产生。
- [ ] 能回答：分块过大或过小会影响什么？Worker 中途退出后任务怎么办？

## 第 3 天：理解 Dense、BM25、RRF、精排

阅读顺序：

1. `backend/app/services/retrieval.py`：`retrieve`，先掌握整体阶段。
2. `backend/app/services/corpus.py`：`CorpusScope`、`sparse`、`hydrate`、`dense_filter`。
3. 回到 `retrieval.py`：`rerank_candidates`、`rerank_scores`；最后再看 `_small_corpus_sparse`。

当天补概念：向量相似度、词频/逆文档频率、召回和精排。先能用例子解释，不必推导完整 BM25。

手算：Dense 排名 A、B、C；BM25 排名 B、D、A。按 `score(d) = Σ 1 / (60 + rank(d))` 计算融合结果，其中排名从 1 开始。解释为什么代码使用排名融合，而不是直接相加两路原始分数。

实验：用同一份语料准备 3 个问题，分别覆盖同义表达、精确术语、具体数值；比较 dense / bm25 / hybrid 的前几个结果。有真实语义模型时再分析语义召回优势；hash 模式只用于观察实现。

- [ ] 产出一张“方法、适用问题、局限、调用位置”对照表。
- [ ] 能解释 candidate 数量与最终 top_k 的区别，以及精排为何放在召回之后。
- [ ] 能说明生产路径的持久 SQL BM25 与小语料列表路径不同，避免只读演示分支。

## 第 4 天：读懂项目最关键的 Agent 循环

阅读顺序：

1. `backend/app/api/routes.py`：`perform_chat`，确认请求入口。
2. `backend/app/services/chat.py`：`run_chat`，第一遍只抓取语料范围、历史和调用问答服务的位置。
3. `backend/app/services/answering.py`：`answer_question`，重点看 `mode == "agent"` 分支。
4. `backend/app/services/answering.py` 的 `_coverage`，以及 `backend/app/services/evidence.py` 的 `coverage`、`terms`。
5. `backend/tests/test_services.py`：`test_agent_reranks_once_after_bounded_rewrites`。

逐行回答：状态有哪些？何时再次检索？改写如何生成？候选如何去重融合？循环为什么终止？模型在哪一步调用？

当前代码检查点：覆盖达到 0.65 时停止改写；改写次数受配置限制，默认 2 次，循环另有最多 3 次的限制；空语料、空查询或重复查询也会结束；多轮候选按 chunk ID 融合，最终统一精排一次。这些都是实现选择，不是通用最优参数。

实验：观察一个覆盖充分的问题和一个混合相关/无关词的问题的 trace；后者是否触发改写要以实际分词、证据和阈值为准。如果覆盖充分，没有 rewrite 也是合理结果。

- [ ] 脱离代码写出 20 行以内伪代码。
- [ ] 能说明普通模式与 Agent 模式的具体差别。
- [ ] 能回答：这个实现的决策依据是什么？与 LLM 规划型 Agent 相比，能力与成本各有什么取舍？

## 第 5 天：答案为什么可信，流式输出如何完成

阅读顺序：

1. `backend/app/services/answering.py`：`_budget_context`、`_extract`，以及 `answer_question` 后半段。
2. `backend/app/services/verification.py`：`verify_answer`、引用处理。
3. `backend/app/api/routes.py`：`stream_chat`。
4. `backend/tests/test_evidence_quality.py`：挑引用、无关答案、数字和多轮主题相关的 2–3 个测试。
5. `backend/tests/test_enterprise.py`：`test_true_provider_stream_and_verified_final_answer`。

重点：历史如何用于指代消解；证据如何装入上下文；预算为何预留输出空间；当前字节预算与精确 tokenizer 计数的区别；引用存在不等于事实有支持；证据不足拒答；不支持的生成内容为何回退摘录；流式草稿与核验后最终结果的区别。

实验：问一个文档没有答案的问题，再问一个需要具体数值的问题，核对引用正文与答案。记录一次错误可能来自召回、证据筛选、生成或核验中的哪一层。

- [ ] 产出“错误类型 → 代码防线 → 剩余局限”表。
- [ ] 能回答：有引用为什么还会幻觉？系统能保证完全不幻觉吗？
- [ ] 能解释为什么客户端应以完成事件中的结果作为最终答案。

## 第 6 天：评测与一次自己的对比实验

阅读顺序：

1. `backend/app/services/evaluation.py`：`_retrieval_metrics` → `compare_retrieval` → `evaluate_cases`。
2. `examples/evaluation-cases.json`、`examples/benchmarks/business-v1.json`。
3. `docs/validation.md`：注意不同日期、不同环境和不同样本规模。
4. `backend/tests/test_services.py`：标注评测、历史答案复评、上下文预算与缓存测试。

先掌握 Hit@5、Recall@5、MRR@5，并手算一条案例；再了解 Ragas 四指标分别关注事实支持、答案相关性、上下文精确率与召回率。当前检索指标以文档 ID 判断相关，前五个槽位按 chunk 排序，同文档多个 chunk 会占用多个槽位。

实验：准备 10 个有 expected_document_ids 的问题，尽量包含精确术语、同义改写和跨文档问题；另准备 2 个无答案问题单独检查拒答。固定语料、模型、top_k 和问题集，优先做 dense 对 hybrid 的对照。记录检索指标、实验环境、耗时范围和至少 2 个失败案例；不必同时跑完所有模型组合。

如果当天无法连接真实模型，完成算法和指标计算实验，并明确结果属于演示配置。Ragas 可放到后续，不要为安装和模型下载耗尽学习时间。

- [ ] 产出一页自己的实验记录，包括设置、结果、失败分析和局限。
- [ ] 能回答：召回命中但答案错误，该排查哪里？为什么小样本满分不能代表真实业务准确率？
- [ ] 能区分历史答案复评与重新生成答案后评测。

## 第 7 天：工程取舍、简历对齐、模拟面试

前 90 分钟：

1. `corpus.py`、`services/access.py`：权限前过滤、SQL 候选验证、活动版本。
2. `worker.py`：租约、心跳、失效后恢复；区分“重新执行任务”和“执行结果可安全提交”。
3. `answering.py`：`_key`、`_read_cache`、`_write_cache`；语料与授权范围如何影响缓存。
4. `chat.py`：请求幂等和会话并发保护；结合 `test_concurrency.py` 或 `test_enterprise.py` 中一个测试阅读。

后 90–150 分钟：

- [ ] 写一版 90 秒项目介绍：解决什么问题 → 两条主链路 → 你掌握或完成的工作 → 一条实测结果 → 已知局限。
- [ ] 录一次 5 分钟架构讲解，检查是否在堆技术名词。
- [ ] 对照简历逐条建立“描述 → 代码 → 测试/实验 → 我的实际贡献”证据表。
- [ ] 选一个失败问题，提出具体改进与验证方法；有时间再实现一处小改动，并运行相关现有测试。

模拟追问：

1. 请从用户提问开始，讲到最终答案保存，中间经过哪些函数？
2. 为什么同时使用 BM25 和向量检索？什么情况下混合检索可能没有收益？
3. RRF 与 reranker 分别解决什么问题？
4. Agent 的判断、行动、状态与终止条件分别在哪里？
5. 关键词覆盖高但答案仍不相关，如何定位？
6. 为什么只在最后统一精排？
7. 文档更新或权限撤销后，缓存与旧向量会发生什么？
8. Redis 通知丢失或 Worker 崩溃后，任务如何被再次发现？
9. SSE 已经发出了草稿，最终核验失败时怎么办？
10. 你实际测过哪些指标，样本和限制是什么？

每题先用 30–60 秒回答；被追问时能打开具体代码，并指出测试或实验依据，才算通过。

## 阅读方法与优先级

不要从 `routes.py` 第一行一路读到底。每次只追一条业务路径，每个函数记五项：输入、输出、修改的状态、依赖、失败行为。先读主流程，再读辅助函数，最后读测试断言；测试中的 stub / monkeypatch 用于隔离依赖，不等同于真实模型效果验证。

本周必读：parsing、embeddings、retrieval、corpus、answering、evaluation，以及 routes / chat / worker 的关键路径。

本周了解：权限过滤、缓存、任务租约、请求幂等、版本切换。

后续再读：前端样式、OIDC 协议细节、完整组织治理、云部署与监控配置。面试若问到未深入部分，说明已经理解的边界与后续阅读方向。

入门阶段优先补：函数、列表/字典、类与方法、异常、模块导入、上下文管理器；再补 FastAPI 参数注入、SQLAlchemy 查询与事务。每项都联系当天代码，不需要先学完一整套课程。

## 可选的定向验证命令

以下为学习时建议自行执行的现有测试，本计划制定过程中没有执行这些测试。先按照 README 准备依赖，在 `backend` 目录运行：

```powershell
python -m pytest tests/test_services.py -q
python -m pytest tests/test_services.py -k agent_reranks_once -q
python -m pytest tests/test_evidence_quality.py -q
python -m pytest tests/test_kb_acl.py tests/test_concurrency.py -q
```

真实模型、重排服务和 Ragas 的实测需另行准备对应依赖与服务。现有测试通过与线上质量达标是不同的证据。

## 时间不足时

只剩 3 天：第 1 天建立闭环并追入库；第 2 天主攻检索与 Agent；第 3 天追生成/引用、手算评测并模拟讲解。至少读懂 `retrieve`、`answer_question` 和 Agent 精排测试。

7 天结束的最低验收：画出两条主链路、写出 Agent 伪代码、手算一次 RRF 和检索指标、解释一次失败案例、展示自己的对比实验、明确说明自己的实际贡献。学习理解、复现、修改与独立设计分别表述，简历效果数据使用自己能够说明条件与复现过程的记录。
