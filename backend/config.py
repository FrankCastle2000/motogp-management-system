import os
from pathlib import Path

from dotenv import load_dotenv


# 本地敏感配置统一保存在项目根目录的 .env 中；该文件不会提交到 Git。
PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")


# MySQL 连接配置。公开仓库中只保留非敏感默认值。
MYSQL_CONFIG = {
    "host": os.getenv("MYSQL_HOST", "127.0.0.1"),
    "port": int(os.getenv("MYSQL_PORT", "3306")),
    "user": os.getenv("MYSQL_USER", "root"),
    "password": os.getenv("MYSQL_PASSWORD", ""),
    "database": os.getenv("MYSQL_DATABASE", "motogp_db"),
    "charset": "utf8mb4",
}

# Flask Session 密钥。部署时必须通过环境变量使用随机强密钥。
SECRET_KEY = os.getenv("SECRET_KEY", "dev-only-change-this-secret-key")

# 内置超级管理员（仅在首次初始化数据库时创建）。
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "change-me-before-first-run")

# Web 服务配置。默认关闭调试模式，避免部署时暴露调试器。
APP_HOST = os.getenv("FLASK_HOST", "127.0.0.1")
APP_PORT = int(os.getenv("FLASK_PORT", "5000"))
APP_DEBUG = os.getenv("FLASK_DEBUG", "false").strip().lower() in {"1", "true", "yes", "on"}

# 操作日志保留策略：超过任一限制的旧日志会自动清理。
OPERATION_LOG_RETENTION_DAYS = int(os.getenv("OPERATION_LOG_RETENTION_DAYS", "365"))
OPERATION_LOG_MAX_ROWS = int(os.getenv("OPERATION_LOG_MAX_ROWS", "10000"))

# MotoGP 官方数据同步配置。同步接口由管理员手动触发，并受冷却时间限制。
MOTOGP_API_BASE_URL = os.getenv(
    "MOTOGP_API_BASE_URL",
    "https://api.pulselive.motogp.com/motogp",
).rstrip("/")
MOTOGP_SYNC_COOLDOWN_SECONDS = int(os.getenv("MOTOGP_SYNC_COOLDOWN_SECONDS", "3600"))
MOTOGP_RACE_RESULTS_SYNC_COOLDOWN_SECONDS = int(
    os.getenv("MOTOGP_RACE_RESULTS_SYNC_COOLDOWN_SECONDS", "3600")
)
MOTOGP_SYNC_TIMEOUT_SECONDS = int(os.getenv("MOTOGP_SYNC_TIMEOUT_SECONDS", "12"))
