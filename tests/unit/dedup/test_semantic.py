"""
Unit tests for SemanticDeduplicator (SDS §5.7).

**Every threshold assertion uses a constructed vector.** The stub embedder
yields only two similarity values in practice — 1.0000 for identical text and
roughly zero for anything else (measured: a trailing period gives 0.0024, one
character truncated 0.0382, unrelated text -0.0457), because 384-dimensional
Gaussian unit vectors are near-orthogonal by construction. There is no middle
ground to reach by varying text, so 0.92 cannot be exercised that way. §5.7's
testing strategy already prescribes the alternative — "known similar pairs
using mock embeddings (cosine similarity calculated directly)" — so these tests
build vectors at a chosen angle and hand them straight to `is_duplicate()`.
`_vector_at_similarity` does the construction and asserts its own accuracy.

numpy and usearch arrive with the optional `embedding` extra, so the module
skips rather than fails on a `[dev]`-only install — as with the backend tests,
this is one skip standing for the whole module, not one per test.
"""

from __future__ import annotations

from pathlib import Path

import pytest

np = pytest.importorskip(
    "numpy",
    reason="numpy arrives with the optional `embedding` extra; "
    "install with `pip install -e \".[embedding]\"` to run these tests",
)
pytest.importorskip(
    "usearch",
    reason="usearch arrives with the optional `embedding` extra; "
    "install with `pip install -e \".[embedding]\"` to run these tests",
)

from arip.config import DedupSettings  # noqa: E402
from arip.dedup.semantic import SemanticDeduplicator  # noqa: E402

DIM = 8
"""Deliberately not 384.

The width is a constructor argument, so a small one makes the constructed
vectors readable and proves nothing in the class assumes the model's dimension.
"""


# ---------------------------------------------------------------------------
# Vector construction — §5.7 "cosine similarity calculated directly"
# ---------------------------------------------------------------------------


def unit(values: list[float]) -> np.ndarray:
    vector = np.asarray(values, dtype=np.float32)
    return (vector / np.linalg.norm(vector)).astype(np.float32)


def cosine(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b)))


def base_vector() -> np.ndarray:
    """A fixed reference direction. Deterministic, so failures are reproducible."""
    return unit([1.0] + [0.0] * (DIM - 1))


def vector_at_similarity(target: float) -> np.ndarray:
    """A unit vector whose cosine similarity to `base_vector()` is `target`.

    Constructed, not sampled: `target * e0 + sqrt(1 - target^2) * e1` is a unit
    vector whose dot product with `e0` is exactly `target`. This is what lets a
    test sit either side of 0.92 by three decimal places, which no text passed
    through the stub embedder could do.

    **Do not use this to test a threshold boundary exactly.** The vector is
    float32 and normalised, and the similarity that comes back through the index
    is close to `target` but not equal to it — `target=0.92` measures
    0.9200000017881393, which is strictly *above* 0.92 and so satisfies both
    `>=` and `>`. The assertion below allows 1e-6 of slack for that reason.
    Exact equality is only reachable by searching a vector against itself, where
    the distance is exactly 0.0; see
    `test_threshold_is_inclusive_at_exact_equality`.
    """
    orthogonal = float(np.sqrt(max(0.0, 1.0 - target * target)))
    vector = unit([target, orthogonal] + [0.0] * (DIM - 2))
    assert cosine(vector, base_vector()) == pytest.approx(target, abs=1e-6)
    return vector


def settings(tmp_path: Path, **overrides: object) -> DedupSettings:
    base: dict[str, object] = {
        "semantic_threshold": 0.92,
        "ann_index_path": str(tmp_path / "ann_index.usearch"),
        "top_k": 5,
    }
    base.update(overrides)
    return DedupSettings(**base)  # type: ignore[arg-type]


@pytest.fixture
def dedup(tmp_path: Path) -> SemanticDeduplicator:
    """A loaded, empty deduplicator over a temp-file index."""
    deduplicator = SemanticDeduplicator(settings(tmp_path), embedding_dim=DIM)
    deduplicator.load_index()
    return deduplicator


# ---------------------------------------------------------------------------
# The construction helper itself
# ---------------------------------------------------------------------------


def test_constructed_vectors_hit_their_target_similarity() -> None:
    """The helper the threshold tests rest on must be right first."""
    for target in (0.0, 0.5, 0.919, 0.921, 1.0):
        assert cosine(vector_at_similarity(target), base_vector()) == pytest.approx(
            target, abs=1e-6
        )


# ---------------------------------------------------------------------------
# Threshold behaviour — the point of the class
# ---------------------------------------------------------------------------


def test_similarity_above_threshold_is_a_duplicate(dedup: SemanticDeduplicator) -> None:
    dedup.add(1, base_vector())

    is_dup, neighbour = dedup.is_duplicate(vector_at_similarity(0.95), item_id=2)

    assert is_dup is True
    assert neighbour == 1


def test_similarity_below_threshold_is_not_a_duplicate(dedup: SemanticDeduplicator) -> None:
    dedup.add(1, base_vector())

    is_dup, neighbour = dedup.is_duplicate(vector_at_similarity(0.80), item_id=2)

    assert is_dup is False
    assert neighbour == 1, "a near miss still reports the nearest neighbour"


def test_similarity_marginally_above_the_threshold_is_a_duplicate(
    dedup: SemanticDeduplicator,
) -> None:
    """A hair above 0.92 is a duplicate — and this test does not pin the operator.

    `vector_at_similarity(0.92)` cannot land exactly on the threshold: the
    vector is float32, and the value that comes back through the index is
    0.9200000017881393 — strictly greater than 0.92. So this case passes under
    both `>=` and `>`, and an earlier version of this test claimed to prove
    inclusivity while proving nothing.

    It is still worth keeping as the "just above the boundary" case. The
    operator itself is pinned by
    `test_threshold_is_inclusive_at_exact_equality`, which is the only
    construction where equality is exact.
    """
    dedup.add(1, base_vector())

    is_dup, _ = dedup.is_duplicate(vector_at_similarity(0.92), item_id=2)

    assert is_dup is True


def test_threshold_is_inclusive_at_exact_equality(tmp_path: Path) -> None:
    """`>=`, not `>`. The one construction where equality is exact.

    An identical vector searched against itself returns distance exactly 0.0 —
    measured, not assumed, and true for both axis-aligned and arbitrary
    directions — so `similarity` is exactly 1.0 with no float32 slack. Running
    that against a threshold of 1.0 is therefore the only comparison in the
    suite that can distinguish the two operators:

        `>=` -> duplicate        `>` -> not a duplicate

    A different `item_id` is used for the query so that self-exclusion does not
    discard the hit before the threshold is ever applied.

    §5.7 and §3.3 both specify "≥" (`cosine similarity ... >= config.dedup.
    semantic_threshold`). Without this test that operator is unpinned: changing
    it to `>` leaves every other test in this file passing.
    """
    dedup = SemanticDeduplicator(
        settings(tmp_path, semantic_threshold=1.0), embedding_dim=DIM
    )
    dedup.load_index()
    dedup.add(1, base_vector())

    is_dup, neighbour = dedup.is_duplicate(base_vector(), item_id=2)

    assert is_dup is True, "similarity == threshold must count as a duplicate (>=, not >)"
    assert neighbour == 1


def test_just_below_threshold_is_not_a_duplicate(dedup: SemanticDeduplicator) -> None:
    """0.919 vs 0.921 — three decimal places either side of the boundary.

    This is the assertion the stub embedder cannot produce: its vectors are
    either identical or near-orthogonal, with nothing in between.
    """
    dedup.add(1, base_vector())

    below, _ = dedup.is_duplicate(vector_at_similarity(0.919), item_id=2)
    above, _ = dedup.is_duplicate(vector_at_similarity(0.921), item_id=3)

    assert below is False
    assert above is True


def test_threshold_comes_from_configuration(tmp_path: Path) -> None:
    """A run configured at 0.50 must treat 0.60 as a duplicate."""
    dedup = SemanticDeduplicator(
        settings(tmp_path, semantic_threshold=0.50), embedding_dim=DIM
    )
    dedup.load_index()
    dedup.add(1, base_vector())

    is_dup, _ = dedup.is_duplicate(vector_at_similarity(0.60), item_id=2)

    assert is_dup is True
    assert dedup.threshold == 0.50


def test_orthogonal_vectors_are_never_duplicates(dedup: SemanticDeduplicator) -> None:
    dedup.add(1, base_vector())

    is_dup, _ = dedup.is_duplicate(vector_at_similarity(0.0), item_id=2)

    assert is_dup is False


# ---------------------------------------------------------------------------
# Self-exclusion — the ruling
# ---------------------------------------------------------------------------


def test_an_item_is_never_its_own_duplicate(dedup: SemanticDeduplicator) -> None:
    """The crash-recovery case.

    A crash between the database commit and save_index() leaves the item in the
    database; reconciliation re-adds it on the next run. Without self-exclusion
    the item then matches itself at similarity 1.0 and is marked a duplicate of
    itself — silently, and permanently, since DUPLICATE is terminal.
    """
    vector = base_vector()
    dedup.add(42, vector)

    is_dup, neighbour = dedup.is_duplicate(vector, item_id=42)

    assert is_dup is False
    assert neighbour is None


def test_self_exclusion_still_finds_a_real_duplicate(dedup: SemanticDeduplicator) -> None:
    """Excluding the self-hit must not discard the genuine neighbour behind it.

    This is what `top_k` buys: the self-match takes the closest slot, and the
    real duplicate is the next one down. With `top_k = 1` this item would be
    reported as unique.
    """
    dedup.add(42, base_vector())
    dedup.add(7, vector_at_similarity(0.98))

    is_dup, neighbour = dedup.is_duplicate(base_vector(), item_id=42)

    assert is_dup is True
    assert neighbour == 7


def test_self_exclusion_only_skips_the_matching_id(dedup: SemanticDeduplicator) -> None:
    """A different id with an identical vector is still a duplicate."""
    dedup.add(1, base_vector())

    is_dup, neighbour = dedup.is_duplicate(base_vector(), item_id=2)

    assert is_dup is True
    assert neighbour == 1


# ---------------------------------------------------------------------------
# Empty and near-empty index
# ---------------------------------------------------------------------------


def test_empty_index_reports_no_duplicate(dedup: SemanticDeduplicator) -> None:
    """The first item of the first run has nothing to be a duplicate of."""
    is_dup, neighbour = dedup.is_duplicate(base_vector(), item_id=1)

    assert is_dup is False
    assert neighbour is None


def test_index_holding_only_the_item_itself_reports_no_neighbour(
    dedup: SemanticDeduplicator,
) -> None:
    dedup.add(1, base_vector())

    assert dedup.is_duplicate(base_vector(), item_id=1) == (False, None)


def test_top_k_larger_than_the_index_is_not_an_error(tmp_path: Path) -> None:
    """top_k defaults to 5; a first run has fewer than five vectors."""
    dedup = SemanticDeduplicator(settings(tmp_path, top_k=50), embedding_dim=DIM)
    dedup.load_index()
    dedup.add(1, base_vector())

    is_dup, neighbour = dedup.is_duplicate(vector_at_similarity(0.99), item_id=2)

    assert (is_dup, neighbour) == (True, 1)


# ---------------------------------------------------------------------------
# add()
# ---------------------------------------------------------------------------


def test_add_grows_the_index(dedup: SemanticDeduplicator) -> None:
    assert dedup.size == 0

    dedup.add(1, base_vector())
    dedup.add(2, vector_at_similarity(0.1))

    assert dedup.size == 2


def test_add_is_idempotent_for_a_known_id(dedup: SemanticDeduplicator) -> None:
    """usearch rejects a duplicate key outright; reconciliation revisits items.

    Without this guard, a reconciliation pass over an index that already holds
    the item would raise and end the run.
    """
    dedup.add(1, base_vector())
    dedup.add(1, base_vector())

    assert dedup.size == 1


def test_contains_reports_membership(dedup: SemanticDeduplicator) -> None:
    """§5.7 reconciliation: "check if item_id is in the loaded index"."""
    dedup.add(1, base_vector())

    assert dedup.contains(1) is True
    assert dedup.contains(2) is False


# ---------------------------------------------------------------------------
# Persistence — §5.7 "Test index save/load round-trip"
# ---------------------------------------------------------------------------


def test_save_and_load_round_trip(tmp_path: Path) -> None:
    first = SemanticDeduplicator(settings(tmp_path), embedding_dim=DIM)
    first.load_index()
    first.add(1, base_vector())
    first.add(2, vector_at_similarity(0.3))
    first.save_index()

    second = SemanticDeduplicator(settings(tmp_path), embedding_dim=DIM)
    second.load_index()

    assert second.size == 2
    assert second.contains(1)
    assert second.contains(2)


def test_a_reloaded_index_still_detects_duplicates(tmp_path: Path) -> None:
    """The round trip has to preserve the vectors, not just the keys."""
    first = SemanticDeduplicator(settings(tmp_path), embedding_dim=DIM)
    first.load_index()
    first.add(1, base_vector())
    first.save_index()

    second = SemanticDeduplicator(settings(tmp_path), embedding_dim=DIM)
    second.load_index()

    assert second.is_duplicate(vector_at_similarity(0.99), item_id=2) == (True, 1)


def test_load_creates_a_new_index_when_no_file_exists(tmp_path: Path) -> None:
    """§5.7: "loads from disk or creates new". First run is not an error."""
    path = tmp_path / "absent.usearch"
    dedup = SemanticDeduplicator(
        settings(tmp_path, ann_index_path=str(path)), embedding_dim=DIM
    )

    dedup.load_index()

    assert dedup.size == 0
    assert not path.exists(), "creating an index must not write a file"


def test_save_creates_the_parent_directory(tmp_path: Path) -> None:
    """container.py makes data/ at startup, but tests and operators need this too."""
    path = tmp_path / "nested" / "deeper" / "ann_index.usearch"
    dedup = SemanticDeduplicator(
        settings(tmp_path, ann_index_path=str(path)), embedding_dim=DIM
    )
    dedup.load_index()
    dedup.add(1, base_vector())

    dedup.save_index()

    assert path.exists()


def test_save_leaves_no_temporary_file(tmp_path: Path) -> None:
    """The atomic write must move its temp file, not leave it beside the index."""
    dedup = SemanticDeduplicator(settings(tmp_path), embedding_dim=DIM)
    dedup.load_index()
    dedup.add(1, base_vector())

    dedup.save_index()

    assert [p.name for p in tmp_path.iterdir()] == ["ann_index.usearch"]


def test_save_overwrites_a_previous_index(tmp_path: Path) -> None:
    dedup = SemanticDeduplicator(settings(tmp_path), embedding_dim=DIM)
    dedup.load_index()
    dedup.add(1, base_vector())
    dedup.save_index()
    dedup.add(2, vector_at_similarity(0.2))
    dedup.save_index()

    reloaded = SemanticDeduplicator(settings(tmp_path), embedding_dim=DIM)
    reloaded.load_index()

    assert reloaded.size == 2


# ---------------------------------------------------------------------------
# Failure modes — §5.7
# ---------------------------------------------------------------------------


def test_corrupt_index_file_is_deleted_and_rebuilt(tmp_path: Path) -> None:
    """§5.7: "ANN index file corrupted: delete file and rebuild from scratch"."""
    path = tmp_path / "ann_index.usearch"
    path.write_bytes(b"this is not a usearch index")
    dedup = SemanticDeduplicator(settings(tmp_path), embedding_dim=DIM)

    dedup.load_index()

    assert dedup.size == 0
    assert not path.exists(), "the unusable file must be removed, not left to fail again"


def test_a_rebuilt_index_is_usable(tmp_path: Path) -> None:
    """Recovery has to leave a working deduplicator, not just avoid raising."""
    (tmp_path / "ann_index.usearch").write_bytes(b"garbage")
    dedup = SemanticDeduplicator(settings(tmp_path), embedding_dim=DIM)
    dedup.load_index()

    dedup.add(1, base_vector())

    assert dedup.is_duplicate(base_vector(), item_id=2) == (True, 1)


def test_index_of_the_wrong_width_is_rebuilt(tmp_path: Path) -> None:
    """usearch adopts the file's ndim silently; the mismatch must not survive.

    Left alone, every later search would raise ValueError, §5.7's
    malformed-vector rule would swallow each one, and the run would find no
    duplicates while reporting nothing wrong.
    """
    narrow = SemanticDeduplicator(settings(tmp_path), embedding_dim=4)
    narrow.load_index()
    narrow.add(1, unit([1.0, 0.0, 0.0, 0.0]))
    narrow.save_index()

    wide = SemanticDeduplicator(settings(tmp_path), embedding_dim=DIM)
    wide.load_index()

    assert wide.size == 0
    wide.add(2, base_vector())
    assert wide.is_duplicate(base_vector(), item_id=3) == (True, 2)


def test_malformed_vector_skips_the_check_rather_than_raising(
    dedup: SemanticDeduplicator,
) -> None:
    """§5.7: "log, skip semantic check, continue with exact check only"."""
    dedup.add(1, base_vector())

    is_dup, neighbour = dedup.is_duplicate(np.ones(3, dtype=np.float32), item_id=2)

    assert is_dup is False
    assert neighbour is None


def test_malformed_vector_does_not_stop_a_later_item(dedup: SemanticDeduplicator) -> None:
    """"Continue" means the run goes on, not that the deduplicator is spent."""
    dedup.add(1, base_vector())
    dedup.is_duplicate(np.ones(3, dtype=np.float32), item_id=2)

    assert dedup.is_duplicate(base_vector(), item_id=3) == (True, 1)


def test_malformed_vector_is_not_added(dedup: SemanticDeduplicator) -> None:
    dedup.add(1, np.ones(3, dtype=np.float32))

    assert dedup.size == 0


# ---------------------------------------------------------------------------
# Lifecycle guards
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("call", "name"),
    [
        (lambda d: d.is_duplicate(base_vector(), 1), "is_duplicate"),
        (lambda d: d.add(1, base_vector()), "add"),
        (lambda d: d.save_index(), "save_index"),
        (lambda d: d.contains(1), "contains"),
        (lambda d: d.size, "size"),
    ],
)
def test_use_before_load_index_raises(tmp_path: Path, call, name: str) -> None:  # noqa: ANN001
    """Silence here would look like "no duplicates found" for a whole run."""
    dedup = SemanticDeduplicator(settings(tmp_path), embedding_dim=DIM)

    with pytest.raises(RuntimeError, match="load_index"):
        call(dedup)


def test_load_index_is_repeatable(tmp_path: Path) -> None:
    """Reconciliation and a re-run must not need a fresh object."""
    dedup = SemanticDeduplicator(settings(tmp_path), embedding_dim=DIM)
    dedup.load_index()
    dedup.add(1, base_vector())
    dedup.save_index()

    dedup.load_index()

    assert dedup.size == 1


# ---------------------------------------------------------------------------
# Optional-dependency discipline
# ---------------------------------------------------------------------------


def test_semantic_module_does_not_import_usearch_at_module_level() -> None:
    """Same rule as the embedding backends, for the same reason."""
    from arip.dedup import semantic as semantic_module

    assert "usearch" not in vars(semantic_module)
    assert "Index" not in vars(semantic_module)
    assert "np" not in vars(semantic_module)
    assert "numpy" not in vars(semantic_module)


def test_dedup_package_init_imports_nothing() -> None:
    """`import arip.dedup` must not drag in the optional extra.

    The package __init__ is documentation only; anything it imported would run
    for every caller, including installs without the `embedding` extra. Unlike
    `arip.backends.embeddings`, this package has no registry and so needs no
    import-for-registration.

    Asserted against the source, not against `vars(arip.dedup)`: Python binds a
    submodule onto its parent package the first time anything imports it, so
    `hasattr(arip.dedup, "semantic")` is true here purely because this test
    module imported it at the top, and would prove nothing either way.
    """
    import ast
    import pathlib

    import arip.dedup

    source = pathlib.Path(arip.dedup.__file__).read_text(encoding="utf-8")
    imports = [
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Import | ast.ImportFrom)
    ]

    assert imports == []
    assert not hasattr(arip.dedup, "SemanticDeduplicator"), "no re-export"
