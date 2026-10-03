"""数据类型定义：物料、BOM、库存、既定到货、独立需求、逐日计划。"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import List, Tuple

DAY_MIN = 0
DAY_MAX = 364
DEMAND_DAY_MIN = 100
DEMAND_DAY_MAX = 364
MAX_LEAD_TIME = 7
MAX_LEVELS = 12


class ItemKind(Enum):
    PURCHASED = "purchased"  # 采购件，无 BOM
    MANUFACTURED = "manufactured"  # 自制件，有版本化 BOM


@dataclass(frozen=True)
class Item:
    item_id: str
    kind: ItemKind
    lead_time: int  # 0..7 天
    lot_size: int  # 正整数批量


@dataclass(frozen=True)
class BomLine:
    child_id: str
    qty_per: int  # 正整数用量


@dataclass(frozen=True)
class BomVersion:
    """自制件的一个 BOM 版本，生效区间 [start_day, end_day)。"""
    item_id: str
    start_day: int
    end_day: int
    lines: Tuple[BomLine, ...]


@dataclass(frozen=True)
class ScheduledReceipt:
    item_id: str
    day: int  # 0..364
    qty: int


@dataclass(frozen=True)
class Demand:
    """带稳定编号的独立需求。"""
    demand_id: str
    item_id: str
    day: int  # 100..364
    qty: int


@dataclass
class DayPlan:
    """某物料某一天的计划行。"""
    day: int
    gross_req: int = 0        # 毛需求（独立+父件展开，合并后）
    suggested_arrival: int = 0  # 建议到货
    suggested_release: int = 0  # 建议下达
    end_inventory: int = 0    # 日末库存


# 查询结果：365 天的逐日计划
ItemPlan = List[DayPlan]
