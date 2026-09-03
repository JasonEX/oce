"""编程式 Alembic 入口（个人模式 ``oce serve`` 初始化 schema 用）。

服务模式仍由 compose/手动执行 ``uv run alembic upgrade head``。项目尚未发布数据库
兼容承诺，因此这里只支持空库或已有 Alembic 版本表的开发库，不猜测或盖章旧 schema。
"""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config

# 迁移脚本随 wheel 一起发布（src/oce/alembic），运行时用包内路径而非仓库 CWD，
# 保证 `uv tool install` 安装的环境也能离线迁移。
_SCRIPT_DIR = Path(__file__).resolve().parents[2] / "alembic"


def run_migrations() -> None:
    """按当前 ``DB_URL`` 初始化或更新到开发 schema head。"""
    cfg = Config()
    cfg.set_main_option("script_location", str(_SCRIPT_DIR))

    command.upgrade(cfg, "head")
