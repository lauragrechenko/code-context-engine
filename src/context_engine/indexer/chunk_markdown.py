"""Markdown chunking by heading section.

Deliberately grammar-free. The only boundary markdown chunking needs is a
heading; `tree-sitter-markdown` would add a second split block+inline parser
(two `Language` objects inside the tree-sitter<0.26 ABI cap) for a rule that
fits in this module, and nothing downstream consumes markdown AST detail.

Holding no parser and no shared state, this path is thread-safe by
construction — the pipeline fans ~50 files out over `asyncio.to_thread`.
"""
import re

_FENCE = re.compile(r"^(?:\s{0,3})(`{3,}|~{3,})")
_ATX = re.compile(r"^(#{1,6})\s+\S")

PREAMBLE = "(preamble)"


def markdown_sections(source: str) -> list[tuple[int, int, str]]:
    """Split into `(start_line, end_line, heading_path)`, both lines 0-based.

    Headings inside fenced code blocks are ignored — a `# comment` in a bash
    fence is not a section, and this repo's own docs are full of them.
    """
    lines = source.split("\n")
    fence: str | None = None
    marks: list[tuple[int, int, str]] = []

    for index, line in enumerate(lines):
        opener = _FENCE.match(line)
        if opener is not None:
            token = opener.group(1)
            if fence is None:
                fence = token[0]
            elif line.strip().startswith(fence * 3):
                fence = None
            continue
        if fence is not None:
            continue
        heading = _ATX.match(line)
        if heading is not None:
            marks.append((index, len(heading.group(1)), line.strip("# ").strip()))

    if not marks or marks[0][0] > 0:
        marks.insert(0, (0, 0, PREAMBLE))

    sections: list[tuple[int, int, str]] = []
    stack: list[tuple[int, str]] = []
    for position, (start, level, title) in enumerate(marks):
        end = marks[position + 1][0] - 1 if position + 1 < len(marks) else len(lines) - 1
        while stack and stack[-1][0] >= level:
            stack.pop()
        path = " > ".join(text for _, text in [*stack, (level, title)])
        stack.append((level, title))
        if end >= start:
            sections.append((start, end, path))
    return sections
