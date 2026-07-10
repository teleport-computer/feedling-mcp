# backend/alembic_tee/__main__.py
import sys
from . import upgrade_head

if __name__ == "__main__":
    assert sys.argv[1:] == ["upgrade"], "usage: python -m backend.alembic_tee upgrade"
    upgrade_head()
    print("[alembic_tee] schema at head")
