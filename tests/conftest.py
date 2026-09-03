import tempfile
from pathlib import Path



# Windows: pytest's default system temp dir may be sandbox-restricted.
# Force the basetemp into the project root so tmp_path fixtures work everywhere.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_LOCAL_TMP = _PROJECT_ROOT / ".tmp_pytest"
_LOCAL_TMP.mkdir(exist_ok=True)
tempfile.tempdir = str(_LOCAL_TMP)

# 事件循环交由 pytest-asyncio 自管：pytest-asyncio 1.x 已废弃自定义
# event_loop fixture 扩展点，保留会与插件内建 loop 管理产生生命周期错乱。