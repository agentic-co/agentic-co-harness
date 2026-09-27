"""Deterministic AST mutation: one bug per mutant, applied to real source.

Five operator families (the run brief's list): binary-operator swap,
comparison-operator swap, boolean-operator swap, constant/off-by-one swap,
and wrong-variable swap. Each `MutationSite` is identified by
`(lineno, col_offset, kind)` in the ORIGINAL source, not by a counter, so
sites are stable across re-parses and debuggable by citing a line number.

Enumeration (`find_sites`) is a single deterministic `ast.walk` — same source,
same sites, same order, every time. Applying a site (`apply_mutation`)
re-parses the source fresh and mutates ONLY the matching node, so mutants
never compound (one AST parse, one edit, one unparse).
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from typing import Optional

# -- operator swap tables (each maps to exactly ONE alternate — no branching
# choice, so a given site always mutates the same way) -----------------------

BINOP_SWAPS: dict[type, type] = {
    ast.Add: ast.Sub, ast.Sub: ast.Add,
    ast.Mult: ast.FloorDiv, ast.FloorDiv: ast.Mult,
    ast.Div: ast.Mult, ast.Mod: ast.Mult,
    ast.LShift: ast.RShift, ast.RShift: ast.LShift,
    ast.BitAnd: ast.BitOr, ast.BitOr: ast.BitAnd,
}

CMP_SWAPS: dict[type, type] = {
    ast.Lt: ast.LtE, ast.LtE: ast.Lt,
    ast.Gt: ast.GtE, ast.GtE: ast.Gt,
    ast.Eq: ast.NotEq, ast.NotEq: ast.Eq,
    ast.Is: ast.IsNot, ast.IsNot: ast.Is,
    ast.In: ast.NotIn, ast.NotIn: ast.In,
}

BOOLOP_SWAPS: dict[type, type] = {ast.And: ast.Or, ast.Or: ast.And}

KINDS = ("binop", "cmpop", "boolop", "constant", "varswap")


@dataclass(frozen=True)
class MutationSite:
    kind: str  # one of KINDS
    lineno: int
    col_offset: int
    detail: str  # human-readable, e.g. "Add -> Sub" or "swap args a/b"
    enclosing_function: Optional[str]  # None if module-level


def find_sites(source: str) -> list[MutationSite]:
    """All mutable sites in `source`, in a single deterministic `ast.walk`
    order. Does not look inside a function's nested functions/lambdas for
    the varswap operator (kept to the function's own direct body to avoid
    inventing a shadowing analysis this build has no time budget for)."""
    tree = ast.parse(source)
    sites: list[MutationSite] = []
    func_stack: list[str] = []

    class Finder(ast.NodeVisitor):
        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            func_stack.append(node.name)
            args = [a.arg for a in node.args.args]
            # Only plain positional args, both used as simple loads somewhere
            # in the body (not just present in the signature) — cheap load
            # check below via a name-count pass avoids proposing a swap that
            # can't possibly change behavior (an unused or write-only arg).
            if len(args) >= 2:
                used = _loaded_names(node.body)
                candidates = [a for a in args if a in used]
                if len(candidates) >= 2:
                    a, b = candidates[0], candidates[1]
                    sites.append(MutationSite(
                        kind="varswap", lineno=node.lineno, col_offset=node.col_offset,
                        detail=f"swap {a}/{b}", enclosing_function=node.name,
                    ))
            self.generic_visit(node)
            func_stack.pop()

        visit_AsyncFunctionDef = visit_FunctionDef  # same handling

        def visit_BinOp(self, node: ast.BinOp) -> None:
            alt = BINOP_SWAPS.get(type(node.op))
            if alt is not None:
                sites.append(MutationSite(
                    kind="binop", lineno=node.lineno, col_offset=node.col_offset,
                    detail=f"{type(node.op).__name__} -> {alt.__name__}",
                    enclosing_function=func_stack[-1] if func_stack else None,
                ))
            self.generic_visit(node)

        def visit_Compare(self, node: ast.Compare) -> None:
            if len(node.ops) == 1 and type(node.ops[0]) in CMP_SWAPS:
                alt = CMP_SWAPS[type(node.ops[0])]
                sites.append(MutationSite(
                    kind="cmpop", lineno=node.lineno, col_offset=node.col_offset,
                    detail=f"{type(node.ops[0]).__name__} -> {alt.__name__}",
                    enclosing_function=func_stack[-1] if func_stack else None,
                ))
            self.generic_visit(node)

        def visit_BoolOp(self, node: ast.BoolOp) -> None:
            alt = BOOLOP_SWAPS.get(type(node.op))
            if alt is not None:
                sites.append(MutationSite(
                    kind="boolop", lineno=node.lineno, col_offset=node.col_offset,
                    detail=f"{type(node.op).__name__} -> {alt.__name__}",
                    enclosing_function=func_stack[-1] if func_stack else None,
                ))
            self.generic_visit(node)

        def visit_Constant(self, node: ast.Constant) -> None:
            v = node.value
            if isinstance(v, bool):
                sites.append(MutationSite(
                    kind="constant", lineno=node.lineno, col_offset=node.col_offset,
                    detail=f"{v} -> {not v}",
                    enclosing_function=func_stack[-1] if func_stack else None,
                ))
            elif isinstance(v, int):  # after the bool check — bool is an int subclass
                sites.append(MutationSite(
                    kind="constant", lineno=node.lineno, col_offset=node.col_offset,
                    detail=f"{v} -> {v + 1}",
                    enclosing_function=func_stack[-1] if func_stack else None,
                ))
            self.generic_visit(node)

    Finder().visit(tree)
    return sites


def _loaded_names(body: list[ast.stmt]) -> set[str]:
    names: set[str] = set()
    for stmt in body:
        for node in ast.walk(stmt):
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                names.add(node.id)
    return names


def apply_mutation(source: str, site: MutationSite) -> str:
    """Re-parse `source` fresh and mutate ONLY the node matching `site`'s
    (kind, lineno, col_offset). Raises ValueError if no such node is found —
    a stale site against changed source is a bug in the caller, not something
    to silently ignore."""
    tree = ast.parse(source)
    applied = False

    class Mutator(ast.NodeTransformer):
        def visit_BinOp(self, node: ast.BinOp):
            nonlocal applied
            if (not applied and site.kind == "binop"
                    and node.lineno == site.lineno and node.col_offset == site.col_offset):
                alt = BINOP_SWAPS[type(node.op)]
                node.op = alt()
                applied = True
                return node
            return self.generic_visit(node)

        def visit_Compare(self, node: ast.Compare):
            nonlocal applied
            if (not applied and site.kind == "cmpop"
                    and node.lineno == site.lineno and node.col_offset == site.col_offset):
                alt = CMP_SWAPS[type(node.ops[0])]
                node.ops = [alt()]
                applied = True
                return node
            return self.generic_visit(node)

        def visit_BoolOp(self, node: ast.BoolOp):
            nonlocal applied
            if (not applied and site.kind == "boolop"
                    and node.lineno == site.lineno and node.col_offset == site.col_offset):
                alt = BOOLOP_SWAPS[type(node.op)]
                node.op = alt()
                applied = True
                return node
            return self.generic_visit(node)

        def visit_Constant(self, node: ast.Constant):
            nonlocal applied
            if (not applied and site.kind == "constant"
                    and node.lineno == site.lineno and node.col_offset == site.col_offset):
                node.value = (not node.value) if isinstance(node.value, bool) else node.value + 1
                applied = True
                return node
            return self.generic_visit(node)

        def visit_FunctionDef(self, node: ast.FunctionDef):
            nonlocal applied
            if (not applied and site.kind == "varswap"
                    and node.lineno == site.lineno and node.col_offset == site.col_offset):
                a, b = site.detail.replace("swap ", "").split("/")
                node.body = [_SwapNames(a, b).visit(stmt) for stmt in node.body]
                applied = True
                return node
            return self.generic_visit(node)

        visit_AsyncFunctionDef = visit_FunctionDef

    class _SwapNames(ast.NodeTransformer):
        def __init__(self, a: str, b: str):
            self.a, self.b = a, b

        def visit_Name(self, node: ast.Name):
            if isinstance(node.ctx, ast.Load):
                if node.id == self.a:
                    node.id = self.b
                elif node.id == self.b:
                    node.id = self.a
            return node

    new_tree = Mutator().visit(tree)
    if not applied:
        raise ValueError(f"mutation site not found in source: {site}")
    ast.fix_missing_locations(new_tree)
    return ast.unparse(new_tree)
