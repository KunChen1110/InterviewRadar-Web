# InterviewRadar Web

[中文](#中文) · [English](#english)

> A Chinese-language AI mock-interview web app. This public code snapshot does not include the production database or interview-question corpus.

## 中文

### 项目定位

InterviewRadar Web 是可完成文字模拟面试流程的 Web Beta 原型：输入或上传简历、选择岗位、回答分环节问题、接受有限次数的动态追问，并获得逐轮反馈和复盘报告。它是 [InterviewRadar Skill](https://github.com/KunChen1110/InterviewRadar) 的网页产品化探索；两个仓库对应不同的产品形态。**界面目前主要为中文**，本仓库没有英文 UI。

### 已实现的能力与限制

| 能力 | 实际边界 |
| --- | --- |
| 账号、历史、数据导出/删除 | 已实现；多用户持久化需要 PostgreSQL。没有支付流程或面试次数收费。 |
| 简历画像与组卷 | 已实现；PDF 仅提取可选中文本，不支持扫描件 OCR。首题和项目题会引用简历项目，但仍有模板成分。 |
| 题目检索 | 来源约束 + 关键词排序 + 可选的 embedding 余弦相似度；未覆盖岗位使用明确提示的通用练习题。不是数据库 ANN 检索。 |
| 反馈、动态追问与报告 | 已实现，并有模型失败时的规则降级。评分尚未经真人面试官标定，不是客观录用预测。 |
| 语音 | 可选的录音后转写，依赖 FFmpeg、阿里云 NLS 和浏览器权限；并非实时流式对话。 |
| 投稿、审核、RAG 导入 | 有管理员审核和版本化导入代码；题目真实性、许可和质量仍须人工判断。 |

本仓库**不包含**线上数据库、生产向量、真实面经题库、用户简历/回答、上传内容、备份或 API 密钥。`tests/fixtures/demo_knowledge/` 只有原创**合成测试题**，不是面经；运行中的生产知识库不能由此仓库单独复现。语义检索还需自行配置 embedding 服务并导入有权使用的题目。

### 目录与数据流

```text
backend/              FastAPI、账号、简历画像、面试状态机、检索、模型适配、导入器
web/ + assets/        原生 HTML/CSS/JavaScript 中文界面与静态资源
tests/                 离线测试与合成题夹具
evaluation/            不含敏感数据的评估用例
deploy/ + docs/deploy/ Nginx 模板与部署说明
```

面试主流程：联系方式脱敏 → 简历结构化画像 → 岗位/环节过滤 → 关键词与可用向量的混合排序 → 模型仅重排候选题 ID → 有上限的追问 → 报告。向量可存于 PostgreSQL/pgvector，但当前候选题的余弦排序主要在应用进程内完成；这里的「Agent」是受约束的面试工作流，不是自主多 Agent 系统。

### 本地运行

需要 Python 3.11+，语音功能另需 FFmpeg。在仓库根目录执行：

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.txt
cp .env.example .env
python -m pytest -q
python -m uvicorn backend.main:app --reload --host 127.0.0.1 --port 8000
```

打开 `http://127.0.0.1:8000/`。未配置 `DATABASE_URL` 时，应用进入仅供本地体验的无库模式，账号和历史不会按生产方式持久化；`/health` 可检查进程，而 `/ready` 返回 503 属于预期。**不要将无库开发模式暴露到公网。**

`.env.example` 是本地模板。聊天模型使用 `OPENAI_API_KEY`、`OPENAI_BASE_URL`、`OPENAI_MODEL`；embedding 使用独立的 `EMBEDDING_API_KEY`、`EMBEDDING_BASE_URL`、`EMBEDDING_MODEL`。不要混用或提交 Key。没有外部 Key 也能跑离线测试，文字面试有规则回退。

### 部署与数据

`Dockerfile`、`docker-compose.yml`、`deploy/` 和 `docs/deploy/` 提供部署参考。生产环境需要 PostgreSQL/pgvector、独立生成的 `APP_SECRET_KEY`、管理员邮箱、聊天模型凭据和 HTTPS。审核后语料的导入入口是 `python -m backend.rag_release_import --manifest <path> --documents <path>`，但**生产导入包不在本仓库**。切勿提交原始简历、用户回答、题库授权材料或服务器配置。

如果将这份代码用于更新旧版原生 systemd 部署，需要先把服务入口从 `main:app` 改为 `backend.main:app`；本次仓库整理**没有改动线上服务器**。

这是公开代码快照，不是面试评分有效性或成熟生产可靠性的证明。目前未添加开源许可证；公开可见不等于授予复用许可。

## English

### Overview

InterviewRadar Web is a **Chinese-language** beta prototype for end-to-end text-based mock interviews. A candidate provides a résumé and target role; the app prepares staged questions, asks bounded follow-ups based on answers, and produces turn-level feedback and a review report. It is a web-product exploration related to the separate [InterviewRadar Skill](https://github.com/KunChen1110/InterviewRadar) repository. The UI has **not** been translated into English.

### Implemented capabilities and limits

| Capability | Actual scope |
| --- | --- |
| Accounts, history, export/deletion | Implemented; persistent multi-user operation requires PostgreSQL. No payment flow or usage cap. |
| Résumé profiling and question plan | Implemented; PDFs need extractable text, not scanned-image OCR. Opening and project questions use résumé context but remain partly template-based. |
| Retrieval | Source-constrained keyword ranking, optionally combined with embedding cosine similarity; uncovered roles use a disclosed generic fallback. No database ANN search. |
| Feedback, follow-ups, report | Implemented with model calls and rule-based fallbacks. Scores have **not** been validated against human interviewers or hiring outcomes. |
| Voice | Optional record-then-transcribe flow using FFmpeg and Alibaba Cloud NLS; not a live streaming voice interview. |
| Contributions, moderation, import | Admin review and versioned RAG-import code exist; authenticity, licensing, and quality still need human review. |

This repository excludes the production database, embeddings, real interview-question corpus, user résumés/answers, uploads, backups, and credentials. `tests/fixtures/demo_knowledge/` contains **original synthetic test questions only**. Cloning this repository does not reproduce production corpus coverage. Semantic retrieval requires an embedding provider and questions you are authorized to use.

### Structure and data flow

`backend/` contains FastAPI, authentication, résumé processing, interview state, retrieval, model adapters, and PostgreSQL/pgvector access. `web/` and `assets/` contain the vanilla HTML/CSS/JavaScript UI; `tests/` holds offline tests and synthetic fixtures; `deploy/` and `docs/deploy/` hold deployment references.

The flow is: redact contact details → build a bounded résumé profile → filter by role and stage → rank by keywords and optional cosine similarity → allow the model to reorder existing candidate IDs → evaluate answers and ask bounded follow-ups → generate a report. Semantic ranking is primarily computed in the application process even when vectors are stored in PostgreSQL. “Agent” means a constrained interview workflow here, not an autonomous multi-agent system.

### Run locally

Use Python 3.11+ and run from the repository root (FFmpeg is additionally needed for voice):

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.txt
cp .env.example .env
python -m pytest -q
python -m uvicorn backend.main:app --reload --host 127.0.0.1 --port 8000
```

Open `http://127.0.0.1:8000/`. Without `DATABASE_URL`, the app uses a non-persistent development mode; **do not expose it publicly**. `/health` checks the process while `/ready` returning 503 is expected in this state. Offline tests do not require API keys; chat and embedding credentials must be configured separately in an untracked `.env` for model-backed behaviour.

### Deployment and limitations

Docker Compose and Nginx files are deployment references, not a production data export. Production requires PostgreSQL/pgvector, a fresh `APP_SECRET_KEY`, an admin email, model credentials, and HTTPS. The reviewed-corpus importer runs as `python -m backend.rag_release_import --manifest <path> --documents <path>`; its production data package is intentionally absent. See `docs/deploy/`.

An older native systemd installation using `main:app` must update its service entry point to `backend.main:app` before deploying this tree. This repository change has **not** modified the live server.

This source snapshot demonstrates an engineering workflow, **not** validated interview scoring, complete role coverage, or mature production-scale reliability. No open-source license is included: public visibility does not grant reuse rights.
