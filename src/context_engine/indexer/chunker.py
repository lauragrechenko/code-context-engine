"""AST-aware code chunking using tree-sitter."""
import hashlib
import threading
from bisect import bisect_right

import tree_sitter_python as tspython
import tree_sitter_javascript as tsjavascript
import tree_sitter_typescript as tstypescript
import tree_sitter_php as tsphp
import tree_sitter_go as tsgo
import tree_sitter_rust as tsrust
import tree_sitter_java as tsjava
import tree_sitter_c_sharp as tscsharp
import tree_sitter_elixir as tselixir
from tree_sitter import Language, Parser

from context_engine.indexer.chunk_elixir import chunk_elixir, elixir_imports
from context_engine.indexer.chunk_markdown import markdown_sections
from context_engine.models import Chunk, ChunkType

# The embedding model caps at 512 tokens — about 1,690 chars at the 3.3
# chars/token this codebase assumes (Chunk._CHARS_PER_TOKEN_CODE). Anything
# longer is embedded only in part, so a 64 KB whole-file chunk gets a vector
# built from its first 40 lines while its metadata claims the whole file.
# Oversized chunks are therefore split into overlapping windows.
_MAX_EMBED_CHARS = 1_500
_OVERLAP_CHARS = 200

_FUNCTION_TYPES = {
    "function_definition", "function_declaration",  # Python, PHP, JS
    "method_definition", "method_declaration",       # JS/TS, PHP/Go/Java/C#
    "arrow_function",                                # JS/TS
    "function_item",                                 # Rust
    "local_function_statement",                      # C#
}
_CLASS_TYPES = {
    "class_definition", "class_declaration",       # Python, JS/TS, PHP, Java, C#
    "struct_declaration", "interface_declaration",  # C#
    "record_declaration", "enum_declaration",       # Java, C#
    "type_declaration",                             # Go (struct/interface)
    "struct_item", "impl_item", "enum_item",        # Rust
}
_IMPORT_TYPES = {
    "import_statement", "import_from_statement",  # Python
    "import_declaration",                          # TypeScript, Go, Java
    "use_declaration",                             # PHP, Rust
    "using_directive",                             # C#
}

def _node_text(src_bytes: bytes, node) -> str:
    """Slice a node's source from the utf-8 BYTES tree-sitter parsed.

    node.start_byte / node.end_byte are byte offsets into the encoded
    source, not str indices — slicing the original str garbles content
    whenever a multi-byte character precedes the node.
    """
    return src_bytes[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


_LANGUAGES = {
    "python": Language(tspython.language()),
    "javascript": Language(tsjavascript.language()),
    "typescript": Language(tstypescript.language_typescript()),
    "tsx": Language(tstypescript.language_tsx()),
    "php": Language(tsphp.language_php()),
    "go": Language(tsgo.language()),
    "rust": Language(tsrust.language()),
    "java": Language(tsjava.language()),
    "csharp": Language(tscsharp.language()),
    "elixir": Language(tselixir.language()),
}


def _line_starts(src_bytes: bytes) -> list[int]:
    """Byte offset of every line start, so a byte span can be given line numbers.

    Built once per file: counting newlines per chunk would be quadratic on the
    files that produce the most chunks.
    """
    starts = [0]
    index = src_bytes.find(b"\n")
    while index != -1:
        starts.append(index + 1)
        index = src_bytes.find(b"\n", index + 1)
    return starts


def _line_of(line_starts: list[int], offset: int) -> int:
    return bisect_right(line_starts, offset)


def _units(content: str, start_line: int) -> list[tuple[str, int]]:
    """Split content into `(text, source line)` windowing units.

    Lines are the natural unit, but a single line longer than the budget (a
    minified blob, a long data literal) has to be cut mid-line or it would
    still overflow the embedder.
    """
    units: list[tuple[str, int]] = []
    for offset, line in enumerate(content.split("\n")):
        number = start_line + offset
        if len(line) <= _MAX_EMBED_CHARS:
            units.append((line, number))
            continue
        for position in range(0, len(line), _MAX_EMBED_CHARS):
            units.append((line[position:position + _MAX_EMBED_CHARS], number))
    return units


class Chunker:
    # A single Chunker is shared across the indexing run, and the pipeline
    # fans chunk_with_imports() out over asyncio.to_thread — up to 50 files
    # of the same language parse concurrently. tree_sitter.Parser holds
    # mutable C parse state and is documented as unsafe to use from multiple
    # threads at once; sharing one Parser per language raced on that state
    # and corrupted memory (SIGSEGV/SIGBUS, issue #113). Parsers are cheap to
    # build, so cache them per worker thread via threading.local instead —
    # each thread gets its own Parser and never contends with another.
    def __init__(self) -> None:
        self._local = threading.local()

    def _get_parser(self, language: str) -> Parser | None:
        if language not in _LANGUAGES:
            return None
        parsers = getattr(self._local, "parsers", None)
        if parsers is None:
            parsers = {}
            self._local.parsers = parsers
        parser = parsers.get(language)
        if parser is None:
            parser = Parser(_LANGUAGES[language])
            parsers[language] = parser
        return parser

    def chunk(self, source: str, file_path: str, language: str) -> list[Chunk]:
        if language == "markdown":
            return self._split_oversize(self._markdown_chunks(source, file_path))
        parser = self._get_parser(language)
        if parser is None:
            return self._split_oversize([self._fallback_chunk(source, file_path, language)])
        # tree-sitter parses the utf-8 BYTES and reports byte offsets.
        # Encode once and slice the bytes — slicing the original str with
        # byte offsets silently garbles every chunk after the first
        # multi-byte character (emoji, CJK, accents).
        src_bytes = source.encode("utf-8")
        tree = parser.parse(src_bytes)
        chunks = []
        if language == "elixir":
            self._walk_elixir(tree.root_node, src_bytes, file_path, chunks)
        else:
            self._walk(tree.root_node, src_bytes, file_path, language, chunks)
        if not chunks:
            chunks = [self._fallback_chunk(source, file_path, language)]
        return self._split_oversize(chunks)

    def _walk_elixir(self, root, src_bytes, file_path, chunks) -> None:
        line_starts = _line_starts(src_bytes)

        def emit(start: int, end: int, chunk_type: ChunkType, symbol: str) -> None:
            content = src_bytes[start:end].decode("utf-8", errors="replace").rstrip()
            if not content.strip():
                return
            # Measure the end line from the content that is kept, not from the
            # raw span: a span runs up to the next item, so the blank lines
            # rstrip drops would otherwise be reported as covered.
            last_byte = start + len(content.encode("utf-8")) - 1
            chunk = self._make_chunk(
                content, file_path, _line_of(line_starts, start),
                _line_of(line_starts, max(start, last_byte)), "elixir", chunk_type,
            )
            chunk.metadata["symbol"] = symbol
            chunks.append(chunk)

        chunk_elixir(root, src_bytes, emit, _MAX_EMBED_CHARS)

    def _markdown_chunks(self, source: str, file_path: str) -> list[Chunk]:
        lines = source.split("\n")
        chunks: list[Chunk] = []
        for start, end, heading in markdown_sections(source):
            content = "\n".join(lines[start:end + 1]).rstrip()
            if not content.strip():
                continue
            # Sections run to the line before the next heading, so the end line
            # comes from the content kept rather than from that boundary.
            chunk = self._make_chunk(
                content, file_path, start + 1, start + content.count("\n") + 1,
                "markdown", ChunkType.DOC,
            )
            chunk.metadata["heading"] = heading
            chunks.append(chunk)
        return chunks or [self._fallback_chunk(source, file_path, "markdown")]

    def _split_oversize(self, chunks: list[Chunk]) -> list[Chunk]:
        out: list[Chunk] = []
        for chunk in chunks:
            if len(chunk.content) <= _MAX_EMBED_CHARS:
                out.append(chunk)
            else:
                out.extend(self._windows(chunk))
        return out

    def _windows(self, chunk: Chunk) -> list[Chunk]:
        """Cut one oversized chunk into overlapping windows within the budget.

        Windows overlap so a definition split across the seam is still whole in
        one of them. Each window keeps the true source line range it covers.
        """
        units = _units(chunk.content, chunk.start_line)
        spans: list[tuple[int, int]] = []
        start = 0
        while start < len(units):
            end, size = start, 0
            while end < len(units) and (
                end == start or size + len(units[end][0]) + 1 <= _MAX_EMBED_CHARS
            ):
                size += len(units[end][0]) + 1
                end += 1
            spans.append((start, end))
            if end >= len(units):
                break
            back, overlap = end - 1, 0
            while back > start and overlap + len(units[back][0]) + 1 <= _OVERLAP_CHARS:
                overlap += len(units[back][0]) + 1
                back -= 1
            start = max(back + 1, start + 1)

        heading = chunk.metadata.get("heading")
        parts: list[Chunk] = []
        for index, (first, last) in enumerate(spans):
            body = "\n".join(text for text, _ in units[first:last])
            # A later window opens mid-document, so repeat the heading path —
            # otherwise the fragment reads as belonging to nothing.
            if index and heading:
                body = f"{heading}\n{body}"
            part = self._make_chunk(
                body, chunk.file_path, units[first][1], units[last - 1][1],
                chunk.language, chunk.chunk_type, salt=str(index),
            )
            part.metadata.update(chunk.metadata)
            part.metadata["part"] = (index + 1, len(spans))
            parts.append(part)
        return parts

    def _make_chunk(
        self, content, file_path, start_line, end_line, language, chunk_type, *, salt="",
    ) -> Chunk:
        chunk_id = hashlib.sha256(
            f"{file_path}:{start_line}:{end_line}:{salt}:{content[:100]}".encode()
        ).hexdigest()[:16]
        return Chunk(
            id=chunk_id, content=content, chunk_type=chunk_type,
            file_path=file_path, start_line=start_line, end_line=end_line, language=language,
        )

    def _walk(self, node, src_bytes, file_path, language, chunks):
        if node.type in _FUNCTION_TYPES:
            chunks.append(self._node_to_chunk(node, src_bytes, file_path, language, ChunkType.FUNCTION))
        elif node.type in _CLASS_TYPES:
            chunks.append(self._node_to_chunk(node, src_bytes, file_path, language, ChunkType.CLASS))
        for child in node.children:
            self._walk(child, src_bytes, file_path, language, chunks)

    def _node_to_chunk(self, node, src_bytes, file_path, language, chunk_type):
        return self._make_chunk(
            _node_text(src_bytes, node), file_path,
            node.start_point.row + 1, node.end_point.row + 1, language, chunk_type,
        )

    def chunk_with_imports(
        self, source: str, file_path: str, language: str
    ) -> tuple[list[Chunk], list[str]]:
        chunks = self.chunk(source, file_path, language)
        imports = self._extract_imports(source, language)
        return chunks, imports

    def _extract_imports(self, source: str, language: str) -> list[str]:
        parser = self._get_parser(language)
        if parser is None:
            return []
        # Same byte-offset contract as chunk(): slice the encoded bytes,
        # never the str (multi-byte chars shift str indices).
        src_bytes = source.encode("utf-8")
        tree = parser.parse(src_bytes)
        if language == "elixir":
            # alias/import/require/use are `call` nodes, so _IMPORT_TYPES
            # cannot see them — see chunk_elixir for why.
            return list(dict.fromkeys(elixir_imports(tree.root_node, src_bytes)))
        imports: list[str] = []
        self._walk_imports(tree.root_node, src_bytes, language, imports)
        return list(dict.fromkeys(imports))  # deduplicate while preserving order

    def _walk_imports(self, node, src_bytes, language, imports):
        if node.type in _IMPORT_TYPES:
            module = self._parse_import_module(node, src_bytes, language)
            if module:
                imports.append(module)
        for child in node.children:
            self._walk_imports(child, src_bytes, language, imports)

    def _parse_import_module(self, node, src_bytes, language) -> str | None:
        if node.type == "import_statement":
            # Python: "import os" or "import os.path"
            # Also handles JS/TS: "import React from 'react'" (string child present)
            for child in node.children:
                if child.type == "string":
                    # JavaScript/TypeScript import with string module specifier
                    raw = _node_text(src_bytes, child).strip("'\"")
                    return raw.split("/")[0] if not raw.startswith("@") else "/".join(raw.split("/")[:2])
                if child.type in ("dotted_name", "aliased_import"):
                    # Python bare import
                    name = _node_text(src_bytes, child)
                    name = name.split(" as ")[0].strip()
                    return name.split(".")[0]
        elif node.type == "import_from_statement":
            # Python: "from pathlib import Path"
            for child in node.children:
                if child.type in ("dotted_name", "relative_import"):
                    name = _node_text(src_bytes, child).strip()
                    name = name.lstrip(".")
                    if name:
                        return name.split(".")[0]
        elif node.type == "using_directive":
            # C#: "using System.Collections.Generic;" — take the root namespace
            # segment, mirroring the Python dotted-name convention below.
            for child in node.children:
                if child.type in ("qualified_name", "identifier"):
                    name = _node_text(src_bytes, child).strip()
                    return name.split(".")[0]
        elif node.type == "import_declaration":
            # TypeScript (tree-sitter-typescript): "import React from 'react'"
            for child in node.children:
                if child.type == "string":
                    raw = _node_text(src_bytes, child).strip("'\"")
                    return raw.split("/")[0] if not raw.startswith("@") else "/".join(raw.split("/")[:2])
        return None

    def _fallback_chunk(self, source, file_path, language):
        chunk_id = hashlib.sha256(f"{file_path}:module".encode()).hexdigest()[:16]
        lines = source.strip().split("\n")
        return Chunk(
            id=chunk_id, content=source, chunk_type=ChunkType.MODULE,
            file_path=file_path, start_line=1, end_line=len(lines), language=language,
        )
