# 装配制造增量 MRP 计划库

纯 Python、进程内数据、由上层直接调用。支持载入物料/BOM/期初库存/
既定到货/独立需求，按稳定编号替换或撤销独立需求，并查询任意物料的
逐日计划（毛需求、建议到货、建议下达、日末库存）。调整后的结果与
只使用当前需求、原始库存及原始既定到货从头全量计算完全一致。

## 用法

```python
from mrp import (BomLine, BomVersion, IndependentDemand, Item,
                 MRPPlanner, ScheduledReceipt)

planner = MRPPlanner()
planner.load(
    items=[Item(1, is_purchased=False, lead_time=2, lot_size=10), ...],
    bom_versions=[BomVersion(1, 0, 365, (BomLine(2, 3),)), ...],
    on_hand={2: 100},
    receipts=[ScheduledReceipt(2, 120, 50)],
    demands=[IndependentDemand(1, 1, 150, 30)],
)
planner.replace_demand(IndependentDemand(1, 1, 160, 45))  # 改期+改量
planner.remove_demand(1)                                   # 撤销
plan = planner.daily_plan(1)      # 第 0..364 日逐日计划
sparse = planner.sparse_plan(1)   # 仅有活动的日期 -> DailyPlan
```

## 规则要点

- 日期为整数日 0..364；独立需求交期 100..364；提前期 0..7 天；数量均为整数。
- 逐日净算：先加当日既定到货，再抵扣当日合并后的全部毛需求，余量结转；
  未来到货不能提前使用。缺口向上取整到批量整数倍作为当日建议到货。
- 建议下达日 = 建议到货日 − 提前期；自制件按下达日选择生效 BOM 版本
  （含起日、不含止日）展开子件需求，被库存/既定到货抵扣的数量不展开。
- 库存与既定到货按物料共用，不区分 BOM 版本；依赖无环、最多 12 层。

## 实现

- `mrp/planner.py`：增量引擎。每种物料维护稀疏逐日状态；需求变化时只
  对受影响物料从最早受影响日期起重做净算，建议下达差量按 BOM 版本传播
  给子件；传播按拓扑层级有序进行，每种物料每次调整至多重算一次。
- `mrp/reference.py`：独立的全量重算参照实现，作为正确性对照。
- `mrp/types.py`：数据类型定义。

## 测试

```bash
python3 -m unittest discover -s tests   # 全部测试（含规模测试，约 2.5 分钟）
python3 -m unittest tests.test_mrp      # 仅功能与随机差分测试
python3 -m tests.test_scale             # 仅规模测试，打印实测耗时
```

规模测试（固定种子可复现）：20000 种物料、80665 条 BOM 行、50000 条
独立需求、164169 个有毛需求的 (物料, 日期) 组合。本机实测：首次载入
约 0.4 秒（限 15 秒）；连续 200 次单行改量/改期/撤销并查询变化物料的
完整日计划约 0.4 秒（限 8 秒），单次调整最多波及 33 种物料（限 50）。
每次调整后都与独立全量重算核对全部物料（核对耗时不计入）。
