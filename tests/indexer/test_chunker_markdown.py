# tests/indexer/test_chunker_markdown.py
"""Markdown chunking — see context_engine/indexer/chunk_markdown.py."""
import pytest

from context_engine.indexer.chunk_markdown import markdown_sections
from context_engine.indexer.chunker import Chunker
from context_engine.models import ChunkType


@pytest.fixture
def chunker():
    return Chunker()


DOC = """# Title

Intro line.

## Setup

Run the thing.

### Prerequisites

Install it first.

## Usage

Use the thing.
"""


def test_each_heading_starts_a_section(chunker):
    chunks = chunker.chunk(DOC, "README.md", "markdown")
    assert [c.metadata["heading"] for c in chunks] == [
        "Title",
        "Title > Setup",
        "Title > Setup > Prerequisites",
        "Title > Usage",
    ]
    assert all(c.chunk_type == ChunkType.DOC for c in chunks)


def test_section_line_numbers_point_at_the_source(chunker):
    lines = DOC.split("\n")
    for chunk in chunker.chunk(DOC, "README.md", "markdown"):
        assert lines[chunk.start_line - 1].startswith("#")
        assert chunk.content.split("\n")[0] == lines[chunk.start_line - 1]


FENCED = """# Guide

Run this:

```bash
# not a heading
echo hi
```

## Real heading

Done.
"""


def test_headings_inside_fenced_code_are_ignored(chunker):
    headings = [c.metadata["heading"] for c in chunker.chunk(FENCED, "g.md", "markdown")]
    assert headings == ["Guide", "Guide > Real heading"]


def test_tilde_fences_are_honoured():
    source = "# T\n\n~~~\n# inside\n~~~\n\n## After\n"
    assert [path for _, _, path in markdown_sections(source)] == ["T", "T > After"]


def test_content_before_the_first_heading_becomes_a_preamble(chunker):
    source = "Front matter text.\n\n# Later\n\nBody.\n"
    chunks = chunker.chunk(source, "d.md", "markdown")
    assert chunks[0].metadata["heading"] == "(preamble)"
    assert "Front matter text." in chunks[0].content


def test_document_without_headings_is_one_chunk(chunker):
    chunks = chunker.chunk("Just prose.\n\nMore prose.\n", "d.md", "markdown")
    assert len(chunks) == 1
    assert chunks[0].metadata["heading"] == "(preamble)"


def test_long_section_is_windowed_and_repeats_its_heading(chunker):
    body = "\n".join(f"line {n} of a long section" for n in range(200))
    chunks = chunker.chunk(f"# Big\n\n{body}\n", "d.md", "markdown")
    assert len(chunks) > 1
    assert all(len(c.content) <= 1_500 for c in chunks)
    assert [c.metadata["part"] for c in chunks] == [(n + 1, len(chunks)) for n in range(len(chunks))]
    # A later window opens mid-document, so it has to say where it came from.
    assert chunks[1].content.startswith("Big\n")
