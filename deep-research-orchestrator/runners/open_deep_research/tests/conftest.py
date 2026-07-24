import sys
from pathlib import Path

# runner直下 (odr_engine.py) をimport可能にする。
# open_deep_research / langchain / mcp 本体はimportしない。
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
