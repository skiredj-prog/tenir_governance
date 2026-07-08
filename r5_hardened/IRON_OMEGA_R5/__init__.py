import sys
from pathlib import Path
for _parent in Path(__file__).resolve().parents:
    if (_parent / 'tenir_governance').exists():
        sys.path.insert(0, str(_parent))
        break

