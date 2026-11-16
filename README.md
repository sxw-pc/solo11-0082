# 点位采录台（pointlog）

**一句话定位：** 点位采录台是一个完全离线的 Modbus RTU 风格寄存器帧回放采集工具，把本地网关导出的“请求—应答帧”按声明式点位契约解码、判定质量、幂等写入 SQLite 台账，并输出报告与待重采清单。

## 是 / 不是

**是：**

- 本地 JSONL 回放文件 + YAML 点位契约的离线采集工具。
- 支持请求帧/应答帧结构校验、CRC16/MODBUS 校验和异常应答识别。
- 支持 `int16`、`uint16`、`int32`、`uint32`、`float32` 与确定性字节序解码。
- 生成 SQLite 采集台账、JSON / Markdown / 自包含 HTML 报告、待重采 CSV。
- 支持基于回放文件指纹和行偏移的断点续采、幂等重跑。
- 附带只读本地复核台，仅监听 `127.0.0.1`。

**不是：**

- 不是真实串口通信、Modbus TCP、设备写寄存器或 SCADA 系统。
- 不是实时监控大屏、告警平台、时序数据库或工业物联网平台。
- 不是通用爬虫、站点巡检、RSS/邮件采集器或表单 RPA。
- 不包含真实厂商、真实设备地址、真实凭据或生产数据。

## 离线声明

运行期不发起任何外部网络请求，不下载依赖、不解析外部域名、不发起 DNS 查询。全部回放帧、设备名、点位值和时间戳均为合成 fixture；生成过程不依赖网络或真实时间。只读复核台也只绑定回环地址。

## 快速开始

环境要求：Python 3.10+，已安装 PyYAML 6+ 与 pytest（用于测试）。项目按 `src/` 布局，但仓库根目录的 `pointlog/` 只是把导入路径转到 `src/pointlog/` 的本地包装包，所以无需安装即可运行。

```bash
# 1. 从零生成 fixture、执行采集、生成 SQLite 台账与报告
python -m pointlog demo

# 2. 运行测试
python -m pytest -q

# 3. 启动只读本地复核台（默认 http://127.0.0.1:5000/）
python -m pointlog serve --port 5000
```

如果本机有 GNU Make，也等价于：

```bash
make demo
make test
make serve
```

如需安装为命令：

```bash
pip install -e . --no-build-isolation
pointlog demo
```

默认输出：

```text
fixtures/replay_demo.jsonl      # 确定性合成回放文件
var/pointlog.db                 # SQLite 采集台账
reports/summary.json            # 机器可读报告
reports/summary.md              # 人读摘要
reports/summary.html            # 自包含 HTML 报告
reports/pending_recollect.csv   # 待重采点位清单
reports/interlock_pending.csv   # 联锁未知缺口待复采清单
```

演示 fixture 共 19 行：8 条正常读数、3 条量程/枚举质量异常、1 条异常应答、2 条 CRC 错误、1 条无契约、2 条帧结构问题、1 条重复行，以及 1 个契约中完全缺失的点位。一次完整采集得到 11 条读数记录（其中 8 条 `good`）、12 条问题记录和 7 个待重采点位。

## 回放文件格式（JSONL）

每行一个 JSON 对象。`response_hex` 可以为空字符串，表示请求没有应答；空应答会进入 `missing_response` 问题与待重采清单。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `ts` | string | 是 | 合成 ISO 8601 时间戳，用于读数幂等键与重复检测 |
| `request_hex` | string | 是 | 请求帧十六进制字符串，允许空白；空格不计入奇偶判断 |
| `response_hex` | string | 是 | 应答帧十六进制字符串；允许为空字符串 |
| `source` | string | 是 | 合成来源标识，例如 `demo-gateway` |

请求帧为 8 字节 RTU 风格读请求：从站地址、功能码 `0x03`/`0x04`、起始寄存器、寄存器数量、CRC16。应答帧为从站地址、功能码、字节数、寄存器数据、CRC16；异常应答为从站地址、功能码最高位置 1、异常码、CRC16。

示例行：

```json
{"ts":"2025-03-01T00:00:00Z","request_hex":"010300000002c40b","response_hex":"01030400003044ee","source":"demo-gateway"}
```

真实 fixture 由 `scripts/make_fixtures.py`（或 `python -m pointlog fixtures`）确定性生成，重复生成字节一致。

## 点位契约（YAML）

契约在采集开始前加载并静态校验；任何错误都会在创建批次前失败，并输出稳定错误码。演示契约见 `config/points.yaml`，其中包含全部数据类型和字节序组合。

完整最小示例：

```yaml
version: 1
devices:
  - id: demo-gateway
    label: Demo Gateway (synthetic)
    points:
      - key: line_a_speed
        slave_id: 1
        function: 3
        start: 0
        count: 2
        data_type: uint32
        byte_order: ABCD
        scale: 0.1
        offset: 0
        unit: rpm
        range: [0, 3000]
        description: Synthetic drive speed.
      - key: line_a_mode
        slave_id: 1
        function: 3
        start: 3
        count: 1
        data_type: uint16
        byte_order: AB
        scale: 1
        offset: 0
        unit: ""
        range: [0, 10]
        enum:
          0: idle
          1: run
          2: fault
```

校验规则摘要：

- 设备 ID、点位 key 唯一；点位 key 只允许字母、数字、点、下划线和短横线。
- 从站地址 1–247；功能码仅 3/4；寄存器范围不得超过 65535。
- 16 位类型必须 1 个寄存器并使用 `AB`/`BA`；32 位类型必须 2 个寄存器并使用 `ABCD`/`CDAB`/`BADC`/`DCBA`。
- `scale` 必须有限且非零；`offset`、`range` 必须为有限数值；`range` 上下界有序。
- 同一 `(slave_id, function, start, count)` 只能对应一个点位。
- 枚举键必须是整数，标签必须为非空字符串；浮点类型不允许枚举。

详细 schema 与错误码见 [docs/CONTRACT.md](docs/CONTRACT.md)。

## 字节序约定

寄存器数据在网络字节序（大端）基础上按如下方式还原：

| 字节序 | 32 位线序 | 16 位线序 |
| --- | --- | --- |
| `ABCD` | 原序 | — |
| `CDAB` | 交换寄存器字 | — |
| `BADC` | 每个字内交换字节 | — |
| `DCBA` | 全字节反转 | — |
| `AB` | — | 原序 |
| `BA` | — | 交换两个字节 |

解码使用 `struct` 的显式大端格式，不依赖宿主机默认字节序。工程值计算为 `engineering = raw * scale + offset`。

## 质量码

判定顺序固定，详见 [docs/QUALITY_CODES.md](docs/QUALITY_CODES.md)。CRC 失败优先于量程判定，异常应答优先于解码错误。

| 质量码 | 含义 | 严重级别 | 进入待重采 |
| --- | --- | --- | --- |
| `good` | 帧、CRC、量程、枚举均正常 | info | 否 |
| `bad_frame` | 帧结构错误、截断、字节数不匹配、从站不一致等 | error | 是 |
| `bad_crc_request` | 请求帧 CRC 错误 | error | 是 |
| `bad_crc_response` | 应答帧 CRC 错误 | error | 是 |
| `exception_response` | 设备返回异常应答 | error | 是 |
| `no_contract` | 请求没有匹配的点位契约 | warning | 否 |
| `missing_response` | 契约点位没有可用应答 | warning | 是 |
| `duplicate` | 同批次同设备、同点位、同时间戳重复出现 | warning | 否 |
| `decode_error` | 寄存器负载无法确定性解码 | error | 是 |
| `overflow` | 工程值高于量程上限 | warning | 否 |
| `under_range` | 工程值低于量程下限 | warning | 否 |
| `not_in_enum` | 原始枚举值不在映射表中 | warning | 否 |

## CLI 命令

```bash
python -m pointlog demo [--contract PATH] [--fixture PATH] [--db PATH] [--reports DIR]
python -m pointlog run --replay PATH --contract PATH [--db PATH] [--reports DIR] [--no-report] [--resume] [--stop-after N]
python -m pointlog fixtures [--contract PATH] [--output PATH]
python -m pointlog batches [--db PATH] [--limit N]
python -m pointlog show BATCH_ID [--db PATH]
python -m pointlog interlock BATCH_ID [--db PATH]
python -m pointlog serve [--db PATH] [--port 5000]
```

| 参数 | 说明 |
| --- | --- |
| `--replay` | 回放 JSONL 路径 |
| `--contract` | 点位契约 YAML 路径 |
| `--db` | SQLite 台账路径，默认 `var/pointlog.db` |
| `--reports` | 报告目录，默认 `reports/` |
| `--resume` | 按回放文件指纹与已处理行偏移续采 |
| `--stop-after` | 调试用，处理 N 行后模拟中断，便于验证续采 |
| `--no-report` | 仅入库，不写报告 |

`pointlog interlock BATCH_ID` 打印该批次各规则的三值时长、已证事件与未知缺口；`pointlog show` 也会附带联锁摘要。两者均为只读，不发任何控制命令。

退出码：契约错误 `2`，续采拒绝 `3`，采集/输入错误 `1`/`2`，模拟中断 `130`。

## SQLite 台账

| 表 | 作用 | 关键字段 |
| --- | --- | --- |
| `batches` | 批次元数据与统计 | `id`、回放/契约 sha256、开始/结束时间、状态、行数、有效读数 |
| `frames` | 每行帧级结果 | 批次、行号、请求/应答帧、CRC 标志、点位、质量码、问题码 |
| `readings` | 解码后的点位读数 | 设备、点位、时间戳、原始值、工程值、单位、质量码 |
| `issues` | 问题明细与待重采标记 | 批次、行号、点位、问题码、帧摘要、说明、`pending_collect` |
| `resume_state` | 续采状态 | 回放路径 + sha256、批次、已处理行偏移、总行数 |
| `interlock_intervals` | 三值联锁时间线 | 规则、版本、状态、起止时间、原因、源行号 |
| `interlock_events` | 已证持续联锁事件 | 事件键、起止、峰值、规则版本、源行号、中断原因 |
| `schema_meta` | schema 版本 | `schema_version` |

幂等策略：批次 ID 由回放 sha256 与契约 sha256 确定性生成；同一输入重跑会复用已完成批次，不新增帧或读数；读数在批次内以 `(batch_id, device_id, point_key, ts)` 唯一约束兜底。同批次重复行记为 `duplicate` 问题，不插入第二条读数。

断点续采：每处理一行，帧/读数/问题与 `resume_state.line_offset` 在同一事务提交。`--resume` 校验当前回放 sha256 与状态一致；不一致时拒绝并提示创建新批次。若 `resume_state` 被删除，可从已有帧的最大行号重建偏移。

## 双点位持续联锁

单点读数只能回答“这个点位此刻是否越限”，不能回答“两路可信读数是否在同一时段持续同时越限”。契约可在设备下声明 `interlocks`，由 `src/pointlog/interlock.py` 在采集完成后做**完全离线**的三值时间线评估；不发送任何设备控制命令，也不把未知当成正常。

```yaml
    interlocks:
      - id: line_a_pressure_level    # 规则 ID，全局唯一
        version: 1                   # 规则版本，写入每个事件
        points:
          - {key: line_a_pressure, threshold: 150, direction: above}
          - {key: line_a_level, threshold: 2.5, direction: above}
        min_duration_minutes: 2      # 最短持续门槛
        max_stale_minutes: 5         # 有界保持模式下样本的最大陈旧时间
        sampling: bounded_hold       # 或 strict_simultaneous
```

- 两个 `point_key` 必须属于声明规则的同一设备；比较方向为 `above`（>，可加 `inclusive: true` 变 `>=`）或 `below`（<，`inclusive` 变 `<=`）。
- **只用可信读数**：帧结构、请求/应答 CRC、解码、量程/枚举全部通过且时间戳可解析的 `good` reading；其余带不可信数值的可归属帧（坏 CRC、坏帧、异常应答、溢出、欠量程、解码失败、矛盾值）都是“坏证据”，会阻断保持。空洞应答（`missing_response`）只是“没有新样本”，有界保持可继续到下一样本或陈旧上限。
- **三值区间**：两路同时为真 → `true`；至少一路明确为假 → `false`；其余（含坏证据、矛盾值、样本过期、无同刻样本）→ `unknown`。未知**不计为正常**。
- 同一时刻既有多条互相矛盾的 `good` 值、或可信值与坏帧同刻，该时刻不可判定，标 `unknown` 并指向行号。
- 只有在连续 `true` 覆盖不少于 `min_duration_minutes` 时才产生一条事件；短暂正常值、坏帧、缺口、陈旧到期都会中断持续时钟，两次离散高值不会被相加。
- 事件包含起止时间、两路峰值及峰值时间、规则版本、源 reading 行号、中断原因与中断行号。
- **`bounded_hold`**：各点最近可信样本在陈旧窗口内延续到下一样本、过期或坏证据时刻；不跨越坏帧与明确正常值。
- **`strict_simultaneous`**：只在两点同一时间戳都有可信样本的时刻确认状态，不推断离散观测之间的持续时间，因此正的最短持续门槛下不会宣称事件，而是列出已确认时刻与未知区间。
- 同一 `replay_sha256` / `contract_sha256` 下事件键由规则、版本、语义与起止时间决定；乱序输入、重复行、续采和重跑都会得到相同事件，且以整批替换方式幂等落库，不重复插入。

合成演示的两模式对比：温度 00:00 越限、00:04 恢复正常，压力 00:02 越限、00:06 恢复正常，陈旧上限 5 分钟且持续门槛 2 分钟。`bounded_hold` 给出 00:02—00:04 的联锁事件；`strict_simultaneous` 因无同刻双点样本不宣称事件，并把整个窗口列为未知。两者在同一证据下的结论不同，但都符合各自被授权的采样语义。

## 报告

每次完整采集写入五个文件：

- `reports/summary.json`：固定字段顺序的机器可读汇总，包含批次、指纹、总量、质量码分布、问题分布、按设备/点位统计、待重采清单，以及 `interlock` 三值区间/事件与 `interlock_pending_recollect` 未知缺口。
- `reports/summary.md`：人读摘要，包含同样的表格，并增加“双点位持续联锁”章节。
- `reports/summary.html`：自包含 HTML，内联 CSS，不引用 CDN、外部字体、脚本或图片。
- `reports/pending_recollect.csv`：按设备/点位/问题码聚合的待重采清单（单点质量口径不变）。
- `reports/interlock_pending.csv`：联锁未知缺口待复采清单，单独列出，不与单点质量混算。

同一输入重复运行时，除首次批次时间外报告内容稳定；golden 测试覆盖 JSON 字段顺序、Markdown 关键片段、HTML 无外部资源和 CSV 结构。仓库中的 `reports/` 是一次演示采集的样例输出。

## 只读复核台

```bash
python -m pointlog serve --port 5000
```

- 固定绑定 `127.0.0.1`，不接受自定义外部网卡。
- 批次列表、批次详情、质量码分布、问题明细、问题帧列表、待重采点位。
- 批次详情包含只读的双点位联锁三值汇总、已证事件与未知缺口；契约不可读时该节静默省略。
- 仅实现 GET；POST/PUT/PATCH/DELETE/HEAD/OPTIONS 均返回 `405` 和 `Allow: GET`。
- 未知路径或不存在批次返回 `404`。
- 不提供写入、删除、上传或配置修改接口。
- 页面样式全部内联，无外部资源。

## 测试

```bash
python -m pytest -q
```

覆盖 CRC 已知向量与错误向量、帧结构问题、全部数据类型/字节序、缩放与量程、质量码优先级、契约校验、幂等重跑、模拟中断续采、指纹变化拒绝续采、报告 golden 片段、只读服务路由与只读性。

## 目录结构

```text
pointlog/
  pyproject.toml
  pointlog/                 # 本地包装包，便于未安装时 python -m pointlog
  Makefile
  config/points.yaml
  fixtures/                 # 确定性生成的 JSONL fixture
  src/pointlog/             # 采集、契约、编解码、存储、报告、服务
  scripts/make_fixtures.py  # fixture 生成入口
  tests/                    # pytest
  docs/                     # 设计、契约、质量码说明
  reports/                  # 演示报告样例
  var/                      # 演示 SQLite 台账
```

## 已知限制与不实现范围

- 只处理本地回放文件，不连接真实串口、PLC、网关或任何工业协议网络。
- 不实现 Modbus TCP、MQTT、OPC UA、SNMP、设备发现、写寄存器或线圈控制。
- 不实现实时采集循环、守护进程、告警通知、短信/邮件推送、SCADA 画面或组态编辑。
- 不实现用户登录、权限系统、多租户、云端同步、分布式调度或消息队列。
- 不实现通用网页爬虫、RSS、邮件附件、WARC、表单 RPA。
- 不实现点位契约图形编辑器、设备指纹识别或厂商识别。
- 不提供远程复核台、HTTPS、认证或任何写操作。
- 演示数据仅用于离线验收，不代表任何真实设备、现场或生产环境。
