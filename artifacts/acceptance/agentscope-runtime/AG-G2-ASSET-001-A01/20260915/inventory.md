# AG-G2-ASSET-001-A01 — Workspace / 文件 / 资产真实入口与事实源盘点

- 卡片：`AG-G2-ASSET-001-A01`（母任务 `G2-ASSET-001`）
- 日期 / attempt：2026-09-15 / `20260915`
- 起始 HEAD：`78fa15631472218839a228126571c4a935c8caf6`（分支 `codex/agentscope-runtime`）
- 验收命令（卡片原文）：
  ```
  rg -n "evoflow_artifacts|evoflow_media_assets|evoflow_org_artifacts" \
     backend/packages/harness/evoflow/persistence/schema.py backend
  ```
  原始输出：`acceptance-rg.txt`（37 行，与本文件同目录）。
- 本卡**只读**：未修改资产 / 存储代码、schema、migration、Runtime、文件上传协议、设备基础设施、前端重构；
  未纳入存储 HA、文件灾备、备份恢复、压测、容量或迁移演练。**未设计统一设备命令 / 回执协议**。

---

## 1. 结论先行

| 问题 | 结论 |
|---|---|
| 三张资产表是否核实？ | **是**。`evoflow_artifacts:schema.py:247`、`evoflow_media_assets:807`、`evoflow_org_artifacts:986`，与卡片登记一致 |
| 真实入口是否可证明？ | **是**。§2 共定位 **4 个 HTTP 面**（`/api` 产物面、`/api/media`、`/api/workspaces`、`/api/assets`）+ 1 个无 HTTP 的写入钩子 |
| 本域 Runtime 接点是多少？ | **0**。四个路由文件对 `qagent_runtime` / `agentscope` 的引用数为 **0** |
| 是否产生 Run / Result？ | **否**。本域没有任何代码调用 `create_run` / `start_run` / 事件回写；`evoflow_media_assets.status` 的 `processing` 是**媒体工具自身的轮询态**，不是 Runtime Run 状态 |
| 是否有需要澄清的边界？ | **有 1 处易混淆**：`/api/assets` 是 **Entity Asset Hub**（企业实体资产中枢），与 `evoflow_artifacts` **不是同一件事**。见 §2.4 |
| 设备命令 / 回执协议 / 设备身份模型 | **全部保持未知**（§6） |

---

## 2. 真实用户入口（HTTP）

### 2.1 会话产物面：`/api` + 线程路径（`backend/app/gateway/routers/artifacts.py`）

- 定义：`artifacts.py:15` —— `APIRouter(prefix="/api", tags=["artifacts"])`。
- 唯一端点：`GET /threads/{thread_id}/artifacts/{path:path}`（`artifacts.py:80-82`，`summary="Get Artifact File"`）。
- 鉴权：`require_thread_visible`（`artifacts.py:9`）。
- 安全约束：`ACTIVE_CONTENT_MIME_TYPES`（`artifacts.py:17-21`）对 `text/html` / `application/xhtml+xml` / `image/svg+xml` 等主动内容做特殊处理；
  支持 zip 打包与 `quote` 编码路径（`artifacts.py:3-5` 导入）。
- **这是产物（artifact）的唯一 HTTP 读出口**；写入不经 HTTP，见 §4。

### 2.2 媒体资产面：`/api/media`（`backend/app/gateway/routers/media_assets.py`）

- 定义：`media_assets.py:13` —— `APIRouter(prefix="/api/media", tags=["media"])`。
- 唯一端点：`GET /assets`（`media_assets.py:37`，`response_model=MediaAssetsListResponse`）。
- **只读**：无写入端点。

### 2.3 工作区 / 文件面：`/api/workspaces`（`backend/app/gateway/routers/workspaces.py`）

- 定义：`workspaces.py:23` —— `APIRouter(prefix="/api/workspaces", tags=["workspaces"])`。
- 鉴权：`require_workspace_root_access`、`require_workspace_path_visible`（`workspaces.py:9`）。
- **23 个端点**，按职责分四组：

| 组 | 端点（行号） |
|---|---|
| 浏览 / 解析 | `GET /browse:589`、`GET /resolve-target:624`、`GET /resolve:1106`、`GET "":1135` |
| 文件读写 | `GET /serve-file:656`、`GET /read:674`、`POST /read-file:686`、`POST /write-file:739`、`POST /delete-file:699`、`POST /mkdir:777` |
| 索引 | `GET /index-status:846`、`POST /index-warm:865`、`POST /index-build:883`、`POST /index-file:913`、`POST /index-watch:941`、`POST /index-watch/stop:957`、`GET /index-watch:973`、`GET /find-files:991`、`GET /search:1024` |
| 用户历史 | `GET /user-history:1076`、`PUT /user-history:1082`、`POST /user-history/remove:1095` |

- **写类端点存在**：`PUT /file`（`workspaces.py` 同族）、`POST /write-file:739`、`POST /delete-file:699`、`POST /mkdir:777`。
  这是本域唯一的"用户可见写路径"。

### 2.4 企业实体资产面：`/api/assets`（`backend/app/gateway/routers/assets.py`）—— **必须与 §2.1 区分**

- 定义：`assets.py:20` —— `APIRouter(prefix="/api/assets", tags=["assets"])`；
  模块 docstring 明确为 **"Entity Asset Hub Gateway API"**（`assets.py:1`）。
- 22 个端点：`GET /entities:44`、`GET /search:62`、`GET /tree:101`、`GET /file:122`、`PUT /file:143`、`DELETE /file:157`、
  `POST /record:212`、`POST /episode:232`、`POST /journal:252`、`POST /craft/save:273`、`GET /profile:293`、`PUT /profile:311`、
  `POST /migrate:330`、`POST /pack/export:374`、`POST /pack/import:388`、`GET /craft:420`、`POST /craft/promote:442`、
  `POST /apply:462`、`GET /stats:483`、`POST /phase2/consolidate:503`、`POST /phase2/scan:520`、`POST /init:536`。
- 实体类型由 `FileWriteBody.entityType` 限定为 `user | agent | employee | workspace`（`assets.py:31`）。
- 鉴权：`require_org_admin`、`resolve_asset_entity_for_request`、`filter_asset_entities_for_request`（`assets.py:12-16`）。

**边界判断（本卡的核心澄清）**：`/api/assets` 服务的是**企业实体资产中枢**（实体 → 档案 / 文件 / 画像 / 资产包），
与 §2.1 的 `evoflow_artifacts`（**会话产物**：`session_key` + `thread_id` + `artifact_id`）是两套不同对象。
A02 若实例化，**不得**把二者合并为"资产域"一个概念。

---

## 3. 旧事实源（SQLite，仅引用源码可见字段）

### 3.1 `evoflow_artifacts`（`schema.py:247-255`）

会话产物表，主键 `PRIMARY KEY (session_key, thread_id, artifact_id)`：

| 列 | 类型 / 默认 | 说明 |
|---|---|---|
| `session_key` | TEXT NOT NULL | 会话键（与 Chat / Task Center 同源） |
| `thread_id` | TEXT NOT NULL | 线程 |
| `artifact_id` | TEXT NOT NULL | 产物 id |
| `path` | TEXT NOT NULL | 路径 |
| `name` | TEXT NOT NULL | 名称 |
| `updated_at` | TEXT NOT NULL | — |
| `type` | TEXT DEFAULT `'file'` | — |
| `url` | TEXT DEFAULT `''` | — |
| `mime` | TEXT DEFAULT `''` | — |
| `label` | TEXT DEFAULT `''` | — |
| `size` | INTEGER（可空） | — |
| `content` | TEXT（可空） | — |
| `status` | TEXT DEFAULT `'new'` | — |
| `created_at` | TEXT（可空） | — |
| `meta_json` | TEXT DEFAULT `'{}'` | 扩展 |

索引：`idx ... ON evoflow_artifacts(session_key, updated_at)`（`schema.py:1620`）。

### 3.2 `evoflow_media_assets`（`schema.py:807-822`）

媒体工具输出表：

| 列 | 类型 / 默认 | 说明 |
|---|---|---|
| `id` | INTEGER PK AUTOINCREMENT | — |
| `thread_id` | TEXT（可空） | — |
| `tool_name` | TEXT NOT NULL | 产生该资产的工具 |
| `media_kind` | TEXT NOT NULL | 媒体种类 |
| `provider` | TEXT（可空） | 外部 provider |
| `task_id` | TEXT（可空） | 外部任务 id（**非 Runtime task_id**） |
| `status` | TEXT DEFAULT `'processing'` | **媒体工具自身状态**，非 Runtime 状态 |
| `remote_url` | TEXT（可空） | 远端地址 |
| `local_path` | TEXT（可空） | 本地路径 |
| `file_size_bytes` | INTEGER（可空） | — |
| `meta_json` | TEXT DEFAULT `'{}'` | — |
| `created_at` / `updated_at` | TEXT NOT NULL | — |

索引：`(media_kind, created_at DESC)`（`:1738`）、`(provider, task_id)`（`:1741`）、`(thread_id, id DESC)`（`:1744`）。

### 3.3 `evoflow_org_artifacts`（`schema.py:986-993`）

组织资产注册表（关联表，不存内容）：

| 列 | 类型 | 说明 |
|---|---|---|
| `id` | INTEGER PK AUTOINCREMENT | — |
| `org_instance_id` | TEXT NOT NULL | 组织实例 |
| `artifact_type` | TEXT NOT NULL | 资产类型 |
| `artifact_id` | TEXT NOT NULL | 资产 id |
| `created_at` | TEXT NOT NULL | — |

唯一约束 `UNIQUE(org_instance_id, artifact_type, artifact_id)`（`schema.py:992`）；
索引 `idx_org_artifacts_instance ON evoflow_org_artifacts(org_instance_id)`（`schema.py:1858`）。

**结构事实**：该表**只有引用**（type + id），没有内容列 —— 它是"某组织实例持有哪些资产"的注册表，
与 `evoflow_artifacts` / `evoflow_media_assets` 通过 `artifact_id` 松散关联（无外键证据）。

---

## 4. 写路径

| 事实源 | 写点 | 证据 |
|---|---|---|
| `evoflow_artifacts` | `upsert_artifacts:20`、`save_artifacts:207`、`delete_artifacts:238`（`persistence/artifact_repositories.py`）；`DELETE FROM evoflow_artifacts WHERE session_key = ?`（`persistence/chat_session_service.py:202`） | 模块 docstring："SQLite CRUD for `evoflow_artifacts` (session-bound, append/upsert)"（`artifact_repositories.py:1`） |
| `evoflow_media_assets` | `record_media_asset:49`、`update_media_asset_by_task:95`（`persistence/media_assets.py`）；`community/media_generation/asset_recorder.py`（docstring："Best-effort persistence hooks for media tools → `evoflow_media_assets`"） | `media_assets.py:1`、`asset_recorder.py:1` |
| `evoflow_org_artifacts` | `INSERT OR IGNORE INTO evoflow_org_artifacts`（`organizations/registry.py:114`）；读 `registry.py:36`、`:141` | — |
| 工作区文件（非 SQLite） | `POST /write-file:739`、`POST /delete-file:699`、`POST /mkdir:777`、`PUT /file:143`（Entity Hub） | `workspaces.py`、`assets.py` |

**读点**：`list_session_artifacts:113`、`load_artifacts:227`（`artifact_repositories.py`）、
`list_media_assets:155`、`find_remote_url_for_local_path:214`（`media_assets.py`）。

---

## 5. Runtime 接点与 Run / Result 边界

### 5.1 Runtime 接点 = **0**（显式结论）

```
$ rg -n "qagent_runtime|agentscope" \
    backend/app/gateway/routers/artifacts.py backend/app/gateway/routers/assets.py \
    backend/app/gateway/routers/media_assets.py backend/app/gateway/routers/workspaces.py | wc -l
0
```

本域**没有**任何 `create_run` / `start_run` / `request_cancel` / 事件回写 / linkage / opt-in 开关。

### 5.2 Run / Result 边界（卡片要求逐项确认）

| 问题 | 结论 | 证据 |
|---|---|---|
| 资产是否被某个真实业务入口引用？ | **是**，且是 4 个入口（§2.1~§2.4） | 各 router 定义行 |
| 资产是否产生 Run？ | **否**。没有任何入口创建 Runtime Run | §5.1 |
| 资产是否产生 Result？ | **否**。`evoflow_artifacts` / `evoflow_media_assets` 是独立 SQLite 表，不写 `qagent_runs.result_payload` | `models.py:21`（Runtime 侧）与 §3（本域侧）无交叉引用 |
| 资产是否有外部回执？ | **未知**。`evoflow_media_assets.provider` / `remote_url` 暗示外部 provider，但**无回执协议证据** | §6 |
| Runtime 侧是否已有资产概念？ | **是**。`qagent_run_assets`（`backend/app/qagent_runtime/models.py:83-94`：`asset_id` / `run_id` / `asset_type` / `uri` / `name` / `content_type` / `metadata` / `created_at`）+ 事件 `asset.available`（`events.py:25`） | 仅登记两侧**都存在**资产概念；**不声称**二者语义等价 |
| 两侧是否有映射证据？ | **无**。本卡未找到任何把 `evoflow_*` 资产写入 `qagent_run_assets` 的代码 | §5.1 |

---

## 6. 未知项（必须保持未知，不得补猜）

卡片明确要求以下各项保持未知，本卡遵守：

| # | 未知项 | 本卡核实到的**最接近**的证据（仅登记，不推论） |
|---|---|---|
| U1 | 统一设备命令实体 | **无**。全仓未找到设备命令实体定义 |
| U2 | 设备命令表 | **无**。`schema.py` 中无设备 / device 命令表证据 |
| U3 | 回执协议 | **无**。仅 `evoflow_media_assets.provider` / `remote_url` / `status` 三个字段暗示外部交互，无协议定义 |
| U4 | 设备身份模型 | **无**。`qagent_runs.executor_key` 默认值 `qagent.server.no_device.v1`（`models.py:24`）**反而说明当前执行器是"无设备"语义**，与设备身份模型不是一回事 |
| U5 | `evoflow_media_assets.status` 的完整取值 | 只知默认 `processing`（`schema.py:812`）；全集未穷举 |
| U6 | `evoflow_artifacts.status` 的完整取值 | 只知默认 `new`（`schema.py:257`）；全集未穷举 |
| U7 | `evoflow_org_artifacts.artifact_type` 的取值域 | 无枚举定义 |
| U8 | `/api/assets` 的 `entityType` 实体与 `evoflow_agents` / `evoflow_proactive_roles` 的关系 | `assets.py:31` 只给出四个字面量 `user/agent/employee/workspace`，与 Agent 域的 `agent_code` 是否同键未核实 |
| U9 | 工作区索引（`index-*` 端点）的事实源 | `workspaces.py:14` 导入 `workspace_repositories as ws_repo`，但本卡未展开其表 |
| U10 | 本域是否有 Runtime 迁移价值 | **未知**。资产是"产物"而非"执行"，是否应进 Runtime 属产品决策，不是本卡能判断的 |

---

## 7. 停止条件检查

| 卡片停止条件 | 本卡是否触发 |
|---|---|
| 需要设计统一设备命令 / 回执协议 | **否**。U1~U4 全部保持未知，未设计 |
| 需要 schema / migration / Runtime 代码 | **否**。未修改任何文件 |
| 检索结果不足以证明真实入口 | **否**。§2 定位 4 个 HTTP 面（共 47 个端点）+ 1 个无 HTTP 写钩子，均有 `文件:行` 证据 |

## 8. A02 是否有证据（结论：**不足以实例化 A02**）

与 Agent / Goal 域不同，本卡**不建议**实例化 `AG-G2-ASSET-002-A01`（字段级映射），理由：

1. **本域没有单一"业务对象"可映射**。四个 HTTP 面服务四个不同对象（会话产物 / 媒体输出 / 工作区文件 / 企业实体资产），
   不存在一个"资产"实体能逐字段对照到 Runtime Run。
2. **两侧资产概念是否等价本身未决**（§5.2 最后一行）。在没有该决策前做字段映射，等于先假定结论。
3. 卡片已明确要求 U1~U4 保持未知，而这些恰是"资产 → Run / Result / 回执"链路的必要环节。

**建议**：本域维持"仅 A01 证据"，A02~A05 不实例化；若负责人认为资产迁移有价值，
需先给出"Runtime 的 `qagent_run_assets` 与既有三张资产表的关系"这一产品 / 契约决策。

## 9. 本卡是否产生代码改动

**否**。只写了 `artifacts/acceptance/agentscope-runtime/AG-G2-ASSET-001-A01/20260915/` 下的
`inventory.md` 与 `acceptance-rg.txt`。未修改任何源代码、测试、schema、migration 或计划外文档；
未创建或删除 branch / worktree；未执行任何被任务包禁止的 git 操作。
