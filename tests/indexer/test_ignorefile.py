"""Tests for `.cceignore` parsing + matching."""
from __future__ import annotations

from pathlib import Path

import pytest

from context_engine.indexer.ignorefile import (
    CCEIGNORE_FILENAME,
    IgnoreNames,
    load_ignore_patterns,
    matches_any,
)
from context_engine.config import DEFAULT_IGNORE
from context_engine.indexer.pipeline import _iter_project_files, _SKIP_EXTENSIONS


# ── load_ignore_patterns ───────────────────────────────────────────────────

def test_load_returns_empty_when_file_missing(tmp_path):
    assert load_ignore_patterns(tmp_path) == []


def test_load_strips_blank_and_comment_lines(tmp_path):
    (tmp_path / CCEIGNORE_FILENAME).write_text(
        "\n# this is a comment\n*.log\n\n  build/  \n# trailing comment\n"
    )
    assert load_ignore_patterns(tmp_path) == ["*.log", "build/"]


def test_load_handles_unreadable_file(tmp_path, monkeypatch):
    """An unreadable .cceignore yields [], not a crash."""
    p = tmp_path / CCEIGNORE_FILENAME
    p.write_text("*.log\n")

    def _bad_read(*a, **kw):
        raise OSError("permission denied")
    monkeypatch.setattr(Path, "read_text", _bad_read)
    assert load_ignore_patterns(tmp_path) == []


# ── matches_any (basename patterns) ────────────────────────────────────────

@pytest.mark.parametrize("rel,expected", [
    ("foo.log", True),
    ("subdir/foo.log", True),
    ("deep/nested/path/file.log", True),
    ("foo.txt", False),
    ("logger.py", False),
])
def test_basename_glob(rel, expected):
    assert matches_any(rel, is_dir=False, patterns=["*.log"]) is expected


# ── matches_any (path patterns with slash) ─────────────────────────────────

@pytest.mark.parametrize("rel,expected", [
    ("temp/foo.txt", True),
    # We deliberately deviate from strict gitignore semantics here:
    # `temp/*` recurses (fnmatch's `*` matches across slashes). In
    # practice users want "exclude everything under temp" not "exclude
    # only direct children", so this is the more useful behaviour.
    ("temp/sub/foo.txt", True),
    ("other/foo.txt", False),
    ("temp", False),  # the dir itself isn't matched by `temp/*`
])
def test_path_glob(rel, expected):
    assert matches_any(rel, is_dir=False, patterns=["temp/*"]) is expected


def test_double_star_recursive():
    """`**/build/foo` matches at any depth."""
    p = ["**/build/foo"]
    assert matches_any("build/foo", is_dir=False, patterns=p)
    assert matches_any("src/build/foo", is_dir=False, patterns=p)
    assert matches_any("a/b/c/build/foo", is_dir=False, patterns=p)
    assert not matches_any("build/bar", is_dir=False, patterns=p)


# ── Directory-only patterns (trailing slash) ───────────────────────────────

def test_trailing_slash_only_matches_dirs():
    p = ["build/"]
    assert matches_any("build", is_dir=True, patterns=p)
    # File named `build` is not matched by the dir-only pattern.
    assert not matches_any("build", is_dir=False, patterns=p)
    # Subdir `build` anywhere in the tree.
    assert matches_any("src/build", is_dir=True, patterns=p)


# ── Empty / no-match guards ────────────────────────────────────────────────

def test_empty_patterns_matches_nothing():
    assert not matches_any("anything.txt", is_dir=False, patterns=[])


def test_anchored_leading_slash_is_treated_as_root_relative():
    """Leading `/` is stripped — patterns are root-relative anyway."""
    p = ["/main.py"]
    assert matches_any("main.py", is_dir=False, patterns=p)
    # Not greedy — doesn't match nested `main.py`.
    assert not matches_any("subdir/main.py", is_dir=False, patterns=p)


# ── Pipeline integration ───────────────────────────────────────────────────

def test_pipeline_walk_respects_cceignore(tmp_path):
    """`.cceignore` patterns honour the same exclusion logic as ignore_set."""
    p = tmp_path / "proj"
    p.mkdir()
    (p / "main.py").write_text("def main(): pass\n")
    (p / "trace.log").write_text("noisy\n")
    (p / "scratch").mkdir()
    (p / "scratch" / "data.txt").write_text("temp\n")
    (p / "src").mkdir()
    (p / "src" / "lib.py").write_text("def lib(): pass\n")

    files = list(_iter_project_files(
        p, ignore_set=set(), skip_extensions=_SKIP_EXTENSIONS,
        cceignore_patterns=["*.log", "scratch/"],
    ))
    names = sorted(f.name for f in files)
    assert "main.py" in names
    assert "lib.py" in names
    assert "trace.log" not in names
    assert "data.txt" not in names  # parent dir excluded by `scratch/`


def test_pipeline_no_cceignore_means_no_filter(tmp_path):
    """Empty/missing patterns must not affect the walk."""
    p = tmp_path / "proj"
    p.mkdir()
    (p / "a.py").write_text("a\n")
    (p / "b.log").write_text("b\n")
    files = list(_iter_project_files(
        p, ignore_set=set(), skip_extensions=_SKIP_EXTENSIONS,
        cceignore_patterns=None,
    ))
    assert sorted(f.name for f in files) == ["a.py", "b.log"]


# ── IgnoreNames (the name-only `indexer.ignore` list) ──────────────────────

def test_ignore_names_unanchored_matches_any_component():
    names = IgnoreNames(["node_modules"])
    assert names.matches(("node_modules",))
    assert names.matches(("packages", "web", "node_modules", "x.js"))
    assert not names.matches(("src", "node_modules_shim.js"))


def test_ignore_names_leading_slash_matches_only_at_root():
    names = IgnoreNames(["/storage"])
    assert names.matches(("storage",))
    assert names.matches(("storage", "logs", "laravel.log"))
    assert not names.matches(("lib", "app", "storage", "dets.ex"))


def test_ignore_names_drops_empty_entries():
    names = IgnoreNames(["", "/", "dist"])
    assert names.anywhere == frozenset({"dist"})
    assert names.root_only == frozenset()
    assert not names.matches(())


def test_default_ignore_keeps_nested_storage_source_dir():
    """Regression: `storage` used to be a bare name, so a source directory
    `lib/<app>/storage/` was pruned at any depth and never indexed."""
    names = IgnoreNames(DEFAULT_IGNORE)
    assert not names.matches(("lib", "key_manager", "storage", "dets.ex"))
    assert names.matches(("storage", "framework", "views", "abc.php"))


def test_pipeline_walk_anchors_leading_slash_to_root(tmp_path):
    p = tmp_path / "proj"
    (p / "storage").mkdir(parents=True)
    (p / "storage" / "cache.php").write_text("<?php // compiled\n")
    (p / "lib" / "app" / "storage").mkdir(parents=True)
    (p / "lib" / "app" / "storage" / "dets.ex").write_text("defmodule Dets do\nend\n")

    files = list(_iter_project_files(
        p, ignore_set={"/storage"}, skip_extensions=_SKIP_EXTENSIONS,
    ))
    rel = sorted(str(f.relative_to(p)) for f in files)
    assert rel == ["lib/app/storage/dets.ex"]

    files = list(_iter_project_files(
        p, ignore_set={"storage"}, skip_extensions=_SKIP_EXTENSIONS,
    ))
    assert files == []
