# backend/alembic_tee/__init__.py
"""TEE 明文库的独立 Alembic 链（spec §4）。owner 凭证独立跑，app 角色不做 DDL。"""
from pathlib import Path


def upgrade_head() -> None:
    from alembic import command
    from alembic.config import Config
    here = Path(__file__).resolve().parent
    cfg = Config(str(here / "alembic.ini"))
    cfg.set_main_option("script_location", str(here))
    command.upgrade(cfg, "head")
