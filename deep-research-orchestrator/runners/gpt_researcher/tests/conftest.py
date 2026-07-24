import sys
from pathlib import Path

# runner直下 (gptr_engine.py) をimport可能にする。gpt_researcher本体はimportしない。
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
