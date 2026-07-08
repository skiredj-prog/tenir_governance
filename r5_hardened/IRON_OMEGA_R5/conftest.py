import sys
from pathlib import Path
ROOT = Path(__file__).resolve()
for parent in ROOT.parents:
    if (parent / 'tenir_governance').exists():
        sys.path.insert(0, str(parent))
        break
