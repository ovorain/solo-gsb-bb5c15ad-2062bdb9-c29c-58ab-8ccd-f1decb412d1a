"""运输延误推演 API 测试(stdlib unittest,无需额外依赖)。

运行:python3 tests/test_delay.py
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.delay import simulate_delay  # noqa: E402

# 分段运输:段1/段2 均为宽温区,鲜奶首选 [3,5]、预算 20、最长运输 20h
SEGMENT_PLAN = {
    "box_capacity": 12,
    "segments": [
        {"name": "干线冷链", "hours": 14,
         "zones": [{"name": "冷藏区", "t_min": 0, "t_max": 8}]},
        {"name": "末端配送", "hours": 4,
         "zones": [{"name": "冷藏区", "t_min": 0, "t_max": 8},
                   {"name": "常温区", "t_min": 15, "t_max": 25}]},
    ],
    "cargoes": [
        {"name": "鲜奶", "units": 10, "unit_volume": 0.4, "t_min": 2, "t_max": 6,
         "preferred_t_min": 3, "preferred_t_max": 5, "max_degree_hours": 20,
         "max_transport_hours": 20},
    ],
}

# 非分段(单一温区)模式
SINGLE_PLAN = {
    "transport_hours": 24,
    "box_capacity": 12,
    "zones": [{"name": "冷藏区", "t_min": 0, "t_max": 8}],
    "cargoes": [
        {"name": "鲜奶", "units": 10, "unit_volume": 0.4, "t_min": 2, "t_max": 6,
         "max_transport_hours": 30},
    ],
}


def _box(result, box_no=1):
    return next(b for b in result["boxes"] if b["box_no"] == box_no)


def _heat(result, box_no=1, batch_no=1):
    return next(h for h in _box(result, box_no)["heat_exposure"]
                if h["batch_no"] == batch_no)


class DelaySimulationTestCase(unittest.TestCase):
    # ---------- 基本不变式:箱号/批次/件数固定,零延误与原值一致 ----------
    def test_zero_delay_matches_baseline(self):
        r = simulate_delay(SEGMENT_PLAN, segment_no=1, added_hours=0.0)
        self.assertEqual(r["transport_hours_original"], 18.0)
        self.assertEqual(r["transport_hours_delayed"], 18.0)
        self.assertTrue(r["summary"]["can_continue"])
        self.assertEqual(r["summary"]["setpoint_changed_count"], 0)
        self.assertEqual(r["affected_batches"], [])
        # 件数保持不变
        self.assertEqual(
            _heat(r)["units"], 10
        )
        for c in _box(r)["segment_checks"]:
            self.assertFalse(c["setpoint_changed"])

    # ---------- 总时长延长,但未超限:可继续运输 ----------
    def test_delay_within_limits_can_continue(self):
        r = simulate_delay(SEGMENT_PLAN, segment_no=2, added_hours=2.0)
        self.assertEqual(r["transport_hours_delayed"], 20.0)
        self.assertEqual(r["delay"]["hours_delayed"], 6.0)
        self.assertTrue(r["summary"]["can_continue"])
        self.assertEqual(r["affected_batches"], [])
        # 只有被选段的 hours_added 为正
        adds = {c["segment_no"]: c["hours_added"]
                for c in _box(r)["segment_checks"]}
        self.assertEqual(adds, {1: 0.0, 2: 2.0})

    # ---------- 超出最长运输时长 ----------
    def test_delay_exceeds_max_transport_hours(self):
        r = simulate_delay(SEGMENT_PLAN, segment_no=1, added_hours=3.0)
        self.assertEqual(r["transport_hours_delayed"], 21.0)
        self.assertFalse(r["summary"]["can_continue"])
        affected = r["affected_batches"]
        self.assertEqual(len(affected), 1)
        self.assertTrue(affected[0]["transport_exceeded"])
        self.assertEqual(affected[0]["max_transport_hours"], 20.0)
        # 度时未超(设定点 3~5℃ 在首选区间内,暴露为 0),无首次超限段
        self.assertIsNone(affected[0]["first_exceeded_segment_no"])
        self.assertTrue(_heat(r)["transport_exceeded"])
        self.assertFalse(_heat(r)["budget_exceeded_delayed"])

    # ---------- 超出度时预算并标出首次超限段 ----------
    def test_delay_exceeds_degree_hour_budget(self):
        plan = {
            "box_capacity": 12,
            "segments": [
                {"name": "段1", "hours": 10,
                 "zones": [{"name": "宽", "t_min": 0, "t_max": 10}]},
                {"name": "段2", "hours": 10,
                 "zones": [{"name": "宽", "t_min": 0, "t_max": 10}]},
            ],
            "cargoes": [
                # 首选区间在可行温度之外:设定点被迫取 10℃,距首选 [12,16] 为 2
                {"name": "A", "units": 1, "unit_volume": 1, "t_min": 0,
                 "t_max": 10, "preferred_t_min": 12, "preferred_t_max": 16,
                 "max_degree_hours": 40},
            ],
        }
        r = simulate_delay(plan, segment_no=2, added_hours=1.0)
        # 设定点恒为 10℃(每小时暴露 2):原累计 40 不超(严格大于才算),
        # 延误后 42 > 40,首次超限段为第 2 段
        h = _heat(r)
        self.assertEqual(h["cumulative_exposure_original"], 40.0)
        self.assertEqual(h["cumulative_exposure_delayed"], 42.0)
        self.assertTrue(h["budget_exceeded_delayed"])
        self.assertEqual(h["first_exceeded_segment_no"], 2)
        self.assertEqual(h["first_exceeded_segment_name"], "段2")
        affected = r["affected_batches"]
        self.assertEqual(len(affected), 1)
        self.assertFalse(affected[0]["transport_exceeded"])
        self.assertEqual(affected[0]["first_exceeded_segment_no"], 2)

    # ---------- 延误后设定点被重新优选(后续独立段选点翻转) ----------
    def test_delay_reoptimizes_later_setpoint(self):
        plan = {
            "box_capacity": 100,
            "segments": [
                {"name": "段1", "hours": 10,
                 "zones": [{"name": "宽", "t_min": 0, "t_max": 10}]},
                {"name": "段2", "hours": 10,
                 "zones": [{"name": "宽", "t_min": 0, "t_max": 10}]},
            ],
            "cargoes": [
                {"name": "A", "units": 1, "unit_volume": 1, "t_min": 0,
                 "t_max": 10, "preferred_t_min": 10, "preferred_t_max": 10,
                 "max_degree_hours": 300},
                {"name": "B", "units": 1, "unit_volume": 1, "t_min": 0,
                 "t_max": 10, "preferred_t_min": 0, "preferred_t_max": 0,
                 "max_degree_hours": 200},
            ],
        }
        r = simulate_delay(plan, segment_no=2, added_hours=10.0)
        checks = {c["segment_no"]: c for c in _box(r)["segment_checks"]}
        # 段1 维持 0℃;段2 由 10℃ 翻转为 0℃
        self.assertEqual(checks[1]["setpoint_delayed"], 0.0)
        self.assertEqual(checks[2]["setpoint_original"], 10.0)
        self.assertEqual(checks[2]["setpoint_delayed"], 0.0)
        self.assertTrue(checks[2]["setpoint_changed"])
        # 翻转后两批均不超限 → 仍可继续运输
        self.assertTrue(r["summary"]["can_continue"])
        self.assertEqual(r["summary"]["setpoint_changed_count"], 1)
        ha = next(h for h in _box(r)["heat_exposure"] if h["batch_no"] == 1)
        self.assertEqual(ha["cumulative_exposure_delayed"], 300.0)
        self.assertFalse(ha["budget_exceeded_delayed"])

    # ---------- 锁温组:延长组内一段,共享设定点仍在组内各段一致 ----------
    def test_lock_group_keeps_shared_setpoint(self):
        plan = {
            "box_capacity": 12,
            "segments": [
                {"name": "干线", "hours": 10, "lock_group": "G",
                 "zones": [{"name": "冷藏区", "t_min": 0, "t_max": 8}]},
                {"name": "支线", "hours": 10, "lock_group": "G",
                 "zones": [{"name": "冷藏区", "t_min": 0, "t_max": 8}]},
            ],
            "cargoes": [
                {"name": "A", "units": 1, "unit_volume": 1, "t_min": 0,
                 "t_max": 8, "preferred_t_min": 2, "preferred_t_max": 4,
                 "max_degree_hours": 1000},
            ],
        }
        r = simulate_delay(plan, segment_no=1, added_hours=5.0)
        checks = _box(r)["segment_checks"]
        self.assertEqual(checks[0]["hours_delayed"], 15.0)
        self.assertEqual(
            checks[0]["shared_setpoint_delayed"],
            checks[1]["shared_setpoint_delayed"],
        )
        self.assertEqual(checks[0]["lock_group_segments"], [1, 2])

    # ---------- 非分段模式:唯一「全程」段 ----------
    def test_single_zone_mode(self):
        r = simulate_delay(SINGLE_PLAN, segment_no=1, added_hours=10.0)
        self.assertFalse(r["segment_mode"])
        self.assertEqual(r["transport_hours_delayed"], 34.0)
        self.assertFalse(r["summary"]["can_continue"])
        self.assertTrue(r["affected_batches"][0]["transport_exceeded"])
        # 未超限场景
        r2 = simulate_delay(SINGLE_PLAN, segment_no=1, added_hours=2.0)
        self.assertTrue(r2["summary"]["can_continue"])

    # ---------- 入参校验 ----------
    def test_invalid_arguments(self):
        with self.assertRaises(ValueError):
            simulate_delay(SEGMENT_PLAN, segment_no=9, added_hours=1.0)
        with self.assertRaises(ValueError):
            simulate_delay(SEGMENT_PLAN, segment_no=1, added_hours=-1.0)

    def test_no_loaded_boxes_rejected(self):
        plan = {
            "box_capacity": 12,
            "zones": [{"name": "冷藏区", "t_min": 0, "t_max": 8}],
            "cargoes": [
                # 与温区无交集,整批未装载
                {"name": "热货", "units": 2, "unit_volume": 1,
                 "t_min": 20, "t_max": 25},
            ],
        }
        with self.assertRaises(ValueError):
            simulate_delay(plan, segment_no=1, added_hours=1.0)


if __name__ == "__main__":
    unittest.main()
