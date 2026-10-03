"""增量 MRP 计划器。

核心思路：每种物料维护稀疏的逐日状态（毛需求、既定到货、建议到货、
建议下达、日末库存）。独立需求变化时，只对受影响的物料从其最早受
影响日期起重做净算，再把建议下达的差量按 BOM 版本传播给子件。
传播按物料拓扑层级（父件层级严格小于子件）有序进行，保证每次调整
每种物料至多重算一次，且结果与全量重算完全一致。
"""
from __future__ import annotations

from bisect import bisect_left, bisect_right, insort
from heapq import heappop, heappush

from .types import (
    BomVersion,
    DailyPlan,
    IndependentDemand,
    Item,
    ScheduledReceipt,
)

DAYS = 365  # 计划窗口：第 0..364 日


class MRPPlanner:
    """进程内增量 MRP 计划库，由上层直接调用。调用串行执行。"""

    def __init__(self) -> None:
        self._items: dict[int, Item] = {}
        # 自制件 parent_id -> 按 start_day 排序的版本列表及起始日列表
        self._versions: dict[int, list[BomVersion]] = {}
        self._version_starts: dict[int, list[int]] = {}
        self._on_hand: dict[int, int] = {}
        self._demands: dict[int, IndependentDemand] = {}
        # 每种物料的稀疏逐日状态：day -> 数量
        self._receipts: dict[int, dict[int, int]] = {}
        self._gross: dict[int, dict[int, int]] = {}
        self._arrivals: dict[int, dict[int, int]] = {}
        self._releases: dict[int, dict[int, int]] = {}
        self._eoh: dict[int, dict[int, int]] = {}
        # 每种物料的事件日（毛需求日 ∪ 既定到货日），升序
        self._events: dict[int, list[int]] = {}
        self._level: dict[int, int] = {}

    # ------------------------------------------------------------------
    # 数据载入
    # ------------------------------------------------------------------
    def load(
        self,
        items: list[Item],
        bom_versions: list[BomVersion],
        on_hand: dict[int, int],
        receipts: list[ScheduledReceipt],
        demands: list[IndependentDemand],
    ) -> None:
        """载入全部基础数据并完成首次完整计划。"""
        self.__init__()
        for it in items:
            self._items[it.item_id] = it
            self._receipts[it.item_id] = {}
            self._gross[it.item_id] = {}
            self._arrivals[it.item_id] = {}
            self._releases[it.item_id] = {}
            self._eoh[it.item_id] = {}
            self._events[it.item_id] = []
        self._on_hand = dict(on_hand)

        parents: dict[int, set[int]] = {it.item_id: set() for it in items}
        for ver in bom_versions:
            self._versions.setdefault(ver.parent_id, []).append(ver)
            for line in ver.lines:
                parents[line.component_id].add(ver.parent_id)
        for pid, vers in self._versions.items():
            vers.sort(key=lambda v: v.start_day)
            self._version_starts[pid] = [v.start_day for v in vers]

        for r in receipts:
            rec = self._receipts[r.item_id]
            rec[r.day] = rec.get(r.day, 0) + r.qty
        for item_id, rec in self._receipts.items():
            self._events[item_id] = sorted(rec)

        for d in demands:
            self._demands[d.demand_id] = d
            g = self._gross[d.item_id]
            g[d.day] = g.get(d.day, 0) + d.qty
        for item_id, g in self._gross.items():
            if g:
                ev = self._events[item_id]
                for day in g:
                    if day not in self._receipts[item_id]:
                        insort(ev, day)

        self._level = self._compute_levels(parents)

        dirty: dict[int, int] = {}
        for item_id, ev in self._events.items():
            if ev:
                self._mark_dirty(dirty, item_id, ev[0])
        self._propagate(dirty)

    @staticmethod
    def _compute_levels(parents: dict[int, set[int]]) -> dict[int, int]:
        """层级 = 从根到该物料的最长路径，父件层级严格小于子件。"""
        level: dict[int, int] = {}

        def visit(item_id: int) -> int:
            if item_id in level:
                return level[item_id]
            level[item_id] = 0  # 占位，输入保证无环
            ps = parents.get(item_id)
            level[item_id] = 1 + max((visit(p) for p in ps), default=-1)
            return level[item_id]

        for item_id in parents:
            visit(item_id)
        return level

    # ------------------------------------------------------------------
    # 独立需求调整
    # ------------------------------------------------------------------
    def replace_demand(self, demand: IndependentDemand) -> None:
        """按稳定编号新增或替换一条独立需求。"""
        old = self._demands.get(demand.demand_id)
        if old == demand:
            return
        self._demands[demand.demand_id] = demand
        dirty: dict[int, int] = {}
        if old is not None:
            self._add_gross(dirty, old.item_id, old.day, -old.qty)
        self._add_gross(dirty, demand.item_id, demand.day, demand.qty)
        self._propagate(dirty)

    def remove_demand(self, demand_id: int) -> None:
        """撤销一条独立需求。不存在的编号视为无操作。"""
        old = self._demands.pop(demand_id, None)
        if old is None:
            return
        dirty: dict[int, int] = {}
        self._add_gross(dirty, old.item_id, old.day, -old.qty)
        self._propagate(dirty)

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def daily_plan(self, item_id: int) -> list[DailyPlan]:
        """查询指定物料第 0..364 日的逐日计划。"""
        gross = self._gross.get(item_id, {})
        arrivals = self._arrivals.get(item_id, {})
        releases = self._releases.get(item_id, {})
        eoh = self._eoh.get(item_id, {})
        events = self._events.get(item_id, [])
        inv = self._on_hand.get(item_id, 0)
        plans: list[DailyPlan] = []
        ei = 0
        n = len(events)
        for day in range(DAYS):
            if ei < n and events[ei] == day:
                inv = eoh[day]
                ei += 1
            plans.append(
                DailyPlan(
                    day=day,
                    gross=gross.get(day, 0),
                    arrival=arrivals.get(day, 0),
                    release=releases.get(day, 0),
                    eoh=inv,
                )
            )
        return plans

    def sparse_plan(self, item_id: int) -> dict[int, DailyPlan]:
        """稀疏查询：只返回有毛需求、既定到货、建议到货或建议下达的日期。"""
        gross = self._gross.get(item_id, {})
        arrivals = self._arrivals.get(item_id, {})
        releases = self._releases.get(item_id, {})
        receipts = self._receipts.get(item_id, {})
        eoh = self._eoh.get(item_id, {})
        events = self._events.get(item_id, [])
        days = sorted(
            set(events) | set(arrivals) | set(releases)
        )
        inv = self._on_hand.get(item_id, 0)
        out: dict[int, DailyPlan] = {}
        ei = 0
        n = len(events)
        for day in days:
            while ei < n and events[ei] <= day:
                inv = eoh[events[ei]]
                ei += 1
            out[day] = DailyPlan(
                day=day,
                gross=gross.get(day, 0),
                arrival=arrivals.get(day, 0),
                release=releases.get(day, 0),
                eoh=inv,
            )
        return out

    # ------------------------------------------------------------------
    # 内部：增量传播
    # ------------------------------------------------------------------
    @staticmethod
    def _mark_dirty(dirty: dict[int, int], item_id: int, day: int) -> None:
        prev = dirty.get(item_id)
        if prev is None or day < prev:
            dirty[item_id] = day

    def _add_gross(
        self, dirty: dict[int, int], item_id: int, day: int, delta: int
    ) -> None:
        """给某物料某天毛需求加差量，维护事件日并标记重算起点。"""
        g = self._gross[item_id]
        new = g.get(day, 0) + delta
        ev = self._events[item_id]
        if new == 0:
            if day in g:
                del g[day]
                if day not in self._receipts[item_id]:
                    i = bisect_left(ev, day)
                    if i < len(ev) and ev[i] == day:
                        ev.pop(i)
        elif day in g:
            g[day] = new
        else:
            g[day] = new
            if day not in self._receipts[item_id]:
                insort(ev, day)
        self._mark_dirty(dirty, item_id, day)

    def _propagate(self, dirty: dict[int, int]) -> None:
        """按（层级, 物料）顺序处理脏物料，直到没有新的派生变化。"""
        heap: list[tuple[int, int]] = []
        for item_id in dirty:
            heappush(heap, (self._level.get(item_id, 0), item_id))
        while heap:
            _, item_id = heappop(heap)
            day = dirty.pop(item_id, None)
            if day is None:
                continue
            for child_id, rel_day, delta in self._net_from(item_id, day):
                self._add_gross(dirty, child_id, rel_day, delta)
                heappush(
                    heap, (self._level.get(child_id, 0), child_id)
                )

    def _net_from(self, item_id: int, start: int) -> list[tuple[int, int, int]]:
        """从 start 日起重做该物料净算，返回子件需求差量列表。

        返回 (子件, 下达日, 差量) 三元组；差量为新建议下达减旧建议下达。
        """
        item = self._items[item_id]
        lot = item.lot_size
        lead = item.lead_time
        events = self._events[item_id]
        gross = self._gross[item_id]
        receipts = self._receipts[item_id]
        arrivals = self._arrivals[item_id]
        releases = self._releases[item_id]
        eoh = self._eoh[item_id]

        idx = bisect_left(events, start)
        inv = eoh[events[idx - 1]] if idx > 0 else self._on_hand.get(item_id, 0)

        old_releases = {
            d: q for d, q in releases.items() if d >= start - lead
        }
        for d in [d for d in arrivals if d >= start]:
            del arrivals[d]
        for d in [d for d in releases if d >= start - lead]:
            del releases[d]
        for d in [d for d in eoh if d >= start]:
            del eoh[d]

        for day in events[idx:]:
            inv += receipts.get(day, 0) - gross.get(day, 0)
            if inv < 0:
                arr = (-inv + lot - 1) // lot * lot
                arrivals[day] = arr
                releases[day - lead] = releases.get(day - lead, 0) + arr
                inv += arr
            eoh[day] = inv

        deltas: list[tuple[int, int, int]] = []
        changed: dict[int, int] = {}
        for d, q in old_releases.items():
            changed[d] = -q
        for d, q in releases.items():
            if d >= start - lead:
                changed[d] = changed.get(d, 0) + q
        for rel_day, delta in changed.items():
            if delta == 0:
                continue
            version = self._version_at(item_id, rel_day)
            if version is None:
                continue
            for line in version.lines:
                deltas.append((line.component_id, rel_day, delta * line.qty_per))
        return deltas

    def _version_at(self, item_id: int, day: int) -> BomVersion | None:
        """自制件在指定日的生效版本（含起日、不含止日）。"""
        vers = self._versions.get(item_id)
        if not vers:
            return None
        i = bisect_right(self._version_starts[item_id], day) - 1
        if i < 0:
            return None
        ver = vers[i]
        return ver if ver.start_day <= day < ver.end_day else None
