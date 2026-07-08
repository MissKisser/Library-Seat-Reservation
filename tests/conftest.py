import asyncio
import tempfile
from pathlib import Path

import pytest


# Windows: pytest's default system temp dir may be sandbox-restricted.
# Force the basetemp into the project root so tmp_path fixtures work everywhere.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_LOCAL_TMP = _PROJECT_ROOT / ".tmp_pytest"
_LOCAL_TMP.mkdir(exist_ok=True)
tempfile.tempdir = str(_LOCAL_TMP)


@pytest.fixture
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()