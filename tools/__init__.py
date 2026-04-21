"""Auto-discovery of tool modules + dynamic skill loading.

Every .py file in this folder (except names starting with '_') is expected to expose:
    TOOL_DEFINITION : dict   — OpenAI-compatible function schema
    run(args: dict) -> str   — executes the tool and returns a string result

Dynamic skills are loaded from Supabase and executed with **defense-in-depth
restrictions** — not true isolation. The layers are: AST validation (blocks
dunder access, restricted imports, async, dangerous builtins), a minimal
`__builtins__` namespace, a SSRF-filtered `httpx` wrapper, and a 10-second
thread timeout. Python in-process sandboxes are known to be escapable through
subtle mechanisms (operator overloading, C-extension quirks, str.format
descriptors). Every approved skill should be reviewed like an unvetted PR.
True isolation requires a subprocess / container with seccomp or similar.
"""

import ast
import importlib
import logging
import pkgutil
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from pathlib import Path

logger = logging.getLogger(__name__)

TOOLS: dict[str, dict] = {}       # STATIC tool schemas (discovered from .py files)
RUNNERS: dict[str, callable] = {} # ALL runners — static + every approved dynamic skill
DYNAMIC_SKILLS: set[str] = set()  # names of approved dynamic skills (Level 0)
# Approved dynamic skills live here with their full tool schema but are NOT
# auto-exposed to the LLM. The model sees only their {name, description} list
# (Level 0). To USE a skill the model must call `load_skill(name)` which queues
# it in _pending_skill_loads; the agent drains that on the next tool round.
DYNAMIC_SKILL_REGISTRY: dict[str, dict] = {}  # name -> schema
_pending_skill_loads: dict[str, set[str]] = {}  # user_id -> set of skill names

ALLOWED_IMPORTS = {"json", "math", "datetime", "re", "urllib.parse", "httpx"}
DYNAMIC_TIMEOUT = 10  # seconds

_SAFE_HTTPX = None


def _get_safe_httpx():
    """Lazily build and cache the SSRF-filtered httpx module for sandboxed skills."""
    global _SAFE_HTTPX
    if _SAFE_HTTPX is None:
        from tools._safe_httpx import build_safe_httpx
        _SAFE_HTTPX = build_safe_httpx()
    return _SAFE_HTTPX

# Dunder attributes that allow sandbox escapes (class walking, globals access, etc.)
_BLOCKED_ATTRS = frozenset({
    "__class__", "__bases__", "__subclasses__", "__mro__",
    "__globals__", "__code__", "__closure__", "__func__",
    "__self__", "__builtins__", "__import__", "__loader__",
    "__spec__", "__qualname__", "__module__", "__dict__",
    "__init_subclass__", "__set_name__", "__reduce__",
    "__reduce_ex__", "__getattr__", "__setattr__", "__delattr__",
})


def _validate_code_ast(code_str: str) -> None:
    """Validate dynamic skill code doesn't use dangerous patterns or unavailable imports."""
    wrapped = f"def _f(args):\n"
    for line in code_str.split("\n"):
        wrapped += f"    {line}\n"

    tree = ast.parse(wrapped)

    for node in ast.walk(tree):
        # Block access to dunder attributes: obj.__class__, obj.__globals__, etc.
        if isinstance(node, ast.Attribute) and node.attr in _BLOCKED_ATTRS:
            raise ValueError(
                f"Forbidden attribute access: '{node.attr}' is not allowed in dynamic skills"
            )

        # Block string literals that look like dunder smuggling via getattr
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node.value in _BLOCKED_ATTRS:
                raise ValueError(
                    f"Forbidden string literal: '{node.value}' suggests attribute smuggling"
                )

        # Block eval/exec/compile/getattr/setattr/delattr calls
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id in (
                "eval", "exec", "compile", "getattr", "setattr", "delattr",
                "vars", "dir", "globals", "locals", "breakpoint",
                "__import__", "open",
            ):
                raise ValueError(
                    f"Forbidden function call: '{func.id}' is not allowed in dynamic skills"
                )

        # Block imports of unavailable modules
        if isinstance(node, ast.Import):
            for alias in node.names:
                mod = alias.name.split(".")[0]
                if mod not in ALLOWED_IMPORTS:
                    raise ValueError(
                        f"Import '{alias.name}' is not allowed. "
                        f"Only these modules are available: {', '.join(sorted(ALLOWED_IMPORTS))}"
                    )
        if isinstance(node, ast.ImportFrom):
            if node.module:
                mod = node.module.split(".")[0]
                if mod not in ALLOWED_IMPORTS:
                    raise ValueError(
                        f"Import from '{node.module}' is not allowed. "
                        f"Only these modules are available: {', '.join(sorted(ALLOWED_IMPORTS))}"
                    )

        # Block async functions — sandbox only supports sync execution
        if isinstance(node, (ast.AsyncFunctionDef, ast.Await)):
            raise ValueError(
                "Async code (async def, await) is not supported in dynamic skills. "
                "Use synchronous code only. For HTTP calls, use httpx.Client() not AsyncClient()."
            )


def _make_runner(code_str: str):
    """Wrap a code string into a sandboxed run(args) function."""
    # Validate code safety at the AST level before compiling
    _validate_code_ast(code_str)

    wrapped = "def _dynamic_run(args):\n"
    for line in code_str.split("\n"):
        wrapped += f"    {line}\n"

    safe_builtins = {
        "str": str, "int": int, "float": float, "bool": bool,
        "list": list, "dict": dict, "tuple": tuple, "set": set,
        "len": len, "range": range, "enumerate": enumerate,
        "zip": zip, "map": map, "filter": filter, "sorted": sorted,
        "min": min, "max": max, "sum": sum, "abs": abs, "round": round,
        "isinstance": isinstance,
        "True": True, "False": False, "None": None,
        "print": lambda *a, **kw: None,  # no-op
        "Exception": Exception, "ValueError": ValueError,
        "KeyError": KeyError, "TypeError": TypeError,
        "RuntimeError": RuntimeError,
    }

    def _safe_import(name, *a, **kw):
        if name not in ALLOWED_IMPORTS:
            raise ImportError(f"Import '{name}' is not allowed in dynamic skills")
        if name == "httpx":
            return _get_safe_httpx()
        return importlib.import_module(name)

    safe_builtins["__import__"] = _safe_import

    namespace = {"__builtins__": safe_builtins}
    exec(compile(wrapped, "<dynamic-skill>", "exec"), namespace)

    inner_fn = namespace["_dynamic_run"]

    _executor = ThreadPoolExecutor(max_workers=1)

    def _timed_runner(args: dict) -> str:
        future = _executor.submit(inner_fn, args)
        try:
            result = future.result(timeout=DYNAMIC_TIMEOUT)
            return str(result) if result is not None else ""
        except FuturesTimeout:
            future.cancel()
            raise TimeoutError("Dynamic skill timed out (10s limit)")

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
    """Register a dynamic skill's runner + Level-0 metadata. Schema is NOT added
    to TOOLS; the LLM must opt in via `load_skill` to make the schema available."""
    runner = _make_runner(code)
    schema = {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": parameters,
        },
    }
    DYNAMIC_SKILL_REGISTRY[name] = schema
    RUNNERS[name] = runner
    DYNAMIC_SKILLS.add(name)
    logger.info(f"Registered dynamic skill (Level 0): {name}")


def unregister_dynamic_skill(name: str) -> None:
    """Remove a dynamic skill from memory (registry + any active exposure)."""
    TOOLS.pop(name, None)
    RUNNERS.pop(name, None)
    DYNAMIC_SKILLS.discard(name)
    DYNAMIC_SKILL_REGISTRY.pop(name, None)
    for pending in _pending_skill_loads.values():
        pending.discard(name)
    logger.info(f"Unregistered dynamic skill: {name}")


def mark_pending_skill(user_id, name: str) -> None:
    """Queue a skill to be added to the next tool round's active tool list."""
    _pending_skill_loads.setdefault(str(user_id), set()).add(name)


def drain_pending_skills(user_id) -> set[str]:
    """Return and clear the skill names queued via mark_pending_skill for this user."""
    return _pending_skill_loads.pop(str(user_id), set())


def get_skill_schemas(names) -> list[dict]:
    """Return the tool schemas for these skill names (skipping unknown ones)."""
    return [DYNAMIC_SKILL_REGISTRY[n] for n in names if n in DYNAMIC_SKILL_REGISTRY]


def list_dynamic_skill_summary() -> list[tuple[str, str]]:
    """Return (name, description) pairs for all registered skills — feeds the Level-0 block."""
    out = []
    for name in sorted(DYNAMIC_SKILL_REGISTRY):
        schema = DYNAMIC_SKILL_REGISTRY[name]
        desc = (schema.get("function") or {}).get("description", "") or ""
        out.append((name, desc))
    return out


# Load static tools immediately
_load_tools()
