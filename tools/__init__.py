"""Auto-discovery of tool modules + dynamic skill loading.

Every .py file in this folder (except __init__.py) is expected to expose:
    TOOL_DEFINITION : dict   — OpenAI-compatible function schema
    run(args: dict) -> str   — executes the tool and returns a string result

Dynamic skills are loaded from Supabase and executed in a sandbox.
"""

import importlib
import logging
import pkgutil
import signal
from pathlib import Path

logger = logging.getLogger(__name__)

TOOLS: dict[str, dict] = {}       # name -> schema
RUNNERS: dict[str, callable] = {} # name -> run function
DYNAMIC_SKILLS: set[str] = set()  # names of dynamic (not static) tools

ALLOWED_IMPORTS = {"json", "math", "datetime", "re", "urllib.parse", "httpx"}
DYNAMIC_TIMEOUT = 10  # seconds


def _make_runner(code_str: str):
    """Wrap a code string into a sandboxed run(args) function."""
    wrapped = "def _dynamic_run(args):\n"
    for line in code_str.split("\n"):
        wrapped += f"    {line}\n"

    safe_builtins = {
        "str": str, "int": int, "float": float, "bool": bool,
        "list": list, "dict": dict, "tuple": tuple, "set": set,
        "len": len, "range": range, "enumerate": enumerate,
        "zip": zip, "map": map, "filter": filter, "sorted": sorted,
        "min": min, "max": max, "sum": sum, "abs": abs, "round": round,
        "isinstance": isinstance, "type": type,
        "True": True, "False": False, "None": None,
        "print": lambda *a, **kw: None,  # no-op
        "Exception": Exception, "ValueError": ValueError,
        "KeyError": KeyError, "TypeError": TypeError,
        "RuntimeError": RuntimeError,
    }

    def _safe_import(name, *a, **kw):
        if name not in ALLOWED_IMPORTS:
            raise ImportError(f"Import '{name}' is not allowed in dynamic skills")
        return importlib.import_module(name)

    safe_builtins["__import__"] = _safe_import

    namespace = {"__builtins__": safe_builtins}
    exec(compile(wrapped, "<dynamic-skill>", "exec"), namespace)

    inner_fn = namespace["_dynamic_run"]

    def _timed_runner(args: dict) -> str:
        def _timeout_handler(signum, frame):
            raise TimeoutError("Dynamic skill timed out (10s limit)")

        old_handler = signal.signal(signal.SIGALRM, _timeout_handler)
        signal.alarm(DYNAMIC_TIMEOUT)
        try:
            result = inner_fn(args)
            return str(result) if result is not None else ""
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, old_handler)

    return _timed_runner


def _load_tools() -> None:
    """Load static tools from .py files in this folder."""
    package_dir = Path(__file__).parent
    for info in pkgutil.iter_modules([str(package_dir)]):
        if info.name.startswith("_"):
            continue
        mod = importlib.import_module(f"tools.{info.name}")
        if hasattr(mod, "TOOL_DEFINITION") and hasattr(mod, "run"):
            name = mod.TOOL_DEFINITION["function"]["name"]
            TOOLS[name] = mod.TOOL_DEFINITION
            RUNNERS[name] = mod.run


def load_dynamic_skills() -> None:
    """Load dynamic skills from Supabase into TOOLS/RUNNERS."""
    try:
        import memory
        skills = memory.load_dynamic_skills()
    except Exception:
        logger.exception("Failed to load dynamic skills from Supabase")
        return

    for skill in skills:
        try:
            register_dynamic_skill(
                name=skill["name"],
                description=skill["description"],
                parameters=skill["parameters"],
                code=skill["code"],
            )
        except Exception:
            logger.exception(f"Failed to load dynamic skill: {skill['name']}")


def register_dynamic_skill(name: str, description: str, parameters: dict, code: str) -> None:
    """Register a dynamic skill in-memory (available immediately)."""
    runner = _make_runner(code)
    TOOLS[name] = {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": parameters,
        },
    }
    RUNNERS[name] = runner
    DYNAMIC_SKILLS.add(name)
    logger.info(f"Registered dynamic skill: {name}")


def unregister_dynamic_skill(name: str) -> None:
    """Remove a dynamic skill from memory."""
    TOOLS.pop(name, None)
    RUNNERS.pop(name, None)
    DYNAMIC_SKILLS.discard(name)
    logger.info(f"Unregistered dynamic skill: {name}")


# Load static tools immediately
_load_tools()
