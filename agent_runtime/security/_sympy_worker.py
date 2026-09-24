from __future__ import annotations

import ast
import contextlib
import io
import json
import math
import sys
from types import SimpleNamespace
from typing import Any

import sympy as _sympy


# A deliberately curated subset of SymPy.  The model does not receive the real
# sympy module object, which avoids exposing its import helpers and module graph.
_ALLOWED_SYMPY_NAMES = (
    "symbols", "Symbol", "Dummy", "Function",
    "Eq", "Ne", "Lt", "Le", "Gt", "Ge",
    "solve", "solveset", "linsolve", "nonlinsolve", "dsolve", "pdsolve",
    "simplify", "trigsimp", "radsimp", "cancel", "factor", "expand",
    "collect", "apart", "together",
    "diff", "Derivative", "integrate", "Integral", "limit", "Limit",
    "series", "summation", "Sum", "product", "Product",
    "Matrix", "ImmutableMatrix", "det", "trace", "eye", "zeros", "ones", "diag",
    "sin", "cos", "tan", "asin", "acos", "atan",
    "sinh", "cosh", "tanh", "exp", "log", "sqrt",
    "Abs", "sign", "re", "im", "conjugate", "Piecewise", "Max", "Min",
    "floor", "ceiling", "gamma", "factorial", "binomial",
    "Rational", "Integer", "Float", "S", "I", "E", "pi", "oo", "zoo", "nan",
    "Interval", "FiniteSet", "Union", "Intersection", "Lambda", "Poly",
    "RootOf", "roots", "nroots", "N", "latex", "pretty",
)

_ALLOWED_BUILTINS = {
    "print": print,
    "range": range,
    "len": len,
    "min": min,
    "max": max,
    "sum": sum,
    "abs": abs,
    "enumerate": enumerate,
    "zip": zip,
    "list": list,
    "tuple": tuple,
    "dict": dict,
    "set": set,
    "int": int,
    "float": float,
    "str": str,
    "bool": bool,
}

_FORBIDDEN_NAMES = {
    "__import__", "open", "exec", "eval", "compile", "input", "breakpoint",
    "globals", "locals", "vars", "dir", "getattr", "setattr", "delattr",
    "type", "object", "help", "memoryview",
    "os", "sys", "subprocess", "socket", "pathlib", "shutil", "tempfile",
    "ctypes", "multiprocessing", "threading", "asyncio", "urllib", "http",
    "requests", "builtins", "importlib", "inspect", "pickle", "marshal",
}

_FORBIDDEN_ATTRIBUTES = {
    "mro", "subclasses", "f_globals", "f_locals", "gi_frame", "cr_frame",
    "tb_frame", "func_globals", "im_class", "__dict__", "__class__",
}

_FORBIDDEN_NODE_TYPES = (
    ast.Import,
    ast.ImportFrom,
    ast.FunctionDef,
    ast.AsyncFunctionDef,
    ast.ClassDef,
    ast.Lambda,
    ast.With,
    ast.AsyncWith,
    ast.Try,
    ast.Raise,
    ast.Global,
    ast.Nonlocal,
    ast.Delete,
    ast.Yield,
    ast.YieldFrom,
    ast.Await,
)


class _PolicyError(ValueError):
    pass


class _RestrictedAstValidator(ast.NodeVisitor):
    def visit(self, node: ast.AST) -> Any:
        if isinstance(node, _FORBIDDEN_NODE_TYPES):
            raise _PolicyError(f"{type(node).__name__} is not allowed")
        return super().visit(node)

    def visit_Name(self, node: ast.Name) -> Any:
        if node.id.startswith("__") or node.id in _FORBIDDEN_NAMES:
            raise _PolicyError(f"name '{node.id}' is not allowed")
        return self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> Any:
        attr = node.attr
        if attr.startswith("_") or attr in _FORBIDDEN_ATTRIBUTES:
            raise _PolicyError(f"attribute '{attr}' is not allowed")
        return self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> Any:
        if isinstance(node.func, ast.Name) and node.func.id in _FORBIDDEN_NAMES:
            raise _PolicyError(f"call to '{node.func.id}' is not allowed")
        if isinstance(node.func, ast.Attribute):
            attr = node.func.attr
            if attr.startswith("_") or attr in _FORBIDDEN_ATTRIBUTES:
                raise _PolicyError(f"call to attribute '{attr}' is not allowed")
        return self.generic_visit(node)


class _BoundedBuffer(io.TextIOBase):
    def __init__(self, limit: int) -> None:
        self.limit = max(1, int(limit))
        self.parts: list[str] = []
        self.length = 0
        self.truncated = False

    def writable(self) -> bool:
        return True

    def write(self, value: str) -> int:
        text = str(value)
        original_length = len(text)
        remaining = self.limit - self.length
        if remaining > 0:
            fragment = text[:remaining]
            self.parts.append(fragment)
            self.length += len(fragment)
        if original_length > max(remaining, 0):
            self.truncated = True
        return original_length

    def flush(self) -> None:
        return None

    def getvalue(self) -> str:
        return "".join(self.parts)


def _clip(value: str, limit: int) -> tuple[str, bool]:
    if len(value) <= limit:
        return value, False
    return value[:limit], True


def _apply_posix_limits(memory_limit_mb: int, cpu_limit_seconds: int) -> None:
    try:
        import resource
    except ImportError:
        return
    try:
        memory_bytes = max(64, int(memory_limit_mb)) * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (memory_bytes, memory_bytes))
    except (ValueError, OSError):
        pass
    try:
        cpu_seconds = max(1, int(cpu_limit_seconds))
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds + 1))
    except (ValueError, OSError):
        pass
    for resource_name, limit in (("RLIMIT_FSIZE", 1_048_576), ("RLIMIT_NOFILE", 32)):
        resource_id = getattr(resource, resource_name, None)
        if resource_id is None:
            continue
        try:
            resource.setrlimit(resource_id, (limit, limit))
        except (ValueError, OSError):
            pass
    core_id = getattr(resource, "RLIMIT_CORE", None)
    if core_id is not None:
        try:
            resource.setrlimit(core_id, (0, 0))
        except (ValueError, OSError):
            pass


def _safe_namespace() -> dict[str, Any]:
    allowed = {
        name: getattr(_sympy, name)
        for name in _ALLOWED_SYMPY_NAMES
        if hasattr(_sympy, name)
    }
    # Compatibility with common model-generated snippets such as sp.symbols(...)
    # without exposing the real module object.
    allowed["sp"] = SimpleNamespace(**allowed)
    allowed["__builtins__"] = dict(_ALLOWED_BUILTINS)
    return allowed


def _evaluate(code: str, max_output_chars: int) -> dict[str, Any]:
    try:
        tree = ast.parse(code, mode="exec")
        _RestrictedAstValidator().visit(tree)
    except (SyntaxError, _PolicyError) as exc:
        return {"status": "rejected", "message": str(exc)}

    namespace = _safe_namespace()
    buffer = _BoundedBuffer(max_output_chars)
    try:
        compiled = compile(tree, "<restricted-sympy>", "exec", dont_inherit=True)
        with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
            exec(compiled, namespace, namespace)
    except BaseException as exc:  # child process: convert execution failures to data
        return {
            "status": "error",
            "error_type": type(exc).__name__,
            "message": str(exc),
            "truncated": buffer.truncated,
        }

    result = namespace.get("result", None)
    if result is not None:
        try:
            result_text, result_truncated = _clip(str(result), max_output_chars)
        except BaseException as exc:
            return {
                "status": "error",
                "error_type": type(exc).__name__,
                "message": "failed to render result",
            }
        latex_text = ""
        latex_truncated = False
        try:
            latex_text, latex_truncated = _clip(_sympy.latex(result), max_output_chars)
        except BaseException:
            latex_text = ""
        return {
            "status": "ok",
            "result": result_text,
            "latex": latex_text or None,
            "stdout": buffer.getvalue() or None,
            "truncated": buffer.truncated or result_truncated or latex_truncated,
        }

    return {
        "status": "ok",
        "result": None,
        "latex": None,
        "stdout": buffer.getvalue() or None,
        "truncated": buffer.truncated,
    }


def main() -> int:
    try:
        request = json.loads(sys.stdin.read())
        if not isinstance(request, dict):
            raise ValueError("request must be an object")
        code = str(request.get("code", ""))
        max_output_chars = max(1, min(int(request.get("max_output_chars", 16000)), 100_000))
        memory_limit_mb = max(64, min(int(request.get("memory_limit_mb", 512)), 4096))
        cpu_limit_seconds = max(1, min(int(request.get("cpu_limit_seconds", 5)), 60))
        _apply_posix_limits(memory_limit_mb, cpu_limit_seconds)
        response = _evaluate(code, max_output_chars)
    except BaseException as exc:
        response = {
            "status": "error",
            "error_type": type(exc).__name__,
            "message": str(exc),
        }
    sys.stdout.write(json.dumps(response, ensure_ascii=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
