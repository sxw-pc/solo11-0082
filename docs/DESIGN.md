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
| `report*.py` | JSON/Markdown/HTML/CSV 渲染 | 采集 |
| `server.py` | 回环只读复核台 | 写接口、远程绑定 |
| `resume.py` | 指纹校验、偏移重建 | 帧处理 |
| `interlock.py` | 双点位三值时间线与持续事件（离线、只读推导） | 设备控制、实时告警 |

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

## 8. 双点位持续联锁

`interlock.py` 在批次完成后，基于已入库的 `readings` 与 `frames` 重新推导，不改变单点 reading、quality 码或待重采口径。

**证据来源**

- 可信样本：`quality_code = 'good'` 且 `ts` 可按 ISO-8601 解析的 reading。`good` 已蕴含帧结构、双端 CRC、解码、量程与枚举全部通过。
- 坏证据（阻断保持）：同一规则点位上带不可信数值的可归属帧（坏 CRC、截断、从站/功能码不一致、异常应答、解码失败、溢出、欠量程、未入枚举）。`duplicate` 若与已存值矛盾，问题码记为 `contradictory_value`，同样算坏证据。
- 空洞应答（`missing_response`）不同于坏值：它只是“没有新样本”，不会立即切断仍有界有效的保持，保持可延续到下一样本或陈旧上限。
- 同刻矛盾：同一 `(device, point, ts)` 出现不同 engineering value，或可信值与坏帧同刻，该时刻判为冲突，不可判安全或越限。

**三值合成**

区间边界由样本时刻、样本过期时刻与坏证据时刻共同构成。每个子区间起点取两路各自“当前有效样本”，单点为：

- 无样本、超过 `max_stale`、或样本之后出现坏证据 → 未知；
- 否则按阈值给出真/假。

两路合成使用三值 AND：任一明确为假 → `false`；两路都为真 → `true`；其余 → `unknown`。未知区间原因取 `bad_evidence`、`conflicting_values`、`stale_evidence` 或 `insufficient_evidence`，并列出支撑/矛盾行号。

**事件与中断**

仅当 `true` 区间连续覆盖 ≥ `min_duration_minutes` 时产生事件。事件记录起止、两路峰值及峰值时刻、规则版本、源 reading 行号；中断原因按优先级取明确正常、冲突、坏证据、陈旧或到达证据末端。短暂正常值会切出独立区间，坏帧/缺口使持续时钟中断，因此两次离散高值不会被相加。

**两种授权语义**

- `bounded_hold`：按上述保持逻辑推进。
- `strict_simultaneous`：只在两点同刻样本处取状态，区间时长为 0；其余时间标 `unknown / no_simultaneous_sample`。持续门槛为正时不会产生事件，但已确认时刻仍被记录。

**幂等与确定性**

- 事件键 `ev_<sha256>` 由 `replay_sha256 | contract_sha256 | rule_id | version | sampling | start | end` 决定。
- 评估是批次行的纯函数；落库时在同一事务内先删除该批次既有区间/事件再整批写入，因此乱序、重复行、续采尾段与显式重算都得到相同结论且不重复插入。
- 报告与查询不输出 SQLite 自增 `id`，保证重复生成报告字节稳定。

**报告口径**

`interlock` 汇总分规则给出“已证联锁 / 已证正常 / 未知”秒数与事件；`interlock_pending_recollect` 与 `interlock_pending.csv` 单独列出未知缺口，未知不计入正常，也不进入原单点 `pending_recollect` 口径。

## 9. 确定性策略

- fixture 使用固定种子式常量与固定基础时间，不使用随机数和当前时间。
- 浮点解码显式使用大端 `struct` 格式，不依赖宿主机字节序。
- 工程值统一保留 6 位小数后转为稳定数字。
- 报告设备按契约顺序，点位按 `(device_id, point_key)` 排序，待重采按 `(device, point, issue)` 排序。
- JSON 使用固定字段插入顺序；质量码分布按目录顺序输出。
- 报告不含生成时刻，只有批次开始/结束时间；复用批次时报告字节稳定。

## 10. 只读复核台

`server.py` 使用标准库 `http.server`，`create_server()` 固定绑定 `127.0.0.1`。每个 GET 请求打开 SQLite `mode=ro` 连接；路由只接受：

- `/`
- `/batch/<batch_id>`

未知路径 404；任何非 GET 方法 405 且返回 `Allow: GET`。页面 CSS 内联，无脚本、无外部资源，不包含任何写入/删除/上传接口。

## 11. 明确取舍

- 一个请求只匹配一个契约点：使用 `(slave_id, function, start, count)` 精确匹配，避免“一个多寄存器响应拆多个点位”的歧义。
- 重复读数不插入，而不是插入一条 `duplicate` 读数，以保持读数表幂等性；`duplicate` 仍出现在帧质量、问题表和待重采之外的分布中。
- 溢出/欠量程/枚举异常读数仍写入台账，便于审计；它们不进入待重采，因为帧和 CRC 正常，问题在数值解释或量程配置。
- 不实现真实设备协议、实时循环、告警和写操作。
