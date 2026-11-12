# 阿里云 ECS 单机部署

本方案面向当前 2 核 2 GiB 的 Ubuntu 22.04 ECS：Docker Compose 启动 FastAPI、Nginx 和 PostgreSQL + pgvector。数据库只接入 Compose 私有网络，公网只开放网站端口。Embedding 由阿里云百炼完成，DeepSeek 负责对话生成与评价。

## 1. ECS 安全组

入方向只需要：

| 端口 | 来源 | 用途 |
| --- | --- | --- |
| 22 | 你的公网 IP `/32` | SSH 运维 |
| 80 | `0.0.0.0/0` | 初始 HTTP 访问 |
| 443 | `0.0.0.0/0` | 配好域名证书后的 HTTPS |

不要开放 `5432`、`8000` 或其他数据库端口。SSH 规则应保持为你的公网 IP；若 IP 变化，只更新该来源。

## 2. 准备密钥

在百炼控制台创建一个中国内地地域的 API Key，后面填入 `EMBEDDING_API_KEY`。Embedding 使用 `text-embedding-v4`、1536 维，与应用已有的 `vector(1536)` 列一致。DeepSeek Key 填 `OPENAI_API_KEY`，两个服务的 Key 不要互换。

以下值由你在服务器上生成，不要发给任何人：

```bash
openssl rand -hex 32  # APP_SECRET_KEY
openssl rand -hex 24  # POSTGRES_PASSWORD
```

管理员邮箱填入 `APP_ADMIN_EMAILS`。NLS 语音识别令牌与 AppKey 是可选的；没配置时，文字面试仍可使用。

## 3. 安装 Docker 并准备项目

在 ECS 上通过 SSH 登录 Ubuntu，然后安装 Docker Engine 与 Compose plugin。把产品版代码上传至 `/opt/interviewradar`，进入目录后：

```bash
cp .env.production.example .env
chmod 600 .env
```

编辑 `.env` 并填写 `OPENAI_API_KEY`、`EMBEDDING_API_KEY`、`APP_SECRET_KEY`、`APP_ADMIN_EMAILS` 和 `POSTGRES_PASSWORD`。`POSTGRES_PASSWORD` 建议使用上面生成的十六进制字符串，Compose 会用同一密码连接应用和数据库。

不要把 `.env`、AccessKey、DeepSeek Key、百炼 Key 或密码提交到 Git，也不要通过聊天或截图发送。

## 4. 启动服务并导入向量题库

```bash
docker compose up -d --build
docker compose ps
curl -f http://127.0.0.1/ready
```

`/ready` 返回 `{"status":"ready"}` 后，再运行一次版本化题库导入：

```bash
docker compose exec -T app python -m backend.rag_release_import \
  --manifest deploy/data/rag-v21/rag_import_manifest.json \
  --documents deploy/data/rag-v21/production_documents.jsonl
```

导入命令会先校验 SHA-256 与行数，只处理审核通过的题目，拒绝没有授权记录的小红书内容；再按百炼接口批次生成 1536 维向量，将向量、岗位标签和来源审计字段写入 PostgreSQL。按题干哈希去重，可安全重跑。成功输出应显示 `embedded_and_inserted` 数量；数据库和百炼不可用时命令会失败，不会报告导入成功。

导入后访问 `http://<ECS公网IP>/`。确认安全组已开放 80。

## 5. HTTPS 与域名

公网 IP 可以用于初始 HTTP 验收。正式长期对外使用时，准备域名、DNS 解析和 HTTPS 证书；把完整证书和私钥放在服务器 `certs/fullchain.pem` 与 `certs/privkey.pem`，然后运行：

```bash
docker compose -f docker-compose.yml -f docker-compose.tls.yml up -d
```

完成后再确认安全组放行 443。不要把证书私钥放进镜像或 Git。

## 6. 2 GiB 内存与备份

Compose 已限制应用、PostgreSQL、Nginx 的内存和 CPU；PostgreSQL 使用较小的 shared buffers、工作内存和连接上限。首次部署前建议在 ECS 配 1 GiB swap，作为短时内存峰值缓冲，不代替物理内存。

数据库数据保存在 Docker 命名卷 `db_data`。定期把转储文件复制到 ECS 之外的安全存储：

```bash
docker compose exec -T db pg_dump -U interviewradar -d interviewradar -Fc > interviewradar.dump
```

不要把转储文件放在可公开下载的目录。

更新服务：

```bash
docker compose up -d --build
docker compose logs --tail=100 app
```

## 7. 首次验收

1. 打开首页，注册管理员邮箱，并确认进入管理页面。
2. 创建一场面试，确认岗位题目来自已审核题库且显示来源。
3. 查看 `docker compose logs --tail=100 app`，确认创建过程中没有 embedding 错误；如果发生错误，应用会回退关键词检索，但初次题库导入必须明确成功后才能算语义检索已启用。
4. 重启 Compose 后登录并检查历史记录仍在。

日常新增题库继续从审核后的版本化导入包进入，不在生产服务器自动爬取外部网站。社区投稿走管理页的预览、审核和确认流程。
