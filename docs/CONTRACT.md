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
| `devices[].interlocks` | 否 | 双点位持续联锁规则列表，默认空 |

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
| `contract_duplicate_key` | 点位 key 重复 |
| `contract_duplicate_match` | 两个点位使用相同四元组 |

校验按设备顺序、点位顺序执行，错误信息带位置，例如：

```text
[contract_register_count] config/points.yaml:devices[1].points[1]: uint32 requires exactly 2 register(s)
```

## 6. 双点位联锁规则

`interlocks` 可选，声明在设备下，引用该设备自己的点位。每条规则：

```yaml
    interlocks:
      - id: line_a_pressure_level
        version: 1
        points:
          - {key: line_a_pressure, threshold: 150, direction: above}
          - {key: line_a_level, threshold: 2.5, direction: above}
        min_duration_minutes: 2
        max_stale_minutes: 5
        sampling: bounded_hold
        description: 可选说明，不参与判定
```

| 字段 | 类型 | 必填 | 规则 |
| --- | --- | --- | --- |
| `id` | string | 是 | 全局唯一，匹配 `[A-Za-z0-9][A-Za-z0-9_.-]{0,63}` |
| `version` | integer | 否 | 默认 1；正整数，写入每个事件 |
| `points` | list | 是 | 恰好两个；`key` 必须是同设备点位且互不相同 |
| `points[].threshold` | number | 是 | 有限数值，与 engineering value 比较 |
| `points[].direction` | string | 否 | `above`（默认）或 `below` |
| `points[].inclusive` | bool | 否 | 默认 false；true 时 `above`→`>=`、`below`→`<=` |
| `min_duration_minutes` | number | 是 | 正有限数；连续同时为真的最短时长 |
| `max_stale_minutes` | number | 是 | 正有限数；有界保持模式下样本最大有效年龄 |
| `sampling` | string | 否 | `bounded_hold`（默认）或 `strict_simultaneous` |
| `description` | string | 否 | 说明文本 |

采样语义授权：

- `bounded_hold`：最近可信样本在 `max_stale_minutes` 内延续，直到下一样本、过期或坏证据；不跨越坏帧和明确正常值。
- `strict_simultaneous`：只在两点同一时间戳均有可信样本时确认；不推断离散观测之间的持续时间，因此正门槛下不会产生持续事件，而是给出已确认时刻和未知区间。

新增错误码：

| 错误码 | 触发条件 |
| --- | --- |
| `contract_interlock` | 规则非映射、id/version 非法、points 数量错误、description 非字符串 |
| `contract_interlock_threshold` | 阈值为非有限数值 |
| `contract_interlock_direction` | 方向非法或 inclusive 非布尔 |
| `contract_interlock_point` | 点位未知、跨设备、或两个点位相同 |
| `contract_interlock_duration` | `min_duration_minutes` 非正/非有限 |
| `contract_interlock_stale` | `max_stale_minutes` 非正/非有限 |
| `contract_interlock_sampling` | 采样语义非法 |
| `contract_duplicate_interlock` | 规则 id 重复 |

## 7. 契约指纹

批次保存契约文件的原始字节 sha256。即使语义相同，只要文件字节改变（包括注释、空白），指纹就会变化，可能产生新批次并导致续采拒绝。这样做优先保证“输入可追溯”和续采安全，而不是语义级归一化。

## 8. 完整演示契约

`config/points.yaml` 是完整可运行示例，包含：

- 两个设备、九个点位；
- 五种数据类型；
- `AB`、`BA`、`ABCD`、`CDAB`、`BADC`、`DCBA` 全部字节序；
- 缩放与偏移；
- 量程与枚举；
- 一个故意在回放中缺失的点位，用于验证 `missing_response`。

## 9. 不支持的契约能力

- 位域、位掩码、字符串、数组、时间类型。
- 一个响应拆分多个点位，或一个点位跨多个响应。
- 写入、线圈、离散输入寄存器。
- 设备模板继承、变量、外部引用或脚本表达式。
- 图形化编辑与在线校验服务。
