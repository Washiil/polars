from __future__ import annotations

from typing import TYPE_CHECKING, Any

import numpy as np
import pytest

import polars as pl
from polars.exceptions import DuplicateError, InvalidOperationError
from polars.testing import assert_frame_equal, assert_series_equal

if TYPE_CHECKING:
    from polars._typing import ConcatMethod


def test_concat_align() -> None:
    a = pl.DataFrame({"a": ["a", "b", "d", "e", "e"], "b": [1, 2, 4, 5, 6]})
    b = pl.DataFrame({"a": ["a", "b", "c"], "c": [5.5, 6.0, 7.5]})
    c = pl.DataFrame({"a": ["a", "b", "c", "d", "e"], "d": ["w", "x", "y", "z", None]})

    for align_full in ("align", "align_full"):
        result = pl.concat([a, b, c], how=align_full)
        expected = pl.DataFrame(
            {
                "a": ["a", "b", "c", "d", "e", "e"],
                "b": [1, 2, None, 4, 5, 6],
                "c": [5.5, 6.0, 7.5, None, None, None],
                "d": ["w", "x", "y", "z", None, None],
            }
        )
        assert_frame_equal(result, expected)

    result = pl.concat([a, b, c], how="align_left")
    expected = pl.DataFrame(
        {
            "a": ["a", "b", "d", "e", "e"],
            "b": [1, 2, 4, 5, 6],
            "c": [5.5, 6.0, None, None, None],
            "d": ["w", "x", "z", None, None],
        }
    )
    assert_frame_equal(result, expected)

    result = pl.concat([a, b, c], how="align_right")
    expected = pl.DataFrame(
        {
            "a": ["a", "b", "c", "d", "e"],
            "b": [1, 2, None, None, None],
            "c": [5.5, 6.0, 7.5, None, None],
            "d": ["w", "x", "y", "z", None],
        }
    )
    assert_frame_equal(result, expected)

    result = pl.concat([a, b, c], how="align_inner")
    expected = pl.DataFrame(
        {
            "a": ["a", "b"],
            "b": [1, 2],
            "c": [5.5, 6.0],
            "d": ["w", "x"],
        }
    )
    assert_frame_equal(result, expected)


@pytest.mark.parametrize(
    "strategy", ["align", "align_full", "align_left", "align_right"]
)
def test_concat_align_no_common_cols(strategy: ConcatMethod) -> None:
    df1 = pl.DataFrame({"a": [1, 2], "b": [1, 2]})
    df2 = pl.DataFrame({"c": [3, 4], "d": [3, 4]})

    with pytest.raises(
        InvalidOperationError,
        match=f"{strategy!r} strategy requires at least one common column",
    ):
        pl.concat((df1, df2), how=strategy)


@pytest.mark.parametrize(
    ("a", "b", "c", "strategy"),
    [
        (
            pl.DataFrame({"a": [1, 2]}),
            pl.DataFrame({"b": ["a", "b"], "c": [3, 4]}),
            pl.DataFrame({"a": [5, 6], "c": [5, 6], "d": [5, 6], "b": ["x", "y"]}),
            "diagonal",
        ),
        (
            pl.DataFrame(
                {"a": [1, 2]},
                schema_overrides={"a": pl.Int32},
            ),
            pl.DataFrame(
                {"b": ["a", "b"], "c": [3, 4]},
                schema_overrides={"c": pl.UInt8},
            ),
            pl.DataFrame(
                {"a": [5, 6], "c": [5, 6], "d": [5, 6], "b": ["x", "y"]},
                schema_overrides={"b": pl.Categorical},
            ),
            "diagonal_relaxed",
        ),
    ],
)
def test_concat_diagonal(
    a: pl.DataFrame, b: pl.DataFrame, c: pl.DataFrame, strategy: ConcatMethod
) -> None:
    for out in [
        pl.concat([a, b, c], how=strategy),
        pl.concat([a.lazy(), b.lazy(), c.lazy()], how=strategy).collect(),
    ]:
        expected = pl.DataFrame(
            {
                "a": [1, 2, None, None, 5, 6],
                "b": [None, None, "a", "b", "x", "y"],
                "c": [None, None, 3, 4, 5, 6],
                "d": [None, None, None, None, 5, 6],
            }
        )
        assert_frame_equal(out, expected)


def test_concat_diagonal_relaxed_with_empty_frame() -> None:
    df1 = pl.DataFrame()
    df2 = pl.DataFrame(
        {
            "a": ["a", "b"],
            "b": [1, 2],
        }
    )
    out = pl.concat((df1, df2), how="diagonal_relaxed")
    expected = df2
    assert_frame_equal(out, expected)


@pytest.mark.parametrize("lazy", [False, True])
def test_concat_horizontal(lazy: bool) -> None:
    a = pl.DataFrame({"a": ["a", "b"], "b": [1, 2]})
    b = pl.DataFrame({"c": [5, 7, 8, 9], "d": [1, 2, 1, 2], "e": [1, 2, 1, 2]})

    if lazy:
        out = pl.concat([a.lazy(), b.lazy()], how="horizontal").collect()
    else:
        out = pl.concat([a, b], how="horizontal")

    expected = pl.DataFrame(
        {
            "a": ["a", "b", None, None],
            "b": [1, 2, None, None],
            "c": [5, 7, 8, 9],
            "d": [1, 2, 1, 2],
            "e": [1, 2, 1, 2],
        }
    )
    assert_frame_equal(out, expected)


@pytest.mark.parametrize("lazy", [False, True])
def test_concat_horizontal_three_dfs(lazy: bool) -> None:
    a = pl.DataFrame({"a1": [1, 2, 3], "a2": ["a", "b", "c"]})
    b = pl.DataFrame({"b1": [0.25, 0.5]})
    c = pl.DataFrame({"c1": [1, 2, 3, 4], "c2": [5, 6, 7, 8], "c3": [9, 10, 11, 12]})

    if lazy:
        out = pl.concat([a.lazy(), b.lazy(), c.lazy()], how="horizontal").collect()
    else:
        out = pl.concat([a, b, c], how="horizontal")

    expected = pl.DataFrame(
        {
            "a1": [1, 2, 3, None],
            "a2": ["a", "b", "c", None],
            "b1": [0.25, 0.5, None, None],
            "c1": [1, 2, 3, 4],
            "c2": [5, 6, 7, 8],
            "c3": [9, 10, 11, 12],
        }
    )
    assert_frame_equal(out, expected)


@pytest.mark.parametrize("lazy", [False, True])
def test_concat_horizontal_single_df(lazy: bool) -> None:
    a = pl.DataFrame({"a": ["a", "b"], "b": [1, 2]})

    if lazy:
        out = pl.concat([a.lazy()], how="horizontal").collect()
    else:
        out = pl.concat([a], how="horizontal")

    expected = a
    assert_frame_equal(out, expected)


def test_concat_horizontal_duplicate_col() -> None:
    a = pl.LazyFrame({"a": ["a", "b"], "b": [1, 2]})
    b = pl.LazyFrame({"c": [5, 7, 8, 9], "d": [1, 2, 1, 2], "a": [1, 2, 1, 2]})

    with pytest.raises(DuplicateError):
        pl.concat([a, b], how="horizontal").collect()


def test_concat_vertical() -> None:
    a = pl.DataFrame({"a": ["a", "b"], "b": [1, 2]})
    b = pl.DataFrame({"a": ["c", "d", "e"], "b": [3, 4, 5]})

    result = pl.concat([a, b], how="vertical")
    expected = pl.DataFrame(
        {
            "a": ["a", "b", "c", "d", "e"],
            "b": [1, 2, 3, 4, 5],
        }
    )
    assert_frame_equal(result, expected)


def test_cov() -> None:
    s1 = pl.Series("a", [10, 37, -40])
    s2 = pl.Series("b", [70, -10, 35])

    # lazy/expression
    lf = pl.LazyFrame([s1, s2])
    res1 = lf.select(
        x=pl.cov("a", "b"),
        y=pl.cov("a", "b", ddof=2),
    ).collect()

    # eager/series
    res2 = (
        pl.cov(s1, s2, eager=True).alias("x"),
        pl.cov(s1, s2, eager=True, ddof=2).alias("y"),
    )

    # expect same result from both approaches
    for idx, (r1, r2) in enumerate(zip(res1, res2)):
        expected_value = -645.8333333333 if idx == 0 else -1291.6666666666
        assert pytest.approx(expected_value) == r1.item()
        assert_series_equal(r1, r2)


def test_corr() -> None:
    s1 = pl.Series("a", [10, 37, -40])
    s2 = pl.Series("b", [70, -10, 35])

    # lazy/expression
    lf = pl.LazyFrame([s1, s2])
    res1 = lf.select(
        x=pl.corr("a", "b"),
        y=pl.corr("a", "b", method="spearman"),
    ).collect()

    # eager/series
    res2 = (
        pl.corr(s1, s2, eager=True).alias("x"),
        pl.corr(s1, s2, method="spearman", eager=True).alias("y"),
    )

    # expect same result from both approaches
    for idx, (r1, r2) in enumerate(zip(res1, res2)):
        assert pytest.approx(-0.412199756 if idx == 0 else -0.5) == r1.item()
        assert_series_equal(r1, r2)


def test_extend_ints() -> None:
    a = pl.DataFrame({"a": [1 for _ in range(1)]}, schema={"a": pl.Int64})
    with pytest.raises(pl.exceptions.SchemaError):
        a.extend(a.select(pl.lit(0, dtype=pl.Int32).alias("a")))


def test_null_handling_correlation() -> None:
    df = pl.DataFrame({"a": [1, 2, 3, None, 4], "b": [1, 2, 3, 10, 4]})

    out = df.select(
        pl.corr("a", "b").alias("pearson"),
        pl.corr("a", "b", method="spearman").alias("spearman"),
    )
    assert out["pearson"][0] == pytest.approx(1.0)
    assert out["spearman"][0] == pytest.approx(1.0)

    # see #4930
    df1 = pl.DataFrame({"a": [None, 1, 2], "b": [None, 2, 1]})
    df2 = pl.DataFrame({"a": [np.nan, 1, 2], "b": [np.nan, 2, 1]})

    assert np.isclose(df1.select(pl.corr("a", "b", method="spearman")).item(), -1.0)
    assert (
        str(
            df2.select(pl.corr("a", "b", method="spearman", propagate_nans=True)).item()
        )
        == "nan"
    )


def test_align_frames() -> None:
    import numpy as np
    import pandas as pd

    # setup some test frames
    pdf1 = pd.DataFrame(
        {
            "date": pd.date_range(start="2019-01-02", periods=9),
            "a": np.array([0, 1, 2, np.nan, 4, 5, 6, 7, 8], dtype=np.float64),
            "b": np.arange(9, 18, dtype=np.float64),
        }
    ).set_index("date")

    pdf2 = pd.DataFrame(
        {
            "date": pd.date_range(start="2019-01-04", periods=7),
            "a": np.arange(9, 16, dtype=np.float64),
            "b": np.arange(10, 17, dtype=np.float64),
        }
    ).set_index("date")

    # calculate dot-product in pandas
    pd_dot = (pdf1 * pdf2).sum(axis="columns").to_frame("dot").reset_index()

    # use "align_frames" to calculate dot-product from disjoint rows. pandas uses an
    # index to automatically infer the correct frame-alignment for the calculation;
    # we need to do it explicitly (which also makes it clearer what is happening)
    pf1, pf2 = pl.align_frames(
        pl.from_pandas(pdf1.reset_index()),
        pl.from_pandas(pdf2.reset_index()),
        on="date",
    )
    pl_dot = (
        (pf1[["a", "b"]] * pf2[["a", "b"]])
        .fill_null(0)
        .select(pl.sum_horizontal("*").alias("dot"))
        .insert_column(0, pf1["date"])
    )
    # confirm we match the same operation in pandas
    assert_frame_equal(pl_dot, pl.from_pandas(pd_dot))
    pd.testing.assert_frame_equal(pd_dot, pl_dot.to_pandas())

    # confirm alignment function works with lazy frames
    lf1, lf2 = pl.align_frames(
        pl.from_pandas(pdf1.reset_index()).lazy(),
        pl.from_pandas(pdf2.reset_index()).lazy(),
        on="date",
    )
    assert isinstance(lf1, pl.LazyFrame)
    assert_frame_equal(lf1.collect(), pf1)
    assert_frame_equal(lf2.collect(), pf2)

    # misc: no frames results in an empty list
    assert pl.align_frames(on="date") == []

    # expected error condition
    with pytest.raises(TypeError):
        pl.align_frames(  # type: ignore[type-var]
            pl.from_pandas(pdf1.reset_index()).lazy(),
            pl.from_pandas(pdf2.reset_index()),
            on="date",
        )


def test_align_frames_misc() -> None:
    df1 = pl.DataFrame([[3, 5, 6], [5, 8, 9]], orient="row")
    df2 = pl.DataFrame([[2, 5, 6], [3, 8, 9], [4, 2, 0]], orient="row")

    # descending result
    pf1, pf2 = pl.align_frames(
        [df1, df2],  # list input
        on="column_0",
        descending=True,
    )
    assert pf1.rows() == [(5, 8, 9), (4, None, None), (3, 5, 6), (2, None, None)]
    assert pf2.rows() == [(5, None, None), (4, 2, 0), (3, 8, 9), (2, 5, 6)]

    # handle identical frames
    pf1, pf2, pf3 = pl.align_frames(
        (df for df in (df1, df2, df2)),  # generator input
        on="column_0",
        descending=True,
    )
    assert pf1.rows() == [(5, 8, 9), (4, None, None), (3, 5, 6), (2, None, None)]
    for pf in (pf2, pf3):
        assert pf.rows() == [(5, None, None), (4, 2, 0), (3, 8, 9), (2, 5, 6)]


def test_align_frames_with_nulls() -> None:
    df1 = pl.DataFrame({"key": ["x", "y", None], "value": [1, 2, 0]})
    df2 = pl.DataFrame({"key": ["x", None, "z", "y"], "value": [4, 3, 6, 5]})

    a1, a2 = pl.align_frames(df1, df2, on="key")

    aligned_frame_data = a1.to_dict(as_series=False), a2.to_dict(as_series=False)
    assert aligned_frame_data == (
        {"key": [None, "x", "y", "z"], "value": [0, 1, 2, None]},
        {"key": [None, "x", "y", "z"], "value": [3, 4, 5, 6]},
    )


def test_align_frames_duplicate_key() -> None:
    # setup some test frames with duplicate key/alignment values
    df1 = pl.DataFrame({"x": ["a", "a", "a", "e"], "y": [1, 2, 4, 5]})
    df2 = pl.DataFrame({"y": [0, 0, -1], "z": [5.5, 6.0, 7.5], "x": ["a", "b", "b"]})

    # align rows, confirming correctness and original column order
    af1, af2 = pl.align_frames(df1, df2, on="x")

    # shape: (6, 2)   shape: (6, 3)
    # ┌─────┬──────┐  ┌──────┬──────┬─────┐
    # │ x   ┆ y    │  │ y    ┆ z    ┆ x   │
    # │ --- ┆ ---  │  │ ---  ┆ ---  ┆ --- │
    # │ str ┆ i64  │  │ i64  ┆ f64  ┆ str │
    # ╞═════╪══════╡  ╞══════╪══════╪═════╡
    # │ a   ┆ 1    │  │ 0    ┆ 5.5  ┆ a   │
    # │ a   ┆ 2    │  │ 0    ┆ 5.5  ┆ a   │
    # │ a   ┆ 4    │  │ 0    ┆ 5.5  ┆ a   │
    # │ b   ┆ null │  │ 0    ┆ 6.0  ┆ b   │
    # │ b   ┆ null │  │ -1   ┆ 7.5  ┆ b   │
    # │ e   ┆ 5    │  │ null ┆ null ┆ e   │
    # └─────┴──────┘  └──────┴──────┴─────┘
    assert af1.rows() == [
        ("a", 1),
        ("a", 2),
        ("a", 4),
        ("b", None),
        ("b", None),
        ("e", 5),
    ]
    assert af2.rows() == [
        (0, 5.5, "a"),
        (0, 5.5, "a"),
        (0, 5.5, "a"),
        (0, 6.0, "b"),
        (-1, 7.5, "b"),
        (None, None, "e"),
    ]

    # align frames the other way round, using "left" alignment strategy
    af1, af2 = pl.align_frames(df2, df1, on="x", how="left")

    # shape: (5, 3)        shape: (5, 2)
    # ┌─────┬─────┬─────┐  ┌─────┬──────┐
    # │ y   ┆ z   ┆ x   │  │ x   ┆ y    │
    # │ --- ┆ --- ┆ --- │  │ --- ┆ ---  │
    # │ i64 ┆ f64 ┆ str │  │ str ┆ i64  │
    # ╞═════╪═════╪═════╡  ╞═════╪══════╡
    # │ 0   ┆ 5.5 ┆ a   │  │ a   ┆ 1    │
    # │ 0   ┆ 5.5 ┆ a   │  │ a   ┆ 2    │
    # │ 0   ┆ 5.5 ┆ a   │  │ a   ┆ 4    │
    # │ 0   ┆ 6.0 ┆ b   │  │ b   ┆ null │
    # │ -1  ┆ 7.5 ┆ b   │  │ b   ┆ null │
    # └─────┴─────┴─────┘  └─────┴──────┘
    assert af1.rows() == [
        (0, 5.5, "a"),
        (0, 5.5, "a"),
        (0, 5.5, "a"),
        (0, 6.0, "b"),
        (-1, 7.5, "b"),
    ]
    assert af2.rows() == [
        ("a", 1),
        ("a", 2),
        ("a", 4),
        ("b", None),
        ("b", None),
    ]


def test_align_frames_single_row_20445() -> None:
    left = pl.DataFrame({"a": [1], "b": [2]})
    right = pl.DataFrame({"a": [1], "c": [3]})
    result = pl.align_frames(left, right, how="left", on="a")
    assert_frame_equal(result[0], left)
    assert_frame_equal(result[1], right)


def test_coalesce() -> None:
    df = pl.DataFrame(
        {
            "a": [1, None, None, None],
            "b": [1, 2, None, None],
            "c": [5, None, 3, None],
        }
    )
    # list inputs
    expected = pl.Series("d", [1, 2, 3, 10]).to_frame()
    result = df.select(pl.coalesce(["a", "b", "c", 10]).alias("d"))
    assert_frame_equal(expected, result)

    # positional inputs
    expected = pl.Series("d", [1.0, 2.0, 3.0, 10.0]).to_frame()
    result = df.select(pl.coalesce(pl.col(["a", "b", "c"]), 10.0).alias("d"))
    assert_frame_equal(result, expected)


def test_coalesce_eager() -> None:
    # eager/series inputs
    s1 = pl.Series("colx", [None, 2, None])
    s2 = pl.Series("coly", [1, None, None])
    s3 = pl.Series("colz", [None, None, 3])

    res = pl.coalesce(s1, s2, s3, eager=True)
    expected = pl.Series("colx", [1, 2, 3])
    assert_series_equal(expected, res)

    for zero in (0, pl.lit(0)):
        res = pl.coalesce(s1, zero, eager=True)
        expected = pl.Series("colx", [0, 2, 0])
        assert_series_equal(expected, res)

        res = pl.coalesce(zero, s1, eager=True)
        expected = pl.Series("literal", [0, 0, 0])
        assert_series_equal(expected, res)

    with pytest.raises(
        ValueError,
        match="expected at least one Series in 'coalesce' if 'eager=True'",
    ):
        pl.coalesce("x", "y", eager=True)


def test_overflow_diff() -> None:
    df = pl.DataFrame({"a": [20, 10, 30]})
    assert df.select(pl.col("a").cast(pl.UInt64).diff()).to_dict(as_series=False) == {
        "a": [None, -10, 20]
    }


@pytest.mark.may_fail_cloud  # reason: unknown type
def test_fill_null_unknown_output_type() -> None:
    df = pl.DataFrame({"a": [None, 2, 3, 4, 5]})
    assert df.with_columns(
        np.exp(pl.col("a")).fill_null(pl.lit(1, pl.Float64))
    ).to_dict(as_series=False) == {
        "a": [
            1.0,
            7.38905609893065,
            20.085536923187668,
            54.598150033144236,
            148.4131591025766,
        ]
    }


def test_approx_n_unique() -> None:
    df1 = pl.DataFrame({"a": [None, 1, 2], "b": [None, 2, 1]})

    assert_frame_equal(
        df1.select(pl.approx_n_unique("b")),
        pl.DataFrame({"b": pl.Series(values=[3], dtype=pl.UInt32)}),
    )

    assert_frame_equal(
        df1.select(pl.col("b").approx_n_unique()),
        pl.DataFrame({"b": pl.Series(values=[3], dtype=pl.UInt32)}),
    )


def test_lazy_functions() -> None:
    df = pl.DataFrame(
        {
            "a": ["foo", "bar", "foo"],
            "b": [1, 2, 3],
            "c": [-1.0, 2.0, 4.0],
        }
    )

    # test function expressions against frame
    out = df.select(
        pl.var("b").name.suffix("_var"),
        pl.std("b").name.suffix("_std"),
        pl.max("a", "b").name.suffix("_max"),
        pl.min("a", "b").name.suffix("_min"),
        pl.sum("b", "c").name.suffix("_sum"),
        pl.mean("b", "c").name.suffix("_mean"),
        pl.median("c", "b").name.suffix("_median"),
        pl.n_unique("b", "a").name.suffix("_n_unique"),
        pl.first("a").name.suffix("_first"),
        pl.first("b", "c").name.suffix("_first"),
        pl.last("c", "b", "a").name.suffix("_last"),
    )
    expected: dict[str, list[Any]] = {
        "b_var": [1.0],
        "b_std": [1.0],
        "a_max": ["foo"],
        "b_max": [3],
        "a_min": ["bar"],
        "b_min": [1],
        "b_sum": [6],
        "c_sum": [5.0],
        "b_mean": [2.0],
        "c_mean": [5 / 3],
        "c_median": [2.0],
        "b_median": [2.0],
        "b_n_unique": [3],
        "a_n_unique": [2],
        "a_first": ["foo"],
        "b_first": [1],
        "c_first": [-1.0],
        "c_last": [4.0],
        "b_last": [3],
        "a_last": ["foo"],
    }
    assert_frame_equal(
        out,
        pl.DataFrame(
            data=expected,
            schema_overrides={
                "a_n_unique": pl.UInt32,
                "b_n_unique": pl.UInt32,
            },
        ),
    )

    # test function expressions against series
    for name, value in expected.items():
        col, fn = name.split("_", 1)
        if series_fn := getattr(df[col], fn, None):
            assert series_fn() == value[0]

    # regex selection
    out = df.select(
        pl.struct(pl.max("^a|b$")).alias("x"),
        pl.struct(pl.min("^.*[bc]$")).alias("y"),
        pl.struct(pl.sum("^[^a]$")).alias("z"),
    )
    assert out.rows() == [
        ({"a": "foo", "b": 3}, {"b": 1, "c": -1.0}, {"b": 6, "c": 5.0})
    ]


def test_count() -> None:
    df = pl.DataFrame({"a": [1, 1, 1], "b": [None, "xx", "yy"]})
    out = df.select(pl.count("a"))
    assert list(out["a"]) == [3]

    for count_expr in (
        pl.count("b", "a"),
        [pl.count("b"), pl.count("a")],
    ):
        out = df.select(count_expr)
        assert out.rows() == [(2, 3)]


def test_head_tail(fruits_cars: pl.DataFrame) -> None:
    res_expr = fruits_cars.select(pl.head("A", 2))
    expected = pl.Series("A", [1, 2])
    assert_series_equal(res_expr.to_series(), expected)

    res_expr = fruits_cars.select(pl.tail("A", 2))
    expected = pl.Series("A", [4, 5])
    assert_series_equal(res_expr.to_series(), expected)


def test_escape_regex() -> None:
    result = pl.escape_regex("abc(\\w+)")
    expected = "abc\\(\\\\w\\+\\)"
    assert result == expected

    df = pl.DataFrame({"text": ["abc", "def", None, "abc(\\w+)"]})
    with pytest.raises(
        TypeError,
        match="escape_regex function is unsupported for `Expr`, you may want use `Expr.str.escape_regex` instead",
    ):
        df.with_columns(escaped=pl.escape_regex(pl.col("text")))  # type: ignore[arg-type]

    with pytest.raises(
        TypeError,
        match="escape_regex function supports only `str` type, got `int`",
    ):
        pl.escape_regex(3)  # type: ignore[arg-type]


@pytest.mark.parametrize("func", ["var", "std"])
def test_var_std_lit_23156(func: str) -> None:
    for n in range(100):
        input = pl.DataFrame({"x": list(range(n))}).select(pl.col("x"), pl.lit(0))
        out = getattr(input, func)()
        if n <= 1:
            assert_series_equal(
                out["literal"], pl.Series("literal", [None], dtype=pl.Float64)
            )
        else:
            assert_series_equal(
                out["literal"], pl.Series("literal", [0.0], dtype=pl.Float64)
            )


def test_row_index_expr() -> None:
    lf = pl.LazyFrame({"x": ["A", "A", "B", "B", "B"]})

    assert_frame_equal(
        lf.with_columns(pl.row_index(), pl.row_index("another_index")).collect(),
        pl.DataFrame(
            {
                "x": ["A", "A", "B", "B", "B"],
                "index": [0, 1, 2, 3, 4],
                "another_index": [0, 1, 2, 3, 4],
            },
            schema={
                "x": pl.String,
                "index": pl.get_index_type(),
                "another_index": pl.get_index_type(),
            },
        ),
    )

    assert_frame_equal(
        (
            lf.group_by("x")
            .agg(pl.row_index(), pl.row_index("another_index"))
            .sort("x")
            .collect()
        ),
        pl.DataFrame(
            {
                "x": ["A", "B"],
                "index": [[0, 1], [0, 1, 2]],
                "another_index": [[0, 1], [0, 1, 2]],
            },
            schema={
                "x": pl.String,
                "index": pl.List(pl.get_index_type()),
                "another_index": pl.List(pl.get_index_type()),
            },
        ),
    )

    assert_frame_equal(
        lf.select(pl.row_index()).collect(),
        pl.DataFrame(
            {"index": [0, 1, 2, 3, 4]},
            schema={"index": pl.get_index_type()},
        ),
    )
