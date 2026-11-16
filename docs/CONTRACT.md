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

## 6. 校准版本表（可选）

现场点位的 `scale`/`offset` 会按生效时刻换版。点位可以声明 `calibrations`
校准：一组按登记时刻排序的**校准修订**，每个修订包含若干**版本**，每个版本有
独立 `version_id`、`scale`/`offset` 和互不重叠的 UTC 生效区间（半开区间
`[start, end)`，`end` 为 `null` 表示开放区间）。

```yaml
      - key: line_a_speed
        # ... 常规点位字段 ...
        calibrations:
          - registered_at: "2025-03-01T08:00:00Z"   # 本修订的登记时刻
            versions:
              - version_id: cal-2025-03-a
                scale: 0.1
                offset: 0
                effective:
                  - ["2025-03-01T00:00:00Z", "2025-06-01T00:00:00Z"]
          - registered_at: "2025-06-01T09:30:00Z"   # 较晚登记，替代旧修订的生效区间
            versions:
              - version_id: cal-2025-06-b
                scale: 0.2
                offset: 0
                effective:
                  - ["2025-03-01T00:00:00Z", null]
```

规则：

- 同一修订内所有版本的生效区间必须互不重叠（重叠是契约错误
  `contract_calibration_overlap`）；区间可以留有缺口。
- `version_id` 在该点位的所有修订间唯一；每个修订的 `registered_at` 唯一。
- 较晚登记的修订整体替代旧修订的生效区间映射；旧修订只作为证据保留，
  解析时永远只看最新登记的修订。
- 声明了校准表的点位只在 `replay.ts` 可解析为 UTC 且落入唯一生效区间时
  产出工程值；缺口或不可解析时间戳记为 `calibration_unknown`（无法判定），
  不会用邻近版本猜测。
- 未声明校准表的点位仍使用静态 `scale`/`offset`，行为不变。

## 7. 报告视图授权

顶层可选 `report.views` 声明报告允许展示的业务视图，取值为
`as_recorded`、`as_effective`：

- `as_recorded`：固定展示首次采录时已登记校准得到的结论（原始 `readings`
  行，永不更新）。
- `as_effective`：展示按当前生效期映射重算后的最新派生结论
  （`derived_readings` 表，只追加、可审计）。

缺省时：契约含校准表则两个视图都展示，否则只展示 `as_recorded`（且不输出
校准视图小节）。两个视图都不改变原始 CRC、`raw_value` 或来源文件摘要。

## 8. 契约指纹

批次保存契约文件的原始字节 sha256。即使语义相同，只要文件字节改变（包括注释、空白），指纹就会变化，可能产生新批次并导致续采拒绝。这样做优先保证“输入可追溯”和续采安全，而不是语义级归一化。

## 9. 完整演示契约

`config/points.yaml` 是完整可运行示例，包含：

- 两个设备、九个点位；
- 五种数据类型；
- `AB`、`BA`、`ABCD`、`CDAB`、`BADC`、`DCBA` 全部字节序；
- 缩放与偏移；
- 量程与枚举；
- 一个故意在回放中缺失的点位，用于验证 `missing_response`。

## 10. 不支持的契约能力

- 位域、位掩码、字符串、数组、时间类型。
- 一个响应拆分多个点位，或一个点位跨多个响应。
- 写入、线圈、离散输入寄存器。
- 设备模板继承、变量、外部引用或脚本表达式。
- 图形化编辑与在线校验服务。
