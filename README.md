# InterviewRadar Web

InterviewRadar 的 Web 产品代码公开快照。界面目前主要为中文；这个仓库不包含线上数据库、真实面经题库、用户内容、备份包或 API 密钥。`tests/fixtures/demo_knowledge/` 只有原创合成测试题，**不是**真实面经。未接入独立题库时，应用会退回通用练习题。

本地运行需要 Python 3.11+。复制 `.env.example` 为 `.env`，按需填写模型和数据库配置；没有 API 密钥时，可运行不依赖外部服务的测试：

```bash
python -m pip install -r requirements.txt -r requirements-dev.txt
python -m pytest -q
```

生产部署模板见 `docs/deploy/`。这些模板不包含线上密钥或数据；要复现语义检索，需要自行配置 embedding 服务和有权使用的题库。此仓库与 [InterviewRadar Skill](https://github.com/KunChen1110/InterviewRadar) 是不同的项目形态。
