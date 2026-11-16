# 离线虚拟测点规范

## 1. 目标与边界

现场往往只采集电压、电流、压力、流量等**原始点位**，复核时却需要功率等**计算量**，
并且必须回答“这个 20 W 是否可能跨越 23 W 告警阈值”。虚拟测点在**离线、可复核**
的前提下把数值、量纲、来源质量与误差范围一起产出，而不是只把两个 float 相乘。

- 只处理已落账的 `readings` / `frames`，不连接现场设备、不写设备寄存器。
- 表达式是同一设备、同一采样时刻上原始点或上游虚拟点的有向无环图（DAG）。
- 全部数值以 `Decimal` 在协调 SI 单位下运算；kW/W、kPa/Pa 等前缀换算是精确
  `Decimal` 因子，不经过二进制浮点。
- 原始 `readings` 永不修改；所有结果写入独立的 `derived_*` 表。
- 来源缺失、CRC/解码不合格、同时刻矛盾值或非有限数时，输出标记为**不可判**，
  并写明原因码，绝不产生看似精确的虚拟读数。

## 2. 文件级结构

```yaml
version: 1
error_semantics: guaranteed        # 或 independent
derived_points:
  - device_id: line-a
    key: power_w
    expression: v * i
    unit: W
    threshold: {direction: above, value: 23}
    inputs:
      v: {point: line_a_voltage, abs_error: 1,   unit: V}
      i: {point: line_a_current, abs_error: 0.2, unit: A}
```

| 顶层字段 | 必填 | 规则 |
| --- | --- | --- |
| `version` | 是 | 必须为整数 `1` |
| `error_semantics` | 否 | `guaranteed`（默认）或 `independent`；可被单点字段覆盖 |
| `derived_points` | 是 | 非空列表，按拓扑顺序求值（声明先后不影响正确性） |

### 点位字段

| 字段 | 类型 | 必填 | 规则 |
| --- | --- | --- | --- |
| `device_id` | string | 是 | 必须存在于原始契约；所有输入必须在同一设备 |
| `key` | string | 是 | 匹配点位 key 规则；不得与原始点重名 |
| `expression` | string | 是 | `+ - * /`、括号、数字字面量与已声明输入名 |
| `unit` | string | 是 | 输出单位；其维度必须与表达式结果维度一致 |
| `inputs` | mapping | 是 | 非空；输入名 → `{point, abs_error, unit}` |
| `error_semantics` | string | 否 | 覆盖顶层语义 |
| `threshold` | mapping | 否 | `{direction: above\|below, value: number}` |
| `description` | string | 否 | 仅说明 |

每个输入的 `abs_error` 是该输入在其声明单位下的**绝对误差**（非负有限数），
`unit` 的维度必须与被引用点位的单位维度一致（允许不同前缀，如 `mV` 对 `V`）。

## 3. 启动前静态校验（全部在打开/复用台账之前）

| 错误码 | 触发条件 |
| --- | --- |
| `derived_yaml` / `derived_root` / `derived_version` | YAML 或顶层结构错误 |
| `derived_semantics` | 误差语义不是两个授权值之一 |
| `derived_semantics_mix` | 同一条表达式链混用了保证界限与 1σ 两种语义 |
| `derived_points` | 点位列表缺失或为空 |
| `derived_point` | 点位不是映射、key 非法等 |
| `derived_unknown_device` | 设备不在原始契约中 |
| `derived_cross_device` | 输入引用了其他设备的点位 |
| `derived_duplicate_key` | 虚拟点重名，或与原始点重名 |
| `derived_expression` | 表达式语法错误、引用未声明输入 |
| `derived_input` | 输入不是映射、`abs_error` 为负/非有限等 |
| `derived_unknown_point` | 输入指向不存在的点位 |
| `derived_unit` | 单位字符串无法解析 |
| `derived_unit_mismatch` | 输入声明单位与点位单位维度不符 |
| `derived_dimension` | 加减两侧维度不相容 |
| `derived_affine` | offset 型单位（如 degC）被直接乘除，或对仿射刻度做加法 |
| `derived_output_unit` | 输出声明单位维度与表达式结果维度不符 |
| `derived_cycle` | 表达式图存在环（含自环） |
| `derived_divide_by_zero` | 除数在某组允许输入下区间跨零 |
| `derived_threshold` | 阈值方向或数值非法 |

### 量纲与 offset 单位

- 单位用七维基本量纲（长度、质量、时间、电流、温度、物质的量、发光强度）表示。
  `V * A` 的维度正好等于 `W`；`V + A` 因维度不相容被拒绝。
- SI 前缀（`m`、`k`、`M` 等）解析为精确 `Decimal` 因子；`m3/h`、`kg*m/s^2`
  等复合单位支持隐式/显式幂次与乘除。
- `degC`、`degF` 是**仿射（带 offset）单位**。它们只能在两个**同刻度**值之间
  相减，得到去掉 offset 的温差；直接相乘、相除、取幂或组成复合单位一律拒绝。

### 除数跨零判定

静态分析结合原始点 `range` 与每个引用的 `abs_error`（上游虚拟点递归展开），
计算除数子表达式能取到的最宽区间。只要该区间包含 0，就在启动前拒绝，
因此正常配置下运行期不会出现除零；求值器仍保留运行期护栏作为防御。

## 4. 两种授权误差语义（不得混称）

### 4.1 `guaranteed`：保证界限（区间运算）

对每个输入取 `[x - e, x + e]`，按区间算术传播。报告的上下界对**任意**
落在允许误差内的输入组合都成立。

- 加减：区间端点对齐加减。
- 乘法：取四个角点乘积的最小值/最大值。
- 除法：除数区间不跨零时取角点商的极值。

### 4.2 `independent`：独立随机误差（一阶传播）

假设各输入统计独立，按一阶偏导传播标准差：

```text
σ²_y = Σ (∂f/∂x_i · σ_i)²
```

给出的是**带置信语义的 1σ 估计区间** `[y - σ, y + σ]`。它**不保证**真值必然
落在区间内。报告、台账 `bound_sigma='1sigma'` 与质量说明都会显式标注这一点，
绝不与保证界限混称为同一精度。

### 4.3 规范算例：10 ± 1 V 与 2 ± 0.2 A 求功率

中心值都是 `10 * 2 = 20 W`。

| 语义 | 下界 | 上界 | 对 23 W 阈值的判定 |
| --- | --- | --- | --- |
| guaranteed | 16.2 W | 24.2 W | `possibly_above`（可能越限） |
| independent（1σ） | ≈17.17 W | ≈22.83 W | `within`（估计范围内未越限） |

同一中心值得到**不同**阈值结论，二者不能都写成“正常”。

阈值状态机（以上限 `above` 为例）：整段区间高于阈值为 `above`；区间与阈值
相交为 `possibly_above`；整段低于阈值为 `within`。下限 `below` 对称。

### 4.4 一条链路只允许一种语义

保证界限的节点没有 σ，1σ 节点没有必然保证，二者无法在同一表达式链内互相
代入而不产生歧义。因此同一连通链路（含多级上下游）必须端到端使用同一种
`error_semantics`，否则启动前以 `derived_semantics_mix` 拒绝；互不相连的
独立虚拟点仍可各自选择不同语义。需要两种结论时（如规范算例），请分别用
两份契约对同一批次各算一次，台账会以不同契约指纹分别留存，不会混称。

## 5. 运行期质量与不可判原因

| 原因码 | 含义 |
| --- | --- |
| `missing_source` | 该时刻缺少某个输入（原始读数与帧均无） |
| `crc_check_failed` | 输入点在该时刻有请求/应答帧但 CRC 不合格 |
| `decode_failed` | 寄存器解码失败 |
| `unacceptable_quality` | 读数质量码不参与派生（如 duplicate、异常帧等） |
| `contradictory_values` | 同一时刻同一点位存在互相矛盾的多个可接受值 |
| `non_finite_value` | 工程值为 NaN/Inf |
| `upstream_indeterminate` | 上游虚拟点在该时刻不可判 |
| `divisor_interval_crosses_zero` | 运行期护栏触发（正常静态配置下不可达） |

可作为叶子参与派生的原始质量码：`good`、`overflow`、`under_range`
（量程越界仍可审计地参与，其余一律不可判）。不可判行的数值与上下界为 NULL，
只保存原因码与已知来源。

## 6. 多级链路

虚拟点可引用其他虚拟点。求值按拓扑序进行，每一级都在 SI 单位下把**值、
区间/σ、来源（point_key + reading 行号）**整体传递给下游，而不是在最终节点
贴一个百分比。上游不可判时，下游以 `upstream_indeterminate` 级联不可判，
并保留最初的叶子来源行号。

## 7. 台账与幂等

新增三张表（schema 版本升至 2，旧库打开时自动补建）：

- `derived_contracts`：契约文档指纹、表达式版本、默认误差语义。
- `derived_points`：每个虚拟点的静态定义、维度、输入与阈值。
- `derived_readings`：每个（虚拟点、时刻）一行，保存表达式版本、来源
  point_key/reading 行号（`sources_json`、`line_no_json`）、输入质量、
  数值、单位、误差范围、阈值状态与不可判原因；同时保存 SI 单位下的
  Decimal 文本以便链路复算。

唯一键 `(contract_id, batch_id, device_id, point_key, ts)` 保证重复回放、
断点续跑或重复执行 `derive` 都不会重复生成；已存在的行直接跳过。
原始 `readings` 表保持不变。

## 8. 用法

```bash
# 采集时一并计算虚拟测点
pointlog run --replay f.jsonl --contract points.yaml \
         --derived-config derived.yaml --db ledger.db --reports reports/

# 对已完成批次单独（重复）计算，幂等
pointlog derive <batch_id> --contract points.yaml \
         --derived-config derived.yaml --db ledger.db
```

报告新增：JSON/Markdown/HTML 的“虚拟测点”小节（含语义声明与不可判清单）
以及 `derived_readings.csv`。
