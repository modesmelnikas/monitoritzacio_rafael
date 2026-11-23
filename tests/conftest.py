import os
import sys
from pathlib import Path

os.environ.setdefault("DATABASE_URL", "sqlite:///" + str(Path(__file__).parent / "test.db"))
os.environ["SIMULATE"] = "true"
os.environ["RUN_SCHEDULER"] = "false"
os.environ["DASHBOARD_PASSWORD"] = ""
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
p = Path(__file__).parent / "test.db"
if p.exists():
    p.unlink()
