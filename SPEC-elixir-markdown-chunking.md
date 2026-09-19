# Spec: real Elixir and markdown chunking for CCE

Status: **draft for review — nothing implemented**
Target: fork `lauragrechenko/code-context-engine` @ 0.4.26 (`7507115`)
Measured against: `crypto-key-enclave` (363 `.ex`/`.exs`, 2.6 MB; 17 `.md`, 368 KB)
Date: 2026-09-19

---

## 1. The defect

CCE has no Elixir and no markdown tree-sitter grammar. `Chunker.chunk()`
(`chunker.py:86`) gets `None` from `_get_parser`, so every such file becomes a
single `_fallback_chunk` (`chunker.py:186`): one chunk, `start_line=1`,
`end_line=<real last line>`. The metadata claims full coverage. Nothing warns.

Three limits then cut that chunk down:

| layer | limit | effect |
|---|---|---|
| embedding model `BAAI/bge-small-en-v1.5` | 512 tokens ≈ 1,690 chars | the searchable vector is built from the first ~40 lines only |
| `storage/vector_store.py:20` | `_MAX_CONTENT_CHARS = 5_000` | stored text cut, `...[truncated]` appended |
| `storage/fts_store.py:12` | `_MAX_CONTENT_CHARS = 5_000` | BM25 text cut, no marker |

For `lib/key_manager/enclave.ex` (88 KB) the vector represents ~3% of the file.
A search miss is indistinguishable from "this code does not exist".

**Consequence today:** 363 Elixir files produce 363 chunks. After this patch
they produce 4,896 chunks covering 98.5% of the bytes.

---

## 2. Design problems and the rules that answer them

All rules below were prototyped against the real grammar
(`tree-sitter-elixir` 0.3.5, ABI 14, via codegraph's wasm build) over all 363
files. Numbers are measured, not estimated. Prototypes, runnable and reproducing every number below:
`spec-prototypes/elixir_yield.mjs`, `spec-prototypes/markdown_split.py`.

### 2.1 Elixir is homoiconic — there is no `function_definition` node

Verified AST of `lib/key_manager/sanitize.ex`:

```
source
  call @1-129            "defmodule KeyManager.Sanitize do"
    identifier @1        "defmodule"
    arguments            → alias "KeyManager.Sanitize"
    do_block @1-129
      unary_operator @2-7    "@moduledoc \"\"\""
      call @9               "require Logger"
      call @11              "alias KeyManager.Sanitize.Redact"
      unary_operator @13-15 "@doc \"\"\""
      call @16-21           "def format_status(:gen_server, status) when is_map(status)"
      call @23-30           "def format_status(:gen_state_machine, status) when ..."
      call @33              "def reason_tag(:normal), do: :normal"
      call @34              "def reason_tag({:EXIT, _}), do: :exit"
      call @36-44           "def reason_tag(reason) do"
      call @59-73           "defmodule SanitizeError do"
      comment @114
      call @115-119         "defp unavailable_exit?(reason) do"
```

`def`, `defmodule`, `alias`, `test` are all `call` nodes whose first named
child is an `identifier` carrying that text. `_walk` (`chunker.py:102`) matches
`node.type` against `_FUNCTION_TYPES` / `_CLASS_TYPES` and would match **zero**
nodes — adding the grammar alone changes nothing. Elixir needs its own walk.

**R1 — boundary set.** A module body item is a direct named child of a
`do_block` that is a `call` whose target identifier is:

- def-family: `def defp defmacro defmacrop defguard defguardp defdelegate defn defnp`
- module-family: `defmodule defprotocol defimpl` (recurse)
- **or any other `call` that owns a `do_block`**, unless its target is a
  directive (`alias import require use`)

That last clause is not cosmetic. Without it, ExUnit files have no `def` at
all, so the preamble chunk runs to EOF and a test file stays one giant chunk —
`test/key_manager/http_api/key_derive_resource_test.exs` measured a **78 KB**
"header" chunk. Adding it catches `test`, `describe`, `setup`, `setup_all`,
Ecto `schema`, Phoenix `pipeline`/`scope`, and lifts coverage **91.3% → 99.3%**.

**R2 — `defmodule` never emits a whole-file chunk.** It emits a *preamble*
chunk: the `defmodule` line, `@moduledoc`, `use`/`alias`/`import`/`require`,
module attributes — everything up to the first boundary item.
`ChunkType.MODULE`. This is the chunk that answers "what is this module for".

**R3 — multi-clause merging, size-capped.** Adjacent def-family siblings with
the same `name/arity` merge into one chunk, skipping intervening `comment` and
`unary_operator` (attribute) siblings. Measured: **731 merges** across the repo.

Uncapped merging has a failure mode: `lib/key_manager/enclave.ex` has 42
`handle_event` clauses, which merge into a single **32 KB** chunk. So the merge
stops when the merged span would exceed `2 × _MAX_EMBED_CHARS`; the next clause
starts a new chunk.

**R4 — attachment.** A run of `comment` / `@doc` / `@spec` / `@impl` siblings
immediately before a boundary belongs to that boundary's chunk, not to the
preamble.

**R5 — name and arity.** Unwrap `when` guards first: while the head is a
`binary_operator` whose `operator` field is `when`, descend to its `left`
field. Then `identifier` → arity 0; `call` → arity =
`arguments.namedChildCount`. (Identical to codegraph's `parseFunHead`,
`src/extraction/languages/elixir.ts:273` — a working reference for every rule
in this section.)

**R6 — imports.** `alias`/`import`/`require`/`use` are `call` nodes, so
`_IMPORT_TYPES` (`chunker.py:31`) and `_parse_import_module` (`:149`) miss
them. Elixir branch: target identifier in the directive set → take the first
`arguments` child; `alias` node → its text; `alias A.{B, C}` → expand to
`A.B`, `A.C`.

**R7 — nested modules recurse** (`defmodule SanitizeError` above).

**R8 — oversized containers recurse, functions never do.** A non-def boundary
larger than `2 × _MAX_EMBED_CHARS` that contains further `do_block` calls
(a `describe` holding 20 `test`s) is re-walked one level down. A def-family
chunk is **never** recursed: measured, recursing into functions split them at
their internal `case`/`with`/`if` nodes — those are `call`s with `do_block`s
too — shattering one function into unrelated fragments. Oversized functions are
windowed instead (§2.3), which keeps them contiguous and overlapping.

Coverage with R8 is 98.5%, marginally below the 99.3% of R1 alone: recursing
into a container re-anchors its preamble, leaving the container's own
`describe "..." do` line and trailing `end`s attributed to no chunk. The trade
is worth it — it cuts the largest test-file chunk from 27 KB to 6.7 KB.

### 2.2 Markdown — heading sections, no new grammar

**Recommendation: do not add `tree-sitter-markdown`.** Rationale: the only
boundary markdown chunking needs is a heading, the grammar ships as a split
block+inline parser (two `Language` objects, another ABI surface to keep inside
the `tree-sitter<0.26` cap), and nothing downstream consumes markdown AST
detail. A fence-aware ATX splitter is ~25 lines and already prototyped.

**R9 — sections.** Split at ATX headings (`^#{1,6}\s`), tracking fenced code
blocks (``` and ~~~) so a `# comment` inside a bash fence is not a heading.
Content before the first heading is a `(preamble)` section. Each section is one
chunk, `ChunkType.DOC` → `NodeType.DOC` (`pipeline.py:36`), so markdown stops
landing in the graph as a module.

**R10 — breadcrumb.** Each chunk carries its heading path
(`Documentation Page > 5. Interacting with the API`) in `metadata["heading"]`.
Window parts after the first repeat the breadcrumb as their opening line, so a
fragment still says what it belongs to.

Measured: 17 files → 407 sections → **523 chunks, 99.9% coverage**.

### 2.3 Generic: size-bounded windowing (the language-agnostic half)

`_MAX_EMBED_CHARS = 1_500`, `_OVERLAP_CHARS = 200`. Derived from the model's
512-token cap and CCE's own `Chunk._CHARS_PER_TOKEN_CODE = 3.3` (≈1,690 chars),
with headroom.

Applied **after** chunking, to every chunk from every language including
`_fallback_chunk`: a chunk over the budget is split at line boundaries into
overlapping windows, each keeping true `start_line`/`end_line` and
`metadata["part"] = (i, n)`.

This is the part that has value on its own. It fixes coverage for every
language CCE cannot parse — YAML (74 files, 417 KB, currently 0 graph nodes),
JSON, plaintext — without any grammar work. 84% of Elixir functions fit in one
window and are never split.

Side effect: with nothing exceeding 1,500 chars, the `_MAX_CONTENT_CHARS =
5_000` truncation in both stores becomes unreachable. **No change needed there
— drop it from the follow-up list.**

---

## 3. The patch

### 3.1 New: `context_engine/indexer/chunk_elixir.py` (~200 lines)

```python
"""Elixir chunking. Elixir has no def/module AST nodes — `defmodule`, `def`,
`test` and ordinary calls are all `call` nodes whose first named child is an
identifier carrying that text, so boundaries are matched by target text, not
node type."""

DEF_FUN = {"def", "defp", "defmacro", "defmacrop", "defguard", "defguardp",
           "defdelegate", "defn", "defnp"}
DEF_MOD = {"defmodule", "defprotocol", "defimpl"}
DIRECTIVES = {"alias", "import", "require", "use"}
ATTACHED = {"comment", "unary_operator"}      # comments, @doc, @spec, @impl

def chunk_elixir(root, src_bytes, emit) -> None: ...      # R1, R2, R7
def _walk_block(block, src_bytes, emit, anchor) -> None:  # R1, R3, R4, R8
def _fun_head(call, src_bytes) -> tuple[str, int] | None: # R5
def _unwrap_when(node, src_bytes): ...                    # R5
def elixir_imports(root, src_bytes) -> list[str]: ...     # R6
```

`emit` is a callback taking `(start_byte, end_byte, chunk_type, symbol)` so the
walk never builds `Chunk` objects and stays independently testable.

### 3.2 New: `context_engine/indexer/chunk_markdown.py` (~60 lines)

`chunk_markdown(source) -> list[(start_line, end_line, heading_path)]`.
Fence-aware, no parser, no shared state — thread-safe by construction.
Prototype already written and measured: `spec-prototypes/markdown_split.py`.

### 3.3 `context_engine/indexer/chunker.py`

```python
 import tree_sitter_c_sharp as tscsharp
+import tree_sitter_elixir as tselixir
 from tree_sitter import Language, Parser

+from context_engine.indexer.chunk_elixir import chunk_elixir, elixir_imports
+from context_engine.indexer.chunk_markdown import chunk_markdown
+
+# The embedding model caps at 512 tokens (~1,690 chars at CCE's own
+# 3.3 chars/token). A chunk above this budget is embedded only in part, so
+# split it into overlapping windows rather than storing a vector that
+# represents the first 40 lines of a 2,000-line file.
+_MAX_EMBED_CHARS = 1_500
+_OVERLAP_CHARS = 200

 _LANGUAGES = {
     ...
     "csharp": Language(tscsharp.language()),
+    "elixir": Language(tselixir.language()),
 }
```

In `chunk()`:

```python
 def chunk(self, source, file_path, language):
+    if language == "markdown":
+        return self._split_oversize(self._markdown_chunks(source, file_path))
     parser = self._get_parser(language)
     if parser is None:
-        return [self._fallback_chunk(source, file_path, language)]
+        return self._split_oversize([self._fallback_chunk(source, file_path, language)])
     src_bytes = source.encode("utf-8")
     tree = parser.parse(src_bytes)
     chunks = []
-    self._walk(tree.root_node, src_bytes, file_path, language, chunks)
+    if language == "elixir":
+        chunk_elixir(tree.root_node, src_bytes,
+                     lambda s, e, t, sym: chunks.append(
+                         self._span_to_chunk(s, e, src_bytes, file_path, language, t, sym)))
+    else:
+        self._walk(tree.root_node, src_bytes, file_path, language, chunks)
     if not chunks:
-        return [self._fallback_chunk(source, file_path, language)]
-    return chunks
+        chunks = [self._fallback_chunk(source, file_path, language)]
+    return self._split_oversize(chunks)
```

Plus `_span_to_chunk` (byte span → `Chunk`, symbol into `metadata["symbol"]`)
and `_split_oversize` (§2.3). `_extract_imports` gets an `elixir` branch
delegating to `elixir_imports`.

**Threading contract is preserved.** The Elixir grammar is registered in
`_LANGUAGES` and its `Parser` comes from the existing `threading.local` cache
(`_get_parser`, `:73`) like every other language — no module-level `Parser`, no
shared `TreeCursor`, nothing passed between threads. This is the exact failure
class of issue #113 (and of VectorCode, which segfaulted on 44% of this repo),
so the test suite gets an explicit concurrency case (§4). The markdown path
holds no parser at all.

**Dependency.** `pyproject.toml`: `"tree-sitter-elixir>=0.3.5"`. It declares
`tree-sitter~=0.23` only under its `core` extra, so it does **not** fight the
deliberate `tree-sitter>=0.22,<0.26` cap; abi3 wheels exist for macOS arm64 +
x86_64, manylinux, musllinux and Windows (170 KB). Maintained by the
elixir-lang org. PyPI metadata is inconsistent about the licence (`license`
field Apache-2.0, classifier MIT) — both are permissive; worth one look before
merging.

### 3.4 `context_engine/indexer/pipeline.py` — graph node names

`pipeline.py:680` derives a graph node name by string-slicing chunk content:

```python
node_name = chunk.content.split("(")[0].split(":")[-1].strip() if "(" in chunk.content else chunk.id
```

For Elixir that yields `"def format_status"`. Prefer the real symbol:

```python
-node_name = (
-    chunk.content.split("(")[0].split(":")[-1].strip()
-    if "(" in chunk.content else chunk.id
-)
+node_name = chunk.metadata.get("symbol") or (
+    chunk.content.split("(")[0].split(":")[-1].strip()
+    if "(" in chunk.content else chunk.id
+)
```

Three lines, and CCE's graph gains real `format_status/2` function nodes for
the first time.

**Not in scope:** `metadata["symbol"]` is read by the pipeline before storage
but is *not* persisted — the `chunks` table has no metadata column (only
`modified_ts` is columnised, `vector_store.py:179`). Surfacing the symbol at
search time needs a schema migration; deliberately excluded. `file_path` +
`start_line` already locate the result.

---

## 4. Tests

New `tests/indexer/test_chunker_elixir.py`:

1. `defmodule` with moduledoc + aliases + 3 defs → 1 MODULE + 3 FUNCTION chunks; the module chunk stops at the first `def`.
2. 4 adjacent `def foo/1` clauses → 1 chunk spanning all four.
3. `def foo/1` then `def foo/2` → 2 chunks (arity is part of identity).
4. Clauses separated by `@doc`/comment still merge; the `@doc` lands in the chunk.
5. `when` guard → name/arity unaffected.
6. `def foo(x), do: x` (no `do_block`) is still a boundary.
7. ExUnit file (`use ExUnit.Case` + `describe` + 3 `test`) → no chunk larger than the window; each `test` is its own chunk.
8. Nested `defmodule` → its own MODULE chunk, not folded into the parent.
9. 50 same-name clauses → splits at the 2× cap, not one giant chunk.
10. `alias A.{B, C}` → imports `["A.B", "A.C"]`.
11. Multi-byte source (`"héllo"` before a def) → chunk text is not garbled (the byte-offset contract at `chunker.py:38`).
12. **Concurrency:** 50 threads × `chunk_with_imports` on the same Chunker over distinct Elixir sources → no crash, identical output to serial. Guards issue #113.

New `tests/indexer/test_chunker_markdown.py`:

13. Nested headings → one chunk per heading, breadcrumb correct.
14. `#` inside a fenced block is not a heading.
15. Content before the first heading becomes `(preamble)`.
16. Document with no headings → one chunk, windowed if long.

Extend `tests/indexer/test_chunker.py`:

17. A 10,000-char chunk → windowed with overlap, `start_line`/`end_line` honest, union of windows covers the source.
18. Unknown language (`.yaml`) → windowed fallback, not one whole-file chunk.

---

## 5. Measured impact

| | before | after |
|---|---|---|
| Elixir chunks | 363 (1/file) | **4,896** |
| Elixir bytes reachable by vector search | ~1,800/file (3% of `enclave.ex`) | **98.5%** |
| Elixir preamble / function / block chunks | — | 492 / 2,270 / 1,735 |
| clause runs merged | — | 731 |
| chunks needing a window | — | 364 (7.4%) |
| markdown chunks | 17 | **523** (99.9% coverage) |
| parse errors over 363 files | n/a | **0** |
| total index | ~700 chunks, 16.5 MB | ~6,000 chunks, ~45 MB (est.) |
| full reindex embed time | — | **~4.0 min** (measured: fastembed, 25 chunks/s, 384-dim; one-off, content-hash cached after) |

Largest remaining chunks are merged clause runs, all windowed:
`enclave.ex` 32 KB (42 `handle_event` clauses), `internal_worker.ex` 14.5 KB,
`bitcoin/script/interpreter.ex` 10.8 KB.

---

## 6. Rollout

1. Branch `elixir-markdown-chunking` in the fork; add the dep to
   `pyproject.toml` so no `--with` flag is needed at install time.
2. `uv tool install --force git+https://github.com/lauragrechenko/code-context-engine@elixir-markdown-chunking`
   — this is what makes the fix survive `uv tool upgrade`, which silently wipes
   any site-packages edit.
3. `~/.local/bin/cce-reindex --full` on `crypto-key-enclave` (~4 min).
4. `scripts/index-health.py` — its truncation check will now pass trivially
   (nothing reaches 5,000 chars), so it needs a **new check**: per-file byte
   coverage computed from chunk spans, failing under ~95%. Without that the
   script would report health it no longer measures.
5. Revert the CCE override paragraph in `crypto-key-enclave/CLAUDE.md` once the
   coverage check passes — but keep `codegraph_explore` as the default for
   Elixir call-graph questions; this patch fixes recall, it does not give CCE
   call edges.

**Effort:** 3–5 h, revised down from the 8–10 h in the handoff — the two hard
parts (the Elixir walk, the markdown splitter) are already prototyped and
measured against the whole repo, in `spec-prototypes/`.

**Rollback:** `uv tool install --force code-context-engine` restores 0.4.26 from
PyPI; reindex.

---

## 7. What this does not fix

- **No call edges.** CCE's graph gains function nodes but still has no
  `CALLS` edges for Elixir. "What calls X" stays a codegraph question.
- **No symbol at search time** (§3.4) — needs a `chunks` schema migration.
- **Windowed functions return as fragments.** 364 chunks (7.4%) are parts of a
  larger symbol; the reader sees `file:line` and an overlapping window, not the
  whole function.
- **Manifest hash drift** (63 files, from the prior session) is untouched and
  still unexplained. Not a staleness signal; do not treat it as one.

---

## 8. Decisions needed before implementation

1. **Window 1,500 / overlap 200** — accept, or trade recall for chunk count?
2. **Markdown without `tree-sitter-markdown`** (§2.2) — accept the 25-line
   splitter, or pay for the grammar?
3. **Upstream or fork-only?** codegraph fixes are fork-only by standing rule.
   CCE is a different upstream (`elara-labs/code-context-engine`) and §2.3
   (windowing) plus the pipeline naming fix are general-purpose, not
   Elixir-specific — worth a PR, or keep everything private?
4. **`scripts/index-health.py` home** — repo `scripts/` (untracked today) or
   `~/.local/bin/` beside `cce-reindex`? Step 6.4 adds a check either way.
