# Aliyun ECS native deployment

This runbook describes the native systemd deployment used when the ECS cannot
pull Docker Hub images. It targets Ubuntu 22.04, PostgreSQL 14 with pgvector,
Nginx, and a Python virtual environment under `/opt/interviewradar`.

## Server layout

- Application: `/opt/interviewradar`
- Runtime configuration: `/etc/interviewradar.env` (root-only, mode `0600`)
- App service: `interviewradar.service`
- Reviewed RAG import: `interviewradar-rag-import.service` (one-shot)
- PostgreSQL: local Unix socket only; do not open port 5432 publicly
- Nginx: public HTTP port 80, reverse-proxies to `127.0.0.1:8000`

## Configure credentials

Edit `/etc/interviewradar.env` through the Aliyun Workbench terminal. Set
`OPENAI_API_KEY` to the DeepSeek key, `EMBEDDING_API_KEY` to the separate
DashScope key, and `APP_ADMIN_EMAILS` to the administrator email. The database
URL and application signing secret are generated on the server. Never send API
keys through chat or commit this file.

Start and check the app:

```bash
systemctl enable --now interviewradar
curl -f http://127.0.0.1:8000/ready
```

Start Nginx and check the public reverse proxy:

```bash
systemctl enable --now nginx
curl -f http://127.0.0.1/ready
```

Import the reviewed release after the DashScope key is configured:

```bash
systemctl start interviewradar-rag-import
systemctl status interviewradar-rag-import --no-pager
```

The importer verifies the manifest hash and row count, embeds eligible records,
and writes them idempotently. Confirm its journal output reports the expected
insert count before treating semantic retrieval as enabled.

Check service logs with:

```bash
journalctl -u interviewradar -n 100 --no-pager
journalctl -u interviewradar-rag-import -n 100 --no-pager
```
