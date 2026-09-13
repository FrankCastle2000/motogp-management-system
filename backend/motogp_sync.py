"""MotoGP 官方积分榜、分站赛果与比赛日程的低频同步客户端。"""

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from zoneinfo import ZoneInfo
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from config import MOTOGP_API_BASE_URL, MOTOGP_SYNC_TIMEOUT_SECONDS


BEIJING_TIMEZONE = ZoneInfo("Asia/Shanghai")

EVENT_COUNTRY_ZH = {
    "THA": "泰国",
    "ARG": "阿根廷",
    "AME": "美国",
    "QAT": "卡塔尔",
    "SPA": "西班牙",
    "FRA": "法国",
    "GBR": "英国",
    "ARA": "阿拉贡",
    "ITA": "意大利",
    "NED": "荷兰",
    "GER": "德国",
    "CZE": "捷克",
    "AUT": "奥地利",
    "HUN": "匈牙利",
    "CAT": "加泰罗尼亚",
    "RSM": "圣马力诺",
    "JPN": "日本",
    "INA": "印度尼西亚",
    "AUS": "澳大利亚",
    "MAL": "马来西亚",
    "POR": "葡萄牙",
    "VAL": "瓦伦西亚",
    "BRA": "巴西",
}


def _country_flag(country_code: str):
    code = str(country_code or "").strip().upper()
    if len(code) != 2 or not code.isalpha():
        return "🏁"
    return "".join(chr(0x1F1E6 + ord(char) - ord("A")) for char in code)


class OfficialApiError(ValueError):
    """官方接口不可用或返回了无法验证的数据。"""


def _get_json(path: str, params=None):
    url = f"{MOTOGP_API_BASE_URL}{path}"
    if params:
        url = f"{url}?{urlencode(params)}"
    request = Request(
        url,
        headers={
            "Accept": "application/json",
            "Origin": "https://www.motogp.com",
            "Referer": "https://www.motogp.com/",
            "User-Agent": "MotoGP-Management-System/1.0 (manual official data sync)",
        },
    )
    try:
        with urlopen(request, timeout=max(1, MOTOGP_SYNC_TIMEOUT_SECONDS)) as response:
            if response.status != 200:
                raise OfficialApiError(f"官方接口返回 HTTP {response.status}")
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        raise OfficialApiError(f"官方接口返回 HTTP {exc.code}") from exc
    except URLError as exc:
        raise OfficialApiError("无法连接 MotoGP 官方接口") from exc
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise OfficialApiError("MotoGP 官方接口返回的数据格式无效") from exc


def _parse_official_datetime(value):
    """解析官方带时区 ISO 时间，并转换成北京时间。"""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("官方时间缺少时区")
        return parsed.astimezone(BEIJING_TIMEZONE)
    except ValueError as exc:
        raise OfficialApiError(f"官方日程包含无法识别的时间：{text}") from exc


def fetch_season_schedules(season_year: int):
    """一次读取全部大奖赛的官方赛道 Session，并统一转换成北京时间。"""
    events = _get_json("/v1/events", {"seasonYear": season_year})
    if isinstance(events, dict) and "value" in events:
        events = events["value"]
    if not isinstance(events, list):
        raise OfficialApiError("官方接口没有返回有效的赛季事件列表")

    grand_prix_events = [
        event for event in events
        if str(event.get("kind") or "").upper() == "GP"
        and int((event.get("season") or {}).get("year") or 0) == season_year
    ]
    grand_prix_events.sort(
        key=lambda event: (
            int(event.get("sequence") or 999),
            str(event.get("date_start") or ""),
        )
    )
    if not grand_prix_events:
        raise OfficialApiError(f"官方接口中没有 {season_year} 赛季大奖赛日程")

    schedules = []
    for fallback_round, event in enumerate(grand_prix_events, start=1):
        circuit = event.get("circuit") or {}
        shortname = str(event.get("shortname") or "").strip().upper()
        country_code = str(
            event.get("country") or circuit.get("iso_code") or ""
        ).strip().upper()
        items = []
        seen = set()
        for session in event.get("broadcasts") or []:
            category = session.get("category") or {}
            category_name = str(category.get("name") or "").strip()
            session_name = str(session.get("name") or "").strip()
            if (
                str(session.get("type") or "").upper() != "SESSION"
                or not category_name
                or not session_name
            ):
                continue
            starts_at = _parse_official_datetime(session.get("date_start"))
            ends_at = _parse_official_datetime(session.get("date_end"))
            if starts_at is None:
                continue
            end_time = ""
            if ends_at is not None and ends_at > starts_at:
                end_time = ends_at.strftime("%H:%M")
            laps = int(session.get("num_laps") or 0)
            if str(session.get("kind") or "").upper() == "RACE" and laps > 0:
                session_name = f"{session_name} ({laps} Laps)"
            item = {
                "schedule_date": starts_at.strftime("%Y-%m-%d"),
                "start_time": starts_at.strftime("%H:%M"),
                "end_time": end_time,
                "category": category_name,
                "session_name": session_name,
            }
            unique_key = (
                item["schedule_date"], item["start_time"], category_name, session_name
            )
            if unique_key in seen:
                continue
            seen.add(unique_key)
            items.append(item)
        items.sort(
            key=lambda item: (
                item["schedule_date"], item["start_time"], item["category"]
            )
        )
        try:
            round_number = int(event.get("sequence") or fallback_round)
        except (TypeError, ValueError):
            round_number = fallback_round
        schedules.append({
            "round_number": round_number,
            "official_event_id": str(event.get("id") or ""),
            "official_event_name": str(event.get("name") or ""),
            "shortname": str(event.get("shortname") or ""),
            "time_zone": str(event.get("time_zone") or ""),
            "start_date": str(event.get("date_start") or "")[:10],
            "end_date": str(event.get("date_end") or "")[:10],
            "flag": _country_flag(country_code),
            "country": EVENT_COUNTRY_ZH.get(
                shortname,
                str(circuit.get("country") or event.get("additional_name") or shortname),
            ),
            "country_en": str(
                event.get("additional_name") or circuit.get("country") or shortname
            ).strip().upper(),
            "circuit": str(circuit.get("name") or event.get("name") or "未知赛道").strip(),
            "items": items,
        })
    return {"season": season_year, "events": schedules}


def fetch_rider_standings(season_year: int):
    """读取指定赛季 MotoGP 车手积分榜并转换为系统所需字段。"""
    seasons = _get_json("/v1/results/seasons")
    season = next(
        (item for item in seasons if int(item.get("year", 0)) == season_year),
        None,
    )
    if not season or not season.get("id"):
        raise OfficialApiError(f"官方接口中没有 {season_year} 赛季")

    categories = _get_json(
        "/v1/results/categories",
        {"seasonUuid": season["id"]},
    )
    if isinstance(categories, dict) and "value" in categories:
        categories = categories["value"]
    category = next(
        (
            item for item in categories
            if item.get("legacy_id") == 3
            or "motogp" in str(item.get("name", "")).lower()
        ),
        None,
    )
    if not category or not category.get("id"):
        raise OfficialApiError(f"官方接口中没有 {season_year} MotoGP 组别")

    result = _get_json(
        "/v2/results/world-standings",
        {
            "type": "rider",
            "season": season["id"],
            "category": category["id"],
        },
    )
    classification = (result.get("classification") or {}).get("rider") or []
    if not classification:
        raise OfficialApiError("官方接口没有返回车手积分数据")

    rows = []
    for item in classification:
        rider = item.get("rider") or {}
        number = rider.get("number")
        try:
            row = {
                "position": int(item["position"]),
                "rider_number": str(number),
                "official_name": str(rider.get("full_name") or ""),
                "points": int(item.get("points") or 0),
                "race_wins": int(item.get("race_wins") or 0),
                "podiums": int(item.get("podiums") or 0),
            }
        except (KeyError, TypeError, ValueError) as exc:
            raise OfficialApiError("官方积分榜包含无法识别的记录") from exc
        if not row["rider_number"] or row["position"] < 1:
            raise OfficialApiError("官方积分榜包含无效的车号或排名")
        rows.append(row)

    return {
        "season": season_year,
        "official": bool(result.get("official")),
        "source_file": (result.get("files") or {}).get("pdf"),
        "rows": rows,
    }


def _find_season_and_motogp_category(season_year: int, event_uuid: str | None = None):
    seasons = _get_json("/v1/results/seasons")
    season = next(
        (item for item in seasons if int(item.get("year", 0)) == season_year),
        None,
    )
    if not season or not season.get("id"):
        raise OfficialApiError(f"官方接口中没有 {season_year} 赛季")

    category_params = {"eventUuid": event_uuid} if event_uuid else {"seasonUuid": season["id"]}
    categories = _get_json("/v1/results/categories", category_params)
    if isinstance(categories, dict) and "value" in categories:
        categories = categories["value"]
    category = next(
        (
            item for item in categories
            if item.get("legacy_id") == 3
            or "motogp" in str(item.get("name", "")).lower()
        ),
        None,
    )
    if not category or not category.get("id"):
        raise OfficialApiError(f"官方接口中没有 {season_year} MotoGP 组别")
    return season, category


def _fetch_event_results(
    season_year: int,
    round_number: int,
    event: dict,
    category: dict,
    race_types=("sprint", "race"),
    allow_missing: bool = False,
):
    sessions = _get_json(
        "/v1/results/sessions",
        {"eventUuid": event["id"], "categoryUuid": category["id"]},
    )
    if isinstance(sessions, dict) and "value" in sessions:
        sessions = sessions["value"]

    result = {
        "season": season_year,
        "round_number": round_number,
        "official_event_id": event["id"],
        "official_event_name": event.get("sponsored_name") or event.get("name") or "",
        "sprint": [],
        "race": [],
        "official": {},
        "source_files": {},
        "errors": {},
        "start_date": str(event.get("date_start") or "")[:10],
        "end_date": str(event.get("date_end") or "")[:10],
    }
    official_types = {"sprint": "SPR", "race": "RAC"}
    for local_type in race_types:
        official_type = official_types[local_type]
        session = next(
            (
                item for item in sessions
                if str(item.get("type") or "").upper() == official_type
                and str(item.get("status") or "").upper() == "FINISHED"
            ),
            None,
        )
        if not session or not session.get("id"):
            message = f"官方接口没有返回{'冲刺赛' if local_type == 'sprint' else '正赛'}场次"
            if allow_missing:
                result["errors"][local_type] = message
                continue
            raise OfficialApiError(message)
        classification_result = _get_json(
            "/v2/results/classifications",
            {"session": session["id"], "seasonYear": season_year},
        )
        classification = classification_result.get("classification") or []
        if not classification:
            message = f"官方接口没有返回{'冲刺赛' if local_type == 'sprint' else '正赛'}排名"
            if allow_missing:
                result["errors"][local_type] = message
                continue
            raise OfficialApiError(message)
        winner_laps = max(
            (int(item.get("total_laps") or 0) for item in classification),
            default=0,
        )
        rows = []
        for item in classification:
            rider = item.get("rider") or {}
            if rider.get("number") is None:
                continue
            try:
                position = int(item["position"]) if item.get("position") is not None else None
                official_status = str(item.get("status") or "").upper()
                finished = position is not None and official_status == "INSTND"
                total_laps = int(item.get("total_laps") or 0)
                remaining_laps = None
                if not finished:
                    gap_laps = (item.get("gap") or {}).get("lap")
                    try:
                        gap_laps = int(gap_laps) if gap_laps not in (None, "") else 0
                    except (TypeError, ValueError):
                        gap_laps = 0
                    remaining_laps = gap_laps if gap_laps > 0 else (
                        winner_laps - total_laps if winner_laps and total_laps < winner_laps else None
                    )
                row = {
                    "position": position,
                    "rider_number": str(rider["number"]),
                    "official_name": str(rider.get("full_name") or ""),
                    "points": int(item.get("points") or 0),
                    "finish_time": str(item.get("time") or ""),
                    "result_status": "FINISHED" if finished else "DNF",
                    "remaining_laps": remaining_laps,
                    "rider_api_id": str(
                        rider.get("riders_api_uuid") or rider.get("riders_id") or ""
                    ),
                    "country_iso": str((rider.get("country") or {}).get("iso") or ""),
                    "country_name": str((rider.get("country") or {}).get("name") or ""),
                    "team_name": str(item.get("team_name") or ""),
                    "manufacturer": str((item.get("constructor") or {}).get("name") or ""),
                }
            except (KeyError, TypeError, ValueError) as exc:
                raise OfficialApiError("官方分站排名包含无法识别的记录") from exc
            if (row["position"] is not None and row["position"] < 1) or not row["rider_number"]:
                continue
            rows.append(row)
        if not rows:
            message = "官方分站排名中没有有效名次"
            if allow_missing:
                result["errors"][local_type] = message
                continue
            raise OfficialApiError(message)
        result[local_type] = rows
        result["official"][local_type] = bool(classification_result.get("official"))
        result["source_files"][local_type] = (
            (classification_result.get("files") or {}).get("pdf")
            or ((session.get("session_files") or {}).get("classification") or {}).get("url")
        )
    return result


def fetch_race_results(
    season_year: int,
    round_number: int,
    start_date: str,
    end_date: str,
    race_types=("sprint", "race"),
):
    """读取指定分站中已结束的指定场次分类结果。"""
    season, category = _find_season_and_motogp_category(season_year)
    events = _get_json(
        "/v1/results/events",
        {"seasonUuid": season["id"]},
    )
    if isinstance(events, dict) and "value" in events:
        events = events["value"]
    grand_prix_events = sorted(
        (item for item in events if not item.get("test")),
        key=lambda item: str(item.get("date_start") or ""),
    )
    event = next(
        (
            item for item in grand_prix_events
            if str(item.get("date_start"))[:10] == start_date
            and str(item.get("date_end"))[:10] == end_date
        ),
        None,
    )
    if event is None and 1 <= round_number <= len(grand_prix_events):
        event = grand_prix_events[round_number - 1]
    if not event or not event.get("id"):
        raise OfficialApiError(f"官方接口中没有 {season_year} 赛季第 {round_number} 站")
    return _fetch_event_results(
        season_year, round_number, event, category, tuple(race_types)
    )


def fetch_finished_season_results(season_year: int):
    """一次读取指定赛季全部已完赛大奖赛，复用赛季和组别请求以降低访问频率。"""
    season, category = _find_season_and_motogp_category(season_year)
    events = _get_json(
        "/v1/results/events",
        {"seasonUuid": season["id"]},
    )
    if isinstance(events, dict) and "value" in events:
        events = events["value"]
    grand_prix_events = sorted(
        (item for item in events if not item.get("test")),
        key=lambda item: str(item.get("date_start") or ""),
    )
    results = []
    failures = []
    for round_number, event in enumerate(grand_prix_events, start=1):
        if str(event.get("status") or "").upper() != "FINISHED":
            continue
        try:
            result = _fetch_event_results(
                season_year,
                round_number,
                event,
                category,
                allow_missing=True,
            )
            if result["sprint"] or result["race"]:
                results.append(result)
            else:
                failures.append({
                    "round_number": round_number,
                    "event_name": result["official_event_name"],
                    "message": "官网尚未发布冲刺赛或正赛排名",
                })
        except OfficialApiError as exc:
            failures.append({
                "round_number": round_number,
                "event_name": str(event.get("sponsored_name") or event.get("name") or ""),
                "message": str(exc),
            })
    return {"season": season_year, "events": results, "failed_events": failures}


def fetch_rider_profile(rider_api_id: str, season_year: int, appearance: dict):
    """读取车手官方资料，并补充其当季参赛时的车队与制造商快照。"""
    if not rider_api_id:
        raise OfficialApiError("官方车手缺少详情接口标识")
    profile = _get_json(f"/v1/riders/{rider_api_id}")
    career = profile.get("career") or []
    season_entry = next(
        (
            item for item in career
            if int(item.get("season") or 0) == season_year
            and int((item.get("category") or {}).get("legacy_id") or 0) == 3
        ),
        None,
    ) or {}
    team = season_entry.get("team") or {}
    constructor = team.get("constructor") or {}
    full_name = " ".join(
        part for part in (str(profile.get("name") or "").strip(), str(profile.get("surname") or "").strip())
        if part
    ) or appearance.get("official_name") or "未知车手"
    return {
        "rider_number": str(appearance["rider_number"]),
        "english_name": full_name,
        # 官网未提供中文译名；英文名仅作为待管理员审核的回退值。
        "chinese_name": full_name,
        "country_iso": str((profile.get("country") or {}).get("iso") or appearance.get("country_iso") or ""),
        "nationality": str((profile.get("country") or {}).get("name") or appearance.get("country_name") or "未知"),
        "team_name": str(
            team.get("name") or season_entry.get("sponsored_team") or appearance.get("team_name") or ""
        ),
        "bike": str(constructor.get("name") or appearance.get("manufacturer") or "未知"),
        "birth_date": str(profile.get("birth_date") or "1900-01-01")[:10],
        "birth_place": str(profile.get("birth_city") or "未知"),
        "official_rider_id": rider_api_id,
        "official_team_id": str(team.get("id") or ""),
    }


def fetch_season_roster(season_year: int):
    """读取指定赛季 MotoGP 车手详情及其官方车队资料。

    积分接口提供当季全部参赛车手、车号和制造商；车手详情接口补充出生信息、
    出生信息以及不受赞助冠名变化影响的车队标识。详情请求并发数保持较低，避免对
    官方公开接口造成突发压力。
    """
    season, category = _find_season_and_motogp_category(season_year)
    result = _get_json(
        "/v2/results/world-standings",
        {
            "type": "rider",
            "season": season["id"],
            "category": category["id"],
        },
    )
    classification = (result.get("classification") or {}).get("rider") or []
    if not classification:
        raise OfficialApiError("官方接口没有返回车手名单")

    appearances = []
    seen_numbers = set()
    for item in classification:
        rider = item.get("rider") or {}
        number = rider.get("number")
        rider_api_id = str(
            rider.get("riders_api_uuid") or rider.get("riders_id") or ""
        ).strip()
        if number is None or not rider_api_id:
            continue
        number = str(number).strip()
        if not number or number in seen_numbers:
            continue
        seen_numbers.add(number)
        appearances.append({
            "rider_number": number,
            "official_name": str(rider.get("full_name") or "").strip(),
            "rider_api_id": rider_api_id,
            "country_iso": str((rider.get("country") or {}).get("iso") or ""),
            "country_name": str((rider.get("country") or {}).get("name") or ""),
            "team_name": str(item.get("team_name") or "").strip(),
            "manufacturer": str((item.get("constructor") or {}).get("name") or "").strip(),
        })
    if not appearances:
        raise OfficialApiError("官方车手名单缺少可用的车手标识")

    profiles = []
    failures = []
    worker_count = min(4, len(appearances))
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        pending = {
            executor.submit(
                fetch_rider_profile,
                appearance["rider_api_id"],
                season_year,
                appearance,
            ): appearance
            for appearance in appearances
        }
        for future in as_completed(pending):
            appearance = pending[future]
            try:
                profiles.append(future.result())
            except OfficialApiError as exc:
                failures.append({
                    "rider_number": appearance["rider_number"],
                    "official_name": appearance["official_name"],
                    "message": str(exc),
                })

    if not profiles:
        raise OfficialApiError("未能读取任何官方车手详情")
    profiles.sort(
        key=lambda row: (
            int(row["rider_number"]) if str(row["rider_number"]).isdigit() else 9999,
            str(row["rider_number"]),
        )
    )

    teams_by_key = {}
    for profile in profiles:
        team_name = str(profile.get("team_name") or "").strip()
        if not team_name:
            continue
        official_team_id = str(profile.get("official_team_id") or "").strip()
        key = official_team_id or team_name.casefold()
        teams_by_key[key] = {
            "official_team_id": official_team_id,
            "name": team_name,
            "manufacturer": str(profile.get("bike") or "未知").strip() or "未知",
        }
    teams = sorted(teams_by_key.values(), key=lambda row: row["name"].casefold())
    if not teams:
        raise OfficialApiError("官方车手资料中没有有效车队信息")

    return {
        "season": season_year,
        "official": bool(result.get("official")),
        "source_file": (result.get("files") or {}).get("pdf"),
        "riders": profiles,
        "teams": teams,
        "failed_profiles": failures,
    }
