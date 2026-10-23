# 阿里云部署

入口：https://knowledge.jlubot.asia
服务器：8.147.114.172，项目目录：`/opt/knowledgeos`。

本部署使用生产模式 PostgreSQL、Redis、Qdrant、API、合并的后台任务 Worker 和 Nginx。
模型使用项目配置的云端 API；不安装 PyTorch 或本地模型。
前端由本机 `npm --prefix frontend run build` 构建后上传 `frontend/dist`。
Caddy 将域名转发到 `127.0.0.1:18081`；API 仅监听 `127.0.0.1:18010`。
普通用户注册已开启（`RAG_ALLOW_REGISTRATION=true`），注册后自动登录。新用户仅具备普通用户权限，平台管理员由 `RAG_PLATFORM_ADMIN_EMAILS` 名单控制；初始管理员为 `admin@knowledge.jlubot.asia`，密码保存在本机 Git 忽略的 `.secrets/aliyun-admin.json`。

## 运维

在服务器执行：

```sh
cd /opt/knowledgeos
./deploy/aliyun/compose.sh ps
./deploy/aliyun/compose.sh logs --tail 100 api worker
./deploy/aliyun/compose.sh up -d --no-build
```

生产密钥仅保存在服务器 `.env.docker`（权限 600）和本机 `.secrets/aliyun.env`，不要提交 Git。
更新代码后先构建镜像，再使用上述 Compose 命令重建服务。后台评测与入库串行执行，适合低并发使用。
需要独立评测进程时先增加服务器内存，再启用 `separate-evaluation` profile，并把 worker 的角色改回 ingestion。

## 数据

Compose 的 `app-data`、`postgres-data`、`redis-data`、`qdrant-data` 卷保存数据并跨容器重建保留。
不要运行 `down -v`。数据卷不等同于备份；协调备份流程见 `docs/deployment.md` 和 `app.maintenance`。

## 构建

```sh
docker build --build-arg PYTHON_IMAGE=docker.m.daocloud.io/library/python:3.12-slim -f deploy/aliyun/Dockerfile -t rag-knowledge-assistant-backend:local .
docker build -f deploy/aliyun/Dockerfile.web -t rag-knowledge-assistant-web:local .
```

初次部署完成数据库迁移后，用 `./deploy/aliyun/compose.sh exec -T api python - < deploy/aliyun/bootstrap_admin.py` 创建管理员。
该脚本只在用户不存在时创建，不会重置已有密码。初始化凭据在应用卷 `/app/data/initial-admin.json` 中，提取后应删除此临时副本。

## 本次验收结果（2026-10-20）

服务器端通过 HTTPS 完成：首页 HTTP 200、管理员登录、PostgreSQL/Redis/Qdrant/Worker readiness、模型接口、文档上传与向量索引、带来源引用的云端模型问答。测试知识库已删除。
初次验收时命令行访问路径曾返回阿里云备案拦截页；用户随后已通过本地浏览器确认 HTTPS 登录页和服务连接正常。
服务已设置 unless-stopped 自动重启，Docker 已启用开机启动。验收后 API 与 Worker 各占约 230–250 MiB，系统可用内存约 444 MiB，适合低并发。
本次安装的完整依赖版本保存在 `requirements.lock.txt`；当前镜像已在服务器保留。临时下载代理已关闭，服务器初始管理员明文凭据临时副本已删除。本机 `.secrets/aliyun-admin.json` 保留登录凭据。
