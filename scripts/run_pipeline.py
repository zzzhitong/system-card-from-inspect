from pathlib import Path
import sys


SCRIPT_DIR = Path(__file__).resolve().parent
PACKAGE_ROOT = SCRIPT_DIR / "system_card_from_inspect"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from system_card_from_inspect.cli import main


if __name__ == "__main__":
    raise SystemExit(main())
