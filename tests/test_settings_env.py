import os, pytest
from archivum.config import Settings

def test_empty_env_var_is_no_directories(monkeypatch):
    """Compose passes TRANSCRIPT_DIRS='' when unset; that must mean 'none'."""
    monkeypatch.setenv("TRANSCRIPT_DIRS", "")
    assert Settings().transcript_dirs == []

def test_a_single_directory_parses(monkeypatch):
    monkeypatch.setenv("TRANSCRIPT_DIRS", "/data/transcripts")
    assert [str(p) for p in Settings().transcript_dirs] == ["/data/transcripts"]

def test_several_directories_parse(monkeypatch):
    monkeypatch.setenv("TRANSCRIPT_DIRS", "/a, /b ,/c")
    assert [str(p) for p in Settings().transcript_dirs] == ["/a", "/b", "/c"]

def test_a_json_list_still_parses(monkeypatch):
    """Do not break anyone who already wrote it the pydantic way."""
    monkeypatch.setenv("TRANSCRIPT_DIRS", '["/a","/b"]')
    assert [str(p) for p in Settings().transcript_dirs] == ["/a", "/b"]

def test_constructing_directly_still_works():
    from pathlib import Path
    assert Settings(transcript_dirs=[Path("/x")]).transcript_dirs == [Path("/x")]
