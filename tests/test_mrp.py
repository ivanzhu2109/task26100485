"""增量 MRP 计划库的自动化测试。"""
from __future__ import annotations

import random
import unittest

from mrp import (
    BomLine,
    BomVersion,
    IndependentDemand,
    Item,
    MRPPlanner,
    ScheduledReceipt,
)
from mrp.reference import full_recompute

DAYS = 365


def expand(sparse: dict[int, object], on_hand: int) -> list[tuple[int, int, int, int]]:
    """把稀疏计划展开为 365 天的 (gross, arrival, release, eoh)。"""
    out = []
    inv = on_hand
    for day in range(DAYS):
        p = sparse.get(day)
        if p is not None:
            inv = p.eoh
            out.append((p.gross, p.arrival, p.release, p.eoh))
        else:
            out.append((0, 0, 0, inv))
    return out


def assert_planner_matches(
    tc: unittest.TestCase,
    planner: MRPPlanner,
    items: list[Item],
    bom_versions: list[BomVersion],
    on_hand: dict[int, int],
    receipts: list[ScheduledReceipt],
    demands: list[IndependentDemand],
) -> None:
    """全部物料的逐日计划须与独立全量重算一致。"""
    ref = full_recompute(items, bom_versions, on_hand, receipts, demands)
    for it in items:
        expected = expand(ref.get(it.item_id, {}), on_hand.get(it.item_id, 0))
        actual = [
            (p.gross, p.arrival, p.release, p.eoh)
            for p in planner.daily_plan(it.item_id)
        ]
        tc.assertEqual(
            actual,
            expected,
            msg=f"item {it.item_id} 逐日计划不一致",
        )


class AcceptanceTest(unittest.TestCase):
    def test_shared_component_merges_same_day(self) -> None:
        """跨层共享子件：同一天 3+3 件需求，批量 10 只建议到货 10 件。"""
        items = [
            Item(1, is_purchased=False, lead_time=0, lot_size=1),   # 成品 A
            Item(2, is_purchased=False, lead_time=0, lot_size=1),   # 半成品 B
            Item(3, is_purchased=True, lead_time=0, lot_size=10),   # 共享子件 C
        ]
        boms = [
            BomVersion(1, 0, 365, (BomLine(2, 1), BomLine(3, 3))),
            BomVersion(2, 0, 365, (BomLine(3, 3),)),
        ]
        demands = [IndependentDemand(1, 1, 100, 1)]
        p = MRPPlanner()
        p.load(items, boms, {}, [], demands)
        plan3 = p.daily_plan(3)
        self.assertEqual(plan3[100].gross, 6)      # 3（经 B）+ 3（A 直接）
        self.assertEqual(plan3[100].arrival, 10)   # 只到货一次 10 件，不是 20
        self.assertEqual(plan3[100].eoh, 4)
        self.assertEqual(sum(d.arrival for d in plan3), 10)
        assert_planner_matches(self, p, items, boms, {}, [], demands)

    def test_future_receipt_cannot_cover_earlier_gap(self) -> None:
        """未来到货不能消除更早的缺口。"""
        items = [Item(1, is_purchased=True, lead_time=2, lot_size=5)]
        receipts = [ScheduledReceipt(1, 200, 10)]
        demands = [IndependentDemand(1, 1, 100, 3)]
        p = MRPPlanner()
        p.load(items, {}, {}, receipts, demands)
        plan = p.daily_plan(1)
        self.assertEqual(plan[100].arrival, 5)   # 第 100 日缺口 3，按批量 5 到货
        self.assertEqual(plan[100].release, 0)
        self.assertEqual(plan[98].release, 5)
        self.assertEqual(plan[199].eoh, 2)
        self.assertEqual(plan[200].eoh, 12)      # 既定到货第 200 日才入库
        self.assertEqual(sum(d.arrival for d in plan), 5)
        assert_planner_matches(self, p, items, {}, {}, receipts, demands)

    def test_release_day_selects_bom_version(self) -> None:
        """提前期使下达日与交期落在不同 BOM 版本。"""
        items = [
            Item(1, is_purchased=False, lead_time=5, lot_size=1),
            Item(2, is_purchased=True, lead_time=0, lot_size=1),
        ]
        boms = [
            BomVersion(1, 0, 100, (BomLine(2, 1),)),    # 旧版：用量 1
            BomVersion(1, 100, 365, (BomLine(2, 2),)),  # 新版：用量 2
        ]
        demands = [
            IndependentDemand(1, 1, 104, 1),  # 下达日 99 -> 旧版
            IndependentDemand(2, 1, 105, 1),  # 下达日 100 -> 新版
        ]
        p = MRPPlanner()
        p.load(items, boms, {}, [], demands)
        plan2 = p.daily_plan(2)
        self.assertEqual(plan2[99].gross, 1)    # 旧版用量 1
        self.assertEqual(plan2[100].gross, 2)   # 新版用量 2
        # 改期：104 -> 106，下达日 99 -> 101，应切换到新版
        p.replace_demand(IndependentDemand(1, 1, 106, 1))
        plan2 = p.daily_plan(2)
        self.assertEqual(plan2[99].gross, 0)
        self.assertEqual(plan2[101].gross, 2)
        demands[0] = IndependentDemand(1, 1, 106, 1)
        assert_planner_matches(self, p, items, boms, {}, [], demands)

    def test_reschedule_then_remove(self) -> None:
        """连续改期后再撤销，所有物料与全量重算一致，含无建议到货的物料。"""
        items = [
            Item(1, is_purchased=False, lead_time=3, lot_size=4),
            Item(2, is_purchased=True, lead_time=1, lot_size=6),
        ]
        boms = [BomVersion(1, 0, 365, (BomLine(2, 2),))]
        receipts = [ScheduledReceipt(2, 150, 5)]
        on_hand = {2: 1}
        demands = [IndependentDemand(1, 1, 120, 3)]
        p = MRPPlanner()
        p.load(items, boms, on_hand, receipts, demands)
        assert_planner_matches(self, p, items, boms, on_hand, receipts, demands)

        # 连续改期 + 改量
        for i, (day, qty) in enumerate([(130, 5), (140, 2), (110, 7)]):
            demands[0] = IndependentDemand(1, 1, day, qty)
            p.replace_demand(demands[0])
            assert_planner_matches(
                self, p, items, boms, on_hand, receipts, demands
            )
        # 撤销：物料 1 不再有任何建议到货
        p.remove_demand(1)
        demands.clear()
        plan1 = p.daily_plan(1)
        self.assertTrue(all(d.arrival == 0 and d.gross == 0 for d in plan1))
        plan2 = p.daily_plan(2)
        self.assertTrue(all(d.arrival == 0 for d in plan2))
        assert_planner_matches(self, p, items, boms, on_hand, receipts, demands)


class RandomDifferentialTest(unittest.TestCase):
    def test_random_ops_match_full_recompute(self) -> None:
        rng = random.Random(20261004)
        n_items = 40
        items: list[Item] = []
        for i in range(n_items):
            items.append(
                Item(
                    i,
                    is_purchased=rng.random() < 0.4,
                    lead_time=rng.randint(0, 7),
                    lot_size=rng.randint(1, 12),
                )
            )
        # 分层：编号小的为上层，保证无环、层级有限
        boms: list[BomVersion] = []
        for it in items:
            if it.is_purchased or it.item_id >= n_items - 3:
                continue
            n_ver = rng.randint(1, 2)
            bounds = [0] + sorted(
                rng.sample(range(80, 300), n_ver - 1)
            ) + [365]
            lines_pool = [j for j in range(it.item_id + 1, n_items)]
            for v in range(n_ver):
                lines = tuple(
                    BomLine(j, rng.randint(1, 4))
                    for j in rng.sample(lines_pool, min(3, len(lines_pool)))
                )
                boms.append(
                    BomVersion(it.item_id, bounds[v], bounds[v + 1], lines)
                )
        on_hand = {i: rng.randint(0, 20) for i in range(n_items)}
        receipts = [
            ScheduledReceipt(rng.randrange(n_items), rng.randint(0, 364),
                             rng.randint(1, 30))
            for _ in range(60)
        ]
        demands: dict[int, IndependentDemand] = {}
        for did in range(80):
            demands[did] = IndependentDemand(
                did, rng.randrange(n_items), rng.randint(100, 364),
                rng.randint(1, 20),
            )

        def snap() -> tuple:
            return (
                items,
                boms,
                dict(on_hand),
                list(receipts),
                list(demands.values()),
            )

        p = MRPPlanner()
        p.load(*snap())
        assert_planner_matches(self, p, *snap())

        next_id = 1000
        for step in range(60):
            op = rng.random()
            if op < 0.45:  # 新增或替换
                did = rng.choice(list(demands) + [next_id])
                if did == next_id:
                    next_id += 1
                demands[did] = IndependentDemand(
                    did, rng.randrange(n_items), rng.randint(100, 364),
                    rng.randint(1, 20),
                )
                p.replace_demand(demands[did])
            elif op < 0.8 and demands:  # 改期/改量
                did = rng.choice(list(demands))
                demands[did] = IndependentDemand(
                    did, rng.randrange(n_items), rng.randint(100, 364),
                    rng.randint(1, 20),
                )
                p.replace_demand(demands[did])
            elif demands:  # 撤销
                did = rng.choice(list(demands))
                del demands[did]
                p.remove_demand(did)
            assert_planner_matches(self, p, *snap())


if __name__ == "__main__":
    unittest.main()
