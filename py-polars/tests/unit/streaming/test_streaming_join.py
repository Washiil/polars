from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Literal

import numpy as np
import pandas as pd
import pytest

import polars as pl
from polars.testing import assert_frame_equal, assert_series_equal

if TYPE_CHECKING:
    from pathlib import Path

    from polars._typing import JoinStrategy

pytestmark = pytest.mark.xdist_group("streaming")


def test_streaming_full_outer_joins() -> None:
    n = 100
    dfa = pl.DataFrame(
        {
            "a": np.random.randint(0, 40, n),
            "idx": np.arange(0, n),
        }
    )

    n = 100
    dfb = pl.DataFrame(
        {
            "a": np.random.randint(0, 40, n),
            "idx": np.arange(0, n),
        }
    )

    join_strategies: list[tuple[JoinStrategy, bool]] = [
        ("full", False),
        ("full", True),
    ]
    for how, coalesce in join_strategies:
        q = (
            dfa.lazy()
            .join(dfb.lazy(), on="a", how=how, coalesce=coalesce)
            .sort(["idx"])
        )
        a = q.collect(engine="streaming")
        b = q.collect(engine="in-memory")
        assert_frame_equal(a, b, check_row_order=False)


def test_streaming_joins() -> None:
    n = 100
    dfa = pd.DataFrame(
        {
            "a": np.random.randint(0, 40, n),
            "b": np.arange(0, n),
        }
    )

    n = 100
    dfb = pd.DataFrame(
        {
            "a": np.random.randint(0, 40, n),
            "b": np.arange(0, n),
        }
    )
    dfa_pl = pl.from_pandas(dfa).sort("a")
    dfb_pl = pl.from_pandas(dfb)

    join_strategies: list[Literal["inner", "left"]] = ["inner", "left"]
    for how in join_strategies:
        pd_result = dfa.merge(dfb, on="a", how=how)
        pd_result.columns = pd.Index(["a", "b", "b_right"])

        pl_result = (
            dfa_pl.lazy()
            .join(dfb_pl.lazy(), on="a", how=how)
            .sort(["a", "b", "b_right"])
            .collect(engine="streaming")
        )

        a = (
            pl.from_pandas(pd_result)
            .with_columns(pl.all().cast(int))
            .sort(["a", "b", "b_right"])
        )
        assert_frame_equal(a, pl_result, check_dtypes=False)

        pd_result = dfa.merge(dfb, on=["a", "b"], how=how)

        pl_result = (
            dfa_pl.lazy()
            .join(dfb_pl.lazy(), on=["a", "b"], how=how)
            .sort(["a", "b"])
            .collect(engine="streaming")
        )

        # we cast to integer because pandas joins creates floats
        a = pl.from_pandas(pd_result).with_columns(pl.all().cast(int)).sort(["a", "b"])
        assert_frame_equal(a, pl_result, check_dtypes=False)


def test_streaming_cross_join_empty() -> None:
    df1 = pl.LazyFrame(data={"col1": ["a"]})

    df2 = pl.LazyFrame(
        data={"col1": []},
        schema={"col1": str},
    )

    out = df1.join(df2, how="cross").collect(engine="streaming")
    assert out.shape == (0, 2)
    assert out.columns == ["col1", "col1_right"]


def test_streaming_join_rechunk_12498() -> None:
    rows = pl.int_range(0, 2)

    a = pl.select(A=rows).lazy()
    b = pl.select(B=rows).lazy()

    q = a.join(b, how="cross")
    assert q.collect(engine="streaming").sort(["B", "A"]).to_dict(as_series=False) == {
        "A": [0, 1, 0, 1],
        "B": [0, 0, 1, 1],
    }


@pytest.mark.parametrize("maintain_order", [False, True])
def test_join_null_matches(maintain_order: bool) -> None:
    # null values in joins should never find a match.
    df_a = pl.LazyFrame(
        {
            "idx_a": [0, 1, 2],
            "a": [None, 1, 2],
        }
    )

    df_b = pl.LazyFrame(
        {
            "idx_b": [0, 1, 2, 3],
            "a": [None, 2, 1, None],
        }
    )
    # Semi
    assert_series_equal(
        df_a.join(
            df_b,
            on="a",
            how="semi",
            nulls_equal=True,
            maintain_order="left" if maintain_order else "none",
        ).collect()["idx_a"],
        pl.Series("idx_a", [0, 1, 2]),
        check_order=maintain_order,
    )
    assert_series_equal(
        df_a.join(
            df_b,
            on="a",
            how="semi",
            nulls_equal=False,
            maintain_order="left" if maintain_order else "none",
        ).collect()["idx_a"],
        pl.Series("idx_a", [1, 2]),
        check_order=maintain_order,
    )

    # Inner
    expected = pl.DataFrame({"idx_a": [2, 1], "a": [2, 1], "idx_b": [1, 2]})
    assert_frame_equal(
        df_a.join(
            df_b,
            on="a",
            how="inner",
            maintain_order="right" if maintain_order else "none",
        ).collect(),
        expected,
        check_row_order=maintain_order,
    )

    # Left outer
    expected = pl.DataFrame(
        {"idx_a": [0, 1, 2], "a": [None, 1, 2], "idx_b": [None, 2, 1]}
    )
    assert_frame_equal(
        df_a.join(
            df_b,
            on="a",
            how="left",
            maintain_order="left" if maintain_order else "none",
        ).collect(),
        expected,
        check_row_order=maintain_order,
    )
    # Full outer
    expected = pl.DataFrame(
        {
            "idx_a": [None, 2, 1, None, 0],
            "a": [None, 2, 1, None, None],
            "idx_b": [0, 1, 2, 3, None],
            "a_right": [None, 2, 1, None, None],
        }
    )
    assert_frame_equal(
        df_a.join(
            df_b,
            on="a",
            how="full",
            maintain_order="right" if maintain_order else "none",
        ).collect(),
        expected,
        check_row_order=maintain_order,
    )


@pytest.mark.parametrize("streaming", [False, True])
def test_join_null_matches_multiple_keys(streaming: bool) -> None:
    df_a = pl.LazyFrame(
        {
            "a": [None, 1, 2],
            "idx": [0, 1, 2],
        }
    )

    df_b = pl.LazyFrame(
        {
            "a": [None, 2, 1, None, 1],
            "idx": [0, 1, 2, 3, 1],
            "c": [10, 20, 30, 40, 50],
        }
    )

    expected = pl.DataFrame({"a": [1], "idx": [1], "c": [50]})
    assert_frame_equal(
        df_a.join(df_b, on=["a", "idx"], how="inner").collect(
            engine="streaming" if streaming else "in-memory"
        ),
        expected,
        check_row_order=False,
    )
    expected = pl.DataFrame(
        {"a": [None, 1, 2], "idx": [0, 1, 2], "c": [None, 50, None]}
    )
    assert_frame_equal(
        df_a.join(df_b, on=["a", "idx"], how="left").collect(
            engine="streaming" if streaming else "in-memory"
        ),
        expected,
        check_row_order=False,
    )

    expected = pl.DataFrame(
        {
            "a": [None, None, None, None, None, 1, 2],
            "idx": [None, None, None, None, 0, 1, 2],
            "a_right": [None, 2, 1, None, None, 1, None],
            "idx_right": [0, 1, 2, 3, None, 1, None],
            "c": [10, 20, 30, 40, None, 50, None],
        }
    )
    assert_frame_equal(
        df_a.join(df_b, on=["a", "idx"], how="full").sort("a").collect(),
        expected,
        check_row_order=False,
    )


def test_streaming_join_and_union() -> None:
    a = pl.LazyFrame({"a": [1, 2]})

    b = pl.LazyFrame({"a": [1, 2, 4, 8]})

    c = a.join(b, on="a", maintain_order="left_right")
    # The join node latest ensures that the dispatcher
    # needs to replace placeholders in unions.
    q = pl.concat([a, b, c])

    out = q.collect(engine="streaming")
    assert_frame_equal(out, q.collect(engine="in-memory"))
    assert out.to_series().to_list() == [1, 2, 1, 2, 4, 8, 1, 2]


def test_non_coalescing_streaming_left_join() -> None:
    df1 = pl.LazyFrame({"a": [1, 2, 3], "b": ["a", "b", "c"]})

    df2 = pl.LazyFrame({"a": [1, 2], "c": ["j", "i"]})

    q = df1.join(df2, on="a", how="left", coalesce=False)
    assert_frame_equal(
        q.collect(engine="streaming"),
        pl.DataFrame(
            {
                "a": [1, 2, 3],
                "b": ["a", "b", "c"],
                "a_right": [1, 2, None],
                "c": ["j", "i", None],
            }
        ),
        check_row_order=False,
    )


@pytest.mark.write_disk
def test_streaming_outer_join_partial_flush(tmp_path: Path) -> None:
    data = {
        "value_at": [datetime(2024, i + 1, 1) for i in range(6)],
        "value": list(range(6)),
    }

    parquet_path = tmp_path / "data.parquet"
    pl.DataFrame(data=data).write_parquet(parquet_path)

    other_parquet_path = tmp_path / "data2.parquet"
    pl.DataFrame(data=data).write_parquet(other_parquet_path)

    lf1 = pl.scan_parquet(other_parquet_path)
    lf2 = pl.scan_parquet(parquet_path)

    join_cols = set(lf1.collect_schema()).intersection(set(lf2.collect_schema()))
    final_lf = lf1.join(lf2, on=list(join_cols), how="full", coalesce=True)

    assert_frame_equal(
        final_lf.collect(engine="streaming"),
        pl.DataFrame(
            {
                "value_at": [
                    datetime(2024, 1, 1, 0, 0),
                    datetime(2024, 2, 1, 0, 0),
                    datetime(2024, 3, 1, 0, 0),
                    datetime(2024, 4, 1, 0, 0),
                    datetime(2024, 5, 1, 0, 0),
                    datetime(2024, 6, 1, 0, 0),
                ],
                "value": [0, 1, 2, 3, 4, 5],
            }
        ),
        check_row_order=False,
    )


def test_flush_join_and_operation_19040() -> None:
    df_A = pl.LazyFrame({"K": [True, False], "A": [1, 1]})

    df_B = pl.LazyFrame({"K": [True], "B": [1]})

    df_C = pl.LazyFrame({"K": [True], "C": [1]})

    q = (
        df_A.join(df_B, how="full", on=["K"], coalesce=True)
        .join(df_C, how="full", on=["K"], coalesce=True)
        .with_columns(B=pl.col("B"))
        .sort("K")
    )
    assert q.collect(engine="streaming").to_dict(as_series=False) == {
        "K": [False, True],
        "A": [1, 1],
        "B": [None, 1],
        "C": [None, 1],
    }


def test_full_coalesce_join_and_rename_15583() -> None:
    df1 = pl.LazyFrame({"a": [1, 2, 3]})
    df2 = pl.LazyFrame({"a": [3, 4, 5]})

    result = (
        df1.join(df2, on="a", how="full", coalesce=True)
        .select(pl.all().name.map(lambda c: c.upper()))
        .sort("A")
        .collect(engine="streaming")
    )
    assert result["A"].to_list() == [1, 2, 3, 4, 5]


def test_invert_order_full_join_22295() -> None:
    lf = pl.LazyFrame(
        {
            "value_at": [datetime(2024, i + 1, 1) for i in range(6)],
            "value": list(range(6)),
        }
    )

    lf.join(lf, on=["value", "value_at"], how="full", coalesce=True).collect(
        engine="streaming"
    )
