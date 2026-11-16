# 质量码规范

## 1. 质量码目录

每个帧级结果和每条读数最终只有一个质量码。`issues` 表可以同时保存更具体的问题码，但质量问题码来自下表。

| 质量码 | 含义 | 严重级别 | 进入待重采 |
| --- | --- | --- | --- |
| `good` | 帧结构、CRC、量程、枚举均正常 | info | 否 |
| `bad_frame` | 帧结构错误、截断、长度/字节数不匹配、从站不一致 | error | 是 |
| `bad_crc_request` | 请求帧 CRC 校验失败 | error | 是 |
| `bad_crc_response` | 应答帧 CRC 校验失败 | error | 是 |
| `exception_response` | 功能码最高位置 1 的异常应答 | error | 是 |
| `no_contract` | 请求无法匹配任何点位契约 | warning | 否 |
| `missing_response` | 契约点位没有可用应答 | warning | 是 |
| `duplicate` | 同批次同设备、同点位、同时间戳已存在读数 | warning | 否 |
| `decode_error` | 负载无法按类型/字节序解码，或浮点非有限 | error | 是 |
| `calibration_unknown` | 时间戳不可解析，或未落入任何校准生效区间（无法判定） | warning | 是 |
| `overflow` | 工程值高于量程上限 | warning | 否 |
| `under_range` | 工程值低于量程下限 | warning | 否 |
| `not_in_enum` | 原始值不在枚举映射中 | warning | 否 |

“进入待重采”是报告与 CSV 的输出规则，不影响入库。`overflow`、`under_range`、`not_in_enum` 的帧和 CRC 正常，通常需要核对量程/枚举配置；`duplicate` 不需要重采；`no_contract` 没有可归属点位。

## 2. 唯一判定顺序

`pointlog.quality.determine_quality()` 按以下顺序返回第一个命中项，调用方不得另行实现质量优先级：

```text
1. bad_frame
2. bad_crc_request
3. bad_crc_response
4. exception_response
5. no_contract
6. missing_response
7. duplicate
8. decode_error
9. calibration_unknown
10. overflow
11. under_range
12. not_in_enum
13. good
```

关键要求：

- **CRC 错误优先于量程判定**：CRC 失败时寄存器内容不可信，不能再解释为 `overflow`/`under_range`。
- **异常应答优先于解码错误**：异常帧没有正常数据负载，应在任何解码前终止。
- **结构错误优先于 CRC**：截断或长度错误时通常没有可信的 CRC 字段；`bad_frame` 是最高优先级。
- **重复优先于值质量问题**：同一读数重复出现时不再重复入库，质量记录为 `duplicate`。
- **帧级 CRC/协议问题优先于校准判定**：只有全部协议检查通过的帧才做校准版本解析；校准缺口或不可解析时间戳记为 `calibration_unknown`，不会用邻近版本猜测，也不会对坏帧重算。
- **good 仅在全部检查通过时产生**。

## 3. 问题码与质量码的区别

`frames.quality_code` 表示行的最终质量；`issues.issue_code` 表示具体原因。两者可以相同，也可以不同：

| 场景 | `issue_code` | `quality_code` |
| --- | --- | --- |
| 请求 CRC 失败 | `bad_crc_request` | `bad_crc_request` |
| 应答截断 | `truncated_frame` | `bad_frame` |
| 应答声明字节数不符 | `byte_count_mismatch` | `bad_frame` |
| 从站地址不一致 | `slave_mismatch` | `bad_frame` |
| 响应功能码不匹配 | `function_mismatch` | `bad_frame` |
| 应答数据奇数字节 | `odd_byte_count` | `bad_frame` |
| 请求十六进制非法 | `invalid_hex` / `odd_hex` | `bad_frame` |
| 枚举越界 | `not_in_enum` | `not_in_enum` |
| 量程越界 | `overflow` / `under_range` | 同左 |
| 重复 | `duplicate` | `duplicate` |

问题码还可能包括：`invalid_line`、`invalid_json`、`empty_frame`、`length_mismatch`、`unsupported_function`、`payload_length`、`non_finite`。这些结构/解码问题统一映射到 `bad_frame` 或 `decode_error`。

## 4. 各质量码的判定上下文

### `bad_frame`

在以下任一阶段命中：

- JSONL 行字段缺失或请求/应答十六进制非法；
- 请求不是 8 字节、功能码不支持；
- 应答截断、尾随字节、异常帧长度不是 5、数据字节数为奇数；
- 应答声明数据字节数与请求契约寄存器数量不一致；
- 应答从站地址与请求不一致；
- 应答功能码与请求不匹配。

该行不产生读数。若能匹配到契约，问题进入待重采。

### `bad_crc_request`

请求结构可解析但 CRC 校验失败。优先于 `no_contract` 和一切解码/量程判断。该行不产生读数。若请求四元组匹配到契约，则关联该点位并进入待重采。

### `bad_crc_response`

应答结构可解析但 CRC 校验失败。优先于异常应答识别、字节数语义检查和解码。该行不产生读数，关联点位进入待重采。

### `exception_response`

应答功能码为 `request.function | 0x80`，长度为 5，CRC 正确。问题说明包含异常码和可读名称（如 `2: illegal data address`）。该行不产生读数，进入待重采。

### `no_contract`

请求结构、CRC 正常，但 `(slave_id, function, start, count)` 不在契约中。不产生读数，没有可归属点位，因此不进入待重采。

### `missing_response`

两种来源：

1. 请求匹配契约，但 `response_hex` 为空；
2. 回放结束仍没有任何匹配该契约点的请求（补写 `line_no=0` 问题）。

两种情况都进入待重采清单。

### `duplicate`

解码成功后发现同批次 `(device_id, point_key, ts)` 已有读数。记录问题与重复帧，但不插入第二条读数。该行仍保留其帧质量 `duplicate`。

### `decode_error`

负载长度正确、CRC 正常，但类型/字节序解码失败或浮点非有限。该行不产生读数，进入待重采。

### `calibration_unknown`

仅出现在声明了校准版本表的点位上：所有帧级 CRC/协议检查通过、解码成功，
但 `replay.ts` 不可解析为 UTC，或未落入最新登记修订的任何生效区间（缺口）。
读数仍写入台账以保留 `raw_value` 证据，`engineering_value` 为空，进入待重采；
事后补录覆盖该区间后，重算会生成派生结论而不是改写原读数。

### `overflow` / `under_range`

工程值严格高于上限/低于下限才触发。边界值本身属于范围内。读数仍写入台账，质量码为对应值，不进入待重采。

### `not_in_enum`

点位声明了非空枚举，且原始寄存器整数值不在映射中。读数仍写入，`enum_label` 为空，质量码 `not_in_enum`，不进入待重采。量程问题优先于枚举问题。

## 5. 分布统计口径

报告中的 `quality_code_distribution`：

- 统计所有 `frames.quality_code`；
- 加上 `issues` 中 `line_no=0` 的补写缺失点位质量；
- 不重复统计同一行的问题明细；
- 按质量码目录顺序输出。

示例演示数据：

```text
good 8, bad_frame 3, bad_crc_request 1, bad_crc_response 1,
exception_response 1, no_contract 1, missing_response 1, duplicate 1,
overflow 1, under_range 1, not_in_enum 1
```

总计 20 个质量事件 = 19 个帧结果 + 1 个完全缺失契约点位。

## 6. 问题明细要求

每条问题记录包含：

- `batch_id`：所属批次；
- `line_no`：回放行号，补写缺失点位为 0；
- `issue_code`：具体原因；
- `quality_code`：最终质量；
- `severity`：来自质量码目录；
- `frame_summary`：原始请求/应答帧摘要；
- `detail`：可读说明；
- `pending_collect`：是否进入待重采清单。

## 7. 测试覆盖

`tests/test_quality.py` 验证目录完整性、严重级别、待重采属性和优先级；`tests/test_ingest.py` 验证实际演示数据的质量分布；`tests/test_codec.py` 验证解码失败和非有限浮点；`tests/test_resume.py` 验证续采后质量与统计不重复。
