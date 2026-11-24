"""逐夜派巡门槛回测。

用逐夜报修工单和逐组现场实测复盘历史派巡：
- 每个片区在整个回测周期内固定一对门槛（起派单数、每百盏单数）。
- 每晚按各灯组截至当晚的累计工单数套用片区门槛，再按全市统一的
  未入列优先级排序，当晚入列组数不超过当晚全市夜巡名额。
- 现场实测为暗区、但到当晚结束仍未入列的灯组，记为当晚漏掉的暗区。
- 在全部候选门槛组合中，先把最高的片区漏报率降到最低；同分时选全市
  漏掉暗区夜数最少的；再同分用门槛本身做确定性裁决。
- 没有实测暗区的片区，漏报率记为零，不参与分母。

本模块只依赖标准库，便于在没有第三方依赖的环境下直接单测。
"""

from collections import defaultdict
from datetime import date, timedelta
from fractions import Fraction

from .spec import SPEC

_REGISTER, _INCIDENT = SPEC["fields"][0]["options"][:2]
_MEASURE, _QUOTA = SPEC["backtest_record_types"]
_DARK_YES, _DARK_NO = SPEC["measure_options"]
# 单个回测任务允许评估的候选组合上限，防止灯组规模放大后组合爆炸。
CANDIDATE_LIMIT = 50_000
# 每个工作租约内连续评估的候选数，便于中断后快速让出并恢复。
BATCH_SIZE = 100


def _text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label}不能为空，请填写文本。")
    return value.strip()


def _iso_date(value, label):
    if isinstance(value, str):
        try:
            if date.fromisoformat(value).isoformat() == value:
                return value
        except ValueError:
            pass
    raise ValueError(f"{label}须使用有效的 YYYY-MM-DD 日期。")


def _night_range(start, end):
    first = date.fromisoformat(start)
    last = date.fromisoformat(end)
    if last < first:
        raise ValueError("回测起日须早于或等于回测止日。")
    nights = []
    cursor = first
    while cursor <= last:
        nights.append(cursor.isoformat())
        cursor += timedelta(days=1)
    return nights


def prepare(rows, params):
    """校验并冻结回测输入，返回逐夜回放所需的不可变快照。

    快照在回测开始时确定，中断恢复时直接复用，不重新读取外部输入。
    """
    if not isinstance(rows, (list, tuple)) or not rows:
        raise ValueError("灯组与工单明细为空，请导入灯组台账。")

    sites = {}
    incidents = []
    measurements = {}
    quotas = {}

    for index, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            raise ValueError(f"第 {index} 条记录格式有误。")
        kind = row.get("record_type")

        if kind == _QUOTA:
            # 夜巡名额是全市口径，不绑定灯组编号。
            site_id = None
        else:
            site_id = _text(row.get("site_id"), f"第 {index} 条记录的灯组编号").upper()

        if kind == _REGISTER:
            if site_id in sites:
                raise ValueError(f"灯组编号「{site_id}」在台账中重复。")
            fixtures = row.get("fixture_count")
            if type(fixtures) is not int or fixtures < 1:
                raise ValueError(f"灯组「{site_id}」的在册灯具数须为正整数。")
            sites[site_id] = {
                "site_id": site_id,
                "district": _text(row.get("district"), f"灯组「{site_id}」的台账片区"),
                "site_name": _text(row.get("site_name"), f"灯组「{site_id}」的台账点位"),
                "fixture_count": fixtures,
            }
        elif kind == _INCIDENT:
            incidents.append({
                "site_id": site_id,
                "incident_id": _text(row.get("incident_id"), f"第 {index} 条记录的暗区工单号").upper(),
                "report_date": _iso_date(row.get("report_date"), f"第 {index} 条工单的报修日期"),
            })
        elif kind == _MEASURE:
            if site_id not in sites:
                raise ValueError(f"实测记录的灯组「{site_id}」不在台账中，请先补录台账。")
            if site_id in measurements:
                raise ValueError(f"灯组「{site_id}」存在多条现场实测，每组只能登记一条。")
            flag = row.get("is_dark")
            if flag not in (_DARK_YES, _DARK_NO):
                raise ValueError(f"灯组「{site_id}」的实测暗区须为「{_DARK_YES}」或「{_DARK_NO}」。")
            measurements[site_id] = flag == _DARK_YES
        elif kind == _QUOTA:
            night = _iso_date(row.get("report_date"), f"第 {index} 条夜巡名额记录的日期")
            quota = row.get("quota")
            if type(quota) is not int or quota < 0:
                raise ValueError(f"{night} 的夜巡名额须为不小于零的整数。")
            if night in quotas:
                raise ValueError(f"{night} 存在多条夜巡名额，每晚只能登记一条。")
            quotas[night] = quota
        else:
            raise ValueError(
                f"第 {index} 条记录类型须为「{_REGISTER}」「{_INCIDENT}」「{_MEASURE}」或「{_QUOTA}」。"
            )

    if not sites:
        raise ValueError("灯组台账为空，请为在册灯组提供台账行。")

    incident_ids = set()
    for incident in incidents:
        if incident["site_id"] not in sites:
            raise ValueError(
                f"工单「{incident['incident_id']}」未匹配灯组「{incident['site_id']}」，请补录或更正台账。"
            )
        if incident["incident_id"] in incident_ids:
            raise ValueError(f"暗区工单号「{incident['incident_id']}」重复。")
        incident_ids.add(incident["incident_id"])

    missing_measure = sorted(set(sites) - set(measurements))
    if missing_measure:
        raise ValueError(
            "以下灯组缺少逐组现场实测：" + "、".join(missing_measure) + "。回测须覆盖全部在册灯组（含未入列灯组）。"
        )

    start = _iso_date(params.get("start_date"), "回测起日")
    end = _iso_date(params.get("end_date"), "回测止日")
    nights = _night_range(start, end)

    scoped_incidents = [
        item for item in incidents
        if start <= item["report_date"] <= end
    ]
    missing_quota = [night for night in nights if night not in quotas]
    if missing_quota:
        raise ValueError("以下夜班缺少全市夜巡名额：" + "、".join(missing_quota) + "。请在名额记录中补齐。")
    if any(quotas[night] <= 0 for night in nights):
        raise ValueError("回测周期内每晚夜巡名额须大于零，否则当晚无法安排任何灯组。")

    # 按夜归集工单，没工单的夜晚保持空列表。
    tickets_by_night = defaultdict(list)
    for incident in sorted(scoped_incidents, key=lambda item: (item["report_date"], item["incident_id"])):
        tickets_by_night[incident["report_date"]].append(incident)

    districts = sorted({site["district"] for site in sites.values()})
    dark_sites = {site_id for site_id, is_dark in measurements.items() if is_dark}
    dark_by_district = {
        district: sorted(
            site_id for site_id in dark_sites if sites[site_id]["district"] == district
        )
        for district in districts
    }

    return {
        "sites": sites,
        "nights": nights,
        "tickets_by_night": {night: tickets_by_night.get(night, []) for night in nights},
        "quotas": {night: quotas[night] for night in nights},
        "districts": districts,
        "dark_sites": dark_sites,
        "dark_by_district": dark_by_district,
        "measurements": dict(measurements),
        "total_incidents": len(scoped_incidents),
    }


def candidate_grids(snapshot):
    """生成每个片区的候选门槛网格。

    单数门槛取该片区灯组在任意夜晚可能达到的累计工单数，并补一个高于
    全部累计值的自然停派档；每百盏门槛取这些累计状态对应的全部不同
    每百盏报修数（精确分数）。只在真实状态变化点取候选，可在不遗漏任何
    不同派巡结果的前提下压缩搜索空间。
    """
    sites = snapshot["sites"]
    counts = {site_id: 0 for site_id in sites}
    reachable_counts = defaultdict(set)
    reachable_rates = defaultdict(set)

    # 零单也是一种可达状态（门槛高于零时零单灯组不会入列）。
    for site_id, site in sites.items():
        reachable_counts[site["district"]].add(0)
        reachable_rates[site["district"]].add(Fraction(0))

    for night in snapshot["nights"]:
        for ticket in snapshot["tickets_by_night"][night]:
            counts[ticket["site_id"]] += 1
        for site_id, site in sites.items():
            total = counts[site_id]
            reachable_counts[site["district"]].add(total)
            reachable_rates[site["district"]].add(Fraction(100 * total, site["fixture_count"]))

    grids = {}
    for district in snapshot["districts"]:
        count_values = sorted(reachable_counts[district])
        stop_value = count_values[-1] + 1
        count_grid = count_values + [stop_value]
        rate_grid = sorted(reachable_rates[district])
        grids[district] = [
            {"min_incidents": count_value, "min_rate": rate}
            for count_value in count_grid
            for rate in rate_grid
        ]
    return grids


def all_candidates(snapshot):
    """按确定性顺序枚举全市候选：每片区选一对固定门槛的笛卡尔积。"""
    grids = candidate_grids(snapshot)
    districts = snapshot["districts"]

    def build(index, chosen):
        if index == len(districts):
            yield dict(chosen)
            return
        district = districts[index]
        for option in grids[district]:
            chosen[district] = option
            yield from build(index + 1, chosen)
        chosen.pop(district, None)

    candidates = list(build(0, {}))
    if len(candidates) > CANDIDATE_LIMIT:
        raise ValueError(
            f"候选门槛组合共 {len(candidates)} 个，超过上限 {CANDIDATE_LIMIT}。请缩小回测片区或灯组范围。"
        )
    return candidates


def replay(snapshot, thresholds):
    """用给定的各片区固定门槛逐夜回放，返回完整派巡与漏报轨迹。"""
    sites = snapshot["sites"]
    counts = {site_id: 0 for site_id in sites}
    dispatched = set()
    first_dispatch_night = {}
    night_rows = []
    missed_night_total = 0
    district_missed = defaultdict(int)

    for night in snapshot["nights"]:
        for ticket in snapshot["tickets_by_night"][night]:
            counts[ticket["site_id"]] += 1

        eligible = []
        for site_id, site in sites.items():
            if site_id in dispatched:
                continue
            total = counts[site_id]
            rule = thresholds[site["district"]]
            rate = Fraction(100 * total, site["fixture_count"])
            if total >= rule["min_incidents"] and rate >= rule["min_rate"]:
                # 与现网派巡队列一致：未舍入每百盏单数、工单数降序，再按编号升序。
                eligible.append((-rate, -total, site_id))
        eligible.sort()

        quota = snapshot["quotas"][night]
        picked = [site_id for _, _, site_id in eligible[:quota]]
        for site_id in picked:
            dispatched.add(site_id)
            first_dispatch_night[site_id] = night

        missed_sites = sorted(
            site_id for site_id in snapshot["dark_sites"] if site_id not in dispatched
        )
        for site_id in missed_sites:
            district_missed[sites[site_id]["district"]] += 1
        missed_night_total += len(missed_sites)

        night_rows.append({
            "night": night,
            "quota": quota,
            "eligible_count": len(eligible),
            "queued_site_ids": picked,
            "missed_dark_site_ids": missed_sites,
            "missed_dark_count": len(missed_sites),
        })

    district_rates = {}
    night_total = len(snapshot["nights"])
    for district in snapshot["districts"]:
        dark_total = len(snapshot["dark_by_district"][district])
        if dark_total == 0:
            # 无暗区的片区漏报率算零。
            district_rates[district] = 0.0
        else:
            # 分子为漏掉暗区组夜数，分母为实测暗区组数乘夜班数。
            district_rates[district] = round(
                district_missed[district] / (dark_total * night_total), 6
            )

    return {
        "night_rows": night_rows,
        "missed_night_total": missed_night_total,
        "district_missed": dict(district_missed),
        "district_rates": district_rates,
        "dispatched": sorted(dispatched),
        "first_dispatch_night": dict(first_dispatch_night),
        "max_miss_rate": max(district_rates.values()),
    }


def _threshold_key(snapshot, thresholds):
    return tuple(
        (
            thresholds[district]["min_incidents"],
            thresholds[district]["min_rate"].numerator,
            thresholds[district]["min_rate"].denominator,
        )
        for district in snapshot["districts"]
    )


def better(snapshot, current, candidate):
    """比较两个已评估候选，返回 candidate 是否严格更优。"""
    current_rates = tuple(current["outcome"]["district_rates"][d] for d in snapshot["districts"])
    candidate_rates = tuple(candidate["outcome"]["district_rates"][d] for d in snapshot["districts"])
    current_key = (max(current_rates), current["outcome"]["missed_night_total"])
    candidate_key = (max(candidate_rates), candidate["outcome"]["missed_night_total"])
    if candidate_key != current_key:
        return candidate_key < current_key
    # 同分再用门槛本身裁决：单数、每百盏单数都偏好更小、更早可派的门槛，结果确定可复现。
    return _threshold_key(snapshot, candidate["thresholds"]) < _threshold_key(snapshot, current["thresholds"])


def candidate_signature(snapshot, thresholds):
    parts = []
    for district in snapshot["districts"]:
        rule = thresholds[district]
        rate = rule["min_rate"]
        parts.append(f"{district}|{rule['min_incidents']}|{rate.numerator}/{rate.denominator}")
    return "||".join(parts)


def to_jsonable(value):
    """把快照与候选中的 Fraction 转成可落库 JSON 的纯结构。"""
    if isinstance(value, Fraction):
        return {"__fraction__": [value.numerator, value.denominator]}
    if isinstance(value, dict):
        return {key: to_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_jsonable(item) for item in value]
    if isinstance(value, set):
        return [to_jsonable(item) for item in sorted(value)]
    return value


def from_jsonable(value):
    """把数据库 JSON 还原成带 Fraction 的回测结构。"""
    if isinstance(value, dict):
        if set(value) == {"__fraction__"} and isinstance(value["__fraction__"], list):
            numerator, denominator = value["__fraction__"]
            return Fraction(numerator, denominator)
        return {key: from_jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [from_jsonable(item) for item in value]
    return value


def run_backtest(rows, params, progress=None):
    """一次性跑完整个回测，返回前端与 CSV 共用的结果结构。

    progress 为可选回调 progress(done, total)，服务层分批执行时不使用本函数，
    本函数主要供离线脚本与测试使用。
    """
    snapshot = prepare(rows, params)
    candidates = all_candidates(snapshot)
    evaluated = {}
    best = None
    for index, thresholds in enumerate(candidates):
        signature = candidate_signature(snapshot, thresholds)
        outcome = replay(snapshot, thresholds)
        candidate = {"thresholds": thresholds, "outcome": outcome, "signature": signature}
        evaluated[signature] = outcome
        if best is None or better(snapshot, best, candidate):
            best = candidate
        if progress:
            progress(index + 1, len(candidates))
    return build_result(snapshot, best, len(candidates), len(evaluated))


def build_result(snapshot, best, candidate_total, evaluated_total):
    """把最优候选整理成与分析页一致的 tables/charts/metrics 结构。"""
    thresholds = best["thresholds"]
    outcome = best["outcome"]
    sites = snapshot["sites"]

    threshold_rows = [
        {
            "district": district,
            "min_incidents": thresholds[district]["min_incidents"],
            "min_rate": round(float(thresholds[district]["min_rate"]), 2),
            "dark_sites": len(snapshot["dark_by_district"][district]),
            "missed_nights": outcome["district_missed"].get(district, 0),
            "miss_rate_pct": round(outcome["district_rates"][district] * 100, 4),
        }
        for district in snapshot["districts"]
    ]

    night_rows = []
    for row in outcome["night_rows"]:
        night_rows.append({
            "night": row["night"],
            "quota": row["quota"],
            "eligible_count": row["eligible_count"],
            "queued_site_ids": "、".join(row["queued_site_ids"]) or "—",
            "missed_dark_site_ids": "、".join(row["missed_dark_site_ids"]) or "—",
            "missed_dark_count": row["missed_dark_count"],
        })

    queue_rows = []
    for site_id in sorted(sites):
        site = sites[site_id]
        queue_rows.append({
            "site_id": site_id,
            "district": site["district"],
            "site_name": site["site_name"],
            "fixture_count": site["fixture_count"],
            "is_dark": _DARK_YES if site_id in snapshot["dark_sites"] else _DARK_NO,
            "dispatch_night": outcome["first_dispatch_night"].get(site_id, "未入列"),
        })

    total_nights = len(snapshot["nights"])
    headline = (
        f"回测 {total_nights} 个夜班、{snapshot['total_incidents']} 张工单，"
        f"评估 {candidate_total} 套候选门槛，最高片区漏报率 "
        f"{outcome['max_miss_rate']:.2%}，全市漏掉暗区 {outcome['missed_night_total']} 组夜。"
    )
    note = (
        f"回测区间 {snapshot['nights'][0]} 至 {snapshot['nights'][-1]}。"
        "每片区固定一对门槛，每晚全市入列组数不超过当晚名额；"
        "未入列的实测暗区灯组同样计入漏报。"
    )

    return {
        "state": "warning" if outcome["missed_night_total"] else "ok",
        "headline": headline,
        "note": note,
        "metrics": [
            {"label": "回测夜班", "value": total_nights, "detail": f"起 {snapshot['nights'][0]}，止 {snapshot['nights'][-1]}"},
            {"label": "在册灯组", "value": len(sites), "detail": f"实测暗区 {len(snapshot['dark_sites'])} 组"},
            {"label": "纳入工单", "value": snapshot["total_incidents"], "detail": "按报修日期逐夜回放"},
            {"label": "候选门槛组合", "value": candidate_total, "detail": f"已评估 {evaluated_total} 套，已评候选不重算"},
            {"label": "最高片区漏报率（%）", "value": round(outcome["max_miss_rate"] * 100, 4),
             "detail": "无暗区片区按零参与比较"},
            {"label": "全市漏掉暗区（组夜）", "value": outcome["missed_night_total"],
             "detail": "每晚未入列的实测暗区组数累加"},
        ],
        "tables": [
            {
                "title": "选中的固定门槛（按片区）",
                "columns": [
                    {"key": "district", "label": "片区"},
                    {"key": "min_incidents", "label": "起派单数（单）"},
                    {"key": "min_rate", "label": "每百盏门槛（单）"},
                    {"key": "dark_sites", "label": "实测暗区组数"},
                    {"key": "missed_nights", "label": "漏掉暗区（组夜）"},
                    {"key": "miss_rate_pct", "label": "片区漏报率（%）"},
                ],
                "rows": threshold_rows,
            },
            {
                "title": "逐夜派巡与漏掉的暗区",
                "columns": [
                    {"key": "night", "label": "夜班日期"},
                    {"key": "quota", "label": "当晚名额"},
                    {"key": "eligible_count", "label": "达标候选组数"},
                    {"key": "queued_site_ids", "label": "当晚入列灯组"},
                    {"key": "missed_dark_site_ids", "label": "当晚漏掉的暗区"},
                    {"key": "missed_dark_count", "label": "漏掉暗区组数"},
                ],
                "rows": night_rows,
            },
            {
                "title": "逐组实测与入列结果",
                "columns": [
                    {"key": "site_id", "label": "灯组编号"},
                    {"key": "district", "label": "片区"},
                    {"key": "site_name", "label": "台账点位"},
                    {"key": "fixture_count", "label": "在册灯具数"},
                    {"key": "is_dark", "label": "现场实测暗区"},
                    {"key": "dispatch_night", "label": "首次入列夜班"},
                ],
                "rows": queue_rows,
            },
        ],
        "charts": [],
        "sections": [
            {"title": "回测结论与门槛口径",
             "text": ("先最小化最高片区漏报率，再最小化全市漏掉暗区组夜数；"
                      "同分时按单数、每百盏单数的确定性顺序裁决，只发布一套门槛。"
                      "中断恢复沿用开算时冻结的工单、实测和名额，已评候选不重算。")},
        ],
        "objective": {
            "max_miss_rate": outcome["max_miss_rate"],
            "missed_night_total": outcome["missed_night_total"],
            "candidate_total": candidate_total,
            "evaluated_total": evaluated_total,
        },
    }
