import json
import re
from datetime import date, datetime, timedelta
from difflib import SequenceMatcher
from zoneinfo import ZoneInfo

import pymysql
from pymysql.cursors import DictCursor
from werkzeug.security import check_password_hash, generate_password_hash

from config import (
    ADMIN_PASSWORD,
    ADMIN_USERNAME,
    MYSQL_CONFIG,
    OPERATION_LOG_MAX_ROWS,
    OPERATION_LOG_RETENTION_DAYS,
)


class ConflictError(ValueError):
    """记录版本已变化，拒绝覆盖其他管理员刚刚提交的修改。"""


class SyncCooldownError(ValueError):
    """外部数据同步仍处于冷却期。"""

    def __init__(self, retry_after: int):
        self.retry_after = max(1, int(retry_after))
        super().__init__(f"请在 {self.retry_after} 秒后再同步")


BEIJING_TIMEZONE = ZoneInfo("Asia/Shanghai")


RACE_EVENT_SEED = [
    (2026, 1, "🇹🇭", "泰国", "THAILAND", "2026-02-27", "2026-03-01", "武里南 · 昌国际赛车场"),
    (2026, 2, "🇧🇷", "巴西", "BRAZIL", "2026-03-20", "2026-03-22", "戈亚尼亚 · 艾尔顿·塞纳国际赛车场"),
    (2026, 3, "🇺🇸", "美国", "USA", "2026-03-27", "2026-03-29", "奥斯汀 · 美洲赛道"),
    (2026, 4, "🇪🇸", "西班牙", "SPAIN", "2026-04-24", "2026-04-26", "赫雷斯 · 安赫尔·涅托赛道"),
    (2026, 5, "🇫🇷", "法国", "FRANCE", "2026-05-08", "2026-05-10", "勒芒 · 布加迪赛道"),
    (2026, 6, "🇪🇸", "加泰罗尼亚", "CATALONIA", "2026-05-15", "2026-05-17", "巴塞罗那-加泰罗尼亚赛道"),
    (2026, 7, "🇮🇹", "意大利", "ITALY", "2026-05-29", "2026-05-31", "穆杰罗赛道"),
    (2026, 8, "🇭🇺", "匈牙利", "HUNGARY", "2026-06-05", "2026-06-07", "巴拉顿公园赛道"),
    (2026, 9, "🇨🇿", "捷克", "CZECHIA", "2026-06-19", "2026-06-21", "布尔诺赛道"),
    (2026, 10, "🇳🇱", "荷兰", "NETHERLANDS", "2026-06-26", "2026-06-28", "阿森 TT 赛道"),
    (2026, 11, "🇩🇪", "德国", "GERMANY", "2026-07-10", "2026-07-12", "萨克森灵赛道"),
    (2026, 12, "🇬🇧", "英国", "GREAT BRITAIN", "2026-08-07", "2026-08-09", "银石赛道"),
    (2026, 13, "🇪🇸", "阿拉贡", "ARAGON", "2026-08-28", "2026-08-30", "阿拉贡赛车场"),
    (2026, 14, "🇸🇲", "圣马力诺", "SAN MARINO", "2026-09-11", "2026-09-13", "米萨诺世界赛道"),
    (2026, 15, "🇦🇹", "奥地利", "AUSTRIA", "2026-09-18", "2026-09-20", "红牛环赛道"),
    (2026, 16, "🇯🇵", "日本", "JAPAN", "2026-10-02", "2026-10-04", "茂木移动度假村"),
    (2026, 17, "🇮🇩", "印度尼西亚", "INDONESIA", "2026-10-09", "2026-10-11", "曼达利卡国际街道赛道"),
    (2026, 18, "🇦🇺", "澳大利亚", "AUSTRALIA", "2026-10-22", "2026-10-25", "菲利普岛大奖赛赛道"),
    (2026, 19, "🇲🇾", "马来西亚", "MALAYSIA", "2026-10-30", "2026-11-01", "雪邦国际赛道"),
    (2026, 20, "🇶🇦", "卡塔尔", "QATAR", "2026-11-06", "2026-11-08", "卢赛尔国际赛道"),
    (2026, 21, "🇵🇹", "葡萄牙", "PORTUGAL", "2026-11-20", "2026-11-22", "阿尔加维国际赛道"),
    (2026, 22, "🇪🇸", "瓦伦西亚", "VALENCIA", "2026-11-27", "2026-11-29", "里卡多·托尔莫赛道"),
]

RIDER_STANDING_SEED = [
    (2026, 1, "89", 208, 1, 5),
    (2026, 2, "79", 194, 1, 4),
    (2026, 3, "93", 190, 3, 3),
    (2026, 4, "72", 186, 4, 6),
    (2026, 5, "49", 184, 1, 3),
    (2026, 6, "25", 159, 0, 3),
    (2026, 7, "37", 148, 0, 3),
    (2026, 8, "63", 143, 0, 4),
    (2026, 9, "73", 87, 1, 1),
    (2026, 10, "10", 79, 0, 0),
    (2026, 11, "54", 76, 0, 1),
    (2026, 12, "23", 76, 0, 0),
    (2026, 13, "33", 64, 0, 0),
    (2026, 14, "20", 55, 0, 0),
    (2026, 15, "11", 48, 0, 0),
    (2026, 16, "21", 46, 0, 0),
    (2026, 17, "5", 34, 0, 0),
    (2026, 18, "36", 26, 0, 0),
    (2026, 19, "42", 21, 0, 0),
    (2026, 20, "43", 19, 0, 0),
    (2026, 21, "7", 12, 0, 0),
    (2026, 22, "12", 10, 0, 0),
]


def get_connection(with_db: bool = True):
    cfg = MYSQL_CONFIG.copy()
    if not with_db:
        cfg.pop("database", None)
    return pymysql.connect(**cfg, cursorclass=DictCursor, autocommit=False)


def init_database():
    """创建数据库、用户表、车队表、车手表，并内置超级管理员"""
    conn = get_connection(with_db=False)
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"CREATE DATABASE IF NOT EXISTS `{MYSQL_CONFIG['database']}` "
                "DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
            )
        conn.commit()
    finally:
        conn.close()

    conn = get_connection(with_db=True)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    username VARCHAR(64) NOT NULL UNIQUE COMMENT '账号',
                    password_hash VARCHAR(255) NOT NULL COMMENT '密码哈希',
                    role ENUM('admin', 'user') NOT NULL DEFAULT 'user' COMMENT '角色',
                    version INT NOT NULL DEFAULT 1 COMMENT '并发控制版本号',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS teams (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    name VARCHAR(128) NOT NULL UNIQUE COMMENT '车队名称',
                    manufacturer VARCHAR(128) NOT NULL COMMENT '车辆制造商',
                    version INT NOT NULL DEFAULT 1 COMMENT '并发控制版本号',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS riders (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    rider_number VARCHAR(16) NOT NULL COMMENT '车手编号',
                    english_name VARCHAR(128) NOT NULL COMMENT '英文名',
                    chinese_name VARCHAR(64) NOT NULL COMMENT '中文名',
                    nickname VARCHAR(64) DEFAULT NULL COMMENT '昵称',
                    nationality VARCHAR(64) NOT NULL COMMENT '国籍',
                    team_id INT DEFAULT NULL COMMENT '所属车队',
                    bike VARCHAR(128) NOT NULL COMMENT '驾驶车辆',
                    birth_date DATE NOT NULL COMMENT '出生日期',
                    birth_place VARCHAR(128) NOT NULL COMMENT '出生地点',
                    version INT NOT NULL DEFAULT 1 COMMENT '并发控制版本号',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    CONSTRAINT fk_rider_team FOREIGN KEY (team_id)
                        REFERENCES teams(id) ON DELETE SET NULL
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS race_events (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    season SMALLINT NOT NULL COMMENT '赛季年份',
                    round_number SMALLINT NOT NULL COMMENT '分站序号',
                    flag VARCHAR(16) NOT NULL DEFAULT '🏁' COMMENT '国家或地区旗帜',
                    country VARCHAR(64) NOT NULL COMMENT '国家或地区中文名',
                    country_en VARCHAR(64) NOT NULL COMMENT '国家或地区英文名',
                    start_date DATE NOT NULL COMMENT '比赛周末开始日期',
                    end_date DATE NOT NULL COMMENT '比赛周末结束日期',
                    circuit VARCHAR(128) NOT NULL COMMENT '赛道名称',
                    version INT NOT NULL DEFAULT 1 COMMENT '并发控制版本号',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    UNIQUE KEY uq_race_events_season_round (season, round_number),
                    INDEX idx_race_events_dates (start_date, end_date)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS race_schedule_sets (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    race_event_id INT NOT NULL COMMENT '所属分站ID',
                    version INT NOT NULL DEFAULT 1 COMMENT '并发控制版本号',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    UNIQUE KEY uq_race_schedule_sets_event (race_event_id),
                    CONSTRAINT fk_schedule_set_event FOREIGN KEY (race_event_id)
                        REFERENCES race_events(id) ON DELETE CASCADE
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS race_schedule_items (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    schedule_set_id INT NOT NULL COMMENT '日程批次ID',
                    schedule_date DATE NOT NULL COMMENT '比赛周末日期',
                    start_time CHAR(5) NOT NULL COMMENT '赛道当地开始时间',
                    end_time CHAR(5) DEFAULT NULL COMMENT '赛道当地结束时间',
                    category VARCHAR(32) NOT NULL DEFAULT 'MotoGP' COMMENT '比赛组别',
                    session_name VARCHAR(128) NOT NULL COMMENT '环节名称',
                    sort_order SMALLINT NOT NULL DEFAULT 0 COMMENT '同日展示顺序',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    INDEX idx_race_schedule_items_order
                        (schedule_set_id, schedule_date, start_time, sort_order),
                    CONSTRAINT fk_schedule_item_set FOREIGN KEY (schedule_set_id)
                        REFERENCES race_schedule_sets(id) ON DELETE CASCADE
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """
            )
            cur.execute("SHOW TABLES LIKE 'rider_standings'")
            standings_table_existed = bool(cur.fetchone())
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS rider_standings (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    season SMALLINT NOT NULL COMMENT '赛季年份',
                    rider_id INT NOT NULL COMMENT '本地车手ID',
                    position SMALLINT NOT NULL COMMENT '官方排名',
                    points INT NOT NULL DEFAULT 0 COMMENT '总积分',
                    race_wins SMALLINT NOT NULL DEFAULT 0 COMMENT '正赛胜场',
                    podiums SMALLINT NOT NULL DEFAULT 0 COMMENT '正赛领奖台次数',
                    version INT NOT NULL DEFAULT 1 COMMENT '并发控制版本号',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    UNIQUE KEY uq_rider_standings_season_rider (season, rider_id),
                    INDEX idx_rider_standings_season_position (season, position),
                    CONSTRAINT fk_standing_rider FOREIGN KEY (rider_id)
                        REFERENCES riders(id) ON DELETE CASCADE
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS race_result_sets (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    race_event_id INT NOT NULL COMMENT '所属分站ID',
                    race_type ENUM('sprint', 'race') NOT NULL COMMENT '冲刺赛或正赛',
                    version INT NOT NULL DEFAULT 1 COMMENT '并发控制版本号',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    UNIQUE KEY uq_race_result_sets_event_type (race_event_id, race_type),
                    CONSTRAINT fk_result_set_event FOREIGN KEY (race_event_id)
                        REFERENCES race_events(id) ON DELETE CASCADE
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS race_results (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    result_set_id INT NOT NULL COMMENT '排名批次ID',
                    rider_id INT NOT NULL COMMENT '车手ID',
                    position SMALLINT NULL COMMENT '完赛排名，DNF 时为空',
                    points SMALLINT NOT NULL DEFAULT 0 COMMENT '本站积分',
                    finish_time VARCHAR(32) DEFAULT NULL COMMENT '官网完赛或退赛前用时',
                    result_status VARCHAR(16) NOT NULL DEFAULT 'FINISHED' COMMENT 'FINISHED或DNF',
                    remaining_laps SMALLINT DEFAULT NULL COMMENT '未完赛时的剩余圈数',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE KEY uq_race_results_set_rider (result_set_id, rider_id),
                    UNIQUE KEY uq_race_results_set_position (result_set_id, position),
                    CONSTRAINT fk_race_result_set FOREIGN KEY (result_set_id)
                        REFERENCES race_result_sets(id) ON DELETE CASCADE,
                    CONSTRAINT fk_race_result_rider FOREIGN KEY (rider_id)
                        REFERENCES riders(id) ON DELETE RESTRICT
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """
            )
            cur.execute("SHOW COLUMNS FROM race_results LIKE 'finish_time'")
            if not cur.fetchone():
                cur.execute(
                    "ALTER TABLE race_results "
                    "ADD COLUMN finish_time VARCHAR(32) NULL AFTER points, "
                    "ADD COLUMN result_status VARCHAR(16) NOT NULL DEFAULT 'FINISHED' AFTER finish_time, "
                    "ADD COLUMN remaining_laps SMALLINT NULL AFTER result_status"
                )
            cur.execute("SHOW COLUMNS FROM race_results LIKE 'position'")
            result_position_column = cur.fetchone()
            if result_position_column and result_position_column.get("Null") == "NO":
                cur.execute(
                    "ALTER TABLE race_results MODIFY COLUMN position SMALLINT NULL "
                    "COMMENT '完赛排名，DNF 时为空'"
                )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS external_sync_state (
                    sync_key VARCHAR(64) PRIMARY KEY,
                    last_attempt_at TIMESTAMP NULL DEFAULT NULL,
                    last_success_at TIMESTAMP NULL DEFAULT NULL,
                    last_status VARCHAR(16) NOT NULL DEFAULT 'idle',
                    last_message VARCHAR(255) NOT NULL DEFAULT ''
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """
            )
            cur.execute("SHOW COLUMNS FROM riders LIKE 'rider_number'")
            rider_number_column = cur.fetchone()
            if not rider_number_column:
                cur.execute(
                    "ALTER TABLE riders ADD COLUMN rider_number VARCHAR(16) NULL COMMENT '车手编号' AFTER id"
                )
                cur.execute(
                    "UPDATE riders SET rider_number = CAST(id AS CHAR) "
                    "WHERE rider_number IS NULL OR TRIM(rider_number) = ''"
                )
                cur.execute(
                    "ALTER TABLE riders MODIFY COLUMN rider_number VARCHAR(16) NOT NULL COMMENT '车手编号'"
                )
            elif rider_number_column.get("Null") == "YES":
                cur.execute(
                    "UPDATE riders SET rider_number = CAST(id AS CHAR) "
                    "WHERE rider_number IS NULL OR TRIM(rider_number) = ''"
                )
                cur.execute(
                    "ALTER TABLE riders MODIFY COLUMN rider_number VARCHAR(16) NOT NULL COMMENT '车手编号'"
                )
            cur.execute("SHOW INDEX FROM riders WHERE Key_name = 'uq_riders_rider_number'")
            if not cur.fetchone():
                cur.execute(
                    "SELECT rider_number, COUNT(*) AS count FROM riders "
                    "GROUP BY rider_number HAVING COUNT(*) > 1 LIMIT 1"
                )
                duplicate = cur.fetchone()
                if duplicate:
                    raise ValueError(f"车手编号 {duplicate['rider_number']} 存在重复，无法创建唯一约束")
                cur.execute(
                    "ALTER TABLE riders ADD UNIQUE INDEX uq_riders_rider_number (rider_number)"
                )
            for table_name in (
                "users", "teams", "riders", "race_events", "race_schedule_sets",
                "rider_standings", "race_result_sets",
            ):
                cur.execute(f"SHOW COLUMNS FROM {table_name} LIKE 'version'")
                if not cur.fetchone():
                    cur.execute(
                        f"ALTER TABLE {table_name} "
                        "ADD COLUMN version INT NOT NULL DEFAULT 1 COMMENT '并发控制版本号' AFTER created_at"
                    )
                cur.execute(f"SHOW COLUMNS FROM {table_name} LIKE 'updated_at'")
                if not cur.fetchone():
                    cur.execute(
                        f"ALTER TABLE {table_name} "
                        "ADD COLUMN updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP "
                        "ON UPDATE CURRENT_TIMESTAMP AFTER version"
                    )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS operation_logs (
                    id BIGINT AUTO_INCREMENT PRIMARY KEY,
                    operator_id INT DEFAULT NULL COMMENT '操作用户ID',
                    operator_username VARCHAR(64) NOT NULL COMMENT '操作账号快照',
                    action VARCHAR(16) NOT NULL COMMENT 'create/update/delete',
                    entity_type VARCHAR(16) NOT NULL COMMENT 'rider/team/user',
                    entity_id INT DEFAULT NULL COMMENT '业务对象ID',
                    entity_name VARCHAR(128) NOT NULL COMMENT '业务对象名称快照',
                    before_data LONGTEXT DEFAULT NULL COMMENT '修改前JSON',
                    after_data LONGTEXT DEFAULT NULL COMMENT '修改后JSON',
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    INDEX idx_operation_logs_created_at (created_at),
                    INDEX idx_operation_logs_entity (entity_type, entity_id),
                    INDEX idx_operation_logs_operator (operator_id)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """
            )
            for index_name, index_columns in (
                ("idx_operation_logs_operator_username", "operator_username"),
                ("idx_operation_logs_action", "action"),
                ("idx_operation_logs_type_action_time", "entity_type, action, created_at"),
            ):
                cur.execute("SHOW INDEX FROM operation_logs WHERE Key_name = %s", (index_name,))
                if not cur.fetchone():
                    cur.execute(f"ALTER TABLE operation_logs ADD INDEX {index_name} ({index_columns})")
            _purge_operation_logs(cur)
            cur.execute("SELECT COUNT(*) AS count FROM race_events")
            if cur.fetchone()["count"] == 0:
                cur.executemany(
                    """
                    INSERT INTO race_events
                        (season, round_number, flag, country, country_en, start_date, end_date, circuit)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    RACE_EVENT_SEED,
                )
            if not standings_table_existed:
                cur.executemany(
                    """
                    INSERT INTO rider_standings
                        (season, position, rider_id, points, race_wins, podiums)
                    SELECT %s, %s, id, %s, %s, %s
                    FROM riders WHERE rider_number = %s
                    """,
                    [
                        (season, position, points, race_wins, podiums, rider_number)
                        for season, position, rider_number, points, race_wins, podiums
                        in RIDER_STANDING_SEED
                    ],
                )
            cur.execute("SELECT id FROM users WHERE username = %s", (ADMIN_USERNAME,))
            if not cur.fetchone():
                cur.execute(
                    "INSERT INTO users (username, password_hash, role) VALUES (%s, %s, 'admin')",
                    (ADMIN_USERNAME, generate_password_hash(ADMIN_PASSWORD)),
                )
        conn.commit()
    finally:
        conn.close()


def _public_user(row):
    if not row:
        return None
    data = {
        "id": row["id"],
        "username": row["username"],
        "role": row["role"],
    }
    if "created_at" in row and row["created_at"] is not None:
        created = row["created_at"]
        data["created_at"] = created.strftime("%Y-%m-%d %H:%M:%S") if hasattr(created, "strftime") else str(created)
    if "version" in row:
        data["version"] = row["version"]
    if "updated_at" in row and row["updated_at"] is not None:
        data["updated_at"] = _format_datetime(row["updated_at"])
    return data


def is_builtin_admin(username: str) -> bool:
    return username == ADMIN_USERNAME


def list_users():
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, username, role, created_at, version, updated_at FROM users ORDER BY id ASC"
            )
            return [_public_user(row) for row in cur.fetchall()]
    finally:
        conn.close()


def update_user_role(user_id: int, role: str, expected_version: int, operator_id=None, operator_username=""):
    if role not in ("admin", "user"):
        raise ValueError("无效的角色类型")

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, username, role, created_at, version, updated_at FROM users WHERE id = %s",
                (user_id,),
            )
            user = cur.fetchone()
            if not user:
                raise ValueError("用户不存在")
            if is_builtin_admin(user["username"]):
                raise ValueError("不能修改内置超级管理员的权限")
            if operator_id is not None and user_id == operator_id:
                raise ValueError("不能修改当前登录账号的权限")
            before_user = _public_user(user)
            cur.execute(
                "UPDATE users SET role = %s, version = version + 1 WHERE id = %s AND version = %s",
                (role, user_id, expected_version),
            )
            if cur.rowcount == 0:
                raise ConflictError("该账号权限已被其他管理员修改，请刷新后重试")
            cur.execute(
                "SELECT id, username, role, created_at, version, updated_at FROM users WHERE id = %s",
                (user_id,),
            )
            updated_user = _public_user(cur.fetchone())
            _insert_operation_log(
                cur, operator_id, operator_username, "update", "user",
                user_id, updated_user["username"], before_data=before_user, after_data=updated_user
            )
        conn.commit()
        return updated_user
    finally:
        conn.close()


def delete_user(user_id: int, operator_id: int | None = None, operator_username=""):
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, username, role, created_at, version, updated_at FROM users WHERE id = %s",
                (user_id,),
            )
            user = cur.fetchone()
            if not user:
                raise ValueError("用户不存在")
            if is_builtin_admin(user["username"]):
                raise ValueError("不能删除内置超级管理员账号")
            if operator_id is not None and user_id == operator_id:
                raise ValueError("不能删除当前登录账号")
            before_user = _public_user(user)
            cur.execute("DELETE FROM users WHERE id = %s", (user_id,))
            _insert_operation_log(
                cur, operator_id, operator_username, "delete", "user",
                user_id, before_user["username"], before_data=before_user
            )
        conn.commit()
    finally:
        conn.close()


def get_user_by_username(username: str):
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, username, password_hash, role FROM users WHERE username = %s",
                (username,),
            )
            return cur.fetchone()
    finally:
        conn.close()


def get_user_by_id(user_id: int):
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, username, role, created_at, version, updated_at FROM users WHERE id = %s",
                (user_id,),
            )
            return _public_user(cur.fetchone())
    finally:
        conn.close()


def create_user(username: str, password: str, role: str = "user"):
    if role not in ("admin", "user"):
        raise ValueError("无效的角色类型")
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO users (username, password_hash, role) VALUES (%s, %s, %s)",
                (username, generate_password_hash(password), role),
            )
            new_id = cur.lastrowid
        conn.commit()
        return get_user_by_id(new_id)
    except pymysql.err.IntegrityError as e:
        conn.rollback()
        raise ValueError("账号已存在") from e
    finally:
        conn.close()


def authenticate_user(username: str, password: str):
    user = get_user_by_username(username)
    if not user or not check_password_hash(user["password_hash"], password):
        return None
    return _public_user(user)


def change_user_password(
    user_id: int,
    current_password: str,
    new_password: str,
    operator_username: str = "",
):
    """验证当前密码后更新密码哈希，审计日志不保存任何密码内容。"""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, username, password_hash, role, created_at, version, updated_at
                FROM users WHERE id = %s
                """,
                (user_id,),
            )
            user = cur.fetchone()
            if not user:
                raise ValueError("用户不存在")
            if not check_password_hash(user["password_hash"], current_password):
                raise ValueError("当前密码错误")
            if check_password_hash(user["password_hash"], new_password):
                raise ValueError("新密码不能与当前密码相同")

            before_user = _public_user(user)
            cur.execute(
                """
                UPDATE users
                SET password_hash = %s, version = version + 1
                WHERE id = %s
                """,
                (generate_password_hash(new_password), user_id),
            )
            cur.execute(
                """
                SELECT id, username, role, created_at, version, updated_at, NOW() AS password_changed_at
                FROM users WHERE id = %s
                """,
                (user_id,),
            )
            updated_row = cur.fetchone()
            updated_user = _public_user(updated_row)
            before_user["password_changed_at"] = None
            updated_user["password_changed_at"] = _format_datetime(
                updated_row["password_changed_at"]
            )
            _insert_operation_log(
                cur,
                user_id,
                operator_username or user["username"],
                "update",
                "user",
                user_id,
                user["username"],
                before_data=before_user,
                after_data=updated_user,
            )
        conn.commit()
    finally:
        conn.close()


def _format_date(value):
    if value is None:
        return None
    if hasattr(value, "strftime"):
        return value.strftime("%Y-%m-%d")
    return str(value)[:10]


def _format_datetime(value):
    if value is None:
        return None
    if hasattr(value, "strftime"):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    return str(value)


def _serialize_rider(row):
    if not row:
        return None
    return {
        "id": row["id"],
        "rider_number": row["rider_number"],
        "english_name": row["english_name"],
        "chinese_name": row["chinese_name"],
        "nickname": row["nickname"] or "",
        "nationality": row["nationality"],
        "team_id": row["team_id"],
        "team_name": row.get("team_name") or "",
        "bike": row["bike"],
        "birth_date": _format_date(row["birth_date"]),
        "birth_place": row["birth_place"],
        "version": row["version"],
        "updated_at": _format_datetime(row["updated_at"]),
    }


def _serialize_team(row):
    if not row:
        return None
    return {
        "id": row["id"],
        "name": row["name"],
        "manufacturer": row["manufacturer"],
        "members": row.get("members") or "",
        "version": row["version"],
        "updated_at": _format_datetime(row["updated_at"]),
    }


def _json_dump(data):
    return json.dumps(data, ensure_ascii=False, separators=(",", ":")) if data is not None else None


def _json_load(data):
    if not data:
        return None
    try:
        return json.loads(data)
    except (TypeError, json.JSONDecodeError):
        return None


def _purge_operation_logs(cur):
    """按保留天数和最大行数清理最旧日志。配置为 0 时关闭对应限制。"""
    retention_days = max(0, int(OPERATION_LOG_RETENTION_DAYS))
    max_rows = max(0, int(OPERATION_LOG_MAX_ROWS))
    if retention_days:
        cur.execute(
            f"DELETE FROM operation_logs "
            f"WHERE created_at < DATE_SUB(NOW(), INTERVAL {retention_days} DAY)"
        )
    if max_rows:
        cur.execute(
            "DELETE FROM operation_logs WHERE id NOT IN ("
            "SELECT id FROM ("
            f"SELECT id FROM operation_logs ORDER BY id DESC LIMIT {max_rows}"
            ") AS retained_logs)"
        )


def _insert_operation_log(
    cur,
    operator_id,
    operator_username,
    action,
    entity_type,
    entity_id,
    entity_name,
    before_data=None,
    after_data=None,
):
    """在业务操作的同一事务中写入审计日志，保证数据修改与日志同时成功或同时回滚。"""
    cur.execute(
        """
        INSERT INTO operation_logs
            (operator_id, operator_username, action, entity_type, entity_id,
             entity_name, before_data, after_data)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            operator_id,
            operator_username or "未知用户",
            action,
            entity_type,
            entity_id,
            entity_name,
            _json_dump(before_data),
            _json_dump(after_data),
        ),
    )
    _purge_operation_logs(cur)


def get_operation_log_policy():
    return {
        "retention_days": max(0, int(OPERATION_LOG_RETENTION_DAYS)),
        "max_rows": max(0, int(OPERATION_LOG_MAX_ROWS)),
    }


def list_operation_logs(
    page: int = 1,
    page_size: int = 20,
    operator: str = "",
    entity_type: str = "",
    action: str = "",
    date_from: str = "",
    date_to: str = "",
):
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            conditions = []
            params = []
            if operator:
                conditions.append("operator_username LIKE %s")
                params.append(f"%{operator}%")
            if entity_type:
                conditions.append("entity_type = %s")
                params.append(entity_type)
            if action:
                conditions.append("action = %s")
                params.append(action)
            if date_from:
                conditions.append("created_at >= %s")
                params.append(f"{date_from} 00:00:00")
            if date_to:
                conditions.append("created_at < DATE_ADD(%s, INTERVAL 1 DAY)")
                params.append(f"{date_to} 00:00:00")
            where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
            cur.execute(f"SELECT COUNT(*) AS total FROM operation_logs {where_clause}", params)
            total = cur.fetchone()["total"]
            offset = (page - 1) * page_size
            cur.execute(
                f"""
                SELECT id, operator_id, operator_username, action, entity_type,
                       entity_id, entity_name, before_data, after_data, created_at
                FROM operation_logs
                {where_clause}
                ORDER BY id DESC
                LIMIT %s
                OFFSET %s
                """,
                (*params, page_size, offset),
            )
            logs = []
            for row in cur.fetchall():
                created_at = row["created_at"]
                logs.append(
                    {
                        "id": row["id"],
                        "operator_id": row["operator_id"],
                        "operator_username": row["operator_username"],
                        "action": row["action"],
                        "entity_type": row["entity_type"],
                        "entity_id": row["entity_id"],
                        "entity_name": row["entity_name"],
                        "before_data": _json_load(row["before_data"]),
                        "after_data": _json_load(row["after_data"]),
                        "created_at": created_at.strftime("%Y-%m-%d %H:%M:%S")
                        if hasattr(created_at, "strftime")
                        else str(created_at),
                    }
                )
            return {"items": logs, "total": total, "page": page, "page_size": page_size}
    finally:
        conn.close()


def list_teams(name: str = "", manufacturer: str = ""):
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            conditions = []
            params = []
            if name:
                conditions.append("t.name LIKE %s")
                params.append(f"%{name}%")
            if manufacturer:
                conditions.append("t.manufacturer LIKE %s")
                params.append(f"%{manufacturer}%")
            where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
            cur.execute(
                f"""
                SELECT t.id, t.name, t.manufacturer, t.version, t.updated_at,
                       GROUP_CONCAT(
                           CONCAT(r.rider_number, ' ', r.english_name)
                           ORDER BY r.id SEPARATOR '、'
                       ) AS members
                FROM teams t
                LEFT JOIN riders r ON r.team_id = t.id
                {where_clause}
                GROUP BY t.id, t.name, t.manufacturer, t.version, t.updated_at
                ORDER BY t.id ASC
                """,
                params,
            )
            return [_serialize_team(row) for row in cur.fetchall()]
    finally:
        conn.close()


def _fetch_team(cur, team_id: int):
    cur.execute(
        """
        SELECT t.id, t.name, t.manufacturer, t.version, t.updated_at,
               GROUP_CONCAT(
                   CONCAT(r.rider_number, ' ', r.english_name)
                   ORDER BY r.id SEPARATOR '、'
               ) AS members
        FROM teams t
        LEFT JOIN riders r ON r.team_id = t.id
        WHERE t.id = %s
        GROUP BY t.id, t.name, t.manufacturer, t.version, t.updated_at
        """,
        (team_id,),
    )
    return _serialize_team(cur.fetchone())


def get_team(team_id: int):
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            return _fetch_team(cur, team_id)
    finally:
        conn.close()


def create_team(name: str, manufacturer: str, operator_id=None, operator_username=""):
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO teams (name, manufacturer) VALUES (%s, %s)",
                (name, manufacturer),
            )
            new_id = cur.lastrowid
            team = _fetch_team(cur, new_id)
            _insert_operation_log(
                cur, operator_id, operator_username, "create", "team",
                new_id, team["name"], after_data=team
            )
        conn.commit()
        return team
    except pymysql.err.IntegrityError as e:
        conn.rollback()
        raise ValueError("车队名称已存在") from e
    finally:
        conn.close()


def update_team(
    team_id: int,
    name: str,
    manufacturer: str,
    expected_version: int,
    operator_id=None,
    operator_username="",
):
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            before_team = _fetch_team(cur, team_id)
            if not before_team:
                raise ValueError("车队不存在")
            cur.execute(
                "UPDATE teams SET name = %s, manufacturer = %s, version = version + 1 "
                "WHERE id = %s AND version = %s",
                (name, manufacturer, team_id, expected_version),
            )
            if cur.rowcount == 0:
                raise ConflictError("该车队已被其他管理员修改，请刷新后重试")
            team = _fetch_team(cur, team_id)
            _insert_operation_log(
                cur, operator_id, operator_username, "update", "team",
                team_id, team["name"], before_data=before_team, after_data=team
            )
        conn.commit()
        return team
    except pymysql.err.IntegrityError as e:
        conn.rollback()
        raise ValueError("车队名称已存在") from e
    finally:
        conn.close()


def delete_team(team_id: int, operator_id=None, operator_username=""):
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            before_team = _fetch_team(cur, team_id)
            if not before_team:
                raise ValueError("车队不存在")
            cur.execute("SELECT COUNT(*) AS cnt FROM riders WHERE team_id = %s", (team_id,))
            if cur.fetchone()["cnt"] > 0:
                raise ValueError("该车队下仍有车手，请先移除或转移车手")
            cur.execute("DELETE FROM teams WHERE id = %s", (team_id,))
            _insert_operation_log(
                cur, operator_id, operator_username, "delete", "team",
                team_id, before_team["name"], before_data=before_team
            )
        conn.commit()
    finally:
        conn.close()


def list_riders(
    rider_number: str = "",
    name: str = "",
    nationality: str = "",
    team_id: int | None = None,
):
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            conditions = []
            params = []
            if rider_number:
                conditions.append("r.rider_number LIKE %s")
                params.append(f"%{rider_number}%")
            if name:
                conditions.append("(r.english_name LIKE %s OR r.chinese_name LIKE %s OR r.nickname LIKE %s)")
                name_pattern = f"%{name}%"
                params.extend([name_pattern, name_pattern, name_pattern])
            if nationality:
                conditions.append("r.nationality LIKE %s")
                params.append(f"%{nationality}%")
            if team_id is not None:
                conditions.append("r.team_id = %s")
                params.append(team_id)
            where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
            cur.execute(
                f"""
                SELECT r.id, r.rider_number, r.english_name, r.chinese_name, r.nickname, r.nationality,
                       r.team_id, t.name AS team_name, r.bike, r.birth_date, r.birth_place,
                       r.version, r.updated_at
                FROM riders r
                LEFT JOIN teams t ON t.id = r.team_id
                {where_clause}
                ORDER BY r.id DESC
                """,
                params,
            )
            return [_serialize_rider(row) for row in cur.fetchall()]
    finally:
        conn.close()


def _match_official_team(teams: list[dict], team_name: str, manufacturer: str):
    """将官网赞助车队名匹配到本地车队，避免因冠名差异创建重复车队。"""
    def normalize(value):
        return " ".join(re.findall(r"[a-z0-9]+", str(value).lower()))

    official = normalize(team_name)
    candidates = [
        team for team in teams
        if not manufacturer or normalize(team["manufacturer"]) == normalize(manufacturer)
    ] or teams
    for team in candidates:
        local = normalize(team["name"])
        if official == local or (official and (official in local or local in official)):
            return team["id"]
    if "factory" in official:
        satellite_tokens = ("pramac", "trackhouse", "gresini", "lcr", "tech3", "vr46")
        factory_candidates = [
            team for team in candidates
            if not any(token in normalize(team["name"]) for token in satellite_tokens)
        ]
        if len(factory_candidates) == 1:
            return factory_candidates[0]["id"]
    if not official:
        return None
    best = max(
        candidates,
        key=lambda team: SequenceMatcher(None, official, normalize(team["name"])).ratio(),
        default=None,
    )
    if best and SequenceMatcher(None, official, normalize(best["name"])).ratio() >= 0.45:
        return best["id"]
    return None


def create_official_riders(profiles: list[dict], operator_id=None, operator_username=""):
    """新增官网赛果中出现但本地不存在的车手，并返回新增及已存在统计。"""
    country_names = {
        "ES": "西班牙", "IT": "意大利", "FR": "法国", "GB": "英国",
        "AU": "澳大利亚", "ZA": "南非", "JP": "日本", "BR": "巴西",
        "TR": "土耳其", "DE": "德国", "PT": "葡萄牙", "US": "美国",
    }
    conn = get_connection()
    created = []
    existing = []
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id, name, manufacturer FROM teams")
            teams = list(cur.fetchall())
            for profile in profiles:
                number = str(profile["rider_number"]).strip()
                cur.execute("SELECT id FROM riders WHERE rider_number = %s", (number,))
                if cur.fetchone():
                    existing.append(number)
                    continue
                team_id = _match_official_team(
                    teams, profile.get("team_name", ""), profile.get("bike", "")
                )
                nationality = country_names.get(
                    str(profile.get("country_iso") or "").upper(),
                    profile.get("nationality") or "未知",
                )
                birth_city = profile.get("birth_place") or "未知"
                birth_place = (
                    f"{birth_city}, {profile.get('nationality')}"
                    if birth_city != "未知" and profile.get("nationality")
                    else birth_city
                )
                cur.execute(
                    """
                    INSERT INTO riders
                        (rider_number, english_name, chinese_name, nickname, nationality,
                         team_id, bike, birth_date, birth_place)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        number,
                        profile["english_name"],
                        profile.get("chinese_name") or profile["english_name"],
                        profile.get("nickname") or None,
                        nationality,
                        team_id,
                        profile.get("bike") or "未知",
                        profile.get("birth_date") or "1900-01-01",
                        birth_place,
                    ),
                )
                rider = _fetch_rider(cur, cur.lastrowid)
                created.append(rider)
                _insert_operation_log(
                    cur, operator_id, operator_username, "create", "rider",
                    rider["id"], rider["english_name"], after_data=rider,
                )
        conn.commit()
        return {"created": created, "existing": existing}
    except pymysql.err.IntegrityError as exc:
        conn.rollback()
        raise ValueError("官网车手编号与本地数据冲突") from exc
    finally:
        conn.close()


def _fetch_rider(cur, rider_id: int):
    cur.execute(
        """
        SELECT r.id, r.rider_number, r.english_name, r.chinese_name, r.nickname, r.nationality,
               r.team_id, t.name AS team_name, r.bike, r.birth_date, r.birth_place,
               r.version, r.updated_at
        FROM riders r
        LEFT JOIN teams t ON t.id = r.team_id
        WHERE r.id = %s
        """,
        (rider_id,),
    )
    return _serialize_rider(cur.fetchone())


def get_rider(rider_id: int):
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            return _fetch_rider(cur, rider_id)
    finally:
        conn.close()


def _validate_team_id(cur, team_id):
    if team_id is None:
        return None
    cur.execute("SELECT id FROM teams WHERE id = %s", (team_id,))
    if not cur.fetchone():
        raise ValueError("所选车队不存在")
    return team_id


def create_rider(
    rider_number: str,
    english_name: str,
    chinese_name: str,
    nickname: str,
    nationality: str,
    team_id: int | None,
    bike: str,
    birth_date: str,
    birth_place: str,
    operator_id=None,
    operator_username="",
):
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            team_id = _validate_team_id(cur, team_id)
            cur.execute(
                """
                INSERT INTO riders
                    (rider_number, english_name, chinese_name, nickname, nationality, team_id, bike, birth_date, birth_place)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    rider_number,
                    english_name,
                    chinese_name,
                    nickname or None,
                    nationality,
                    team_id,
                    bike,
                    birth_date,
                    birth_place,
                ),
            )
            new_id = cur.lastrowid
            rider = _fetch_rider(cur, new_id)
            _insert_operation_log(
                cur, operator_id, operator_username, "create", "rider",
                new_id, rider["english_name"], after_data=rider
            )
        conn.commit()
        return rider
    except pymysql.err.IntegrityError as e:
        conn.rollback()
        raise ValueError("车手编号已存在") from e
    finally:
        conn.close()


def update_rider(
    rider_id: int,
    rider_number: str,
    english_name: str,
    chinese_name: str,
    nickname: str,
    nationality: str,
    team_id: int | None,
    bike: str,
    birth_date: str,
    birth_place: str,
    expected_version: int,
    operator_id=None,
    operator_username="",
):
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            team_id = _validate_team_id(cur, team_id)
            before_rider = _fetch_rider(cur, rider_id)
            if not before_rider:
                raise ValueError("车手不存在")
            cur.execute(
                """
                UPDATE riders
                SET rider_number = %s, english_name = %s, chinese_name = %s, nickname = %s, nationality = %s,
                    team_id = %s, bike = %s, birth_date = %s, birth_place = %s, version = version + 1
                WHERE id = %s AND version = %s
                """,
                (
                    rider_number,
                    english_name,
                    chinese_name,
                    nickname or None,
                    nationality,
                    team_id,
                    bike,
                    birth_date,
                    birth_place,
                    rider_id,
                    expected_version,
                ),
            )
            if cur.rowcount == 0:
                raise ConflictError("该车手已被其他管理员修改，请刷新后重试")
            rider = _fetch_rider(cur, rider_id)
            _insert_operation_log(
                cur, operator_id, operator_username, "update", "rider",
                rider_id, rider["english_name"], before_data=before_rider, after_data=rider
            )
        conn.commit()
        return rider
    except pymysql.err.IntegrityError as e:
        conn.rollback()
        raise ValueError("车手编号已存在") from e
    finally:
        conn.close()


def delete_rider(rider_id: int, operator_id=None, operator_username=""):
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            before_rider = _fetch_rider(cur, rider_id)
            if not before_rider:
                raise ValueError("车手不存在")
            cur.execute("DELETE FROM riders WHERE id = %s", (rider_id,))
            _insert_operation_log(
                cur, operator_id, operator_username, "delete", "rider",
                rider_id, before_rider["english_name"], before_data=before_rider
            )
        conn.commit()
    finally:
        conn.close()


def _serialize_race_event(row):
    if not row:
        return None
    return {
        "id": row["id"],
        "season": row["season"],
        "round_number": row["round_number"],
        "flag": row["flag"],
        "country": row["country"],
        "country_en": row["country_en"],
        "start_date": _format_date(row["start_date"]),
        "end_date": _format_date(row["end_date"]),
        "circuit": row["circuit"],
        "version": row["version"],
        "updated_at": _format_datetime(row["updated_at"]),
    }


def _fetch_race_event(cur, event_id: int):
    cur.execute(
        """
        SELECT id, season, round_number, flag, country, country_en,
               start_date, end_date, circuit, version, updated_at
        FROM race_events WHERE id = %s
        """,
        (event_id,),
    )
    return _serialize_race_event(cur.fetchone())


def list_race_events(season: int | None = None):
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            sql = """
                SELECT id, season, round_number, flag, country, country_en,
                       start_date, end_date, circuit, version, updated_at
                FROM race_events
            """
            params = ()
            if season is not None:
                sql += " WHERE season = %s"
                params = (season,)
            sql += " ORDER BY season DESC, round_number ASC"
            cur.execute(sql, params)
            return [_serialize_race_event(row) for row in cur.fetchall()]
    finally:
        conn.close()


def create_race_event(
    season: int,
    round_number: int,
    flag: str,
    country: str,
    country_en: str,
    start_date: str,
    end_date: str,
    circuit: str,
    operator_id=None,
    operator_username="",
):
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO race_events
                    (season, round_number, flag, country, country_en, start_date, end_date, circuit)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (season, round_number, flag, country, country_en, start_date, end_date, circuit),
            )
            event_id = cur.lastrowid
            event = _fetch_race_event(cur, event_id)
            _insert_operation_log(
                cur, operator_id, operator_username, "create", "race",
                event_id, f"{season} 第{round_number}站 {country}", after_data=event
            )
        conn.commit()
        return event
    except pymysql.err.IntegrityError as exc:
        conn.rollback()
        raise ValueError("该赛季的分站序号已存在") from exc
    finally:
        conn.close()


def update_race_event(
    event_id: int,
    season: int,
    round_number: int,
    flag: str,
    country: str,
    country_en: str,
    start_date: str,
    end_date: str,
    circuit: str,
    expected_version: int,
    operator_id=None,
    operator_username="",
):
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            before_event = _fetch_race_event(cur, event_id)
            if not before_event:
                raise ValueError("赛程不存在")
            cur.execute(
                """
                UPDATE race_events
                SET season = %s, round_number = %s, flag = %s, country = %s,
                    country_en = %s, start_date = %s, end_date = %s, circuit = %s,
                    version = version + 1
                WHERE id = %s AND version = %s
                """,
                (
                    season, round_number, flag, country, country_en, start_date,
                    end_date, circuit, event_id, expected_version,
                ),
            )
            if cur.rowcount == 0:
                raise ConflictError("该赛程已被其他管理员修改，请刷新后重试")
            event = _fetch_race_event(cur, event_id)
            _insert_operation_log(
                cur, operator_id, operator_username, "update", "race",
                event_id, f"{season} 第{round_number}站 {country}",
                before_data=before_event, after_data=event
            )
        conn.commit()
        return event
    except pymysql.err.IntegrityError as exc:
        conn.rollback()
        raise ValueError("该赛季的分站序号已存在") from exc
    finally:
        conn.close()


def delete_race_event(event_id: int, operator_id=None, operator_username=""):
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            event = _fetch_race_event(cur, event_id)
            if not event:
                raise ValueError("赛程不存在")
            cur.execute("DELETE FROM race_events WHERE id = %s", (event_id,))
            _insert_operation_log(
                cur, operator_id, operator_username, "delete", "race",
                event_id,
                f"{event['season']} 第{event['round_number']}站 {event['country']}",
                before_data=event,
            )
        conn.commit()
    finally:
        conn.close()


def _fetch_race_schedule_items(cur, schedule_set_id: int):
    cur.execute(
        """
        SELECT id, schedule_date, start_time, end_time, category, session_name
        FROM race_schedule_items
        WHERE schedule_set_id = %s
        ORDER BY schedule_date ASC, start_time ASC, sort_order ASC, id ASC
        """,
        (schedule_set_id,),
    )
    return [
        {
            "id": row["id"],
            "schedule_date": _format_date(row["schedule_date"]),
            "start_time": row["start_time"],
            "end_time": row["end_time"] or "",
            "category": row["category"],
            "session_name": row["session_name"],
        }
        for row in cur.fetchall()
    ]


def _race_schedule_date_bounds(event: dict):
    """北京时间相对赛道当地日期最多跨一天，编辑和同步时允许前后各一天。"""
    return (
        (date.fromisoformat(event["start_date"]) - timedelta(days=1)).isoformat(),
        (date.fromisoformat(event["end_date"]) + timedelta(days=1)).isoformat(),
    )


def list_race_schedule(event_id: int):
    """返回指定分站按北京时间排列的比赛周末日程。"""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            event = _fetch_race_event(cur, event_id)
            if not event:
                raise ValueError("赛程不存在")
            cur.execute(
                "SELECT id, version FROM race_schedule_sets WHERE race_event_id = %s",
                (event_id,),
            )
            schedule_set = cur.fetchone()
            return {
                "event": event,
                "version": schedule_set["version"] if schedule_set else 0,
                "items": (
                    _fetch_race_schedule_items(cur, schedule_set["id"])
                    if schedule_set else []
                ),
            }
    finally:
        conn.close()


def replace_race_schedule(
    event_id: int,
    items: list[dict],
    expected_version: int,
    operator_id=None,
    operator_username="",
):
    """原子替换一个分站的全部日程，并以版本号防止管理员相互覆盖。"""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            event = _fetch_race_event(cur, event_id)
            if not event:
                raise ValueError("赛程不存在")
            earliest_date, latest_date = _race_schedule_date_bounds(event)
            invalid_date = next(
                (
                    row["schedule_date"] for row in items
                    if not earliest_date <= row["schedule_date"] <= latest_date
                ),
                None,
            )
            if invalid_date:
                raise ValueError(
                    f"日程日期 {invalid_date} 超出该分站北京时间范围"
                )

            cur.execute(
                """
                SELECT id, version FROM race_schedule_sets
                WHERE race_event_id = %s FOR UPDATE
                """,
                (event_id,),
            )
            schedule_set = cur.fetchone()
            before_items = (
                _fetch_race_schedule_items(cur, schedule_set["id"])
                if schedule_set else []
            )
            if schedule_set:
                if schedule_set["version"] != expected_version:
                    raise ConflictError("该分站日程已被其他管理员修改，请刷新后重试")
                cur.execute(
                    "UPDATE race_schedule_sets SET version = version + 1 WHERE id = %s",
                    (schedule_set["id"],),
                )
                schedule_set_id = schedule_set["id"]
                action = "update"
            else:
                if expected_version != 0:
                    raise ConflictError("该分站日程已发生变化，请刷新后重试")
                cur.execute(
                    "INSERT INTO race_schedule_sets (race_event_id) VALUES (%s)",
                    (event_id,),
                )
                schedule_set_id = cur.lastrowid
                action = "create"

            cur.execute(
                "DELETE FROM race_schedule_items WHERE schedule_set_id = %s",
                (schedule_set_id,),
            )
            if items:
                cur.executemany(
                    """
                    INSERT INTO race_schedule_items
                        (schedule_set_id, schedule_date, start_time, end_time,
                         category, session_name, sort_order)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """,
                    [
                        (
                            schedule_set_id,
                            row["schedule_date"],
                            row["start_time"],
                            row.get("end_time") or None,
                            row["category"],
                            row["session_name"],
                            index,
                        )
                        for index, row in enumerate(items)
                    ],
                )
            after_items = _fetch_race_schedule_items(cur, schedule_set_id)
            cur.execute(
                "SELECT version FROM race_schedule_sets WHERE id = %s",
                (schedule_set_id,),
            )
            version = cur.fetchone()["version"]

            def snapshot(entries):
                return {
                    "entries": "\n".join(
                        f"{row['schedule_date']} {row['start_time']}"
                        f"{('-' + row['end_time']) if row['end_time'] else ''} "
                        f"{row['category']} · {row['session_name']}"
                        for row in entries
                    ) or "暂无日程",
                }

            _insert_operation_log(
                cur,
                operator_id,
                operator_username,
                action,
                "race_schedule",
                event_id,
                f"{event['season']} 第{event['round_number']}站 {event['country']} · 比赛日程",
                before_data=snapshot(before_items) if action == "update" else None,
                after_data=snapshot(after_items),
            )
        conn.commit()
        return {"version": version, "items": after_items}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def sync_season_race_schedules(
    season: int,
    official_events: list[dict],
    operator_id=None,
    operator_username="",
):
    """在单个事务中用官网数据替换一个赛季的全部分站日程。"""
    conn = get_connection()
    summary = {"season": season, "events": 0, "items": 0, "details": []}
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, season, round_number, flag, country, country_en,
                       start_date, end_date, circuit, version, updated_at
                FROM race_events WHERE season = %s
                ORDER BY round_number FOR UPDATE
                """,
                (season,),
            )
            local_events = {
                row["round_number"]: _serialize_race_event(row)
                for row in cur.fetchall()
            }
            if not local_events:
                raise ValueError(f"本地数据库中没有 {season} 赛季")

            official_rounds = {int(event["round_number"]) for event in official_events}
            missing_rounds = sorted(set(local_events) - official_rounds)
            unknown_rounds = sorted(official_rounds - set(local_events))
            if missing_rounds or unknown_rounds:
                messages = []
                if missing_rounds:
                    messages.append(f"官网缺少本地分站 {missing_rounds}")
                if unknown_rounds:
                    messages.append(f"官网包含未知分站 {unknown_rounds}")
                raise ValueError("；".join(messages))

            for official_event in sorted(
                official_events, key=lambda event: int(event["round_number"])
            ):
                round_number = int(official_event["round_number"])
                event = local_events[round_number]
                items = official_event.get("items") or []
                if not items:
                    raise ValueError(f"官网第 {round_number} 站没有可导入的赛道日程")
                earliest_date, latest_date = _race_schedule_date_bounds(event)
                invalid_date = next(
                    (
                        row["schedule_date"] for row in items
                        if not earliest_date <= row["schedule_date"] <= latest_date
                    ),
                    None,
                )
                if invalid_date:
                    raise ValueError(
                        f"官网第 {round_number} 站日程日期 {invalid_date} 与本地比赛周末不一致"
                    )

                cur.execute(
                    "SELECT id FROM race_schedule_sets WHERE race_event_id = %s FOR UPDATE",
                    (event["id"],),
                )
                schedule_set = cur.fetchone()
                before_items = (
                    _fetch_race_schedule_items(cur, schedule_set["id"])
                    if schedule_set else []
                )
                if schedule_set:
                    schedule_set_id = schedule_set["id"]
                    cur.execute(
                        "UPDATE race_schedule_sets SET version = version + 1 WHERE id = %s",
                        (schedule_set_id,),
                    )
                    action = "update"
                else:
                    cur.execute(
                        "INSERT INTO race_schedule_sets (race_event_id) VALUES (%s)",
                        (event["id"],),
                    )
                    schedule_set_id = cur.lastrowid
                    action = "create"

                cur.execute(
                    "DELETE FROM race_schedule_items WHERE schedule_set_id = %s",
                    (schedule_set_id,),
                )
                cur.executemany(
                    """
                    INSERT INTO race_schedule_items
                        (schedule_set_id, schedule_date, start_time, end_time,
                         category, session_name, sort_order)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """,
                    [
                        (
                            schedule_set_id,
                            row["schedule_date"],
                            row["start_time"],
                            row.get("end_time") or None,
                            row["category"],
                            row["session_name"],
                            index,
                        )
                        for index, row in enumerate(items)
                    ],
                )
                after_items = _fetch_race_schedule_items(cur, schedule_set_id)

                def snapshot(entries):
                    return {
                        "entries": "\n".join(
                            f"{row['schedule_date']} {row['start_time']}"
                            f"{('-' + row['end_time']) if row['end_time'] else ''} "
                            f"{row['category']} · {row['session_name']}"
                            for row in entries
                        ) or "暂无日程",
                    }

                _insert_operation_log(
                    cur,
                    operator_id,
                    operator_username,
                    action,
                    "race_schedule",
                    event["id"],
                    f"{season} 第{round_number}站 {event['country']} · 比赛日程",
                    before_data=snapshot(before_items) if action == "update" else None,
                    after_data=snapshot(after_items),
                )
                summary["events"] += 1
                summary["items"] += len(after_items)
                summary["details"].append({
                    "round_number": round_number,
                    "country": event["country"],
                    "items": len(after_items),
                    "time_zone": official_event.get("time_zone") or "",
                })
        conn.commit()
        return summary
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _fetch_race_result_entries(cur, result_set_id: int):
    cur.execute(
        """
        SELECT rr.rider_id, rr.position, rr.points, rr.finish_time,
               rr.result_status, rr.remaining_laps, r.rider_number,
               r.english_name, r.chinese_name, t.name AS team_name
        FROM race_results rr
        JOIN riders r ON r.id = rr.rider_id
        LEFT JOIN teams t ON t.id = r.team_id
        WHERE rr.result_set_id = %s
        ORDER BY rr.position IS NULL ASC, rr.position ASC,
                 rr.remaining_laps ASC, rr.id ASC
        """,
        (result_set_id,),
    )
    return [
        {
            "rider_id": row["rider_id"],
            "position": row["position"],
            "points": row["points"],
            "finish_time": row["finish_time"] or "",
            "result_status": row["result_status"],
            "remaining_laps": row["remaining_laps"],
            "rider_number": row["rider_number"],
            "english_name": row["english_name"],
            "chinese_name": row["chinese_name"],
            "team_name": row["team_name"],
        }
        for row in cur.fetchall()
    ]


def list_race_results(event_id: int):
    """返回指定分站的冲刺赛及正赛排名。"""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            event = _fetch_race_event(cur, event_id)
            if not event:
                raise ValueError("赛程不存在")
            data = {
                "event": event,
                "versions": {"sprint": 0, "race": 0},
                "sprint": [],
                "race": [],
            }
            cur.execute(
                "SELECT id, race_type, version FROM race_result_sets WHERE race_event_id = %s",
                (event_id,),
            )
            for result_set in cur.fetchall():
                race_type = result_set["race_type"]
                data["versions"][race_type] = result_set["version"]
                data[race_type] = _fetch_race_result_entries(cur, result_set["id"])
            data["availability"] = _race_result_availability(cur, event, data)
            return data
    finally:
        conn.close()


def _race_result_availability(cur, event: dict, result_data: dict | None = None):
    """按北京时间判断冲刺赛和正赛是否已结束，可否录入或同步排名。"""
    now = datetime.now(BEIJING_TIMEZONE)
    event_finished = date.fromisoformat(event["end_date"]) < now.date()
    states = {
        "sprint": {
            "available": event_finished,
            "started": event_finished,
            "scheduled_start_at": None,
            "completed_at": None,
        },
        "race": {
            "available": event_finished,
            "started": event_finished,
            "scheduled_start_at": None,
            "completed_at": None,
        },
    }
    cur.execute(
        """
        SELECT rsi.schedule_date, rsi.start_time, rsi.end_time,
               rsi.category, rsi.session_name
        FROM race_schedule_items rsi
        JOIN race_schedule_sets rss ON rss.id = rsi.schedule_set_id
        WHERE rss.race_event_id = %s
        ORDER BY rsi.schedule_date, rsi.start_time
        """,
        (event["id"],),
    )
    for item in cur.fetchall():
        category = str(item.get("category") or "").casefold()
        name = str(item.get("session_name") or "").casefold()
        if "motogp" not in category.replace(" ", ""):
            continue
        if "sprint" in name or "冲刺" in name:
            race_type = "sprint"
        elif (
            re.search(r"(^|\W)race(\W|$)", name)
            or "grand prix" in name
            or "正赛" in name
        ):
            race_type = "race"
        else:
            continue
        start_at = datetime.fromisoformat(
            f"{_format_date(item['schedule_date'])}T{item['start_time']}"
        ).replace(tzinfo=BEIJING_TIMEZONE)
        has_end_time = bool(item.get("end_time"))
        end_time = str(item.get("end_time") or item.get("start_time") or "")
        completed_at = datetime.fromisoformat(
            f"{_format_date(item['schedule_date'])}T{end_time}"
        ).replace(tzinfo=BEIJING_TIMEZONE)
        if not has_end_time:
            # 官网部分正赛类日程只提供发车时间，保留合理缓冲，避免比赛刚开始就开放排名。
            completed_at += timedelta(minutes=30 if race_type == "sprint" else 60)
        states[race_type] = {
            "available": now >= completed_at,
            "started": now >= start_at,
            "scheduled_start_at": start_at.isoformat(timespec="minutes"),
            "completed_at": completed_at.isoformat(timespec="minutes"),
        }

    # 已有排名始终允许管理员继续维护，避免后来修改日程导致历史数据被锁住。
    for race_type in ("sprint", "race"):
        if result_data and result_data.get(race_type):
            states[race_type]["available"] = True
            states[race_type]["started"] = True
        if states[race_type]["available"]:
            states[race_type]["reason"] = "该场次已结束，可以录入或同步排名"
        elif states[race_type]["started"]:
            states[race_type]["reason"] = (
                "比赛已经发车，可从官网检查正式赛果；手动录入将在兜底时间后开放"
            )
        else:
            states[race_type]["reason"] = "该场次尚未开始"
    return states


def replace_race_results(
    event_id: int,
    race_type: str,
    results: list[dict],
    expected_version: int,
    operator_id=None,
    operator_username="",
):
    """原子替换一场冲刺赛或正赛排名，并以批次版本避免并发覆盖。"""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            event = _fetch_race_event(cur, event_id)
            if not event:
                raise ValueError("赛程不存在")
            cur.execute(
                """
                SELECT id, version FROM race_result_sets
                WHERE race_event_id = %s AND race_type = %s FOR UPDATE
                """,
                (event_id, race_type),
            )
            result_set = cur.fetchone()
            before_entries = _fetch_race_result_entries(cur, result_set["id"]) if result_set else []
            if result_set:
                if result_set["version"] != expected_version:
                    raise ConflictError("该场排名已被其他管理员修改，请刷新后重试")
                cur.execute(
                    "UPDATE race_result_sets SET version = version + 1 WHERE id = %s",
                    (result_set["id"],),
                )
                result_set_id = result_set["id"]
                action = "update"
            else:
                if expected_version != 0:
                    raise ConflictError("该场排名已发生变化，请刷新后重试")
                cur.execute(
                    "INSERT INTO race_result_sets (race_event_id, race_type) VALUES (%s, %s)",
                    (event_id, race_type),
                )
                result_set_id = cur.lastrowid
                action = "create"

            if results:
                rider_ids = [row["rider_id"] for row in results]
                placeholders = ",".join(["%s"] * len(rider_ids))
                cur.execute(f"SELECT id FROM riders WHERE id IN ({placeholders})", rider_ids)
                if len(cur.fetchall()) != len(rider_ids):
                    raise ValueError("排名中包含不存在的车手")

            cur.execute("DELETE FROM race_results WHERE result_set_id = %s", (result_set_id,))
            if results:
                cur.executemany(
                    """
                    INSERT INTO race_results
                        (result_set_id, rider_id, position, points, finish_time,
                         result_status, remaining_laps)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """,
                    [(
                        result_set_id, row["rider_id"], row["position"], row["points"],
                        row.get("finish_time") or None,
                        row.get("result_status") or "FINISHED",
                        row.get("remaining_laps"),
                    ) for row in results],
                )
            after_entries = _fetch_race_result_entries(cur, result_set_id)
            cur.execute("SELECT version FROM race_result_sets WHERE id = %s", (result_set_id,))
            version = cur.fetchone()["version"]

            type_label = "冲刺赛" if race_type == "sprint" else "正赛"
            def snapshot(entries):
                return {
                    "race_type": type_label,
                    "entries": "\n".join(
                        f"{row['position'] if row['position'] is not None else 'DNF'}. "
                        f"{row['rider_number']} {row['chinese_name']}（{row['points']} 分，"
                        f"{row['finish_time'] if row['result_status'] == 'FINISHED' else ('剩余 {} 圈'.format(row['remaining_laps']) if row['remaining_laps'] is not None else 'DNF')}）"
                        for row in entries
                    ) or "暂无排名",
                }
            _insert_operation_log(
                cur, operator_id, operator_username, action, "race_result", event_id,
                f"{event['season']} 第{event['round_number']}站 {event['country']} · {type_label}",
                before_data=snapshot(before_entries) if action == "update" else None,
                after_data=snapshot(after_entries),
            )
        conn.commit()
        return {"version": version, "results": after_entries}
    except pymysql.err.IntegrityError as exc:
        conn.rollback()
        raise ValueError("排名或车手存在重复") from exc
    finally:
        conn.close()


def sync_race_results(
    event_id: int,
    official_results: dict,
    expected_versions: dict,
    race_types=("sprint", "race"),
    operator_id=None,
    operator_username="",
):
    """按车号匹配本地车手并原子同步一站的冲刺赛与正赛结果。"""
    conn = get_connection()
    summary = {"types": {}, "skipped_total": 0}
    try:
        with conn.cursor() as cur:
            event = _fetch_race_event(cur, event_id)
            if not event:
                raise ValueError("赛程不存在")

            for race_type in race_types:
                official_rows = official_results.get(race_type) or []
                if not official_rows:
                    raise ValueError("官方赛果不完整，已取消本次同步")
                cur.execute(
                    """
                    SELECT id, version FROM race_result_sets
                    WHERE race_event_id = %s AND race_type = %s FOR UPDATE
                    """,
                    (event_id, race_type),
                )
                result_set = cur.fetchone()
                current_version = result_set["version"] if result_set else 0
                if current_version != int(expected_versions.get(race_type, 0)):
                    raise ConflictError("同步期间排名已被其他管理员修改，请重新刷新")

                before_entries = _fetch_race_result_entries(cur, result_set["id"]) if result_set else []
                mapped = []
                skipped = []
                for row in official_rows:
                    cur.execute(
                        "SELECT id FROM riders WHERE rider_number = %s",
                        (row["rider_number"],),
                    )
                    rider = cur.fetchone()
                    if not rider:
                        skipped.append({
                            "position": row["position"],
                            "rider_number": row["rider_number"],
                            "official_name": row.get("official_name", ""),
                        })
                        continue
                    mapped.append({
                        "rider_id": rider["id"],
                        "position": row["position"],
                        "points": row["points"],
                        "finish_time": row.get("finish_time") or "",
                        "result_status": row.get("result_status") or "FINISHED",
                        "remaining_laps": row.get("remaining_laps"),
                    })
                if not mapped:
                    raise ValueError("官方排名没有匹配到任何本地车手，已取消同步")

                if result_set:
                    result_set_id = result_set["id"]
                    cur.execute(
                        "UPDATE race_result_sets SET version = version + 1 WHERE id = %s",
                        (result_set_id,),
                    )
                    action = "update"
                else:
                    cur.execute(
                        "INSERT INTO race_result_sets (race_event_id, race_type) VALUES (%s, %s)",
                        (event_id, race_type),
                    )
                    result_set_id = cur.lastrowid
                    action = "create"

                cur.execute("DELETE FROM race_results WHERE result_set_id = %s", (result_set_id,))
                cur.executemany(
                    """
                    INSERT INTO race_results
                        (result_set_id, rider_id, position, points, finish_time,
                         result_status, remaining_laps)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """,
                    [
                        (
                            result_set_id, row["rider_id"], row["position"], row["points"],
                            row.get("finish_time") or None, row["result_status"],
                            row.get("remaining_laps"),
                        )
                        for row in mapped
                    ],
                )
                after_entries = _fetch_race_result_entries(cur, result_set_id)
                cur.execute("SELECT version FROM race_result_sets WHERE id = %s", (result_set_id,))
                version = cur.fetchone()["version"]
                type_label = "冲刺赛" if race_type == "sprint" else "正赛"

                def snapshot(entries):
                    return {
                        "race_type": type_label,
                        "entries": "\n".join(
                            f"{item['position'] if item['position'] is not None else 'DNF'}. "
                            f"{item['rider_number']} {item['chinese_name']}（{item['points']} 分，"
                            f"{item['finish_time'] if item['result_status'] == 'FINISHED' else ('剩余 {} 圈'.format(item['remaining_laps']) if item['remaining_laps'] is not None else 'DNF')}）"
                            for item in entries
                        ) or "暂无排名",
                    }

                _insert_operation_log(
                    cur, operator_id, operator_username, action, "race_result", event_id,
                    f"{event['season']} 第{event['round_number']}站 {event['country']} · {type_label}",
                    before_data=snapshot(before_entries) if action == "update" else None,
                    after_data=snapshot(after_entries),
                )
                summary["types"][race_type] = {
                    "official_total": len(official_rows),
                    "matched": len(mapped),
                    "skipped": skipped,
                    "version": version,
                    "results": after_entries,
                }
                summary["skipped_total"] += len(skipped)
        conn.commit()
        return summary
    except pymysql.err.IntegrityError as exc:
        conn.rollback()
        raise ValueError("官方排名中存在重复名次或车手") from exc
    finally:
        conn.close()


def _serialize_rider_standing(row):
    if not row:
        return None
    return {
        "id": row["id"],
        "season": row["season"],
        "position": row["position"],
        "points": row["points"],
        "race_wins": row["race_wins"],
        "podiums": row["podiums"],
        "rider_id": row["rider_id"],
        "rider_number": row["rider_number"],
        "english_name": row["english_name"],
        "chinese_name": row["chinese_name"],
        "nationality": row["nationality"],
        "team_name": row.get("team_name") or "",
        "manufacturer": row.get("manufacturer") or "",
        "version": row["version"],
        "updated_at": _format_datetime(row["updated_at"]),
    }


def _standing_select_sql():
    return """
        SELECT s.id, s.season, s.position, s.points, s.race_wins, s.podiums,
               s.rider_id, s.version, s.updated_at,
               r.rider_number, r.english_name, r.chinese_name, r.nationality,
               t.name AS team_name, t.manufacturer
        FROM rider_standings s
        INNER JOIN riders r ON r.id = s.rider_id
        LEFT JOIN teams t ON t.id = r.team_id
    """


def _fetch_rider_standing(cur, standing_id: int):
    cur.execute(_standing_select_sql() + " WHERE s.id = %s", (standing_id,))
    return _serialize_rider_standing(cur.fetchone())


def list_rider_standings(season: int | None = None):
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            sql = _standing_select_sql()
            params = ()
            if season is not None:
                sql += " WHERE s.season = %s"
                params = (season,)
            sql += " ORDER BY s.season DESC, s.position ASC, s.points DESC, s.id ASC"
            cur.execute(sql, params)
            return [_serialize_rider_standing(row) for row in cur.fetchall()]
    finally:
        conn.close()


def _validate_standing_rider(cur, rider_id: int):
    cur.execute("SELECT id FROM riders WHERE id = %s", (rider_id,))
    if not cur.fetchone():
        raise ValueError("所选车手不存在")


def create_rider_standing(
    season: int,
    rider_id: int,
    position: int,
    points: int,
    race_wins: int,
    podiums: int,
    operator_id=None,
    operator_username="",
):
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            _validate_standing_rider(cur, rider_id)
            cur.execute(
                """
                INSERT INTO rider_standings
                    (season, rider_id, position, points, race_wins, podiums)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (season, rider_id, position, points, race_wins, podiums),
            )
            standing_id = cur.lastrowid
            standing = _fetch_rider_standing(cur, standing_id)
            _insert_operation_log(
                cur, operator_id, operator_username, "create", "standing",
                standing_id,
                f"{season} 第{position}名 {standing['english_name']}",
                after_data=standing,
            )
        conn.commit()
        return standing
    except pymysql.err.IntegrityError as exc:
        conn.rollback()
        raise ValueError("该车手在此赛季已有积分记录") from exc
    finally:
        conn.close()


def update_rider_standing(
    standing_id: int,
    season: int,
    rider_id: int,
    position: int,
    points: int,
    race_wins: int,
    podiums: int,
    expected_version: int,
    operator_id=None,
    operator_username="",
):
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            _validate_standing_rider(cur, rider_id)
            before_standing = _fetch_rider_standing(cur, standing_id)
            if not before_standing:
                raise ValueError("积分记录不存在")
            cur.execute(
                """
                UPDATE rider_standings
                SET season = %s, rider_id = %s, position = %s, points = %s,
                    race_wins = %s, podiums = %s, version = version + 1
                WHERE id = %s AND version = %s
                """,
                (
                    season, rider_id, position, points, race_wins, podiums,
                    standing_id, expected_version,
                ),
            )
            if cur.rowcount == 0:
                raise ConflictError("该积分记录已被其他管理员修改，请刷新后重试")
            standing = _fetch_rider_standing(cur, standing_id)
            _insert_operation_log(
                cur, operator_id, operator_username, "update", "standing",
                standing_id,
                f"{season} 第{position}名 {standing['english_name']}",
                before_data=before_standing,
                after_data=standing,
            )
        conn.commit()
        return standing
    except pymysql.err.IntegrityError as exc:
        conn.rollback()
        raise ValueError("该车手在此赛季已有积分记录") from exc
    finally:
        conn.close()


def delete_rider_standing(standing_id: int, operator_id=None, operator_username=""):
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            standing = _fetch_rider_standing(cur, standing_id)
            if not standing:
                raise ValueError("积分记录不存在")
            cur.execute("DELETE FROM rider_standings WHERE id = %s", (standing_id,))
            _insert_operation_log(
                cur, operator_id, operator_username, "delete", "standing",
                standing_id,
                f"{standing['season']} 第{standing['position']}名 {standing['english_name']}",
                before_data=standing,
            )
        conn.commit()
    finally:
        conn.close()


def reserve_external_sync(
    sync_key: str,
    cooldown_seconds: int,
    failure_cooldown_seconds: int | None = None,
):
    """以数据库锁预留一次同步，避免多进程或多管理员重复请求官方接口。"""
    cooldown_seconds = max(0, int(cooldown_seconds))
    failure_cooldown_seconds = (
        cooldown_seconds
        if failure_cooldown_seconds is None
        else max(0, int(failure_cooldown_seconds))
    )
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT IGNORE INTO external_sync_state (sync_key)
                VALUES (%s)
                """,
                (sync_key,),
            )
            cur.execute(
                """
                SELECT last_attempt_at, last_success_at, last_status,
                       TIMESTAMPDIFF(SECOND, last_attempt_at, NOW()) AS attempt_elapsed,
                       TIMESTAMPDIFF(SECOND, last_success_at, NOW()) AS success_elapsed
                FROM external_sync_state
                WHERE sync_key = %s
                FOR UPDATE
                """,
                (sync_key,),
            )
            state = cur.fetchone()
            last_succeeded = state["last_status"] == "success"
            active_cooldown = cooldown_seconds if last_succeeded else failure_cooldown_seconds
            elapsed = state["success_elapsed"] if last_succeeded else state["attempt_elapsed"]
            if active_cooldown and elapsed is not None and elapsed < active_cooldown:
                raise SyncCooldownError(active_cooldown - elapsed)
            cur.execute(
                """
                UPDATE external_sync_state
                SET last_attempt_at = NOW(), last_status = 'running', last_message = ''
                WHERE sync_key = %s
                """,
                (sync_key,),
            )
        conn.commit()
    finally:
        conn.close()


def finish_external_sync(sync_key: str, success: bool, message: str):
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE external_sync_state
                SET last_success_at = CASE WHEN %s THEN NOW() ELSE last_success_at END,
                    last_status = %s,
                    last_message = %s
                WHERE sync_key = %s
                """,
                (
                    1 if success else 0,
                    "success" if success else "failed",
                    str(message)[:255],
                    sync_key,
                ),
            )
        conn.commit()
    finally:
        conn.close()


def sync_rider_standings(
    season: int,
    official_rows: list[dict],
    operator_id=None,
    operator_username="",
):
    """按车号更新官方积分，并将官网未列出的本地车手以 0 分追加到榜尾。"""
    conn = get_connection()
    summary = {
        "season": season,
        "official_total": len(official_rows),
        "matched": 0,
        "created": 0,
        "updated": 0,
        "unchanged": 0,
        "skipped": [],
        "zero_point_added": [],
    }
    try:
        with conn.cursor() as cur:
            for row in official_rows:
                cur.execute(
                    """
                    SELECT id, english_name FROM riders
                    WHERE rider_number = %s
                    """,
                    (row["rider_number"],),
                )
                rider = cur.fetchone()
                if not rider:
                    summary["skipped"].append(
                        {
                            "position": row["position"],
                            "rider_number": row["rider_number"],
                            "official_name": row["official_name"],
                        }
                    )
                    continue

                summary["matched"] += 1
                cur.execute(
                    """
                    SELECT id FROM rider_standings
                    WHERE season = %s AND rider_id = %s
                    """,
                    (season, rider["id"]),
                )
                existing = cur.fetchone()
                if not existing:
                    cur.execute(
                        """
                        INSERT INTO rider_standings
                            (season, rider_id, position, points, race_wins, podiums)
                        VALUES (%s, %s, %s, %s, %s, %s)
                        """,
                        (
                            season,
                            rider["id"],
                            row["position"],
                            row["points"],
                            row["race_wins"],
                            row["podiums"],
                        ),
                    )
                    standing_id = cur.lastrowid
                    standing = _fetch_rider_standing(cur, standing_id)
                    _insert_operation_log(
                        cur, operator_id, operator_username, "create", "standing",
                        standing_id,
                        f"{season} 第{row['position']}名 {standing['english_name']}",
                        after_data=standing,
                    )
                    summary["created"] += 1
                    continue

                before = _fetch_rider_standing(cur, existing["id"])
                changes = (
                    before["position"] != row["position"]
                    or before["points"] != row["points"]
                    or before["race_wins"] != row["race_wins"]
                    or before["podiums"] != row["podiums"]
                )
                if not changes:
                    summary["unchanged"] += 1
                    continue

                cur.execute(
                    """
                    UPDATE rider_standings
                    SET position = %s, points = %s, race_wins = %s, podiums = %s,
                        version = version + 1
                    WHERE id = %s
                    """,
                    (
                        row["position"],
                        row["points"],
                        row["race_wins"],
                        row["podiums"],
                        existing["id"],
                    ),
                )
                standing = _fetch_rider_standing(cur, existing["id"])
                _insert_operation_log(
                    cur, operator_id, operator_username, "update", "standing",
                    standing["id"],
                    f"{season} 第{row['position']}名 {standing['english_name']}",
                    before_data=before,
                    after_data=standing,
                )
                summary["updated"] += 1

            cur.execute(
                "SELECT COALESCE(MAX(position), 0) AS max_position "
                "FROM rider_standings WHERE season = %s",
                (season,),
            )
            next_position = cur.fetchone()["max_position"] + 1
            cur.execute(
                """
                SELECT r.id, r.rider_number, r.english_name
                FROM riders r
                LEFT JOIN rider_standings s
                    ON s.rider_id = r.id AND s.season = %s
                WHERE s.id IS NULL
                ORDER BY CAST(r.rider_number AS UNSIGNED), r.rider_number
                """,
                (season,),
            )
            for rider in cur.fetchall():
                cur.execute(
                    """
                    INSERT INTO rider_standings
                        (season, rider_id, position, points, race_wins, podiums)
                    VALUES (%s, %s, %s, 0, 0, 0)
                    """,
                    (season, rider["id"], next_position),
                )
                standing = _fetch_rider_standing(cur, cur.lastrowid)
                _insert_operation_log(
                    cur, operator_id, operator_username, "create", "standing",
                    standing["id"],
                    f"{season} 第{next_position}名 {standing['english_name']}",
                    after_data=standing,
                )
                summary["zero_point_added"].append({
                    "rider_number": rider["rider_number"],
                    "english_name": rider["english_name"],
                    "position": next_position,
                })
                summary["created"] += 1
                next_position += 1
        conn.commit()
        return summary
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
