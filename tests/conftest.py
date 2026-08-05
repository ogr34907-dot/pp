"""保证从仓库内任意 cwd 运行 pytest 时能找到项目包（pythonpath 含仓库根）。"""
import os
import shutil
import sys
import tempfile
from pathlib import Path

# Add paths immediately at module import time
_root = Path(__file__).resolve().parent.parent
_parent = _root.parent
_TEST_DATA_DIR_ATTR = "_plotpilot_test_data_dir"
_PREVIOUS_PROD_DATA_DIR_ATTR = "_plotpilot_previous_prod_data_dir"
_HAD_PROD_DATA_DIR_ATTR = "_plotpilot_had_prod_data_dir"

# Add project root FIRST (for infrastructure, domain, etc.) - this is most important
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

# Add parent for project root - but AFTER project root
if str(_parent) not in sys.path:
    sys.path.append(str(_parent))  # Use append instead of insert to keep it lower priority


def pytest_configure(config):
    """Configure pytest - ensure paths are set before test collection"""
    os.environ.setdefault("PLOTPILOT_ALLOW_DIRECT_SQLITE_WRITES", "1")

    # API modules construct the application during collection. Give every
    # pytest process its own runtime directory before those imports can open
    # SQLite, rather than sharing the repository's data/ database.
    previous_data_dir = os.environ.get("PLOTPILOT_PROD_DATA_DIR")
    setattr(config, _HAD_PROD_DATA_DIR_ATTR, previous_data_dir is not None)
    setattr(config, _PREVIOUS_PROD_DATA_DIR_ATTR, previous_data_dir)
    test_data_dir = Path(tempfile.mkdtemp(prefix="plotpilot-pytest-"))
    setattr(config, _TEST_DATA_DIR_ATTR, test_data_dir)
    os.environ["PLOTPILOT_PROD_DATA_DIR"] = str(test_data_dir)

    # Ensure project root is first in sys.path
    root_str = str(_root)
    if root_str in sys.path:
        sys.path.remove(root_str)
    sys.path.insert(0, root_str)


def pytest_unconfigure(config):
    """Close only the per-session runtime database before removing it."""
    test_data_dir = getattr(config, _TEST_DATA_DIR_ATTR, None)
    if test_data_dir is not None:
        try:
            from infrastructure.persistence.database import connection as connection_module

            for database in list(connection_module._db_instances_by_path.values()):
                database_path = Path(database.db_path).resolve()
                if database_path.is_relative_to(test_data_dir):
                    database.close_all(skip_checkpoint=True)
        except Exception:
            # Test-process shutdown must not hide the original test result.
            pass
        shutil.rmtree(test_data_dir, ignore_errors=True)

    if getattr(config, _HAD_PROD_DATA_DIR_ATTR, False):
        os.environ["PLOTPILOT_PROD_DATA_DIR"] = getattr(
            config, _PREVIOUS_PROD_DATA_DIR_ATTR
        )
    else:
        os.environ.pop("PLOTPILOT_PROD_DATA_DIR", None)
