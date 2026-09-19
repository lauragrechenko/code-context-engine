# tests/indexer/test_chunker_elixir.py
"""Elixir chunking — see context_engine/indexer/chunk_elixir.py.

Every boundary rule here exists because the naive reading fails on real code:
Elixir has no function/module AST nodes, so all of `def`, `defmodule`, `alias`
and `test` arrive as generic `call` nodes.
"""
import concurrent.futures

import pytest

from context_engine.indexer.chunker import Chunker
from context_engine.models import ChunkType


@pytest.fixture
def chunker():
    return Chunker()


def chunk(chunker, source, path="lib/demo.ex"):
    return chunker.chunk(source, file_path=path, language="elixir")


def symbols(chunks):
    return [c.metadata.get("symbol") for c in chunks]


MODULE = '''defmodule Demo.Thing do
  @moduledoc """
  What the thing is for.
  """

  require Logger
  alias Demo.Other

  @doc "Adds."
  def add(a, b) do
    a + b
  end

  defp helper(x) do
    Other.call(x)
  end
end
'''


def test_module_preamble_stops_at_the_first_definition(chunker):
    chunks = chunk(chunker, MODULE)
    preamble = chunks[0]
    assert preamble.chunk_type == ChunkType.MODULE
    assert preamble.metadata["symbol"] == "Demo.Thing"
    assert "@moduledoc" in preamble.content
    assert "alias Demo.Other" in preamble.content
    assert "def add" not in preamble.content


def test_definitions_become_their_own_chunks(chunker):
    chunks = chunk(chunker, MODULE)
    assert symbols(chunks) == ["Demo.Thing", "Demo.Thing.add/2", "Demo.Thing.helper/1"]
    assert all(c.chunk_type == ChunkType.FUNCTION for c in chunks[1:])


def test_doc_attribute_travels_with_the_function_it_documents(chunker):
    add = chunk(chunker, MODULE)[1]
    assert add.content.startswith('@doc "Adds."')


ADJACENT_CLAUSES = '''defmodule Demo.Clauses do
  def handle(:a), do: 1
  def handle(:b), do: 2
  # a comment between clauses
  @doc false
  def handle(:c), do: 3
  def handle(x, y), do: {x, y}
end
'''


def test_adjacent_clauses_of_one_function_merge(chunker):
    chunks = chunk(chunker, ADJACENT_CLAUSES)
    assert symbols(chunks) == ["Demo.Clauses", "Demo.Clauses.handle/1", "Demo.Clauses.handle/2"]
    merged = chunks[1]
    for clause in (":a", ":b", ":c"):
        assert clause in merged.content


def test_clause_merging_stops_at_the_span_cap(chunker):
    body = "\n".join(f'  def big({n}), do: "{"x" * 200}"' for n in range(40))
    chunks = chunk(chunker, f"defmodule Demo.Big do\n{body}\nend\n")
    big = [c for c in chunks if c.metadata.get("symbol") == "Demo.Big.big/1"]
    assert len(big) > 1, "40 clauses must not collapse into one oversized chunk"
    assert all(len(c.content) <= 3_000 for c in big)


GUARDS = '''defmodule Demo.Guards do
  def parse(value) when is_binary(value) and byte_size(value) > 0 do
    value
  end

  def parse(value) when is_integer(value) do
    Integer.to_string(value)
  end

  defp noargs, do: :ok
end
'''


def test_when_guards_do_not_change_name_or_arity(chunker):
    assert symbols(chunk(chunker, GUARDS)) == [
        "Demo.Guards", "Demo.Guards.parse/1", "Demo.Guards.noargs/0",
    ]


EXUNIT = '''defmodule Demo.ThingTest do
  use ExUnit.Case, async: true

  setup do
    {:ok, %{}}
  end

  describe "add/2" do
    test "adds two numbers" do
      assert Demo.Thing.add(1, 2) == 3
    end

    test "is commutative" do
      assert Demo.Thing.add(1, 2) == Demo.Thing.add(2, 1)
    end
  end
end
'''


def test_exunit_blocks_are_boundaries_even_though_they_are_not_defs(chunker):
    # Without this rule a test file has no `def` at all, so the whole file
    # stays one chunk — measured at 78 KB on a real suite.
    labels = symbols(chunk(chunker, EXUNIT, path="test/thing_test.exs"))
    assert labels == [
        "Demo.ThingTest", "Demo.ThingTest.setup", 'Demo.ThingTest.describe "add/2"',
    ]


def test_an_oversized_describe_splits_into_its_tests(chunker):
    cases = "\n\n".join(
        f'    test "case {n}" do\n      assert {n} == {n}\n      # {"pad " * 60}\n    end'
        for n in range(12)
    )
    source = (
        "defmodule Demo.BigTest do\n  use ExUnit.Case\n\n"
        f'  describe "a big group" do\n{cases}\n  end\nend\n'
    )
    labels = symbols(chunk(chunker, source, path="test/big_test.exs"))
    assert 'Demo.BigTest.describe "a big group"' in labels
    for n in range(12):
        assert any(label.endswith(f'test "case {n}"') for label in labels)


def test_use_directive_stays_in_the_preamble(chunker):
    preamble = chunk(chunker, EXUNIT, path="test/thing_test.exs")[0]
    assert "use ExUnit.Case" in preamble.content


NESTED = '''defmodule Demo.Outer do
  @moduledoc "outer"

  defmodule Inner do
    defexception [:message]

    def message(%{message: m}), do: m
  end

  def outer_fun, do: :ok
end
'''


def test_nested_modules_get_their_own_chunks(chunker):
    labels = symbols(chunk(chunker, NESTED))
    assert labels == [
        "Demo.Outer", "Demo.Outer.Inner", "Demo.Outer.Inner.message/1", "Demo.Outer.outer_fun/0",
    ]


def test_module_attributes_stay_out_of_the_next_function(chunker):
    source = (
        "defmodule Demo.Attrs do\n"
        "  def first, do: 1\n\n"
        "  @timeout 5_000\n"
        "  @behaviour GenServer\n\n"
        "  def second, do: @timeout\n"
        "end\n"
    )
    chunks = chunk(chunker, source)
    second = next(c for c in chunks if c.metadata.get("symbol") == "Demo.Attrs.second/0")
    assert "@timeout 5_000" not in second.content
    assert any("@timeout 5_000" in c.content for c in chunks), "attributes must not be dropped"


def test_script_without_a_module_still_chunks_its_top_level(chunker):
    source = (
        "import Config\n\n"
        "config :app, key: 1\n\n"
        "defmodule Helper do\n"
        "  def value, do: 2\n"
        "end\n\n"
        "config :app, other: Helper.value()\n"
    )
    chunks = chunk(chunker, source, path="config/runtime.exs")
    joined = "\n".join(c.content for c in chunks)
    assert "config :app, key: 1" in joined
    assert "config :app, other: Helper.value()" in joined
    assert "Helper.value/0" in symbols(chunks)


MULTIBYTE = '''defmodule Demo.Unicode do
  @moduledoc "Größe — 日本語 🚀"

  def label, do: "façade"
end
'''


def test_multibyte_source_is_not_garbled(chunker):
    chunks = chunk(chunker, MULTIBYTE)
    assert 'def label, do: "façade"' in chunks[-1].content
    assert "日本語 🚀" in chunks[0].content


IMPORTS = '''defmodule Demo.Imports do
  use ExUnit.Case
  require Logger
  alias Demo.DB.{Interface, Queries}
  alias Demo.Long.Name, as: Short
  import Demo.Helpers, only: [help: 1]

  def go, do: :ok
end
'''


def test_directives_are_extracted_as_imports(chunker):
    _, imports = chunker.chunk_with_imports(IMPORTS, "lib/imports.ex", "elixir")
    assert imports == [
        "ExUnit.Case", "Logger", "Demo.DB.Interface", "Demo.DB.Queries",
        "Demo.Long.Name", "Demo.Helpers",
    ]


def test_chunking_is_thread_safe():
    # One Chunker is shared across the run and the pipeline fans ~50 files out
    # over asyncio.to_thread. Sharing a tree-sitter Parser across threads
    # corrupted memory in issue #113, so every language must go through the
    # threading.local cache.
    chunker = Chunker()
    sources = [
        MODULE.replace("Demo.Thing", f"Demo.Thing{n}") for n in range(50)
    ]
    expected = [chunker.chunk(s, f"lib/m{n}.ex", "elixir") for n, s in enumerate(sources)]
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        got = list(pool.map(
            lambda pair: chunker.chunk(pair[1], f"lib/m{pair[0]}.ex", "elixir"),
            enumerate(sources),
        ))
    assert [[c.content for c in one] for one in got] == \
           [[c.content for c in one] for one in expected]
