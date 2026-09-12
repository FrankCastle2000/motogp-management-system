from functools import wraps
from datetime import date, time
from pathlib import Path

from flask import Flask, jsonify, redirect, request, session
from flask_cors import CORS

import db
import motogp_sync
from config import (
    APP_DEBUG,
    APP_HOST,
    APP_PORT,
    MOTOGP_RACE_RESULTS_SYNC_COOLDOWN_SECONDS,
    MOTOGP_SYNC_COOLDOWN_SECONDS,
    SECRET_KEY,
)

ROOT_DIR = Path(__file__).resolve().parent.parent
FRONTEND_DIR = ROOT_DIR / "frontend"

app = Flask(__name__, static_folder=str(FRONTEND_DIR), static_url_path="")
app.secret_key = SECRET_KEY
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_PERMANENT=False,
)
CORS(app, supports_credentials=True)


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if "user_id" not in session:
            return jsonify({"ok": False, "message": "请先登录"}), 401
        return view(*args, **kwargs)

    return wrapped


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if "user_id" not in session:
            return jsonify({"ok": False, "message": "请先登录"}), 401
        if session.get("role") != "admin":
            return jsonify({"ok": False, "message": "仅管理员可执行此操作"}), 403
        return view(*args, **kwargs)

    return wrapped


@app.errorhandler(Exception)
def handle_error(err):
    """统一异常响应：输入为路由执行时抛出的异常；输出为包含错误消息的 JSON 和 400/500 状态码。"""
    if isinstance(err, db.SyncCooldownError):
        return jsonify(
            {"ok": False, "message": str(err), "retry_after": err.retry_after}
        ), 429
    if isinstance(err, motogp_sync.OfficialApiError):
        return jsonify({"ok": False, "message": str(err)}), 502
    if isinstance(err, db.ConflictError):
        return jsonify({"ok": False, "message": str(err)}), 409
    if isinstance(err, ValueError):
        return jsonify({"ok": False, "message": str(err)}), 400
    return jsonify({"ok": False, "message": str(err)}), 500


@app.get("/")
def index():
    """主页跳转接口。

    输入：无请求参数；从 Session Cookie 判断是否已登录。
    输出：已登录时返回 302 并跳转 /standings.html，否则返回 302 并跳转 /login.html。
    """
    if "user_id" in session:
        return redirect("/standings.html")
    return redirect("/login.html")


@app.get("/password.html")
def legacy_password_page():
    """兼容旧的修改密码页面地址，并永久跳转到规范化后的文件名。"""
    return redirect("/change-password.html", code=301)


@app.get("/api/auth/me")
def api_me():
    """获取当前登录用户接口。

    输入：Session Cookie 中的 user_id。
    输出：成功时返回 200 和 {ok, data: {id, username, role, created_at}}；未登录或用户失效时返回 401。
    """
    user_id = session.get("user_id")
    if not user_id:
        return jsonify({"ok": False, "message": "未登录"}), 401
    user = db.get_user_by_id(user_id)
    if not user:
        session.clear()
        return jsonify({"ok": False, "message": "未登录"}), 401
    return jsonify({"ok": True, "data": user})


@app.post("/api/auth/register")
def api_register():
    """普通用户注册接口。

    输入：JSON {username: 3-32 个字符, password: 至少 6 位}。
    输出：成功时返回 201 和新用户公开信息；参数无效或账号已存在时返回 400。
    """
    body = request.get_json(silent=True) or {}
    username = (body.get("username") or "").strip()
    password = body.get("password") or ""

    if len(username) < 3 or len(username) > 32:
        return jsonify({"ok": False, "message": "账号长度需为 3-32 个字符"}), 400
    if len(password) < 6:
        return jsonify({"ok": False, "message": "密码至少 6 位"}), 400
    if username.lower() == "admin":
        return jsonify({"ok": False, "message": "该账号不可注册"}), 400

    user = db.create_user(username, password, role="user")
    return jsonify({"ok": True, "data": user, "message": "注册成功，请登录"}), 201


@app.post("/api/auth/login")
def api_login():
    """用户登录接口。

    输入：JSON {username, password}。
    输出：成功时写入非永久 Session，并返回 200、用户公开信息和登录成功消息；凭据缺失返回 400，凭据错误返回 401。
    """
    body = request.get_json(silent=True) or {}
    username = (body.get("username") or "").strip()
    password = body.get("password") or ""

    if not username or not password:
        return jsonify({"ok": False, "message": "请输入账号和密码"}), 400

    user = db.authenticate_user(username, password)
    if not user:
        return jsonify({"ok": False, "message": "账号或密码错误"}), 401

    session["user_id"] = user["id"]
    session["username"] = user["username"]
    session["role"] = user["role"]
    session.permanent = False
    return jsonify({"ok": True, "data": user, "message": "登录成功"})


@app.post("/api/auth/logout")
def api_logout():
    """用户退出登录接口。

    输入：当前请求携带的 Session Cookie，无 JSON 参数。
    输出：清空服务器 Session，返回 200 和 {ok: true, message: "已退出登录"}。
    """
    session.clear()
    return jsonify({"ok": True, "message": "已退出登录"})


@app.put("/api/auth/password")
def api_change_password():
    """修改用户密码接口，可在未登录状态使用。

    输入：未登录时为 JSON {username, current_password, new_password}；已登录时 username 可省略。
    输出：成功时更新密码、清除当前 Session 并返回 200；账号或当前密码错误返回 401，新密码无效返回 400。
    """
    body = request.get_json(silent=True) or {}
    username = (body.get("username") or "").strip()
    current_password = body.get("current_password") or ""
    new_password = body.get("new_password") or ""
    user_id = session.get("user_id")
    operator_username = session.get("username", "")

    if not user_id:
        if not username or not current_password:
            return jsonify({"ok": False, "message": "请输入账号和当前密码"}), 400
        user = db.authenticate_user(username, current_password)
        if not user:
            return jsonify({"ok": False, "message": "账号或当前密码错误"}), 401
        user_id = user["id"]
        operator_username = user["username"]

    if not current_password:
        return jsonify({"ok": False, "message": "请输入当前密码"}), 400
    if len(new_password) < 6:
        return jsonify({"ok": False, "message": "新密码至少 6 位"}), 400
    if len(new_password) > 128:
        return jsonify({"ok": False, "message": "新密码不能超过 128 位"}), 400

    db.change_user_password(
        user_id,
        current_password,
        new_password,
        operator_username=operator_username,
    )
    session.clear()
    return jsonify({"ok": True, "message": "密码修改成功，请使用新密码重新登录"})


@app.get("/api/users")
@admin_required
def api_list_users():
    """账号列表查询接口，仅管理员可用。

    输入：管理员 Session Cookie，无查询参数。
    输出：成功时返回 200 和用户数组，每项包含 id、username、role、created_at；未登录返回 401，非管理员返回 403。
    """
    users = db.list_users()
    return jsonify({"ok": True, "data": users})


@app.put("/api/users/<int:user_id>/role")
@admin_required
def api_update_user_role(user_id: int):
    """修改指定用户角色接口，仅管理员可用。

    输入：路径参数 user_id；JSON {role: "admin" | "user", version: 查询时取得的版本号}。
    输出：成功时返回 200、更新后的用户信息和操作消息；参数无效返回 400，数据已变化返回 409，鉴权失败返回 401/403。
    """
    body = request.get_json(silent=True) or {}
    role = (body.get("role") or "").strip()
    if role not in ("admin", "user"):
        return jsonify({"ok": False, "message": "角色只能是 admin 或 user"}), 400

    user = db.update_user_role(
        user_id,
        role,
        _parse_version(body),
        operator_id=session.get("user_id"),
        operator_username=session.get("username", ""),
    )
    action = "已设为管理员" if role == "admin" else "已取消管理员权限"
    return jsonify({"ok": True, "data": user, "message": action})


@app.delete("/api/users/<int:user_id>")
@admin_required
def api_delete_user(user_id: int):
    """删除指定账号接口，仅管理员可用。

    输入：路径参数 user_id，以及管理员 Session Cookie。
    输出：成功时返回 200 和删除成功消息；账号不存在、受保护或为当前账号时返回 400，鉴权失败返回 401/403。
    """
    db.delete_user(
        user_id,
        operator_id=session.get("user_id"),
        operator_username=session.get("username", ""),
    )
    return jsonify({"ok": True, "message": "账号已删除"})


@app.get("/api/logs")
@admin_required
def api_list_operation_logs():
    """操作日志查询接口，仅管理员可用。

    输入：管理员 Session Cookie；可选 page、page_size、operator、entity_type、action、date_from、date_to 查询参数。
    输出：成功时返回 200 和分页日志、总数及保留策略；参数无效返回 400，鉴权失败返回 401/403。
    """
    page = request.args.get("page", default=1, type=int)
    page_size = request.args.get("page_size", default=20, type=int)
    if page is None or page_size is None or page < 1 or not 1 <= page_size <= 100:
        return jsonify({"ok": False, "message": "分页参数无效"}), 400
    operator = (request.args.get("operator") or "").strip()
    entity_type = (request.args.get("entity_type") or "").strip()
    action = (request.args.get("action") or "").strip()
    date_from = (request.args.get("date_from") or "").strip()
    date_to = (request.args.get("date_to") or "").strip()
    if entity_type and entity_type not in ("rider", "team", "user", "race", "race_result", "standing"):
        return jsonify({"ok": False, "message": "对象类型无效"}), 400
    if action and action not in ("create", "update", "delete"):
        return jsonify({"ok": False, "message": "操作类型无效"}), 400
    for label, value in (("开始日期", date_from), ("结束日期", date_to)):
        if value:
            try:
                date.fromisoformat(value)
            except ValueError:
                return jsonify({"ok": False, "message": f"{label}格式无效"}), 400
    if date_from and date_to and date_from > date_to:
        return jsonify({"ok": False, "message": "开始日期不能晚于结束日期"}), 400
    result = db.list_operation_logs(
        page, page_size, operator, entity_type, action, date_from, date_to
    )
    result["retention"] = db.get_operation_log_policy()
    return jsonify({"ok": True, "data": result})


def _parse_rider_body(body):
    rider_number = (body.get("rider_number") or "").strip()
    english_name = (body.get("english_name") or "").strip()
    chinese_name = (body.get("chinese_name") or "").strip()
    nickname = (body.get("nickname") or "").strip()
    nationality = (body.get("nationality") or "").strip()
    bike = (body.get("bike") or "").strip()
    birth_date = (body.get("birth_date") or "").strip()
    birth_place = (body.get("birth_place") or "").strip()
    team_id = body.get("team_id")
    if team_id in ("", None):
        team_id = None
    else:
        try:
            team_id = int(team_id)
        except (TypeError, ValueError) as exc:
            raise ValueError("所属车队无效") from exc
        if team_id <= 0:
            raise ValueError("所属车队无效")

    if not all([rider_number, english_name, chinese_name, nationality, bike, birth_date, birth_place]):
        raise ValueError("车手编号、英文名、中文名、国籍、车辆、出生日期、出生地点均不能为空")
    limits = {
        "车手编号": (rider_number, 16),
        "英文名": (english_name, 128),
        "中文名": (chinese_name, 64),
        "昵称": (nickname, 64),
        "国籍": (nationality, 64),
        "驾驶车辆": (bike, 128),
        "出生地点": (birth_place, 128),
    }
    for label, (value, max_length) in limits.items():
        if len(value) > max_length:
            raise ValueError(f"{label}不能超过 {max_length} 个字符")
    try:
        date.fromisoformat(birth_date)
    except ValueError as exc:
        raise ValueError("出生日期格式无效") from exc

    return rider_number, english_name, chinese_name, nickname, nationality, team_id, bike, birth_date, birth_place


def _parse_version(body, allow_zero=False):
    """读取前端提交的乐观锁版本号，阻止无版本更新覆盖新数据。"""
    try:
        version = int(body.get("version"))
    except (TypeError, ValueError) as exc:
        raise ValueError("数据版本无效，请刷新后重试") from exc
    if version < (0 if allow_zero else 1):
        raise ValueError("数据版本无效，请刷新后重试")
    return version


@app.get("/api/riders")
@login_required
def api_list_riders():
    """车手总览查询接口。

    输入：已登录用户的 Session Cookie；可选 rider_number、name、nationality、team_id 查询参数。
    输出：成功时返回 200 和车手数组；每项包含编号、姓名、车队、车辆及出生信息；未登录返回 401。
    """
    rider_number = (request.args.get("rider_number") or "").strip()
    name = (request.args.get("name") or "").strip()
    nationality = (request.args.get("nationality") or "").strip()
    team_id = request.args.get("team_id")
    if team_id in (None, ""):
        team_id = None
    else:
        try:
            team_id = int(team_id)
        except ValueError:
            return jsonify({"ok": False, "message": "车队筛选条件无效"}), 400
        if team_id <= 0:
            return jsonify({"ok": False, "message": "车队筛选条件无效"}), 400
    riders = db.list_riders(rider_number, name, nationality, team_id)
    return jsonify({"ok": True, "data": riders})


@app.get("/api/riders/<int:rider_id>")
@login_required
def api_get_rider(rider_id: int):
    """单个车手详情查询接口。

    输入：路径参数 rider_id，以及已登录用户的 Session Cookie。
    输出：成功时返回 200 和完整车手对象；未登录返回 401，车手不存在返回 404。
    """
    rider = db.get_rider(rider_id)
    if not rider:
        return jsonify({"ok": False, "message": "车手不存在"}), 404
    return jsonify({"ok": True, "data": rider})


@app.post("/api/riders")
@admin_required
def api_create_rider():
    """新增车手接口，仅管理员可用。

    输入：JSON {rider_number, english_name, chinese_name, nickname?, nationality, team_id?, bike, birth_date, birth_place}。
    输出：成功时返回 201 和新车手对象；字段或车队无效时返回 400，鉴权失败返回 401/403。
    """
    body = request.get_json(silent=True) or {}
    fields = _parse_rider_body(body)
    rider = db.create_rider(
        *fields,
        operator_id=session.get("user_id"),
        operator_username=session.get("username", ""),
    )
    return jsonify({"ok": True, "data": rider}), 201


@app.put("/api/riders/<int:rider_id>")
@admin_required
def api_update_rider(rider_id: int):
    """修改指定车手接口，仅管理员可用。

    输入：路径参数 rider_id；JSON 字段与新增车手接口相同，并包含查询时取得的 version。
    输出：成功时返回 200 和更新后的车手对象；字段无效返回 400，数据已被他人修改返回 409，鉴权失败返回 401/403。
    """
    body = request.get_json(silent=True) or {}
    fields = _parse_rider_body(body)
    version = _parse_version(body)
    rider = db.update_rider(
        rider_id,
        *fields,
        version,
        operator_id=session.get("user_id"),
        operator_username=session.get("username", ""),
    )
    return jsonify({"ok": True, "data": rider})


@app.delete("/api/riders/<int:rider_id>")
@admin_required
def api_delete_rider(rider_id: int):
    """删除指定车手接口，仅管理员可用。

    输入：路径参数 rider_id，以及管理员 Session Cookie。
    输出：成功时返回 200 和删除成功消息；车手不存在时返回 400，鉴权失败返回 401/403。
    """
    db.delete_rider(
        rider_id,
        operator_id=session.get("user_id"),
        operator_username=session.get("username", ""),
    )
    return jsonify({"ok": True, "message": "删除成功"})


def _parse_team_body(body):
    name = (body.get("name") or "").strip()
    manufacturer = (body.get("manufacturer") or "").strip()
    if not name or not manufacturer:
        raise ValueError("车队名称和车辆制造商均不能为空")
    if len(name) > 128 or len(manufacturer) > 128:
        raise ValueError("车队名称和车辆制造商均不能超过 128 个字符")
    return name, manufacturer


@app.get("/api/teams")
@login_required
def api_list_teams():
    """车队总览查询接口。

    输入：已登录用户的 Session Cookie；可选 name、manufacturer 查询参数。
    输出：成功时返回 200 和车队数组；每项包含名称、制造商和成员；未登录返回 401。
    """
    name = (request.args.get("name") or "").strip()
    manufacturer = (request.args.get("manufacturer") or "").strip()
    teams = db.list_teams(name, manufacturer)
    return jsonify({"ok": True, "data": teams})


@app.get("/api/teams/<int:team_id>")
@login_required
def api_get_team(team_id: int):
    """单个车队详情查询接口。

    输入：路径参数 team_id，以及已登录用户的 Session Cookie。
    输出：成功时返回 200 和车队对象；未登录返回 401，车队不存在返回 404。
    """
    team = db.get_team(team_id)
    if not team:
        return jsonify({"ok": False, "message": "车队不存在"}), 404
    return jsonify({"ok": True, "data": team})


@app.post("/api/teams")
@admin_required
def api_create_team():
    """新增车队接口，仅管理员可用。

    输入：JSON {name: 车队名称, manufacturer: 车辆制造商}。
    输出：成功时返回 201 和新车队对象；字段无效或车队名称重复时返回 400，鉴权失败返回 401/403。
    """
    body = request.get_json(silent=True) or {}
    name, manufacturer = _parse_team_body(body)
    team = db.create_team(
        name,
        manufacturer,
        operator_id=session.get("user_id"),
        operator_username=session.get("username", ""),
    )
    return jsonify({"ok": True, "data": team}), 201


@app.put("/api/teams/<int:team_id>")
@admin_required
def api_update_team(team_id: int):
    """修改指定车队接口，仅管理员可用。

    输入：路径参数 team_id；JSON {name, manufacturer, version}，version 为查询时取得的版本号。
    输出：成功时返回 200 和更新后的车队对象；字段无效返回 400，数据已被他人修改返回 409，鉴权失败返回 401/403。
    """
    body = request.get_json(silent=True) or {}
    name, manufacturer = _parse_team_body(body)
    version = _parse_version(body)
    team = db.update_team(
        team_id,
        name,
        manufacturer,
        version,
        operator_id=session.get("user_id"),
        operator_username=session.get("username", ""),
    )
    return jsonify({"ok": True, "data": team})


@app.delete("/api/teams/<int:team_id>")
@admin_required
def api_delete_team(team_id: int):
    """删除指定车队接口，仅管理员可用。

    输入：路径参数 team_id，以及管理员 Session Cookie。
    输出：成功时返回 200 和删除成功消息；车队不存在或仍有关联车手时返回 400，鉴权失败返回 401/403。
    """
    db.delete_team(
        team_id,
        operator_id=session.get("user_id"),
        operator_username=session.get("username", ""),
    )
    return jsonify({"ok": True, "message": "删除成功"})


@app.post("/api/roster/sync")
@admin_required
def api_sync_official_roster():
    """从 MotoGP 官方接口同步指定赛季的车队和车手资料。"""
    body = request.get_json(silent=True) or {}
    try:
        season = int(body.get("season", date.today().year))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "message": "赛季年份无效"}), 400
    if not 1949 <= season <= 2100:
        return jsonify({"ok": False, "message": "赛季年份无效"}), 400

    sync_key = f"motogp-roster:{season}"
    db.reserve_external_sync(
        sync_key,
        MOTOGP_SYNC_COOLDOWN_SECONDS,
        failure_cooldown_seconds=min(300, MOTOGP_SYNC_COOLDOWN_SECONDS),
    )
    try:
        official = motogp_sync.fetch_season_roster(season)
        summary = db.sync_official_roster(
            official["teams"],
            official["riders"],
            operator_id=session.get("user_id"),
            operator_username=session.get("username", ""),
        )
        summary.update({
            "season": season,
            "official": official["official"],
            "source_file": official["source_file"],
            "failed_profiles": official["failed_profiles"],
            "cooldown_seconds": MOTOGP_SYNC_COOLDOWN_SECONDS,
        })
        db.finish_external_sync(
            sync_key,
            True,
            (
                f"车队新增 {summary['teams']['created']}、更新 {summary['teams']['updated']}；"
                f"车手新增 {summary['riders']['created']}、更新 {summary['riders']['updated']}"
            ),
        )
        return jsonify({"ok": True, "data": summary, "message": "官方车手与车队同步完成"})
    except Exception as exc:
        db.finish_external_sync(sync_key, False, str(exc))
        raise


def _parse_race_event_body(body):
    """校验赛程新增和修改接口共用的 JSON 字段。"""
    try:
        season = int(body.get("season"))
        round_number = int(body.get("round_number"))
    except (TypeError, ValueError) as exc:
        raise ValueError("赛季年份和分站序号必须是整数") from exc
    if not 1949 <= season <= 2100:
        raise ValueError("赛季年份必须在 1949-2100 之间")
    if not 1 <= round_number <= 99:
        raise ValueError("分站序号必须在 1-99 之间")

    flag = (body.get("flag") or "🏁").strip()
    country = (body.get("country") or "").strip()
    country_en = (body.get("country_en") or "").strip()
    start_date = (body.get("start_date") or "").strip()
    end_date = (body.get("end_date") or "").strip()
    circuit = (body.get("circuit") or "").strip()
    if not all((flag, country, country_en, start_date, end_date, circuit)):
        raise ValueError("旗帜、国家或地区名称、日期和赛道均不能为空")
    for label, value, limit in (
        ("旗帜", flag, 16),
        ("中文名称", country, 64),
        ("英文名称", country_en, 64),
        ("赛道名称", circuit, 128),
    ):
        if len(value) > limit:
            raise ValueError(f"{label}不能超过 {limit} 个字符")
    try:
        start = date.fromisoformat(start_date)
        end = date.fromisoformat(end_date)
    except ValueError as exc:
        raise ValueError("比赛日期格式无效") from exc
    if start > end:
        raise ValueError("开始日期不能晚于结束日期")
    return season, round_number, flag, country, country_en, start_date, end_date, circuit


@app.get("/api/races")
@login_required
def api_list_race_events():
    """赛程日历查询接口。

    输入：已登录用户 Session Cookie；可选 season 查询参数。
    输出：成功时返回按赛季和分站排序的赛程数组；年份无效返回 400，未登录返回 401。
    """
    season_text = (request.args.get("season") or "").strip()
    season = None
    if season_text:
        try:
            season = int(season_text)
        except ValueError:
            return jsonify({"ok": False, "message": "赛季年份无效"}), 400
        if not 1949 <= season <= 2100:
            return jsonify({"ok": False, "message": "赛季年份无效"}), 400
    return jsonify({"ok": True, "data": db.list_race_events(season)})


@app.post("/api/races")
@admin_required
def api_create_race_event():
    """新增赛程接口，仅管理员可用。

    输入：JSON {season, round_number, flag, country, country_en, start_date, end_date, circuit}。
    输出：成功返回 201 和赛程对象；字段或分站序号重复返回 400，鉴权失败返回 401/403。
    """
    fields = _parse_race_event_body(request.get_json(silent=True) or {})
    event = db.create_race_event(
        *fields,
        operator_id=session.get("user_id"),
        operator_username=session.get("username", ""),
    )
    return jsonify({"ok": True, "data": event}), 201


@app.put("/api/races/<int:event_id>")
@admin_required
def api_update_race_event(event_id: int):
    """修改指定赛程接口，仅管理员可用。

    输入：路径参数 event_id；新增赛程的全部 JSON 字段以及 version。
    输出：成功返回更新对象；字段无效返回 400，并发冲突返回 409，鉴权失败返回 401/403。
    """
    body = request.get_json(silent=True) or {}
    fields = _parse_race_event_body(body)
    event = db.update_race_event(
        event_id,
        *fields,
        _parse_version(body),
        operator_id=session.get("user_id"),
        operator_username=session.get("username", ""),
    )
    return jsonify({"ok": True, "data": event})


@app.delete("/api/races/<int:event_id>")
@admin_required
def api_delete_race_event(event_id: int):
    """删除指定赛程接口，仅管理员可用。

    输入：路径参数 event_id 和管理员 Session Cookie。
    输出：成功返回删除消息；赛程不存在返回 400，鉴权失败返回 401/403。
    """
    db.delete_race_event(
        event_id,
        operator_id=session.get("user_id"),
        operator_username=session.get("username", ""),
    )
    return jsonify({"ok": True, "message": "赛程已删除"})


def _parse_race_schedule_items(body):
    """校验分站日程批量保存接口中的北京时间、组别和环节名称。"""
    raw_items = body.get("items")
    if not isinstance(raw_items, list):
        raise ValueError("日程数据必须是数组")
    if len(raw_items) > 100:
        raise ValueError("单个分站最多保存 100 条日程")

    items = []
    unique_items = set()
    for index, item in enumerate(raw_items, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"第 {index} 条日程格式无效")
        schedule_date = str(item.get("schedule_date") or "").strip()
        start_time = str(item.get("start_time") or "").strip()
        end_time = str(item.get("end_time") or "").strip()
        category = str(item.get("category") or "MotoGP").strip()
        session_name = str(item.get("session_name") or "").strip()
        if not all((schedule_date, start_time, category, session_name)):
            raise ValueError(f"第 {index} 条日程的日期、开始时间、组别和环节不能为空")
        try:
            date.fromisoformat(schedule_date)
        except ValueError as exc:
            raise ValueError(f"第 {index} 条日程日期格式无效") from exc
        try:
            parsed_start = time.fromisoformat(start_time)
            parsed_end = time.fromisoformat(end_time) if end_time else None
        except ValueError as exc:
            raise ValueError(f"第 {index} 条日程时间格式无效") from exc
        if len(start_time) != 5 or (end_time and len(end_time) != 5):
            raise ValueError(f"第 {index} 条日程时间必须使用 HH:MM 格式")
        if parsed_end is not None and parsed_end <= parsed_start:
            raise ValueError(f"第 {index} 条日程结束时间必须晚于开始时间")
        if len(category) > 32 or len(session_name) > 128:
            raise ValueError(f"第 {index} 条日程的组别或环节名称过长")
        unique_key = (schedule_date, start_time, category.casefold(), session_name.casefold())
        if unique_key in unique_items:
            raise ValueError(f"第 {index} 条日程与前面的内容重复")
        unique_items.add(unique_key)
        items.append({
            "schedule_date": schedule_date,
            "start_time": start_time,
            "end_time": end_time,
            "category": category,
            "session_name": session_name,
        })
    return items


@app.get("/api/races/<int:event_id>/schedule")
@login_required
def api_list_race_schedule(event_id: int):
    """查询指定分站的比赛周末日程。

    输入：路径参数 event_id 和已登录用户 Session Cookie。
    输出：分站信息、日程版本号及按北京时间排序的日程数组。
    """
    return jsonify({"ok": True, "data": db.list_race_schedule(event_id)})


@app.put("/api/races/<int:event_id>/schedule")
@admin_required
def api_replace_race_schedule(event_id: int):
    """批量保存指定分站的比赛周末日程，仅管理员可用。

    输入：路径参数 event_id；JSON {version, items: [{schedule_date,
    start_time, end_time?, category, session_name}]}，时间统一使用北京时间。
    输出：新版本号和保存后的日程；字段无效返回 400，并发冲突返回 409。
    """
    body = request.get_json(silent=True) or {}
    result = db.replace_race_schedule(
        event_id,
        _parse_race_schedule_items(body),
        _parse_version(body, allow_zero=True),
        operator_id=session.get("user_id"),
        operator_username=session.get("username", ""),
    )
    return jsonify({"ok": True, "data": result, "message": "分站日程保存成功"})


@app.post("/api/races/schedules/sync")
@admin_required
def api_sync_race_schedules():
    """从 MotoGP 官方接口同步指定赛季全部分站日程并转换为北京时间。"""
    body = request.get_json(silent=True) or {}
    try:
        season = int(body.get("season", date.today().year))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "message": "赛季年份无效"}), 400
    if not 1949 <= season <= 2100:
        return jsonify({"ok": False, "message": "赛季年份无效"}), 400

    sync_key = f"motogp-race-schedules:{season}"
    db.reserve_external_sync(
        sync_key,
        MOTOGP_SYNC_COOLDOWN_SECONDS,
        failure_cooldown_seconds=min(300, MOTOGP_SYNC_COOLDOWN_SECONDS),
    )
    try:
        official = motogp_sync.fetch_season_schedules(season)
        summary = db.sync_season_race_schedules(
            season,
            official["events"],
            operator_id=session.get("user_id"),
            operator_username=session.get("username", ""),
        )
        summary["cooldown_seconds"] = MOTOGP_SYNC_COOLDOWN_SECONDS
        db.finish_external_sync(
            sync_key,
            True,
            f"同步 {summary['events']} 个分站、{summary['items']} 个日程环节",
        )
        return jsonify({"ok": True, "data": summary, "message": "官方赛程同步完成"})
    except Exception as exc:
        db.finish_external_sync(sync_key, False, str(exc))
        raise


@app.get("/api/races/<int:event_id>/results")
@login_required
def api_list_race_results(event_id: int):
    """查询指定分站的冲刺赛和正赛排名。

    输入：路径参数 event_id 和已登录用户 Session Cookie。
    输出：赛程信息、两类排名数组及各自的并发版本号；赛程不存在返回 400。
    """
    data = db.list_race_results(event_id)
    data["sync_cooldown_seconds"] = MOTOGP_RACE_RESULTS_SYNC_COOLDOWN_SECONDS
    return jsonify({"ok": True, "data": data})


@app.put("/api/races/<int:event_id>/results/<race_type>")
@admin_required
def api_replace_race_results(event_id: int, race_type: str):
    """保存指定分站的一整套冲刺赛或正赛排名，仅管理员可用。

    输入：路径参数 event_id、race_type；JSON {version, results: [{rider_id, position?, points, finish_time?, result_status, remaining_laps?}]}。
    输出：新版本号和保存后的排名；数据重复或无效返回 400，并发冲突返回 409。
    """
    if race_type not in ("sprint", "race"):
        return jsonify({"ok": False, "message": "比赛类型无效"}), 400
    availability = db.list_race_results(event_id)["availability"][race_type]
    if not availability["available"]:
        return jsonify({"ok": False, "message": availability["reason"]}), 400
    body = request.get_json(silent=True) or {}
    raw_results = body.get("results")
    if not isinstance(raw_results, list):
        return jsonify({"ok": False, "message": "排名数据必须是数组"}), 400
    if len(raw_results) > 50:
        return jsonify({"ok": False, "message": "单场排名不能超过 50 位车手"}), 400

    results = []
    for index, item in enumerate(raw_results, start=1):
        if not isinstance(item, dict):
            return jsonify({"ok": False, "message": f"第 {index} 条排名格式无效"}), 400
        result_status = str(item.get("result_status") or "FINISHED").upper()
        if result_status not in ("FINISHED", "DNF"):
            return jsonify({"ok": False, "message": f"第 {index} 条完赛状态无效"}), 400
        try:
            rider_id = int(item.get("rider_id"))
            position = (
                int(item.get("position"))
                if item.get("position") not in (None, "")
                else None
            )
            points = int(item.get("points", 0))
            remaining_laps = (
                int(item.get("remaining_laps"))
                if item.get("remaining_laps") not in (None, "")
                else None
            )
        except (TypeError, ValueError):
            return jsonify({"ok": False, "message": f"第 {index} 条排名必须使用整数"}), 400
        if result_status == "FINISHED" and position is None:
            return jsonify({"ok": False, "message": f"第 {index} 条完赛车手必须填写排名"}), 400
        if result_status == "DNF":
            position = None
        if (
            rider_id <= 0
            or (position is not None and not 1 <= position <= 99)
            or not 0 <= points <= 100
            or (remaining_laps is not None and not 0 <= remaining_laps <= 999)
        ):
            return jsonify({"ok": False, "message": f"第 {index} 条排名或积分超出允许范围"}), 400
        finish_time = str(item.get("finish_time") or "").strip()
        if len(finish_time) > 32:
            return jsonify({"ok": False, "message": f"第 {index} 条完赛时间过长"}), 400
        results.append({
            "rider_id": rider_id,
            "position": position,
            "points": points,
            "finish_time": finish_time,
            "result_status": result_status,
            "remaining_laps": remaining_laps,
        })

    positions = [row["position"] for row in results if row["position"] is not None]
    rider_ids = [row["rider_id"] for row in results]
    if len(positions) != len(set(positions)):
        return jsonify({"ok": False, "message": "同一场比赛不能出现重复排名"}), 400
    if len(rider_ids) != len(set(rider_ids)):
        return jsonify({"ok": False, "message": "同一车手不能在一场比赛中重复出现"}), 400

    result = db.replace_race_results(
        event_id,
        race_type,
        results,
        _parse_version(body, allow_zero=True),
        operator_id=session.get("user_id"),
        operator_username=session.get("username", ""),
    )
    return jsonify({"ok": True, "data": result, "message": "分站排名保存成功"})


@app.post("/api/races/<int:event_id>/results/sync")
@admin_required
def api_sync_race_results(event_id: int):
    """从 MotoGP 官方接口同步指定分站中已经结束的一场比赛排名。

    输入：路径参数 event_id、查询参数 race_type（sprint 或 race）和管理员 Session Cookie。
    输出：所选场次赛果的匹配、跳过统计及最新本地排名；冷却期内返回 429。
    """
    race_type = str(request.args.get("race_type") or "").strip().lower()
    if race_type not in ("sprint", "race"):
        return jsonify({"ok": False, "message": "请选择要同步的冲刺赛或正赛"}), 400
    local_data = db.list_race_results(event_id)
    event = local_data["event"]
    availability = local_data["availability"][race_type]
    if not availability["started"]:
        return jsonify({"ok": False, "message": "该场次尚未开始，暂不能检查官方排名"}), 400

    sync_key = f"motogp-race-results:{event_id}:{race_type}"
    db.reserve_external_sync(
        sync_key,
        MOTOGP_RACE_RESULTS_SYNC_COOLDOWN_SECONDS,
        failure_cooldown_seconds=min(300, MOTOGP_RACE_RESULTS_SYNC_COOLDOWN_SECONDS),
    )
    try:
        official = motogp_sync.fetch_race_results(
            event["season"],
            event["round_number"],
            event["start_date"],
            event["end_date"],
            race_types=(race_type,),
        )
        local_numbers = {rider["rider_number"] for rider in db.list_riders()}
        appearances = {}
        for selected_type in (race_type,):
            for row in official[selected_type]:
                appearances.setdefault(row["rider_number"], row)
        profiles = [
            motogp_sync.fetch_rider_profile(row["rider_api_id"], event["season"], row)
            for number, row in appearances.items()
            if number not in local_numbers
        ]
        rider_summary = db.create_official_riders(
            profiles,
            operator_id=session.get("user_id"),
            operator_username=session.get("username", ""),
        )
        summary = db.sync_race_results(
            event_id,
            official,
            local_data["versions"],
            race_types=(race_type,),
            operator_id=session.get("user_id"),
            operator_username=session.get("username", ""),
        )
        db.finish_external_sync(sync_key, True, "分站排名同步成功")
    except Exception as exc:
        db.finish_external_sync(sync_key, False, str(exc))
        raise

    latest = db.list_race_results(event_id)
    return jsonify({
        "ok": True,
        "data": {
            "results": latest,
            "summary": summary,
            "riders": {
                "created": len(rider_summary["created"]),
                "items": rider_summary["created"],
            },
            "official": {
                "event_name": official["official_event_name"],
                "is_official": official["official"],
                "source_files": official["source_files"],
            },
            "cooldown_seconds": MOTOGP_RACE_RESULTS_SYNC_COOLDOWN_SECONDS,
        },
        "message": f"已从 MotoGP 官网更新{'冲刺赛' if race_type == 'sprint' else '正赛'}排名",
    })


@app.post("/api/races/results/sync-finished")
@admin_required
def api_sync_finished_race_results():
    """批量同步指定赛季中所有已完赛分站的冲刺赛和正赛排名。"""
    body = request.get_json(silent=True) or {}
    try:
        season = int(body.get("season", date.today().year))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "message": "赛季年份无效"}), 400
    if not 1949 <= season <= 2100:
        return jsonify({"ok": False, "message": "赛季年份无效"}), 400
    if not db.list_riders():
        return jsonify({
            "ok": False,
            "message": "尚未导入车手资料，请先同步官网车队与车手",
            "code": "ROSTER_REQUIRED",
        }), 400

    sync_key = f"motogp-finished-results:{season}"
    db.reserve_external_sync(
        sync_key,
        MOTOGP_RACE_RESULTS_SYNC_COOLDOWN_SECONDS,
        failure_cooldown_seconds=min(300, MOTOGP_RACE_RESULTS_SYNC_COOLDOWN_SECONDS),
    )
    try:
        official = motogp_sync.fetch_finished_season_results(season)
        if not official["events"] and not official["failed_events"]:
            raise ValueError(f"{season} 赛季暂时没有已完赛分站")

        local_events = db.list_race_events(season)
        local_by_round = {event["round_number"]: event for event in local_events}
        local_by_dates = {
            (event["start_date"], event["end_date"]): event
            for event in local_events
        }

        local_numbers = {rider["rider_number"] for rider in db.list_riders()}
        appearances = {}
        for official_event in official["events"]:
            for race_type in ("sprint", "race"):
                for row in official_event.get(race_type) or []:
                    if row["rider_number"] not in local_numbers:
                        appearances.setdefault(row["rider_number"], row)
        profiles = []
        profile_failures = []
        for row in appearances.values():
            try:
                profiles.append(
                    motogp_sync.fetch_rider_profile(row["rider_api_id"], season, row)
                )
            except motogp_sync.OfficialApiError as exc:
                profile_failures.append({
                    "rider_number": row["rider_number"],
                    "rider_name": row.get("official_name", ""),
                    "message": str(exc),
                })
        rider_summary = db.create_official_riders(
            profiles,
            operator_id=session.get("user_id"),
            operator_username=session.get("username", ""),
        )

        summary = {
            "season": season,
            "official_events": len(official["events"]),
            "synced_events": 0,
            "synced_types": 0,
            "matched": 0,
            "skipped": 0,
            "events": [],
            "failures": list(official["failed_events"]),
            "rider_failures": profile_failures,
            "riders_created": len(rider_summary["created"]),
            "cooldown_seconds": MOTOGP_RACE_RESULTS_SYNC_COOLDOWN_SECONDS,
        }
        for official_event in official["events"]:
            for race_type, message in official_event.get("errors", {}).items():
                summary["failures"].append({
                    "round_number": official_event["round_number"],
                    "event_name": official_event["official_event_name"],
                    "race_type": race_type,
                    "message": message,
                })
            local_event = local_by_dates.get(
                (official_event["start_date"], official_event["end_date"])
            ) or local_by_round.get(official_event["round_number"])
            if not local_event:
                summary["failures"].append({
                    "round_number": official_event["round_number"],
                    "event_name": official_event["official_event_name"],
                    "message": "本地数据库中没有对应分站",
                })
                continue
            race_types = tuple(
                race_type for race_type in ("sprint", "race")
                if official_event.get(race_type)
            )
            try:
                local_data = db.list_race_results(local_event["id"])
                event_summary = db.sync_race_results(
                    local_event["id"],
                    official_event,
                    local_data["versions"],
                    race_types=race_types,
                    operator_id=session.get("user_id"),
                    operator_username=session.get("username", ""),
                )
                matched = sum(
                    item["matched"] for item in event_summary["types"].values()
                )
                summary["synced_events"] += 1
                summary["synced_types"] += len(event_summary["types"])
                summary["matched"] += matched
                summary["skipped"] += event_summary["skipped_total"]
                summary["events"].append({
                    "event_id": local_event["id"],
                    "round_number": local_event["round_number"],
                    "country": local_event["country"],
                    "types": list(event_summary["types"]),
                    "matched": matched,
                    "skipped": event_summary["skipped_total"],
                })
            except Exception as exc:
                summary["failures"].append({
                    "round_number": local_event["round_number"],
                    "event_name": official_event["official_event_name"],
                    "message": str(exc),
                })

        succeeded = summary["synced_events"] > 0
        db.finish_external_sync(
            sync_key,
            succeeded,
            (
                f"同步 {summary['synced_events']} 站、{summary['synced_types']} 场；"
                f"失败 {len(summary['failures'])} 站"
            ),
        )
        if not succeeded:
            return jsonify({
                "ok": False,
                "data": summary,
                "message": "没有任何已完赛分站同步成功，请查看失败详情后重试",
            }), 502
        return jsonify({
            "ok": True,
            "data": summary,
            "message": "已完赛分站成绩批量同步完成",
        })
    except Exception as exc:
        db.finish_external_sync(sync_key, False, str(exc))
        raise


def _parse_rider_standing_body(body):
    """校验车手积分记录新增和修改接口共用的 JSON 字段。"""
    try:
        season = int(body.get("season"))
        rider_id = int(body.get("rider_id"))
        position = int(body.get("position"))
        points = int(body.get("points"))
        race_wins = int(body.get("race_wins"))
        podiums = int(body.get("podiums"))
    except (TypeError, ValueError) as exc:
        raise ValueError("赛季、车手、排名和积分数据必须是整数") from exc
    if not 1949 <= season <= 2100:
        raise ValueError("赛季年份必须在 1949-2100 之间")
    if rider_id <= 0:
        raise ValueError("所选车手无效")
    if not 1 <= position <= 99:
        raise ValueError("排名必须在 1-99 之间")
    if min(points, race_wins, podiums) < 0:
        raise ValueError("积分、胜场和领奖台次数不能为负数")
    return season, rider_id, position, points, race_wins, podiums


@app.get("/api/standings")
@login_required
def api_list_rider_standings():
    """车手积分榜查询接口。

    输入：已登录用户 Session Cookie；可选 season 查询参数。
    输出：成功返回仅关联本地车手的积分记录；年份无效返回 400，未登录返回 401。
    """
    season_text = (request.args.get("season") or "").strip()
    season = None
    if season_text:
        try:
            season = int(season_text)
        except ValueError:
            return jsonify({"ok": False, "message": "赛季年份无效"}), 400
        if not 1949 <= season <= 2100:
            return jsonify({"ok": False, "message": "赛季年份无效"}), 400
    return jsonify({"ok": True, "data": db.list_rider_standings(season)})


@app.post("/api/standings/sync")
@admin_required
def api_sync_rider_standings():
    """从 MotoGP 官方接口手动同步车手积分，仅管理员可用。

    输入：JSON {season}，同一赛季同步受服务器冷却时间限制。
    输出：成功返回匹配、新增、更新、未变化及跳过的车手统计；过于频繁返回 429，官方接口失败返回 502。
    """
    body = request.get_json(silent=True) or {}
    try:
        season = int(body.get("season", 2026))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "message": "赛季年份无效"}), 400
    if not 1949 <= season <= 2100:
        return jsonify({"ok": False, "message": "赛季年份无效"}), 400

    if not db.list_riders():
        return jsonify({
            "ok": False,
            "message": "尚未导入车手资料，请先到“车手信息”或“车队信息”页面同步官网车队与车手",
            "code": "ROSTER_REQUIRED",
        }), 400

    sync_key = f"motogp-rider-standings:{season}"
    db.reserve_external_sync(
        sync_key,
        MOTOGP_SYNC_COOLDOWN_SECONDS,
        failure_cooldown_seconds=min(300, MOTOGP_SYNC_COOLDOWN_SECONDS),
    )
    try:
        official = motogp_sync.fetch_rider_standings(season)
        summary = db.sync_rider_standings(
            season,
            official["rows"],
            operator_id=session.get("user_id"),
            operator_username=session.get("username", ""),
        )
        summary["official"] = official["official"]
        summary["source_file"] = official["source_file"]
        db.finish_external_sync(
            sync_key,
            True,
            f"匹配 {summary['matched']}，更新 {summary['updated']}，新增 {summary['created']}，跳过 {len(summary['skipped'])}",
        )
        return jsonify({"ok": True, "data": summary, "message": "官方积分同步完成"})
    except Exception as exc:
        db.finish_external_sync(sync_key, False, str(exc))
        raise


@app.post("/api/standings")
@admin_required
def api_create_rider_standing():
    """新增车手积分记录接口，仅管理员可用。

    输入：JSON {season, rider_id, position, points, race_wins, podiums}。
    输出：成功返回 201 和积分对象；字段或车手无效返回 400，鉴权失败返回 401/403。
    """
    fields = _parse_rider_standing_body(request.get_json(silent=True) or {})
    standing = db.create_rider_standing(
        *fields,
        operator_id=session.get("user_id"),
        operator_username=session.get("username", ""),
    )
    return jsonify({"ok": True, "data": standing}), 201


@app.put("/api/standings/<int:standing_id>")
@admin_required
def api_update_rider_standing(standing_id: int):
    """修改指定车手积分记录接口，仅管理员可用。

    输入：路径参数 standing_id；新增接口的全部 JSON 字段以及 version。
    输出：成功返回更新对象；字段无效返回 400，并发冲突返回 409，鉴权失败返回 401/403。
    """
    body = request.get_json(silent=True) or {}
    fields = _parse_rider_standing_body(body)
    standing = db.update_rider_standing(
        standing_id,
        *fields,
        _parse_version(body),
        operator_id=session.get("user_id"),
        operator_username=session.get("username", ""),
    )
    return jsonify({"ok": True, "data": standing})


@app.delete("/api/standings/<int:standing_id>")
@admin_required
def api_delete_rider_standing(standing_id: int):
    """删除指定车手积分记录接口，仅管理员可用。

    输入：路径参数 standing_id 和管理员 Session Cookie。
    输出：成功返回删除消息；记录不存在返回 400，鉴权失败返回 401/403。
    """
    db.delete_rider_standing(
        standing_id,
        operator_id=session.get("user_id"),
        operator_username=session.get("username", ""),
    )
    return jsonify({"ok": True, "message": "积分记录已删除"})


if __name__ == "__main__":
    db.init_database()
    app.run(host=APP_HOST, port=APP_PORT, debug=APP_DEBUG)
