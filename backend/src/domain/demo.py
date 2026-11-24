"""临汀市虚拟路灯台账、已归并的暗区事件、现场实测与夜巡名额。"""


_SITES = (
    # (片区, 点位, 灯具数, 有工单的日期), 是否实测暗区
    ("滨河片区", "柳汀大道北段", 32, (14, 14, 15, 16, 17, 18, 19, 20), False),
    ("老城片区", "东门巷", 5, (14, 16, 17, 19, 20), True),
    ("滨河片区", "汀桥路公交站段", 24, (15, 17, 18, 20), False),
    ("滨河片区", "长堤辅道", 3, (16, 20), True),
    ("老城片区", "西仓里路口", 2, (19,), True),
    ("滨河片区", "渡口支路", 9, (), True),
    ("滨河片区", "桐浦堤下通道", 6, (), False),
    ("老城片区", "埠头街南段", 14, (), False),
    ("老城片区", "石平码头广场路", 11, (), False),
    ("老城片区", "钟楼西街", 17, (), False),
)

# 11 月 14–20 日每晚全市可入列灯组上限。
_QUOTA_BY_NIGHT = {
    "2026-11-14": 2,
    "2026-11-15": 2,
    "2026-11-16": 2,
    "2026-11-17": 2,
    "2026-11-18": 2,
    "2026-11-19": 2,
    "2026-11-20": 2,
}


def demo_rows() -> list[dict]:
    rows = []
    for number, (district, site, fixtures, days, is_dark) in enumerate(_SITES, start=1):
        site_id = f"LH-{number:02d}"
        rows.append({
            "record_type": "灯组台账", "site_id": site_id, "district": district,
            "site_name": site, "fixture_count": fixtures, "incident_id": None, "report_date": None,
            "is_dark": None, "quota": None,
        })
        for sequence, day in enumerate(days, start=1):
            rows.append({
                "record_type": "暗区工单", "site_id": site_id, "district": None,
                "site_name": None, "fixture_count": None,
                "incident_id": f"LT-2611{day}-{number:02d}-{sequence:02d}",
                "report_date": f"2026-11-{day}", "is_dark": None, "quota": None,
            })
        rows.append({
            "record_type": "现场实测", "site_id": site_id, "district": None,
            "site_name": None, "fixture_count": None, "incident_id": None,
            "report_date": None, "is_dark": "是" if is_dark else "否", "quota": None,
        })
    for night, quota in _QUOTA_BY_NIGHT.items():
        rows.append({
            "record_type": "夜巡名额", "site_id": "全市", "district": None,
            "site_name": None, "fixture_count": None, "incident_id": None,
            "report_date": night, "is_dark": None, "quota": quota,
        })
    return rows
