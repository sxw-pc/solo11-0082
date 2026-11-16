# 点位采录台设计说明

## 1. 边界与设计目标

点位采录台只处理**本地已导出的请求—应答帧回放文件**和**声明式点位契约**，不打开串口、不连接现场网络、不写设备寄存器。设计目标按优先级排列：

1. 离线与确定性：同样输入得到同样台账内容与报告内容（批次时间除外）。
2. 可解释：每一行回放都有帧级结果、问题码和可读说明。
3. 可恢复：进程中断后能从已提交的行偏移继续，且不重复写入。
4. 可验收：模块边界清晰、单文件小、测试覆盖关键质量顺序和失败路径。

## 2. 模块边界

| 模块 | 职责 | 明确不负责 |
| --- | --- | --- |
| `cli.py` | 参数解析、错误码映射、命令输出 | 业务解码、SQL |
| `fixtures_gen.py` | 确定性生成合成回放 | 采集、报表 |
| `replay.py` | JSONL 指纹、逐行解析、字段校验 | 帧语义、契约匹配 |
| `modbus.py` | 请求/应答帧结构、CRC 标志、异常帧 | 工程值、量程 |
| `crc.py` | CRC-16/MODBUS | 帧结构 |
| `contract.py` | YAML 契约 schema 与静态校验 | 帧解析 |
| `codec.py` | 寄存器字节序、类型、缩放、枚举 | 数据库、质量优先级 |
| `quality.py` | 质量码目录、优先级、待重采属性 | I/O |
| `lineproc.py` | 单行帧级到读数级处理 | 批次生命周期 |
| `pipeline.py` | 批次、续采、提交、完成/失败状态 | HTTP、报告排版 |
| `store.py` | SQLite schema、事务写入、续采状态 | 聚合展示 |
| `store_queries.py` | 报告/复核台只读聚合 | 写操作 |
| `recollect_plan.py` | 离线重采计划：请求合并、预算选择、调度与阻断原因 | 连接设备、发送帧、写操作 |
| `report*.py` | JSON/Markdown/HTML/CSV 渲染 | 采集 |
| `server.py` | 回环只读复核台 | 写接口、远程绑定 |
| `resume.py` | 指纹校验、偏移重建 | 帧处理 |

依赖方向：`cli -> pipeline -> lineproc -> modbus/codec/contract/quality`，`pipeline -> store/resume/report`。底层模块不反向依赖 CLI 或 HTML。

## 3. 数据流

```text
contracts YAML ──静态校验──┐
                           v
replay JSONL ──sha256──> pipeline ──逐行──> lineproc
     │                       │               │
     │                       │               ├─ CRC / 结构 / 异常 → issues
     │                       │               ├─ 字节序 / 类型 / 缩放 → readings
     │                       │               └─ 量程 / 枚举 / 重复 → quality
     │                       v
     └──────────────> batches + frames + readings + issues
                             │
                             ├─ resume_state（每行同事务更新）
                             └─ summary.json/md/html + pending_recollect.csv
```

处理顺序：

1. 加载契约并静态校验。失败立即退出，数据库不建立批次。
2. 读取回放文件并计算 sha256、统计总行数。
3. 计算批次 ID，建立或复用批次；续采时校验状态。
4. 从偏移开始逐行处理。每行构造帧级记录、问题列表和可选读数。
5. 单行事务提交：`frames` + `issues` + `readings` + `resume_state` 一起提交。
6. 全部行完成后补写缺失契约点位，更新批次统计为 `completed`。
7. 从只读聚合查询生成报告和待重采清单。

## 4. 批次 ID 与幂等键

批次 ID 为：

```text
b_<replay_sha256[:12]>_<contract_sha256[:12]>
```

同一回放内容 + 同一契约内容必然得到同一批次 ID。再次运行同一命令时：

- 若批次已 `completed`，直接复用，不重新解析回放行，不新增帧/读数。
- 若批次为 `partial`/`failed`，普通运行拒绝并要求使用 `--resume`，避免静默覆盖半成品。
- 若回放或契约内容变化，sha256 变化，产生新批次 ID。

读数表约束：

```text
UNIQUE (batch_id, device_id, point_key, ts)
```

批次 ID 已消除“同文件重跑”的重复；该约束再消除“同批次重复行”和潜在并发重复。重复行仍保留帧记录与 `duplicate` 问题，但不插入第二条读数。

## 5. 事务与断点续采状态机

**状态：** `partial`（初始/中断）、`completed`（全部行完成）、`failed`（意外异常）。

```text
新建批次 ──> partial ──全部行完成──> completed
                │
                ├──意外异常──> failed
                │
                └──--resume──> partial（继续）──> completed
```

每处理一行：

1. `INSERT OR IGNORE` 帧记录；
2. 写问题明细；
3. 若该行产生非重复读数，`INSERT OR IGNORE` 读数；
4. 更新 `resume_state.line_offset = line_no`；
5. 同一 SQLite 事务提交。

因此状态偏移和帧/读数不会出现“偏移已前进但数据未写入”的撕裂。中断后：

- 先用当前回放路径 + 当前 sha256 查状态。
- 若状态存在：校验状态中的批次、批次 sha、契约 sha、偏移范围；偏移落后于已有帧最大行号时自动修复到最大行号。
- 若状态缺失：查找该路径最近批次，校验回放 sha 与契约 sha，使用 `MAX(frames.line_no)` 重建偏移。
- 路径上存在旧 sha 的状态、但当前 sha 不同：拒绝续采，错误码 `replay_changed`。
- 契约变化：拒绝续采，错误码 `contract_changed`。
- 没有状态也没有历史批次：拒绝，错误码 `no_resume_state`。

续采从偏移之后的行开始，已提交行不会重复处理。完成后统计与 `resume_state` 同时更新。

## 6. 缺失点位判定

“契约中存在但回放中没有响应”分两种情况：

- 行内有匹配请求但 `response_hex` 为空：该行写 `missing_response` 问题。
- 整个回放中从未出现能匹配该契约点的请求：批次完成后以 `line_no=0` 补写 `missing_response` 问题。

为了在续采后仍能正确判断“是否出现过请求”，`frames.contract_matched` 和 `point_key` 会持久化；续采开始时从已提交帧恢复已见点位集合。

## 7. 质量判定与问题记录

`quality.py` 的 `determine_quality()` 是唯一质量判定入口。`frames.quality_code` 表示该行的最终质量；`readings.quality_code` 保存实际写入读数的质量。`issues` 同时保存更具体的问题码（如 `truncated_frame`、`slave_mismatch`）与对应的质量问题码（如 `bad_frame`）。

报告的质量码分布 = 帧级质量分布 + `line_no=0` 的补写缺失点位问题。这样既不会把同一行的读数与问题重复计数，也不会漏掉没有帧的契约缺失点位。

完整顺序见 [QUALITY_CODES.md](QUALITY_CODES.md)。

## 8. 确定性策略

- fixture 使用固定种子式常量与固定基础时间，不使用随机数和当前时间。
- 浮点解码显式使用大端 `struct` 格式，不依赖宿主机字节序。
- 工程值统一保留 6 位小数后转为稳定数字。
- 报告设备按契约顺序，点位按 `(device_id, point_key)` 排序，待重采按 `(device, point, issue)` 排序。
- JSON 使用固定字段插入顺序；质量码分布按目录顺序输出。
- 报告不含生成时刻，只有批次开始/结束时间；复用批次时报告字节稳定。

## 9. 只读复核台

`server.py` 使用标准库 `http.server`，`create_server()` 固定绑定 `127.0.0.1`。每个 GET 请求打开 SQLite `mode=ro` 连接；路由只接受：

- `/`
- `/batch/<batch_id>`

未知路径 404；任何非 GET 方法 405 且返回 `Allow: GET`。页面 CSS 内联，无脚本、无外部资源，不包含任何写入/删除/上传接口。

## 10. 明确取舍

- 一个请求只匹配一个契约点：使用 `(slave_id, function, start, count)` 精确匹配，避免“一个多寄存器响应拆多个点位”的歧义。
- 重复读数不插入，而不是插入一条 `duplicate` 读数，以保持读数表幂等性；`duplicate` 仍出现在帧质量、问题表和待重采之外的分布中。
- 溢出/欠量程/枚举异常读数仍写入台账，便于审计；它们不进入待重采，因为帧和 CRC 正常，问题在数值解释或量程配置。
- 不实现真实设备协议、实时循环、告警和写操作。

## 11. 离线重采计划

`recollect_plan.py` 把“待重采清单”变成一份可复核的离线轮询计划。它只生成计划：不打开串口、不连接设备、不声称任何帧已发送或读数已成功。输入是一个**已完成批次**的待重采问题（`pending_collect = 1`）、**当前点位契约**和契约 `recollect` 段给出的总线窗口预算；契约缺少 `recollect` 段时报告保持旧格式，不生成计划。

**逻辑义务与物理请求分别计数。** 计划先把同一 `(device_id, point_key)` 的多个问题去重成一条逻辑重采义务，再把义务合并成物理请求。重叠的点位契约（同一物理寄存器被不同逻辑点位引用）共享同一个物理请求：预算只计一次请求，覆盖的义务按点位逐个计数。

**合并规则。** 仅同一 `slave_id` 且同一功能码的相邻寄存器可以合并；合并后的请求跨度不得超过 125 寄存器；跨越的“空洞”寄存器必须全部属于同从站/同功能码的契约点位——未映射寄存器是禁止空洞（读取可能触发异常应答，导致整批覆盖点位失败）。空洞中非待重采的契约点位可以被物理覆盖，但只记为 `incidental_points`，绝不计为重采目标；仅因 `duplicate`、越程等不进入待重采的读数同样不会成为目标。

**预算与选择。** 总线窗口由 `max_requests`、`device_cooldown_s`、`request_cost_s` 和可选的 `window_seconds` 描述。请求在单条总线上串行调度，同一从站的相邻请求至少间隔一个冷却时间；超出窗口的请求被阻断。预算不足时在可行候选集合中按业务授权目标选择，而不是简单截断待重采列表：

- `priority`：优先覆盖最早到期的高优先级点位（点位可声明 `recollect_priority` 与 `recollect_ttl_seconds`，到期时刻 = 批次完成时刻 + 有效期）；
- `coverage`：优先用最少请求覆盖最多不同的待重采点位。

**输出与风险口径。** 计划逐请求给出寄存器跨度、可复核的请求帧（hex）、覆盖点位清单与计划时刻；逐点标明它由哪个请求覆盖，或对每个未覆盖点位给出具体阻断原因（`point_not_in_contract`、`contract_device_mismatch`、`request_budget_exceeded`、`window_time_exceeded`）。任一请求失败会同时影响其覆盖的全部点位，因此风险统计使用**独立失败机会数**（= 选中请求数），而不是把覆盖点位当作多个独立机会。`report_stats.collect_summary()` 在契约启用规划时把该计划嵌入 `summary["recollect_plan"]`，Markdown/HTML 报告同步渲染；CLI `pointlog plan <batch_id>` 可直接查看（`--json` 输出机器可读计划）。

**确定性。** 相同批次、契约和预算产生字节稳定的计划：到期时刻以批次完成时刻为锚，所有排序键显式固定，不使用当前时间或随机数。
