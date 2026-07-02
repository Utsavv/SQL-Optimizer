"""Offline tests for the index guardrails (pure parsing + overlap logic)."""
from scripts.guardrails import (
    check_overlap,
    is_left_prefix,
    parse_index_ddl,
)


def test_parse_basic_create_index():
    ddl = "CREATE NONCLUSTERED INDEX IX_T_A ON dbo.T (ColA, ColB DESC) INCLUDE (ColC);"
    p = parse_index_ddl(ddl)
    assert p.name == "IX_T_A"
    assert p.table == "dbo.T"
    assert p.key_columns == ["cola", "colb"]
    assert p.include_columns == ["colc"]


def test_parse_bracketed_and_unique():
    ddl = ("CREATE UNIQUE INDEX [IX Weird Name] ON [Sales].[Order Lines] "
           "([Stock Item ID] ASC)")
    p = parse_index_ddl(ddl)
    assert p.name == "IX Weird Name"
    assert p.table == "Sales.OrderLines"
    assert p.key_columns == ["stock item id"]


def test_parse_returns_none_without_create_index():
    assert parse_index_ddl("ALTER PROCEDURE p AS SELECT 1;") is None
    assert parse_index_ddl("") is None


def test_is_left_prefix():
    assert is_left_prefix(["a"], ["a", "b"])
    assert is_left_prefix(["a", "b"], ["a", "b"])
    assert not is_left_prefix(["b"], ["a", "b"])
    assert not is_left_prefix(["a", "b", "c"], ["a", "b"])
    assert not is_left_prefix([], ["a"])


def test_overlap_rejects_redundant_prefix():
    p = parse_index_ddl("CREATE INDEX IX_new ON dbo.T (ColA);")
    ok, notes = check_overlap(p, {"IX_existing": ["cola", "colb"]})
    assert not ok
    assert "left-prefix" in notes[0]


def test_overlap_rejects_exact_duplicate():
    p = parse_index_ddl("CREATE INDEX IX_new ON dbo.T (ColA, ColB);")
    ok, _ = check_overlap(p, {"IX_existing": ["cola", "colb"]})
    assert not ok


def test_overlap_allows_but_notes_extension():
    p = parse_index_ddl("CREATE INDEX IX_new ON dbo.T (ColA, ColB, ColC);")
    ok, notes = check_overlap(p, {"IX_existing": ["cola", "colb"]})
    assert ok
    assert any("extends existing" in n for n in notes)


def test_overlap_allows_disjoint_keys():
    p = parse_index_ddl("CREATE INDEX IX_new ON dbo.T (ColX);")
    ok, notes = check_overlap(p, {"IX_existing": ["cola", "colb"]})
    assert ok and not notes
