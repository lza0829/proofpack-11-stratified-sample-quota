import os
import tempfile

# 必须在导入 app 之前指向独立数据目录（app.config 在导入时定型）
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="proofpack-test-")

import pytest


@pytest.fixture(autouse=True)
def fresh_db():
    from app.db import DB_PATH
    from app.seed import init_db

    if DB_PATH.exists():
        DB_PATH.unlink()
    init_db()
    yield
