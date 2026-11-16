# 点位采录台采集报告

## 批次信息

| 字段 | 值 |
| --- | --- |
| 批次 ID | b_6db3b1425d1f_17fd25084aa0 |
| 状态 | completed |
| 开始时间 | 2026-11-16T21:27:17Z |
| 结束时间 | 2026-11-16T21:27:17Z |
| 回放文件 | replay_demo.jsonl |
| 回放 SHA-256 | 6db3b1425d1fa8a3bd5b755fa0cfae3b2294503e2d02bde3a7bea167414387aa |
| 点位契约 | points.yaml |
| 契约 SHA-256 | 17fd25084aa0e62976e7cbccf1b2c9d00cebd13bb1907199902d4f3000e921eb |
| 回放总行数 | 19 |
| 已处理行数 | 19 |

## 概览

| 指标 | 数量 |
| --- | --- |
| 帧记录 | 19 |
| 读数记录 | 11 |
| 有效读数（good） | 8 |
| 问题记录 | 12 |
| 待重采点位 | 7 |

## 质量码分布

| 质量码 | 严重级别 | 进入待重采 | 数量 |
| --- | --- | --- | --- |
| good | info | 否 | 8 |
| bad_frame | error | 是 | 3 |
| bad_crc_request | error | 是 | 1 |
| bad_crc_response | error | 是 | 1 |
| exception_response | error | 是 | 1 |
| no_contract | warning | 否 | 1 |
| missing_response | warning | 是 | 1 |
| duplicate | warning | 否 | 1 |
| overflow | warning | 否 | 1 |
| under_range | warning | 否 | 1 |
| not_in_enum | warning | 否 | 1 |

## 问题分布

| 问题码 | 数量 |
| --- | --- |
| bad_crc_request | 1 |
| bad_crc_response | 1 |
| byte_count_mismatch | 1 |
| duplicate | 1 |
| exception_response | 1 |
| missing_response | 1 |
| no_contract | 1 |
| not_in_enum | 1 |
| overflow | 1 |
| slave_mismatch | 1 |
| truncated_frame | 1 |
| under_range | 1 |

## 按设备统计

| 设备 | 读数 | good | 问题 | 待重采 |
| --- | --- | --- | --- | --- |
| demo-gateway | 7 | 4 | 6 | 2 |
| line-a | 4 | 4 | 5 | 5 |

## 按点位统计

| 设备 | 点位 | 单位 | 读数 | good | 问题 | 待重采 |
| --- | --- | --- | --- | --- | --- | --- |
| demo-gateway | line_a_flow | m3/h | 1 | 1 | 1 | 是 |
| demo-gateway | line_a_mode | - | 2 | 1 | 1 | 否 |
| demo-gateway | line_a_speed | rpm | 2 | 1 | 3 | 是 |
| demo-gateway | line_a_temperature | degC | 2 | 1 | 1 | 否 |
| line-a | line_a_energy | kWh | 0 | 0 | 1 | 是 |
| line-a | line_a_level | m | 1 | 1 | 1 | 是 |
| line-a | line_a_pressure | kPa | 1 | 1 | 1 | 是 |
| line-a | line_a_status | - | 1 | 1 | 1 | 是 |
| line-a | line_a_voltage | V | 1 | 1 | 1 | 是 |

## 待重采点位

| 设备 | 点位 | 问题码 | 质量码 | 次数 | 行号 | 说明 |
| --- | --- | --- | --- | --- | --- | --- |
| demo-gateway | line_a_flow | exception_response | exception_response | 1 | 12 | exception code 2: illegal data address |
| demo-gateway | line_a_speed | slave_mismatch | bad_frame | 1 | 18 | response slave 9 != request slave 1 |
| line-a | line_a_energy | missing_response | missing_response | 1 | - | contract point has no request/response in this replay |
| line-a | line_a_level | truncated_frame | bad_frame | 1 | 16 | response: response declares 4 data bytes but frame is 6 bytes |
| line-a | line_a_pressure | bad_crc_request | bad_crc_request | 1 | 14 | request CRC check failed |
| line-a | line_a_status | byte_count_mismatch | bad_frame | 1 | 17 | response byte count 4 != expected 2 |
| line-a | line_a_voltage | bad_crc_response | bad_crc_response | 1 | 13 | response CRC check failed |

## 双点位持续联锁

| 规则 | 设备 | 采样语义 | 阈值 | 最短持续(min) | 陈旧上限(min) | 已证联锁(s) | 已证正常(s) | 未知(s) | 事件 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| line_a_pressure_level@v1 | line-a | bounded_hold | line_a_pressure > 150 AND line_a_level > 2.5 | 2.0 | 5.0 | 0.0 | 35.0 | 20.0 | 0 |

### 已证联锁事件

无达到最短持续时间的联锁事件。

### 未知缺口（不计为正常）

| 规则 | 起止 | 持续(s) | 原因 | 源行号 |
| --- | --- | --- | --- | --- |
| line_a_pressure_level | 2025-03-01T00:00:20Z — 2025-03-01T00:00:30Z | 10.0 | insufficient_evidence | 5 |
| line_a_pressure_level | 2025-03-01T00:01:05Z — 2025-03-01T00:01:15Z | 10.0 | bad_evidence | 5,14 |

### 联锁未知待复采

| 设备 | 点位 | 规则 | 起止 | 原因 | 行号 |
| --- | --- | --- | --- | --- | --- |
| line-a | line_a_pressure+line_a_level | line_a_pressure_level | 2025-03-01T00:00:20Z — 2025-03-01T00:00:30Z | insufficient_evidence | 5 |
| line-a | line_a_pressure+line_a_level | line_a_pressure_level | 2025-03-01T00:01:05Z — 2025-03-01T00:01:15Z | bad_evidence | 5,14 |
