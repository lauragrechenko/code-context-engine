"""Elixir-aware chunking.

Elixir is homoiconic: `defmodule`, `def`, `alias` and `test` are all `call`
nodes whose first named child is an `identifier` holding that text. There is no
`function_definition` node, so `Chunker._walk`'s node-type matching finds
nothing and every file would fall back to one whole-file chunk. Boundaries here
are matched on the target text of a `call` instead.

The walk emits byte spans through a callback rather than building `Chunk`
objects, so it stays independent of the store and is testable on its own.
"""
from collections.abc import Callable

from context_engine.models import ChunkType

# Definition macros that introduce a named function-like body.
DEF_FUN = frozenset({
    "def", "defp", "defmacro", "defmacrop", "defguard", "defguardp",
    "defdelegate", "defn", "defnp",
})
# Definition macros that introduce a nested namespace with its own body.
DEF_MOD = frozenset({"defmodule", "defprotocol", "defimpl"})
# Directives are part of a module's preamble, never a boundary of their own —
# without this, `use ExUnit.Case do ... end` would split off as a body item.
DIRECTIVES = frozenset({"alias", "import", "require", "use"})
# Module attributes that document the definition below them, as opposed to the
# module itself (`@moduledoc`) or its configuration (`@timeout 5_000`). Only
# these travel into the following chunk; the rest stay where they are written.
DOC_ATTRS = frozenset({
    "doc", "spec", "impl", "typedoc", "type", "opaque", "typep",
    "callback", "macrocallback", "deprecated", "since", "tag", "describetag",
})

# Label for chunks of a script's top-level code, which belongs to no module.
SCRIPT_SYMBOL = "(script)"

# emit(start_byte, end_byte, chunk_type, symbol)
Emit = Callable[[int, int, ChunkType, str], None]


def _text(src_bytes: bytes, node) -> str:
    return src_bytes[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


def _target(node, src_bytes: bytes) -> str | None:
    """The identifier a `call` is made through — `def`, `alias`, `test`, ...

    Returns None for calls made through anything else (`Mod.fun(...)` targets a
    `dot` node), which is exactly the set that never forms a boundary.
    """
    if node.type != "call" or not node.named_children:
        return None
    first = node.named_children[0]
    return _text(src_bytes, first) if first.type == "identifier" else None


def _arguments(node):
    for child in node.named_children:
        if child.type == "arguments":
            return child
    return None


def _do_block(node):
    for child in node.named_children:
        if child.type == "do_block":
            return child
    return None


def _unwrap_when(node, src_bytes: bytes):
    """Strip `when` guards off a definition head: `foo(x) when is_map(x)`."""
    current = node
    while current.type == "binary_operator":
        operator = current.child_by_field_name("operator")
        left = current.child_by_field_name("left")
        if left is None or operator is None or _text(src_bytes, operator) != "when":
            break
        current = left
    return current


def fun_head(call, src_bytes: bytes) -> tuple[str, int] | None:
    """`(name, arity)` of a def-family call, or None when it is not nameable.

    A bare `defp foo, do: :ok` head is an `identifier` (arity 0); the usual
    `def foo(a, b)` head is itself a `call` whose arguments give the arity.
    `def unquote(name)(...)` is generated code with no static name.
    """
    args = _arguments(call)
    if args is None:
        return None
    positional = [c for c in args.named_children if c.type != "keywords"]
    if not positional:
        return None
    head = _unwrap_when(positional[0], src_bytes)
    if head.type == "identifier":
        name = _text(src_bytes, head)
        return None if name == "unquote" else (name, 0)
    if head.type == "call":
        name = _target(head, src_bytes)
        if not name or name == "unquote":
            return None
        head_args = _arguments(head)
        return name, (len(head_args.named_children) if head_args else 0)
    return None


def _module_name(call, src_bytes: bytes) -> str:
    args = _arguments(call)
    if args and args.named_children:
        return _text(src_bytes, args.named_children[0]).replace("\n", " ").strip()
    return _target(call, src_bytes) or "module"


def _block_label(call, src_bytes: bytes) -> str:
    """Label a non-def block macro by its first string argument.

    `test "rejects an expired share" do` is the unit a reader searches for, and
    the macro name alone would make every ExUnit chunk read as `test`.
    """
    name = _target(call, src_bytes) or "block"
    args = _arguments(call)
    if args:
        for child in args.named_children:
            if child.type in ("string", "atom"):
                label = " ".join(_text(src_bytes, child).split())
                return f"{name} {label}"[:120]
    return name


def _attr_name(node, src_bytes: bytes) -> str | None:
    """`@doc "..."` → "doc". None when the node is not a module attribute."""
    if node.type != "unary_operator" or not node.named_children:
        return None
    operator = node.child_by_field_name("operator")
    if operator is not None and _text(src_bytes, operator) != "@":
        return None
    inner = node.named_children[-1]
    if inner.type == "identifier":
        return _text(src_bytes, inner)
    return _target(inner, src_bytes)


def _documents_next(node, src_bytes: bytes) -> bool:
    """Whether this node belongs to the definition that follows it."""
    if node.type == "comment":
        return True
    name = _attr_name(node, src_bytes)
    return name is not None and name in DOC_ATTRS


def _qualify(path: tuple[str, ...], suffix: str) -> str:
    return ".".join((*path, suffix)) if path else suffix


def _has_nested_blocks(node) -> bool:
    block = _do_block(node)
    if block is None:
        return False
    return any(
        child.type == "call" and _do_block(child) is not None
        for child in block.named_children
    )


class _Pending:
    """One boundary being accumulated, so later clauses can extend it."""

    __slots__ = ("key", "start", "end", "node", "is_def")

    def __init__(self, key, start, end, node, is_def):
        self.key = key
        self.start = start
        self.end = end
        self.node = node
        self.is_def = is_def


def chunk_elixir(root, src_bytes: bytes, emit: Emit, max_span: int) -> None:
    """Chunk an Elixir source tree.

    The file root is walked as if it were a module body, so a script that mixes
    top-level code with a helper module (`config/runtime.exs`) keeps both: the
    module is chunked by definition and the surrounding statements land in
    file-level chunks instead of being skipped.
    """
    _walk_block(
        root, src_bytes, emit, max_span,
        path=(), symbol=SCRIPT_SYMBOL, preamble_start=root.start_byte,
    )


def _walk_module(call, src_bytes: bytes, emit: Emit, max_span: int, path: tuple[str, ...]) -> None:
    name = _module_name(call, src_bytes)
    block = _do_block(call)
    if block is None:
        emit(call.start_byte, call.end_byte, ChunkType.MODULE, _qualify(path, name))
        return
    _walk_block(
        block, src_bytes, emit, max_span,
        path=(*path, name), symbol=_qualify(path, name), preamble_start=call.start_byte,
    )


def _walk_block(
    block, src_bytes: bytes, emit: Emit, max_span: int,
    *, path: tuple[str, ...], symbol: str, preamble_start: int,
) -> None:
    """Split one `do_block` into a preamble chunk plus one chunk per body item.

    Three positions are tracked while scanning the body. `preamble` is the open
    module header, closed by the first body item. `loose` is the start of any
    run of non-boundary nodes since the last body item — `@moduledoc`,
    directives, constants. `doc` is the tail of that run which documents the
    item about to come (`@doc`, `@spec`, a comment), and only that tail travels
    into the item's chunk.
    """
    preamble: int | None = preamble_start
    loose: int | None = None
    doc: int | None = None
    current: _Pending | None = None

    def flush(pending: _Pending | None) -> None:
        if pending is None:
            return
        span = pending.end - pending.start
        # An oversized container (a `describe` holding twenty `test`s) is worth
        # re-walking one level down. A function never is: its internal `case`
        # and `with` are `call`s owning `do_block`s too, so recursing would
        # shatter one function into unrelated fragments. Oversized functions
        # are windowed by the caller instead, which keeps them contiguous.
        if not pending.is_def and span > 2 * max_span and _has_nested_blocks(pending.node):
            _walk_block(
                _do_block(pending.node), src_bytes, emit, max_span,
                path=path,
                symbol=_qualify(path, _block_label(pending.node, src_bytes)),
                preamble_start=pending.start,
            )
            return
        if pending.is_def and pending.key:
            name, arity = pending.key
            label = _qualify(path, f"{name}/{arity}")
        elif pending.is_def:
            label = _qualify(path, _target(pending.node, src_bytes) or "def")
        else:
            label = _qualify(path, _block_label(pending.node, src_bytes))
        emit(pending.start, pending.end, ChunkType.FUNCTION, label)

    for child in block.named_children:
        target = _target(child, src_bytes)
        is_def = target in DEF_FUN
        is_mod = target in DEF_MOD
        is_block_macro = (
            target is not None and target not in DIRECTIVES and _do_block(child) is not None
        )
        if not (is_def or is_mod or is_block_macro):
            if loose is None:
                loose = child.start_byte
            if _documents_next(child, src_bytes):
                if doc is None:
                    doc = child.start_byte
            else:
                doc = None
            continue

        key = fun_head(child, src_bytes) if is_def else None
        # Adjacent clauses of the same name/arity are one unit — but only up to
        # a bounded span: `enclave.ex` has 42 `handle_event` clauses that would
        # otherwise merge into a single 32 KB chunk. A merged clause absorbs
        # whatever was written between the two, so nothing is emitted here.
        if (
            current is not None
            and key is not None
            and current.key == key
            and child.end_byte - current.start <= 2 * max_span
        ):
            current.end = child.end_byte
            loose = doc = None
            continue

        start = doc if doc is not None else child.start_byte
        flush(current)
        current = None
        if preamble is not None:
            if start > preamble:
                emit(preamble, start, ChunkType.MODULE, symbol)
            preamble = None
        elif loose is not None and start > loose:
            # Constants and directives written between two definitions belong
            # to neither; they would otherwise be dropped from the index.
            emit(loose, start, ChunkType.MODULE, symbol)
        loose = doc = None

        if is_mod:
            _walk_module(child, src_bytes, emit, max_span, path)
            continue
        current = _Pending(key, start, child.end_byte, child, is_def)

    if preamble is not None:
        # No body item at all — the module is its header.
        emit(preamble, block.end_byte, ChunkType.MODULE, symbol)
        return
    flush(current)
    if loose is not None and loose < block.end_byte:
        emit(loose, block.end_byte, ChunkType.MODULE, symbol)


def elixir_imports(root, src_bytes: bytes) -> list[str]:
    """Module names brought in by `alias` / `import` / `require` / `use`.

    Unlike the dotted-root convention the other languages use, the full name is
    kept: `KeyManager.DB.Interface` is the node the graph wants an edge to, and
    its root segment alone would collapse every app module onto one node.
    """
    out: list[str] = []
    _walk_imports(root, src_bytes, out)
    return out


def _walk_imports(node, src_bytes: bytes, out: list[str]) -> None:
    if node.type == "call" and (_target(node, src_bytes) or "") in DIRECTIVES:
        args = _arguments(node)
        if args is not None:
            positional = [c for c in args.named_children if c.type != "keywords"]
            if positional:
                out.extend(_expand_alias(positional[0], src_bytes))
    for child in node.named_children:
        _walk_imports(child, src_bytes, out)


def _expand_alias(node, src_bytes: bytes) -> list[str]:
    """`A.B` → ["A.B"]; `A.{B, C}` → ["A.B", "A.C"] (a `dot` over a `tuple`)."""
    if node.type == "alias":
        return [_text(src_bytes, node)]
    if node.type == "dot":
        parts = node.named_children
        if len(parts) == 2 and parts[1].type == "tuple":
            prefix = _text(src_bytes, parts[0])
            return [
                f"{prefix}.{_text(src_bytes, leaf)}"
                for leaf in parts[1].named_children
                if leaf.type == "alias"
            ]
    return []
