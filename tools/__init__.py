"""Auto-discovery of tool modules.

Every .py file in this folder (except __init__.py) is expected to expose:
    TOOL_DEFINITION : dict   — OpenAI-compatible function schema
    run(args: dict) -> str   — executes the tool and returns a string result
"""

import importlib
import pkgutil
from pathlib import Path

TOOLS: dict[str, dict] = {}       # name -> schema
RUNNERS: dict[str, callable] = {} # name -> run function


def _load_tools() -> None:
    package_dir = Path(__file__).parent
    for info in pkgutil.iter_modules([str(package_dir)]):
        if info.name.startswith("_"):
            continue
        mod = importlib.import_module(f"tools.{info.name}")
        if hasattr(mod, "TOOL_DEFINITION") and hasattr(mod, "run"):
            name = mod.TOOL_DEFINITION["function"]["name"]
            TOOLS[name] = mod.TOOL_DEFINITION
            RUNNERS[name] = mod.run


_load_tools()
