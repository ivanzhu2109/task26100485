# 增量 MRP 计划库

面向装配制造的进程内 MRP 计划库（纯 Python，无外部依赖、无服务进程），
支持计划员高频调整成品需求日期/数量，并快速得到与完整重算一致的物料计划。

## 功能

- 载入物料、版本化 BOM、期初库存、既定到货、独立需求（`MrpPlanner.load`）
- 新增/替换/撤销带稳定编号的独立需求（`replace_demand` / `cancel_demand`）
- 查询指定物料 0..364 日的逐日计划（`query`）：毛需求、建议到货、建议下达、日末库存

## 业务规则要点

- 同一物料同一天的独立需求与父件展开需求先合并再净算
- 逐日先入账当日既定到货，再抵扣全部毛需求，余量结转；未来到货不提前使用
- 缺口按物料批量向上取整为当天建议到货；建议下达日 = 到货日 − 提前期
- 自制件按**下达日**选择 BOM 版本，仅展开建议到货数量
- 增量调整只重算毛需求实际变化的物料，结果与全量重算严格一致

## 用法

```python
from mrp import MrpPlanner, Item, ItemKind, BomVersion, BomLine, Demand, ScheduledReceipt

planner = MrpPlanner()
planner.load(
    items=[Item("FG", ItemKind.MANUFACTURED, lead_time=2, lot_size=5),
           Item("C", ItemKind.PURCHASED, lead_time=0, lot_size=10)],
    bom_versions=[BomVersion("FG", 0, 365, (BomLine("C", 2),))],
    on_hand={"C": 3},
    receipts=[ScheduledReceipt("C", 150, 20)],
    demands=[Demand("d1", "FG", 200, 7)],
)
planner.replace_demand(Demand("d1", "FG", 220, 9))  # 改期+改量
planner.cancel_demand("d1")                          # 撤销
for row in planner.query("C"):
    ...  # DayPlan(day, gross_req, suggested_arrival, suggested_release, end_inventory)
```

## 设计

- `mrp/model.py`：数据类型（物料、BOM 版本、需求、逐日计划）
- `mrp/planner.py`：引擎。单件净算按事件日稀疏扫描；增量传播按依赖层级
  （父件层级 > 子件层级）分桶处理，每个物料每次调整最多重算一次
- `tests/reference.py`：独立编写的全量重算参照实现，用于核对增量结果

## 测试

```bash
python3 -m unittest discover -s tests -v
```

- `tests/test_correctness.py`：验收用例（共享子件合并净算、未来到货不抵更早缺口、
  提前期跨 BOM 版本、连续改期后撤销）+ 随机操作与参照实现逐步对比
- `tests/test_scale.py`：规模测试，2 万物料 / 8 万 BOM 行 / 5 万独立需求，
  完整计划毛需求（物料,日期）组合约 15.2 万（≤20 万）

实测耗时（本机，Python 3.14）：

| 指标 | 实测 | 预算 |
| --- | --- | --- |
| 首次载入至可查询 | ~0.22s | ≤15s |
| 200 次调整+查询受影响物料 | ~0.30s | ≤8s |

规模测试中每次调整后均与参照实现核对（核对耗时不计入上述指标）。
