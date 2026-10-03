"""正确性测试：验收用例 + 与独立全量参照实现对比。"""
import random
import unittest

from mrp import (
    BomLine,
    BomVersion,
    Demand,
    Item,
    ItemKind,
    MrpPlanner,
    ScheduledReceipt,
)
from tests.reference import ReferenceMRP


def plans_equal(a, b):
    return [
        (x.gross_req, x.suggested_arrival, x.suggested_release, x.end_inventory)
        for x in a
    ] == [
        (x.gross_req, x.suggested_arrival, x.suggested_release, x.end_inventory)
        for x in b
    ]


def assert_all_items_match(case, planner, ref, item_ids):
    ref.recompute()
    for item_id in item_ids:
        got, want = planner.query(item_id), ref.query(item_id)
        case.assertTrue(
            plans_equal(got, want),
            f"物料 {item_id} 计划不一致\n"
            + "\n".join(
                f"day {d}: got {(g.gross_req, g.suggested_arrival, g.suggested_release, g.end_inventory)}"
                f" want {(w.gross_req, w.suggested_arrival, w.suggested_release, w.end_inventory)}"
                for d, (g, w) in enumerate(zip(got, want))
                if (g.gross_req, g.suggested_arrival, g.suggested_release, g.end_inventory)
                != (w.gross_req, w.suggested_arrival, w.suggested_release, w.end_inventory)
            ),
        )


class AcceptanceTest(unittest.TestCase):
    def test_shared_child_merges_same_day_requirements(self):
        """跨层共享子件：两个父件同日各产生3件需求，批量10只建议到货10件。"""
        items = [
            Item("FG1", ItemKind.MANUFACTURED, 1, 1),
            Item("FG2", ItemKind.MANUFACTURED, 2, 1),
            Item("SUB", ItemKind.MANUFACTURED, 1, 1),
            Item("C", ItemKind.PURCHASED, 0, 10),
        ]
        boms = [
            BomVersion("FG1", 0, 365, (BomLine("SUB", 1),)),
            BomVersion("FG2", 0, 365, (BomLine("SUB", 1),)),
            BomVersion("SUB", 0, 365, (BomLine("C", 1),)),
        ]
        # 两条需求经不同层级展开后，C 在同一天各收到 3 件需求
        demands = [Demand("d1", "FG1", 202, 3), Demand("d2", "FG2", 203, 3)]
        p = MrpPlanner()
        p.load(items, boms, {}, [], demands)
        cplan = p.query("C")
        self.assertEqual(cplan[200].gross_req, 6)
        self.assertEqual(cplan[200].suggested_arrival, 10)
        self.assertEqual(cplan[200].end_inventory, 4)
        self.assertEqual(sum(x.suggested_arrival for x in cplan), 10)

    def test_future_receipt_cannot_cover_earlier_shortage(self):
        """未来到货不能消除更早的缺口。"""
        items = [Item("C", ItemKind.PURCHASED, 0, 5)]
        p = MrpPlanner()
        p.load(items, [], {}, [ScheduledReceipt("C", 210, 100)],
               [Demand("d1", "C", 200, 7)])
        plan = p.query("C")
        # 第200天缺口7件，按批量5上取整建议到货10件
        self.assertEqual(plan[200].suggested_arrival, 10)
        self.assertEqual(plan[200].end_inventory, 3)
        # 第210天的既定到货正常入账
        self.assertEqual(plan[210].end_inventory, 103)

    def test_lead_time_spans_bom_versions(self):
        """提前期使下达日与交期落在不同版本：按下达日选版。"""
        items = [
            Item("FG", ItemKind.MANUFACTURED, 10, 1),
            Item("A", ItemKind.PURCHASED, 0, 1),
            Item("B", ItemKind.PURCHASED, 0, 1),
        ]
        boms = [
            BomVersion("FG", 0, 150, (BomLine("A", 2),)),
            BomVersion("FG", 150, 365, (BomLine("B", 5),)),
        ]
        # 交期160，下达日150 -> 用新版本，展开到B
        # 交期155，下达日145 -> 用旧版本，展开到A
        p = MrpPlanner()
        p.load(items, boms, {}, [],
               [Demand("d1", "FG", 160, 4), Demand("d2", "FG", 155, 3)])
        aplan, bplan = p.query("A"), p.query("B")
        self.assertEqual(aplan[145].gross_req, 6)   # 3 * 2（旧版）
        self.assertEqual(bplan[150].gross_req, 20)  # 4 * 5（新版）
        self.assertEqual(aplan[150].gross_req, 0)

    def test_reschedule_then_cancel_matches_full_recompute(self):
        """连续改期后再撤销：所有物料逐日结果与全量重算一致，
        包括不再需要任何建议到货的物料。"""
        items = [
            Item("FG", ItemKind.MANUFACTURED, 5, 4),
            Item("SUB", ItemKind.MANUFACTURED, 3, 7),
            Item("C", ItemKind.PURCHASED, 2, 10),
        ]
        boms = [
            BomVersion("FG", 0, 180, (BomLine("SUB", 2),)),
            BomVersion("FG", 180, 365, (BomLine("SUB", 3),)),
            BomVersion("SUB", 0, 365, (BomLine("C", 1),)),
        ]
        receipts = [ScheduledReceipt("C", 150, 30), ScheduledReceipt("SUB", 120, 5)]
        on_hand = {"C": 8, "SUB": 2, "FG": 1}
        demands = [Demand("d1", "FG", 200, 9), Demand("d2", "FG", 250, 6)]
        p = MrpPlanner()
        p.load(items, boms, on_hand, receipts, demands)
        ref = ReferenceMRP(items, boms, on_hand, receipts)
        for d in demands:
            ref.replace_demand(d)
        ids = ["FG", "SUB", "C"]
        assert_all_items_match(self, p, ref, ids)

        ops = [
            ("replace", Demand("d1", "FG", 300, 9)),   # 改期跨版本
            ("replace", Demand("d1", "FG", 175, 20)),  # 改期+改量
            ("replace", Demand("d2", "FG", 175, 1)),   # 合并到同一天
            ("cancel", "d1"),                          # 撤销
            ("cancel", "d2"),                          # 全部撤销，不应再有任何建议
        ]
        for op, payload in ops:
            if op == "replace":
                p.replace_demand(payload)
                ref.replace_demand(payload)
            else:
                p.cancel_demand(payload)
                ref.cancel_demand(payload)
            assert_all_items_match(self, p, ref, ids)
        # 全部撤销后：无任何建议到货/下达，毛需求清零
        for item_id in ids:
            for row in p.query(item_id):
                self.assertEqual(row.gross_req, 0)
                self.assertEqual(row.suggested_arrival, 0)
                self.assertEqual(row.suggested_release, 0)


class FuzzTest(unittest.TestCase):
    def test_random_ops_match_reference(self):
        """随机 BOM/库存/需求 + 随机增改删，每步后与全量参照对比。"""
        rng = random.Random(20261004)
        n_purchased = 6
        items = [Item(f"C{i}", ItemKind.PURCHASED, rng.randint(0, 7),
                      rng.choice([1, 3, 5, 10])) for i in range(n_purchased)]
        boms = []
        # 3 层自制件，每层 2 个，子件来自下一层或采购件
        prev = [f"C{i}" for i in range(n_purchased)]
        for layer in range(3):
            curr = []
            for j in range(2):
                iid = f"M{layer}_{j}"
                items.append(Item(iid, ItemKind.MANUFACTURED,
                                  rng.randint(0, 7), rng.choice([1, 2, 4])))
                # 2~3 个版本，覆盖 [0,365)
                cuts = sorted(rng.sample(range(50, 330), rng.randint(1, 2)))
                starts = [0] + cuts
                ends = cuts + [365]
                for s, e in zip(starts, ends):
                    lines = tuple(
                        BomLine(rng.choice(prev), rng.randint(1, 3))
                        for _ in range(rng.randint(1, 3))
                    )
                    boms.append(BomVersion(iid, s, e, lines))
                curr.append(iid)
            prev = curr + prev  # 允许跨层共享
        item_ids = [it.item_id for it in items]
        on_hand = {iid: rng.randint(0, 20) for iid in item_ids}
        receipts = [
            ScheduledReceipt(rng.choice(item_ids), rng.randint(0, 364),
                             rng.randint(1, 30))
            for _ in range(15)
        ]
        top = [f"M2_{j}" for j in range(2)]
        demands = [
            Demand(f"d{k}", rng.choice(top), rng.randint(100, 364),
                   rng.randint(1, 12))
            for k in range(8)
        ]
        p = MrpPlanner()
        p.load(items, boms, on_hand, receipts, demands)
        ref = ReferenceMRP(items, boms, on_hand, receipts)
        for d in demands:
            ref.replace_demand(d)
        assert_all_items_match(self, p, ref, item_ids)

        live = {d.demand_id for d in demands}
        for step in range(60):
            action = rng.random()
            if action < 0.45:  # 新增/替换
                did = rng.choice(sorted(live)) if live and rng.random() < 0.6 \
                    else f"d_new_{step}"
                d = Demand(did, rng.choice(top), rng.randint(100, 364),
                           rng.randint(1, 15))
                p.replace_demand(d)
                ref.replace_demand(d)
                live.add(did)
            elif action < 0.75 and live:  # 撤销
                did = rng.choice(sorted(live))
                p.cancel_demand(did)
                ref.cancel_demand(did)
                live.discard(did)
            else:  # 改期（同编号替换）
                did = rng.choice(sorted(live)) if live else f"d_new_{step}"
                d = Demand(did, rng.choice(top), rng.randint(100, 364),
                           rng.randint(1, 15))
                p.replace_demand(d)
                ref.replace_demand(d)
                live.add(did)
            assert_all_items_match(self, p, ref, item_ids)


if __name__ == "__main__":
    unittest.main(verbosity=2)
