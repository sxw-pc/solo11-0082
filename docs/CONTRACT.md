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
| `recollect_priority` | integer | 否 | — | 0–9，数值越大越紧急；供重采规划排序 |
| `recollect_ttl_seconds` | number | 否 | — | 正的有限数值；读数有效期，供重采规划计算到期时刻 |

`enum` 的键在 YAML 中可写成整数或可转为整数的字符串。实现会归一化为整数。枚举检查针对**原始寄存器值**，不是工程值。

## 3. 重采规划段（recollect，可选）

顶层可声明一个 `recollect` 映射，启用离线重采计划（见 [DESIGN.md](DESIGN.md) 第 11 节）。缺省时报告保持旧格式，不生成计划。

```yaml
recollect:
  objective: coverage        # priority | coverage
  max_requests: 4            # 总线窗口内允许的最大请求数
  device_cooldown_s: 0.25    # 同一从站两次请求之间的最小间隔（秒）
  request_cost_s: 0.05       # 单个请求占用的总线时间（秒），默认 1.0
  window_seconds: 30         # 总线窗口总时长（秒），可选
```

| 字段 | 类型 | 必填 | 默认 | 规则 |
| --- | --- | --- | --- | --- |
| `objective` | string | 是 | — | `priority`（优先最早到期的高优先级点位）或 `coverage`（最少请求覆盖最多不同点位） |
| `max_requests` | integer | 是 | — | 非负整数 |
| `device_cooldown_s` | number | 是 | — | 非负有限数值 |
| `request_cost_s` | number | 否 | `1.0` | 正的有限数值 |
| `window_seconds` | number | 否 | — | 正的有限数值 |

## 4. 类型、字数量与字节序

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

## 5. 匹配规则

请求帧解析后得到 `(slave_id, function, start, count)`。契约按完全相同的四元组匹配；匹配是 1:1，不允许两个点位声明相同四元组。

- 没有匹配：记录 `no_contract`，不生成读数。
- 匹配到点位但帧结构、CRC 或应答异常：按质量码判定写问题。
- 匹配点位的应答负载字节数必须等于 `count * 2`，否则 `byte_count_mismatch`（质量码 `bad_frame`）。

## 6. 静态校验顺序与错误码

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
| `contract_recollect` | 点位重采优先级/有效期非法，或 `recollect` 段结构、目标、预算、冷却非法 |

校验按设备顺序、点位顺序执行，错误信息带位置，例如：

```text
[contract_register_count] config/points.yaml:devices[1].points[1]: uint32 requires exactly 2 register(s)
```

## 7. 契约指纹

批次保存契约文件的原始字节 sha256。即使语义相同，只要文件字节改变（包括注释、空白），指纹就会变化，可能产生新批次并导致续采拒绝。这样做优先保证“输入可追溯”和续采安全，而不是语义级归一化。

## 8. 完整演示契约

`config/points.yaml` 是完整可运行示例，包含：

- 两个设备、九个点位；
- 五种数据类型；
- `AB`、`BA`、`ABCD`、`CDAB`、`BADC`、`DCBA` 全部字节序；
- 缩放与偏移；
- 量程与枚举；
- 一个故意在回放中缺失的点位，用于验证 `missing_response`；
- 一个 `recollect` 规划段与若干点位的重采优先级/有效期，用于演示离线重采计划。

## 9. 不支持的契约能力

- 位域、位掩码、字符串、数组、时间类型。
- 一个响应拆分多个点位，或一个点位跨多个响应。
- 写入、线圈、离散输入寄存器。
- 设备模板继承、变量、外部引用或脚本表达式。
- 图形化编辑与在线校验服务。
