# 点位契约规范

## 1. 文件级结构

点位契约是 UTF-8 YAML 文件。顶层只识别以下结构：

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
```

| 顶层字段 | 必填 | 规则 |
| --- | --- | --- |
| `version` | 是 | 必须为整数 `1` |
| `devices` | 是 | 非空列表；至少一个设备 |
| `devices[].id` | 是 | 唯一，匹配 `[A-Za-z0-9][A-Za-z0-9_.-]{0,63}` |
| `devices[].label` | 否 | 非空字符串，缺省等于 `id` |
| `devices[].points` | 是 | 非空列表 |

## 2. 点位字段

| 字段 | 类型 | 必填 | 默认 | 规则 |
| --- | --- | --- | --- | --- |
| `key` | string | 是 | — | 全局唯一，匹配 `[A-Za-z0-9][A-Za-z0-9_.-]{0,63}` |
| `slave_id` | integer | 是 | — | 1–247 |
| `function` | integer | 是 | — | 仅 3（读保持寄存器）或 4（读输入寄存器） |
| `start` | integer | 是 | — | 0–65535；`start + count - 1 <= 65535` |
| `count` | integer | 是 | — | 16 位类型必须 1；32 位类型必须 2 |
| `data_type` | string | 是 | — | `int16` / `uint16` / `int32` / `uint32` / `float32` |
| `byte_order` | string | 是 | — | 16 位：`AB` / `BA`；32 位：`ABCD` / `CDAB` / `BADC` / `DCBA` |
| `scale` | number | 否 | `1` | 有限且非零 |
| `offset` | number | 否 | `0` | 有限数值 |
| `unit` | string | 否 | `""` | 单位显示文本 |
| `range` | `[min, max]` | 是 | — | 两个有限数值，`min <= max` |
| `enum` | mapping | 否 | `{}` | 整数原始值 → 非空标签；浮点类型不允许 |
| `description` | string | 否 | `""` | 仅说明，不参与判定 |

`enum` 的键在 YAML 中可写成整数或可转为整数的字符串。实现会归一化为整数。枚举检查针对**原始寄存器值**，不是工程值。

### 2.1 枚举模式时序契约 `mode_state`

普通 `enum` 只把 raw 值映射为标签，单条读数“在映射中”并不能说明状态时序合理。可选的 `mode_state` 为一个枚举点位声明时序语义：

```yaml
        enum:
          0: idle
          1: warmup
          2: run
          3: fault
        mode_state:
          verification: bounded      # hop | bounded
          max_gap_seconds: 900
          min_dwell_seconds:
            1: 120                   # warmup 至少停留 120 秒
          reset_state: 3
          terminal_states: []        # 允许没有出口的状态（终态）
          transitions:
            0: [1, 3]
            1: [2, 3]
            2: [0, 3]
            3: [0]
```

| 字段 | 必填 | 规则 |
| --- | --- | --- |
| `verification` | 否 | `hop`（默认）或 `bounded`；见 §2.2 |
| `transitions` | 是 | 非空映射，源状态 → 目标状态列表；引用值必须已在 `enum` 中声明 |
| `min_dwell_seconds` | 否 | 状态 → 非负整数秒；只对**中间状态**计时 |
| `max_gap_seconds` | 否 | 非负整数，`bounded` 语义下可解释的最大未知间隔 |
| `reset_state` | 否 | 必须既有合法入边又有合法出边；否则拒绝 |
| `terminal_states` | 否 | 允许没有出边的状态列表；与出边互斥 |

启动前（打开 SQLite 之前）静态拒绝：引用不存在 enum 值、非终态没有出口、可达死端、负停留时间、无法被任何入边进入的正停留状态、不在任何合法环上的正停留状态、无入口/无出口的 `reset_state`、非法 `verification`。错误码见 §5。

### 2.2 两种核验语义

- `hop`：逐跳必须观察。相邻两个确证观测若不是直接合法边，即使时间上可行，也判“未证成”并建议查流程日志。
- `bounded`：有界漏采可解释。合法的中间状态路径在 `max_gap_seconds` 内可解释，但仍只判“未证成”，并给出中间状态的可行时间窗口，建议补采，不得直接宣称设备已合法运行。

两种语义在同一输入下可能产生不同的排查/补采决定；对“中间状态最短停留时间无法满足”则都判“明确不可能”。

## 3. 类型、字数量与字节序

| `data_type` | 字节数 | 寄存器数 | 允许字节序 |
| --- | --- | --- | --- |
| `int16` | 2 | 1 | `AB`, `BA` |
| `uint16` | 2 | 1 | `AB`, `BA` |
| `int32` | 4 | 2 | `ABCD`, `CDAB`, `BADC`, `DCBA` |
| `uint32` | 4 | 2 | `ABCD`, `CDAB`, `BADC`, `DCBA` |
| `float32` | 4 | 2 | `ABCD`, `CDAB`, `BADC`, `DCBA` |

字节序以“线上字节序列”命名。32 位四字节记为 `A B C D`：

| 字节序 | 解码前重排 |
| --- | --- |
| `ABCD` | `A B C D` |
| `CDAB` | `C D A B` |
| `BADC` | `B A D C` |
| `DCBA` | `D C B A` |

16 位两字节记为 `A B`：`AB` 保持原序，`BA` 交换。重排后统一按大端解释，因此结果与宿主机平台无关。

浮点负载必须是有限值；`NaN`、`+Inf`、`-Inf` 会产生解码错误。

## 4. 匹配规则

请求帧解析后得到 `(slave_id, function, start, count)`。契约按完全相同的四元组匹配；匹配是 1:1，不允许两个点位声明相同四元组。

- 没有匹配：记录 `no_contract`，不生成读数。
- 匹配到点位但帧结构、CRC 或应答异常：按质量码判定写问题。
- 匹配点位的应答负载字节数必须等于 `count * 2`，否则 `byte_count_mismatch`（质量码 `bad_frame`）。

## 5. 静态校验顺序与错误码

校验在打开 SQLite 之前完成。任何错误都会中止采集，不创建半成品批次。

| 错误码 | 触发条件 |
| --- | --- |
| `contract_read` | 文件不可读 |
| `contract_yaml` | YAML 语法错误 |
| `contract_root` | 顶层不是映射 |
| `contract_version` | `version != 1` |
| `contract_devices` | `devices` 缺失或为空 |
| `contract_device` | 设备不是映射、ID 非法/重复、标签为空、点位列表为空 |
| `contract_point` | 点位不是映射、key 非法、unit/description 类型错误 |
| `contract_slave` | `slave_id` 不在 1–247 |
| `contract_function` | 功能码不是 3/4 |
| `contract_address` | `start` 越界或 `start + count - 1 > 65535` |
| `contract_register_count` | `count` 非正或与数据类型不匹配 |
| `contract_data_type` | 不支持的数据类型 |
| `contract_byte_order` | 字节序非法或与位宽不匹配 |
| `contract_scale` | `scale` 非数值、非有限或为 0 |
| `contract_offset` | `offset` 非有限数值 |
| `contract_range` | `range` 结构错误或 `min > max` |
| `contract_enum` | 枚举不是映射、键非整数、标签为空、浮点类型声明枚举 |
| `contract_mode` | `mode_state` 结构错误、非终态无出口、终态又声明出边、状态引用不存在、`verification` 非法 |
| `contract_mode_enum` | `mode_state` 引用的状态不在同点位 `enum` 中 |
| `contract_mode_dead_end` | 可达状态没有出边且未声明为 `terminal_states` |
| `contract_mode_dwell` | 停留时间为负、无入边、或不在任何合法环上导致不可满足 |
| `contract_mode_reset` | `reset_state` 无声明出边或无法通过任何声明边进入 |
| `contract_mode_gap` | `max_gap_seconds` 不是非负整数 |
| `contract_duplicate_key` | 点位 key 重复 |
| `contract_duplicate_match` | 两个点位使用相同四元组 |

校验按设备顺序、点位顺序执行，错误信息带位置，例如：

```text
[contract_register_count] config/points.yaml:devices[1].points[1]: uint32 requires exactly 2 register(s)
```

## 6. 契约指纹

批次保存契约文件的原始字节 sha256。即使语义相同，只要文件字节改变（包括注释、空白），指纹就会变化，可能产生新批次并导致续采拒绝。这样做优先保证“输入可追溯”和续采安全，而不是语义级归一化。

## 7. 完整演示契约

`config/points.yaml` 是完整可运行示例，包含：

- 两个设备、九个点位；
- 五种数据类型；
- `AB`、`BA`、`ABCD`、`CDAB`、`BADC`、`DCBA` 全部字节序；
- 缩放与偏移；
- 量程与枚举；
- `line_a_mode` 声明 `mode_state` 时序契约（`hop`、warmup 至少 120 秒、`fault` 为复位态）；
- 一个故意在回放中缺失的点位，用于验证 `missing_response`。

## 8. 不支持的契约能力

- 位域、位掩码、字符串、数组、时间类型。
- 一个响应拆分多个点位，或一个点位跨多个响应。
- 写入、线圈、离散输入寄存器。
- 设备模板继承、变量、外部引用或脚本表达式。
- 图形化编辑与在线校验服务。
- 跨点位的联合状态机、概率/统计模型或从数据自动学习状态图。

## 9. 枚举模式时序复核

`mode_state` 只声明语义，不改变任何单条读数的质量码。`pointlog.mode_state`
在报告阶段读取**已完成批次**中 CRC、解码、量程、枚举全部合格且时间戳可解析的
读数，按时间戳（而非文件行序）构造每点位时间线，并输出：

| 证据等级 | 含义 |
| --- | --- |
| `direct` | 两个确证观测由直接声明的合法边连接（或同一状态） |
| `unproven` | 存在可行假设路径，但中间状态未被观测，不能证明合法运行 |
| `impossible` | 状态图或最短停留时间不存在任何满足 `elapsed` 的路径 |
| `unknown` | 坏帧、异常、同时间戳冲突或超长缺口形成的未知段 |

规则：

- 同时间戳出现不同状态时，该时刻的观测全部降级为未知，不用行号强行排序。
- 连续多行坏帧、异常与超长缺口合并为**一条**未知段，不拆成多个故障。
- 复位只在观测到的最后一跳是“声明边 → `reset_state`”时才标记 `via_reset`。
- 原始 `readings`、`issues` 与质量码保持不变；报告可重复生成且字节稳定。
- 复核只接受 `status=completed` 的批次；未完成批次直接拒绝（`batch_not_completed`）。
