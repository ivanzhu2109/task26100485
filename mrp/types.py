"""装配制造增量 MRP 计划库的类型定义。"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Item:
    """物料。is_purchased 为 True 表示采购件（无 BOM），否则为自制件。"""

    item_id: int
    is_purchased: bool
    lead_time: int  # 0..7 天
    lot_size: int   # 正整数批量


@dataclass(frozen=True)
class BomLine:
    """BOM 单行：一个父件用量。"""

    component_id: int
    qty_per: int


@dataclass(frozen=True)
class BomVersion:
    """自制件的一个 BOM 版本，生效区间 [start_day, end_day)。"""

    parent_id: int
    start_day: int
    end_day: int
    lines: tuple[BomLine, ...] = ()


@dataclass(frozen=True)
class ScheduledReceipt:
    """既定到货。"""

    item_id: int
    day: int
    qty: int


@dataclass(frozen=True)
class IndependentDemand:
    """带稳定编号的独立需求。"""

    demand_id: int
    item_id: int
    day: int  # 100..364
    qty: int


@dataclass(frozen=True)
class DailyPlan:
    """某物料某一天的计划。"""

    day: int
    gross: int      # 毛需求
    arrival: int    # 建议到货
    release: int    # 建议下达
    eoh: int        # 日末库存
