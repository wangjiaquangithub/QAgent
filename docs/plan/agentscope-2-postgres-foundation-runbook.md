# AgentScope 2 PostgreSQL 基础设施 Runbook

## 1. 范围与不可变边界

本 Runbook 只覆盖 QAgent 后续运行底座所需的 PostgreSQL 本地开发、测试、迁移、健康检查和备份恢复基础设施。它不改变现有业务运行路径。

明确边界：

- 现有 SQLite 正式读写逻辑保持不变，不迁移现有 SQLite 数据。
- 不修改、删除或禁用 LangGraph、GoalService、`/api/langgraph`、SSE 或自动化任务。
- PostgreSQL migration 与现有 SQLite schema/migration 完全分离，不共用 SQLite 的 `APP_SCHEMA_VERSION`。
- 当前只创建 `qagent_migration_probe` 验证表。它是 migration/备份恢复演练的最小基础设施表，不是 Run、Task、Session 或其他正式业务表。
- `backend/scripts/postgres_verify.py` 会执行 `DROP SCHEMA public CASCADE`，因此只能使用专用验证库 `qagent_foundation_test`。
- 破坏性验证唯一读取 `QAGENT_POSTGRES_VERIFY_URL`，不会读取、推导或回退到 `QAGENT_POSTGRES_URL` 及普通 `QAGENT_POSTGRES_*` 变量。
- 验证脚本拒绝 `qagent_dev`、`qagent_prod`、`qagent_production`、`qagent_test`、共享库以及任何不是精确名称 `qagent_foundation_test` 的数据库；没有专用 URL 时直接失败。
- 不要将 `.env`、真实密码或生产连接串提交到 Git。仓库只提供 `.env.example` 占位配置。

> **红线：** 不要对 `qagent_dev`、生产库或任何共享库执行 `postgres_verify.py`，也不要尝试通过参数绕过数据库校验。当前没有绕过参数。

## 2. 当前技术栈与采用方案

当前后端的 Python 依赖由 `uv` 管理，依赖定义在 `backend/pyproject.toml`，锁文件为 `backend/uv.lock`，Python 要求为 `>=3.12`。仓库已有 Docker Compose，但现有 Compose 负责 Gateway/LangGraph 等服务；本基础设施使用独立的 `docker/docker-compose-postgres.yaml`，不改变现有服务编排。

现状中没有可复用的 PostgreSQL ORM 或 Alembic migration。现有 SQLite schema/migration 继续原样保留。本阶段采用：

- SQLAlchemy 2：连接和最小验证 SQL；
- psycopg 3（`psycopg[binary]`）：PostgreSQL 驱动；
- Alembic：独立的 PostgreSQL migration 目录 `backend/migrations/`；
- `backend/app/postgres/healthcheck.py`：普通开发库的只读连接健康检查；
- `backend/scripts/postgres_verify.py`：只针对专用验证库执行空库初始化、重复 migration、连接、备份和恢复验证。

## 3. 前置条件

本地需要：

- Docker Engine / Docker Desktop，以及 Docker Compose；
- `uv`；
- PostgreSQL client utilities：`pg_dump` 和 `pg_restore`。macOS 可使用 Homebrew 或其他 PostgreSQL client 安装包；Linux CI 通过 `postgresql-client` 安装；
- 已在仓库根目录执行过依赖安装。

检查工具：

```bash
cd /Users/wangjiaquan/project/QAgent
docker --version
docker compose version
uv --version
pg_dump --version
pg_restore --version
```

如果 `pg_dump` 或 `pg_restore` 不在 `PATH`，可通过 `PG_DUMP_BIN`、`PG_RESTORE_BIN` 指定绝对路径。

## 4. 安装后端依赖

```bash
cd /Users/wangjiaquan/project/QAgent/backend
uv sync --group dev
```

依赖变更后不要手工编辑 `uv.lock`；使用：

```bash
cd /Users/wangjiaquan/project/QAgent/backend
uv lock
uv sync --group dev
```

## 5. 配置环境变量

从样例复制一份本地配置；如果 `.env` 已存在，不要覆盖：

```bash
cd /Users/wangjiaquan/project/QAgent
cp .env.example .env
```

### 5.1 普通开发库

普通后端连接和非破坏性 migration 使用 `QAGENT_POSTGRES_URL`，或者使用以下拆分变量：

```dotenv
QAGENT_POSTGRES_HOST=127.0.0.1
QAGENT_POSTGRES_PORT=54329
QAGENT_POSTGRES_DB=qagent_dev
QAGENT_POSTGRES_USER=qagent
QAGENT_POSTGRES_PASSWORD=change-me-local-only
# QAGENT_POSTGRES_URL=postgresql+psycopg://qagent:change-me-local-only@127.0.0.1:54329/qagent_dev
```

`QAGENT_POSTGRES_URL`（如果设置）优先于普通拆分变量。它只用于普通开发连接和 `make postgres-migrate`，**绝不能用于破坏性验证**。

### 5.2 专用破坏性验证库

破坏性验证必须单独配置完整 URL：

```dotenv
QAGENT_POSTGRES_VERIFY_PORT=54330
QAGENT_POSTGRES_VERIFY_DB=qagent_foundation_test
QAGENT_POSTGRES_VERIFY_USER=qagent
QAGENT_POSTGRES_VERIFY_PASSWORD=change-me-verify-only
QAGENT_POSTGRES_VERIFY_URL=postgresql+psycopg://qagent:change-me-verify-only@127.0.0.1:54330/qagent_foundation_test
```

其中：

- `QAGENT_POSTGRES_VERIFY_URL` 是 `postgres_verify.py` 的唯一配置入口；
- URL 中的数据库名必须严格、区分大小写地等于 `qagent_foundation_test`；`QAGENT_FOUNDATION_TEST`、`qagent_foundation_test_2` 等名称全部拒绝；
- `QAGENT_POSTGRES_VERIFY_DB/USER/PASSWORD/PORT` 用于 Compose 创建隔离验证容器，必须与 URL 的主机、端口、用户、密码保持一致；
- 普通 `QAGENT_POSTGRES_URL` 即使指向 `qagent_dev`，也不会被验证脚本使用；缺少 `QAGENT_POSTGRES_VERIFY_URL` 时 `make postgres-verify` 直接返回失败。

密码包含 `@`、`:`、`/`、`#` 等 URL 特殊字符时，应对 URL 中的用户名/密码进行 percent-encoding；不要把密码写进命令行参数。

## 6. 启动本地 PostgreSQL

从仓库根目录执行：

```bash
cd /Users/wangjiaquan/project/QAgent
make postgres-up

docker compose -f docker/docker-compose-postgres.yaml ps
```

Compose 会启动两个完全分离的 PostgreSQL 服务：

| 服务 | 数据库 | 宿主机端口 | 数据卷 | 用途 |
|---|---|---:|---|---|
| `postgres` | `qagent_dev` | `54329` | `qagent-postgres-data` | 普通开发连接、非破坏性 migration |
| `postgres-foundation-test` | `qagent_foundation_test` | `54330` | `qagent-postgres-foundation-test-data` | 破坏性验证、备份恢复演练 |

两个服务不共享容器、端口或 volume。只启动 PostgreSQL，不启动 Gateway 或 LangGraph。

停止容器但保留数据：

```bash
make postgres-down
```

如需删除两个本地数据库的数据，必须明确确认后执行：

```bash
docker compose -f docker/docker-compose-postgres.yaml down -v
```

## 7. 健康检查

`make postgres-health` 检查普通开发库，自动加载根目录 `.env`，只执行只读查询：

```bash
cd /Users/wangjiaquan/project/QAgent
make postgres-health
```

也可以检查 Compose 两个容器的健康状态：

```bash
docker compose -f docker/docker-compose-postgres.yaml ps
```

若直接从 `backend/` 执行后端命令，请先在同一个 shell 导出 `.env`：

```bash
cd /Users/wangjiaquan/project/QAgent/backend
set -a
source ../.env
set +a
PYTHONPATH=. uv run python -m app.postgres.healthcheck
```

## 8. 执行 migration

普通开发库 migration 使用 `QAGENT_POSTGRES_URL` 或普通拆分变量，不会使用验证库：

```bash
cd /Users/wangjiaquan/project/QAgent
make postgres-migrate
```

等价命令：

```bash
cd /Users/wangjiaquan/project/QAgent/backend
set -a
source ../.env
set +a
PYTHONPATH=. uv run alembic -c alembic.ini upgrade head
PYTHONPATH=. uv run alembic -c alembic.ini current
```

空数据库执行 `upgrade head` 会创建：

- Alembic 自己的 `alembic_version` 版本表；
- `qagent_migration_probe` 最小验证表；
- marker `foundation-ready`。

当前没有正式业务表。生成后续 migration 时，在 `backend/migrations/versions/` 新建 revision，并保持其只描述已经拍板的 PostgreSQL schema；不要复用或修改现有 SQLite migration。

## 9. 自动化验证

### 9.1 破坏性一键验证

先确认专用验证容器已经启动并且 `QAGENT_POSTGRES_VERIFY_URL` 指向 `qagent_foundation_test`：

```bash
cd /Users/wangjiaquan/project/QAgent
make postgres-up
make postgres-verify
```

`make postgres-verify` 在启动 Python 脚本前检查专用 URL。它不会因为普通 URL 已配置就放行，也不会把普通 URL 作为 fallback。脚本随后再次解析并精确校验数据库名，然后按以下顺序运行：

1. 只对 `qagent_foundation_test` 清空并重建 `public` schema，模拟空数据库；
2. 执行 `alembic upgrade head`，确认基础表和 marker 创建；
3. 再次执行同一 migration，确认重复执行安全；
4. 执行 PostgreSQL 连接检查；
5. 写入 `backup-restore-verified` 测试 marker；
6. 使用 `pg_dump` 生成 custom-format 备份；
7. 再次清空专用验证库的 `public` schema；
8. 使用 `pg_restore --clean --if-exists --no-owner --exit-on-error` 恢复；
9. 查询恢复后的 marker，确认数据正确。

脚本通过 `PGPASSWORD` 传递密码给 libpq 工具，不把密码放进 `pg_dump`/`pg_restore` 的 argv。默认备份文件写入临时目录并在脚本结束后清理；如需保留备份，可设置：

```bash
mkdir -p artifacts
export QAGENT_POSTGRES_BACKUP_FILE="$PWD/artifacts/qagent-foundation.dump"
make postgres-verify
```

不存在任何 `--allow-non-dev-db` 或类似绕过选项。若 URL 不是 `qagent_foundation_test`，脚本会在建立数据库连接前拒绝执行。

### 9.2 自动化安全测试

不依赖 PostgreSQL 的安全测试：

```bash
cd /Users/wangjiaquan/project/QAgent/backend
PYTHONPATH=. uv run pytest --noconftest tests/test_postgres_foundation.py -v
```

这些测试证明：

- `qagent_dev`、`qagent_prod`、`qagent_production` 等数据库会被拒绝；
- `qagent_foundation_test` 会被允许；
- 未配置 `QAGENT_POSTGRES_VERIFY_URL` 时验证配置失败；
- 只有普通 `QAGENT_POSTGRES_URL` 时，CLI 失败且不会进入 `DROP SCHEMA`。

显式启用 PostgreSQL 集成验证：

```bash
cd /Users/wangjiaquan/project/QAgent
set -a
source .env
set +a
QAGENT_RUN_POSTGRES_TESTS=1 \
  PYTHONPATH=backend uv run --project backend \
  pytest --noconftest backend/tests/test_postgres_foundation.py -m postgres -v
```

日常推荐使用 `make postgres-verify`，因为它明确输出空库初始化、重复 migration、连接检查和备份恢复结果。

### 9.4 Runtime migration 与双连接并发验证语义

验证脚本只在已经通过专用 URL 和 `current_database()` 身份检查后执行 destructive SQL。Runtime migration 的最小验证顺序固定为：

1. `upgrade 0002_qagent_runtime`，检查 Runtime 表和旧的全局幂等约束；
2. `upgrade 0003_qagent_runtime_org_scoping`，检查 `org_id` 字段和索引；
3. `upgrade 0004_qagent_runtime_org_idempotency`，检查按组织范围的幂等约束已替换旧约束；
4. 重复 `upgrade head`，确认重复升级安全；
5. `downgrade 0004` 到 `0003`，确认旧幂等约束恢复；
6. `downgrade 0003` 到 `0002`，确认组织字段和索引移除；
7. 再次 `upgrade head`，确认最终 schema 可恢复到 `0004`。

并发验证使用两个独立 SQLAlchemy engine，并在操作前分别读取两个不同的 PostgreSQL backend PID；两个 worker 共享同一个 approval/run，但不共享数据库连接：

- **Grant vs Reject**：两个 approval decision 竞争同一个 `run`/`approval`，最终只能是 `approval=granted, run=running` 或 `approval=rejected, run=failed`，且恰好一个 decision 成功；
- **Cancel vs Grant**：cancel 与 grant 竞争同一个 run 行锁，最终只能是 `approval=requested, run=cancelled` 或 `approval=granted, run=running`，且恰好一个操作成功。

脚本同时检查：

- 不出现 `Approval=granted` 且 `Run=failed`；
- 不出现 `Approval=rejected` 且 `Run=running`；
- approval 已决策时，对应的 Runtime Event 存在且每种 required event 恰好一次；
- 并发后 event `sequence` 唯一、连续，并从 1 开始；
- 两个 worker 的 backend PID 不同。

事务语义要求：

- approval decision 先锁 run 行，再锁 approval 行；
- cancel 与 grant 以相同 run 行锁作为串行化点；
- approval/run 状态变更和对应 Runtime Event 在同一个事务中；
- Runtime Event 写入失败必须使状态变更一起回滚，而不是留下半完成状态。

若专用 URL 未配置、密码不可用、连接失败，或 URL/`current_database()` 不是精确的 `qagent_foundation_test`，验证必须安全失败并报告真实错误；绝不能尝试 `qagent_dev`、`qagent_test` 或其他数据库。

### 9.3 CI

`.github/workflows/postgres-foundation.yml` 使用 PostgreSQL 17 service container，数据库名固定为 `qagent_foundation_test`，运行同一份验证脚本和 opt-in 集成测试。CI 只设置 `QAGENT_POSTGRES_VERIFY_URL`，不设置普通 `QAGENT_POSTGRES_URL`，以便持续验证“验证脚本不能回退到普通连接”的边界。CI 使用虚假的测试密码，不依赖仓库 secrets，也不会接触生产数据库。

## 10. 手工备份与恢复演练

手工备份恢复只允许对专用验证库执行。先加载 `.env`，再从专用 URL 解析连接参数；不要使用普通 `QAGENT_POSTGRES_*` 变量，也不要把密码写进命令行：

```bash
cd /Users/wangjiaquan/project/QAgent
set -a
source .env
set +a

# 先让脚本验证 URL 的数据库名；不是 qagent_foundation_test 时立即失败。
cd backend
PYTHONPATH=. uv run python -c \
  'from app.postgres.config import get_postgres_verify_url; print(get_postgres_verify_url())'
```

推荐直接运行完整演练并保留 dump：

```bash
cd /Users/wangjiaquan/project/QAgent
mkdir -p artifacts
export QAGENT_POSTGRES_BACKUP_FILE="$PWD/artifacts/qagent-foundation.dump"
make postgres-verify
```

该命令会自动：写入测试 marker、备份、清空专用验证库、恢复并校验 `backup-restore-verified`。如果需要手工检查 dump，连接参数必须来自已经验证过的专用 URL：

```bash
cd /Users/wangjiaquan/project/QAgent/backend
set -a
source ../.env
set +a

# 仅示意：先确认下列值与 QAGENT_POSTGRES_VERIFY_URL 完全一致，且 DB 必须是 qagent_foundation_test。
export PGPASSWORD="$QAGENT_POSTGRES_VERIFY_PASSWORD"
pg_dump --format=custom --no-owner \
  --host 127.0.0.1 --port "$QAGENT_POSTGRES_VERIFY_PORT" \
  --username "$QAGENT_POSTGRES_VERIFY_USER" \
  --dbname qagent_foundation_test \
  --file ../artifacts/qagent-foundation-manual.dump

pg_restore --clean --if-exists --no-owner --exit-on-error \
  --host 127.0.0.1 --port "$QAGENT_POSTGRES_VERIFY_PORT" \
  --username "$QAGENT_POSTGRES_VERIFY_USER" \
  --dbname qagent_foundation_test \
  ../artifacts/qagent-foundation-manual.dump

psql --host 127.0.0.1 --port "$QAGENT_POSTGRES_VERIFY_PORT" \
  --username "$QAGENT_POSTGRES_VERIFY_USER" --dbname qagent_foundation_test \
  --command 'SELECT id, marker, created_at FROM qagent_migration_probe;'
```

生产环境的备份保留、PITR、RPO/RTO、跨区域复制和恢复授权不在本阶段范围内，必须由负责人和运维一起制定。

## 11. 常见问题排查

### Docker daemon 未启动

`docker compose` 报无法连接 daemon 时，启动 Docker Desktop/Engine 后重试：

```bash
docker info
make postgres-up
```

### 54329 或 54330 端口冲突

查看端口占用，或在 `.env` 中分别修改 `QAGENT_POSTGRES_PORT` / `QAGENT_POSTGRES_VERIFY_PORT`，然后重启 Compose。容器内部端口仍是 `5432`，并且普通库与验证库必须保持不同宿主机端口。

### 容器不是 healthy

查看状态和对应日志：

```bash
docker compose -f docker/docker-compose-postgres.yaml ps
docker compose -f docker/docker-compose-postgres.yaml logs postgres
docker compose -f docker/docker-compose-postgres.yaml logs postgres-foundation-test
```

常见原因是首次初始化较慢、volume 中旧凭据与当前 `.env` 不一致，或端口/资源不足。若是可丢弃的本地环境，停止后用 `down -v` 删除两个 volume，再重新启动。不要为了修复验证失败而把验证 URL 改成开发库。

### `make postgres-verify` 提示未配置专用 URL

这是安全保护，不是普通连接故障。设置并检查：

```bash
printf '%s\n' "$QAGENT_POSTGRES_VERIFY_URL"
# 必须以 /qagent_foundation_test 结尾（可带 URL 查询参数）。
make postgres-verify
```

即使 `QAGENT_POSTGRES_URL` 指向 `qagent_dev`，也不会放行。

### 验证脚本拒绝目标数据库

确认 URL 的数据库名精确为 `qagent_foundation_test`。以下目标必须拒绝：`qagent_dev`、`qagent_prod`、`qagent_production`、`qagent_test` 以及其他未明确保留为验证用途的数据库。不要寻找或添加绕过参数。

### Alembic 报未配置连接

普通 `make postgres-migrate` 在同一 shell 中必须设置完整的 `QAGENT_POSTGRES_*` 五元组，或设置完整的 `QAGENT_POSTGRES_URL`。破坏性验证则必须设置完整的 `QAGENT_POSTGRES_VERIFY_URL`；两套配置不可混用。

查看普通 migration 当前版本：

```bash
cd /Users/wangjiaquan/project/QAgent/backend
set -a
source ../.env
set +a
PYTHONPATH=. uv run alembic current
PYTHONPATH=. uv run alembic history
```

不要手工修改 `alembic_version`，也不要将 SQLite 的 schema version 写入该表。若要验证空库和重复 migration，使用专用验证库上的 `make postgres-verify`。

### 依赖缺失

不要依赖系统 Python 的 SQLAlchemy/Alembic；从 `backend/` 执行：

```bash
uv sync --group dev
uv run python -c 'import alembic, psycopg, sqlalchemy; print("postgres dependencies: ok")'
```

### `pg_dump` / `pg_restore` 缺失或版本不兼容

检查：

```bash
pg_dump --version
pg_restore --version
echo "$PG_DUMP_BIN"
echo "$PG_RESTORE_BIN"
```

必要时把两个变量设置为同一套 PostgreSQL client 的绝对路径。client 大版本最好与 server 接近；遇到 custom-format 兼容性错误时，优先统一 client/server 主版本后重试。

## 12. 需要负责人拍板的技术问题

后续正式业务 schema 设计前，需要负责人确认：

1. PostgreSQL 生产大版本和升级窗口；
2. 生产连接池、SSL/TLS、secret manager、最小权限角色和审计策略；
3. schema 命名、多租户边界、组织级隔离及是否使用 RLS；
4. 是否纳入 `pgvector`，以及向量索引和升级策略；
5. migration 的发布、锁定、回滚/前向修复和版本兼容策略；
6. 备份保留周期、PITR、跨区域复制、RPO/RTO 和恢复演练频率；
7. Run、Task、Session 等正式业务表及其状态机、幂等键、审计字段的最终设计。

## 13. 实际执行记录 / 2026-09-13

本节记录 2026 年 9 月 13 日在本机执行的真实 PostgreSQL 验证。执行者使用仓库自带的 `docker/docker-compose-postgres.yaml` 启动了独立的 PostgreSQL 17 验证服务；所有破坏性 SQL、Runtime migration 往返、并发场景和备份恢复均只针对专用数据库 `qagent_foundation_test`。密码、Token 和完整连接串不记录在本文档中。

### 13.1 环境与安全边界

- Compose 服务：`qagent-postgres-foundation-test`，宿主端口 `54330`；普通开发服务 `qagent-postgres` 使用不同端口 `54329`。
- 实际验证库：`qagent_foundation_test`。
- `make postgres-verify` 只接收 `QAGENT_POSTGRES_VERIFY_URL`；执行前后由验证脚本在同一连接上检查 URL 目标和 `SELECT current_database()`，不使用普通 `QAGENT_POSTGRES_URL` 或其他普通 `QAGENT_POSTGRES_*` fallback。
- 本地 `.env` 未作为普通连接 fallback；本次专用凭据仅在本地进程 / Compose 运行时使用，未写入 Git 已跟踪文件，也未在报告中输出。
- 未连接、清空、迁移或修改 `qagent_dev`、`qagent_test`、`qagent_prod`、`qagent_production` 或其他业务数据库。

环境启动命令及原始结果：

```text
$ make postgres-up
... qagent-postgres ... Started
... qagent-postgres-foundation-test ... Started

$ make postgres-health
postgres-health refused: PostgreSQL is not configured.
Set QAGENT_POSTGRES_URL or the complete QAGENT_POSTGRES_HOST/PORT/DB/USER/PASSWORD tuple.

$ make postgres-migrate
postgres-migrate refused: PostgreSQL is not configured.
Set QAGENT_POSTGRES_URL or the complete QAGENT_POSTGRES_HOST/PORT/DB/USER/PASSWORD tuple.

$ docker compose -f docker/docker-compose-postgres.yaml ps
NAME                              IMAGE                COMMAND                  SERVICE                    CREATED       STATUS                 PORTS
qagent-postgres                   postgres:17-alpine   "docker-entrypoint.s…"   postgres                   Up 8 hours (healthy)   0.0.0.0:54329->5432/tcp
qagent-postgres-foundation-test   postgres:17-alpine   "docker-entrypoint.s…"   postgres-foundation-test   Up 8 hours (healthy)   0.0.0.0:54330->5432/tcp
```

普通 `postgres-health` / `postgres-migrate` 的失败是预期的安全失败：普通连接未配置，命令没有回退到专用库，也没有接触其他数据库。专用验证使用单独的 `QAGENT_POSTGRES_VERIFY_URL` 进程环境继续执行。

### 13.2 Foundation 自动化验证

```text
$ cd /Users/wangjiaquan/project/QAgent/backend
$ PYTHONPATH=. uv run pytest --noconftest tests/test_postgres_foundation.py -v
21 passed, 1 skipped in 1.28s
```

结果：通过。此前带 `--no-cov` 的一次尝试因本地环境缺少 `pytest-cov` 失败，属于测试依赖缺失，不是 foundation 测试逻辑失败。

### 13.3 `make postgres-verify` 与 Runtime migration

为证明不会从普通连接回退，执行时显式移除了普通 `QAGENT_POSTGRES_*` 环境变量，并只注入专用 `QAGENT_POSTGRES_VERIFY_URL`：

```text
$ env -u QAGENT_POSTGRES_URL \
    -u QAGENT_POSTGRES_HOST -u QAGENT_POSTGRES_PORT -u QAGENT_POSTGRES_DB \
    -u QAGENT_POSTGRES_USER -u QAGENT_POSTGRES_PASSWORD \
    QAGENT_POSTGRES_VERIFY_URL=<redacted dedicated URL> make postgres-verify
[postgres-verify] Target: postgresql+psycopg://<redacted>/qagent_foundation_test
[postgres-verify] Verified current_database(): qagent_foundation_test
[postgres-verify] upgrade 0002_qagent_runtime: passed
[postgres-verify] upgrade 0003_qagent_runtime_org_scoping: passed
[postgres-verify] upgrade 0004_qagent_runtime_org_idempotency: failed
make[1]: *** [postgres-verify] Error 1
make: *** [postgres-verify] Error 2
```

独立 Alembic 往返的实际结果：

```text
$ ... alembic upgrade 0002_qagent_runtime
PASSED

$ ... alembic upgrade 0003_qagent_runtime_org_scoping
PASSED

$ ... alembic upgrade 0004_qagent_runtime_org_idempotency
FAILED

$ ... alembic downgrade 0002_qagent_runtime
PASSED

$ ... alembic upgrade 0003_qagent_runtime_org_scoping
PASSED
```

`0004` 在 PostgreSQL 上的原始失败核心为：

```text
psycopg.errors.DependentObjectsStillExist:
cannot drop constraint qagent_runs_pkey on table qagent_runs because other objects depend on it
DETAIL:
constraint qagent_run_events_run_id_fkey on table qagent_run_events depends on index qagent_runs_pkey
constraint qagent_approvals_run_id_fkey on table qagent_approvals depends on index qagent_runs_pkey
constraint qagent_recovery_points_run_id_fkey on table qagent_recovery_points depends on index qagent_runs_pkey
constraint qagent_run_assets_run_id_fkey on table qagent_run_assets depends on index qagent_runs_pkey
HINT:  Use DROP ... CASCADE to drop the dependent objects too.
[SQL: ALTER TABLE qagent_runs DROP CONSTRAINT qagent_runs_pkey]
```

失败位置：`backend/migrations/versions/0004_qagent_runtime_org_idempotency.py` 的 `op.batch_alter_table("qagent_runs", recreate="always")`。失败事务已回滚。因此本次 Runtime migration 结论为：

- `0002_qagent_runtime`：PostgreSQL upgrade 通过；
- `0003_qagent_runtime_org_scoping`：PostgreSQL upgrade 通过；
- `0004_qagent_runtime_org_idempotency`：PostgreSQL upgrade 失败；
- 重复 upgrade head：未能通过，因为 head 包含失败的 0004；
- `0002` / `0003` 最小 downgrade / upgrade 往返：通过；
- 最终 upgrade head：未通过，同一 0004 外键依赖错误阻断。

这不是通过调整验证脚本得到的假通过；需要后续修复 0004 migration 的 PostgreSQL 主键重建 / 外键依赖处理后重新验证。正式 migration 文件本次未修改。

验证日志保留于本机：`/tmp/qagent-postgres-verify-20260913.log`。

### 13.4 双连接并发验证

验证使用两个独立 SQLAlchemy engine / PostgreSQL 连接，并在 worker 中读取 `pg_backend_pid()`。两个场景均确认 PID 不同；状态写入和对应 Runtime Event 在同一个事务内，竞争操作通过同一 `run` 行锁串行化。

#### Grant vs Reject

```text
backend_pid(connection_1)=41978
backend_pid(connection_2)=41979
winner=reject
approval=rejected
run=failed
decision_success_count=1
runtime_event_sequences=[1, 2, 3, 4, 5, 6]
event_sequence_unique=true
event_sequence_contiguous=true
event_sequence_starts_at_1=true
approval_decision_event_present=true
forbidden_granted_failed=false
forbidden_rejected_running=false
result=PASS
```

#### Cancel vs Grant

```text
backend_pid(connection_1)=41980
backend_pid(connection_2)=41981
winner=cancel
approval=requested
run=cancelled
operation_success_count=1
runtime_event_sequences=[1, 2, 3, 4, 5]
event_sequence_unique=true
event_sequence_contiguous=true
event_sequence_starts_at_1=true
runtime_events_complete=true
runtime_events_without_duplicates=true
result=PASS
```

总体原始结果：

```text
Two-connection Runtime concurrency checks passed
```

并发验证覆盖并确认：

- 两个 backend PID 不同，确实使用两个独立数据库连接；
- Grant vs Reject 最终只能落在 `approval=rejected, run=failed`（本次 Reject 获胜）；
- Cancel vs Grant 最终落在 `approval=requested, run=cancelled`（本次 Cancel 获胜）；
- 没有出现 `Approval=granted` 且 `Run=failed`；
- 没有出现 `Approval=rejected` 且 `Run=running`；
- approval 已决策时，对应 Runtime Event 存在；
- Runtime Event sequence 唯一、连续且从 1 开始，没有重复。

### 13.5 备份、reset、restore

备份恢复只操作 `qagent_foundation_test`，使用 PostgreSQL custom format；密码没有放进 argv。实际 dump 文件保留在本机临时路径 `/tmp/qagent-foundation-final-gc0t4huu.dump`。

```text
$ pg_dump --format=custom --no-owner ... --dbname qagent_foundation_test --file /tmp/qagent-foundation-final-gc0t4huu.dump
pg_dump_return_code= 0
dump_size= 17440

$ pg_restore --clean --if-exists --no-owner --exit-on-error ... qagent_foundation_test /tmp/qagent-foundation-final-gc0t4huu.dump
reset_return_code=0
pg_restore_return_code= 0

$ SELECT current_database();
current_database=qagent_foundation_test

$ SELECT marker FROM qagent_migration_probe WHERE id = 1;
marker_rows=[{'id': 1, 'marker': 'backup-restore-verified'}]

$ SELECT COUNT(*), array_agg(event_type ORDER BY sequence), array_agg(sequence ORDER BY sequence) FROM qagent_run_events ...;
runtime_event_count= 3
runtime_event_types=['run.created', 'run.queued', 'run.planning']
runtime_event_sequences=[1, 2, 3]
backup_restore_result=PASS
```

恢复后的 Alembic revision 为 `0003_qagent_runtime_org_scoping`，而不是 0004/head；这是因为 0004 在本次 PostgreSQL 验证中真实失败，不能将恢复结果报告为 head 已恢复。

### 13.6 结论、修改文件与 Git 操作

| 项目 | 结果 |
| --- | --- |
| 本地 PostgreSQL Compose 环境启动 | 通过；两个容器均 healthy |
| 验证数据库名称 | 严格为 `qagent_foundation_test` |
| Foundation 测试 | 通过：21 passed, 1 skipped |
| Runtime 0002 | 通过 |
| Runtime 0003 | 通过 |
| Runtime 0004 / upgrade head | 失败：PostgreSQL 外键依赖阻止 `qagent_runs` 主键重建 |
| 0002/0003 最小 downgrade / upgrade | 通过 |
| 两连接 Grant vs Reject | 通过 |
| 两连接 Cancel vs Grant | 通过 |
| Event sequence invariant | 通过；唯一、连续、从 1 开始 |
| 备份 / reset / restore | 通过，仅针对专用验证库 |

本轮实际新增 / 修改的仓库文件：

- `/Users/wangjiaquan/project/QAgent/docs/plan/agentscope-2-postgres-foundation-runbook.md`：追加本节实际执行记录。

本轮未修改 Runtime 核心业务代码、正式 0004 migration、LangGraph、Gateway 或业务主链。没有写入本地 `.env`，没有新增凭据文件。

Git 写操作：未执行。没有执行 `git add`、`git commit`、`git reset`、`git checkout`、`git switch`、`git restore`、`git clean`、`git stash`，也没有创建分支或 worktree。
