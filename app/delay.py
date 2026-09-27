"""运输延误推演。

在分箱结果页选择一个运输段并填写新增时长(小时,非负),以生成方案时的
输入快照(plan_input,与 /api/plan 请求体相同)和已装箱明细为基准:

- 保持 **箱号、批次及每批件数不变**(不重新装箱),只把指定运输段的时长
  增加指定小时数(非分段模式只有唯一的「全程」段);
- 按规划器原有规则 **重新计算延误后的总时长、逐段设定点与各批累计度时**:
  相邻同锁温组的段仍共用一个设定点,选点与预算判定完全沿用规划器规则
  (候选取可行温度段端点与首选区间端点,使单元结束时箱内最大累计预算
  使用率最低,平局取较低温度;累计暴露严格大于预算即超限);
- 逐箱对比原值与延误后值,列出超出 **最长运输时长** 或 **度时预算** 的
  批次,并标出度时 **首次超限段**;没有受影响批次时明确给出可继续运输。

响应携带输入快照的 SHA-256 指纹,输入变化后前端据此将旧推演标记为已过期。
"""
from __future__ import annotations

from typing import Any

from .planner import (
    EPS,
    _Iv,
    _simulate_heat,
    _r,
    input_fingerprint,
    parse_batches,
    parse_segments,
    plan,
)


def _delay_segments(
    segments: list[Any], segment_no: int, added_hours: float
) -> list[Any]:
    """返回把第 segment_no 段时长增加 added_hours 后的运输段(深拷贝)。"""
    delayed = []
    for seg in segments:
        if seg.no == segment_no:
            delayed.append(
                type(seg)(
                    no=seg.no,
                    name=seg.name,
                    hours=_r(seg.hours + added_hours),
                    zones=list(seg.zones),
                    lock_group=seg.lock_group,
                )
            )
        else:
            delayed.append(seg)  # 其余段只读复用即可
    return delayed


def _setpoint_changed(a: float | None, b: float | None) -> bool:
    if a is None or b is None:
        return False
    return abs(a - b) > 1e-6


def simulate_delay(
    payload: dict[str, Any], segment_no: int, added_hours: float
) -> dict[str, Any]:
    """运输延误推演入口;输入不合法时抛 ValueError(由入口统一转为 400)。"""
    if added_hours < 0:
        raise ValueError("新增时长必须为非负数")

    # 基准方案:按快照重新规划,作为箱号/批次/件数与原值的唯一基准
    baseline = plan(payload)
    segments, base_hours, segment_mode = parse_segments(payload)
    seg_nos = [s.no for s in segments]
    if segment_no not in seg_nos:
        raise ValueError(
            f"运输段序号 {segment_no} 不存在(当前共 {len(seg_nos)} 个运输段)"
        )
    target_seg = next(s for s in segments if s.no == segment_no)
    if not baseline["boxes"]:
        raise ValueError("基准方案没有任何已装箱货物,无法进行延误推演")

    batches = parse_batches(payload)
    lots_by_no = {lot.no: lot for lot in batches}

    delayed_segments = _delay_segments(segments, segment_no, added_hours)
    delayed_hours = _r(base_hours + added_hours)

    # 批号 -> 跨箱汇总(已装件数/所在箱;延误后最大累计度时;各类超限)
    loaded_units: dict[int, int] = {}
    loaded_boxes: dict[int, set[int]] = {}
    for box in baseline["boxes"]:
        for it in box["items"]:
            loaded_units[it["batch_no"]] = (
                loaded_units.get(it["batch_no"], 0) + it["units"]
            )
            loaded_boxes.setdefault(it["batch_no"], set()).add(box["box_no"])

    out_boxes: list[dict[str, Any]] = []
    # 批号 -> 延误后跨箱最大累计暴露与首次超限段(用于顶部受影响批次汇总)
    max_exposure: dict[int, float] = {}
    affected: set[int] = set()
    setpoint_changed_count = 0
    name_by_no = {seg.no: seg.name for seg in delayed_segments}

    for box in baseline["boxes"]:
        # 已装箱明细固定:可行温度交集由箱内各批允许温区重新求交,与规划器一致
        lots = [lots_by_no[it["batch_no"]] for it in box["items"]]
        feasible: _Iv | None = None
        for lot in lots:
            feasible = lot.iv if feasible is None else feasible.intersect(lot.iv)
        assert feasible is not None

        # 延误后按规划器规则重新选点(锁温组仍共用设定点)并累计各批度时
        sim = _simulate_heat(delayed_segments, feasible, lots)
        new_sp = sim["setpoints"]

        # 逐段对比:原时长/设定点 vs 延误后时长/设定点(温度段与温区不变)
        seg_checks: list[dict[str, Any]] = []
        for i, seg in enumerate(delayed_segments):
            base_seg = segments[i]
            chk = box["segment_checks"][i]
            changed = _setpoint_changed(
                chk["recommended_setpoint"], new_sp[i]
            )
            if changed:
                setpoint_changed_count += 1
            seg_checks.append(
                {
                    "segment_no": seg.no,
                    "name": seg.name,
                    "hours_original": _r(base_seg.hours),
                    "hours_delayed": _r(seg.hours),
                    "hours_added": _r(added_hours if seg.no == segment_no else 0.0),
                    "temperature_segments": chk["temperature_segments"],
                    "matched_zones": chk["matched_zones"],
                    "setpoint_original": chk["recommended_setpoint"],
                    "setpoint_delayed": _r(new_sp[i]) if new_sp[i] is not None else None,
                    "setpoint_changed": changed,
                    "lock_group": chk["lock_group"],
                    "lock_group_segments": chk["lock_group_segments"],
                    "shared_setpoint_original": chk["shared_setpoint"],
                    "shared_setpoint_delayed": (
                        _r(new_sp[i])
                        if chk["lock_group"] and new_sp[i] is not None
                        else None
                    ),
                }
            )

        # 各批对比:原累计度时/预算判定 vs 延误后,并检查最长运输时长
        # (顺序沿用基准方案该箱的装载顺序)
        heat: list[dict[str, Any]] = []
        box_affected = False
        for chk in box["heat_exposure"]:
            no = chk["batch_no"]
            lot = lots_by_no[no]
            new_exp = sim["exposures"].get(no)
            first_no = sim["first_exceeded"].get(no)
            budget_new_exceeded = (
                no in sim["first_exceeded"] if lot.budget is not None else None
            )
            # 基准方案由规划器保证已装箱批次不超限、不超时;仍保留原值字段对比
            trans_exceeded = (
                lot.max_hours is not None and delayed_hours > lot.max_hours + EPS
            )
            is_affected = bool(
                trans_exceeded
                or (budget_new_exceeded if budget_new_exceeded is not None else False)
            )
            if is_affected:
                box_affected = True
                affected.add(no)
            if new_exp is not None:
                prev = max_exposure.get(no, float("-inf"))
                if new_exp > prev:
                    max_exposure[no] = new_exp

            reasons: list[str] = []
            if trans_exceeded:
                reasons.append(
                    f"延误后总时长 {_r(delayed_hours)}h 超过最长运输时长 "
                    f"{_r(lot.max_hours)}h"
                )
            if budget_new_exceeded:
                reasons.append(
                    f"延误后累计度时 {_r(new_exp)} ℃·h 超过度时预算 "
                    f"{_r(lot.budget)} ℃·h"
                    + (
                        f"(首次超限:第 {first_no} 段「{name_by_no[first_no]}」)"
                        if first_no
                        else ""
                    )
                )

            heat.append(
                {
                    "batch_no": no,
                    "name": lot.name,
                    "units": next(
                        it["units"] for it in box["items"] if it["batch_no"] == no
                    ),
                    "preferred_t_min": _r(lot.pref.lo) if lot.pref else None,
                    "preferred_t_max": _r(lot.pref.hi) if lot.pref else None,
                    "max_degree_hours": (
                        _r(lot.budget) if lot.budget is not None else None
                    ),
                    "cumulative_exposure_original": chk["cumulative_exposure"],
                    "cumulative_exposure_delayed": (
                        _r(new_exp) if new_exp is not None else None
                    ),
                    "exposure_increase": (
                        _r(new_exp - chk["cumulative_exposure"])
                        if new_exp is not None
                        and chk["cumulative_exposure"] is not None
                        else None
                    ),
                    "budget_exceeded_original": chk["exceeded"],
                    "budget_exceeded_delayed": budget_new_exceeded,
                    "first_exceeded_segment_no": first_no,
                    "first_exceeded_segment_name": (
                        name_by_no.get(first_no) if first_no else None
                    ),
                    "max_transport_hours": (
                        _r(lot.max_hours) if lot.max_hours is not None else None
                    ),
                    "transport_exceeded": trans_exceeded,
                    "affected": is_affected,
                    "reasons": reasons,
                }
            )

        out_boxes.append(
            {
                "box_no": box["box_no"],
                "temperature_segments": box["temperature_segments"],
                "segment_checks": seg_checks,
                "heat_exposure": heat,
                "affected": box_affected,
            }
        )

    # 批号 -> 跨箱最早的度时首次超限段(不同箱设定点可能不同,取最早)
    earliest_first_exceed: dict[int, int] = {}
    for box in out_boxes:
        for h in box["heat_exposure"]:
            no = h["batch_no"]
            fno = h["first_exceeded_segment_no"]
            if fno is not None and (
                no not in earliest_first_exceed or fno < earliest_first_exceed[no]
            ):
                earliest_first_exceed[no] = fno

    # 顶部汇总:受影响批次(跨箱去重,按批号排序)
    affected_batches: list[dict[str, Any]] = []
    for no in sorted(affected):
        lot = lots_by_no[no]
        first_no = earliest_first_exceed.get(no)
        trans_exceeded = (
            lot.max_hours is not None and delayed_hours > lot.max_hours + EPS
        )
        affected_batches.append(
            {
                "batch_no": no,
                "name": lot.name,
                "loaded_units": loaded_units[no],
                "box_nos": sorted(loaded_boxes[no]),
                "max_transport_hours": (
                    _r(lot.max_hours) if lot.max_hours is not None else None
                ),
                "transport_hours_delayed": delayed_hours,
                "transport_exceeded": trans_exceeded,
                "max_degree_hours": (
                    _r(lot.budget) if lot.budget is not None else None
                ),
                "max_cumulative_exposure_delayed": (
                    _r(max_exposure[no]) if no in max_exposure else None
                ),
                "first_exceeded_segment_no": first_no,
                "first_exceeded_segment_name": (
                    name_by_no.get(first_no) if first_no else None
                ),
            }
        )

    return {
        "input_fingerprint": input_fingerprint(payload),
        "delay": {
            "segment_no": target_seg.no,
            "segment_name": target_seg.name,
            "hours_original": _r(target_seg.hours),
            "added_hours": _r(added_hours),
            "hours_delayed": _r(target_seg.hours + added_hours),
        },
        "segment_mode": segment_mode,
        "transport_hours_original": _r(base_hours),
        "transport_hours_delayed": delayed_hours,
        "transport_hours_increase": _r(added_hours),
        "boxes": out_boxes,
        "affected_batches": affected_batches,
        "summary": {
            "box_count": len(out_boxes),
            "affected_box_count": sum(1 for b in out_boxes if b["affected"]),
            "affected_batch_count": len(affected_batches),
            "setpoint_changed_count": setpoint_changed_count,
            "can_continue": not affected_batches,
        },
        "volume_unit": baseline["volume_unit"],
    }
