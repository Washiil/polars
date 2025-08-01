from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import TYPE_CHECKING, Any

import pytest

import polars as pl
from polars.exceptions import ComputeError, SchemaError, ShapeError
from polars.testing import assert_frame_equal, assert_series_equal

if TYPE_CHECKING:
    from polars._typing import PolarsDataType


@pytest.mark.parametrize(
    ("value", "n", "dtype", "expected_dtype"),
    [
        (2**31, 5, None, pl.Int64),
        (2**31 - 1, 5, None, pl.Int32),
        (-(2**31) - 1, 3, None, pl.Int64),
        (-(2**31), 3, None, pl.Int32),
        ("foo", 2, None, pl.String),
        (1.0, 5, None, pl.Float64),
        (True, 4, None, pl.Boolean),
        (None, 7, None, pl.Null),
        (0, 0, None, pl.Int32),
        (datetime(2023, 2, 2), 3, None, pl.Datetime),
        (date(2023, 2, 2), 3, None, pl.Date),
        (time(10, 15), 1, None, pl.Time),
        (timedelta(hours=3), 10, None, pl.Duration),
        (8, 2, pl.UInt8, pl.UInt8),
        (date(2023, 2, 2), 3, pl.Datetime, pl.Datetime),
        (7.5, 5, pl.UInt16, pl.UInt16),
        ([1, 2, 3], 2, pl.List(pl.Int64), pl.List(pl.Int64)),
        (b"ab12", 3, pl.Binary, pl.Binary),
    ],
)
def test_repeat(
    value: Any,
    n: int,
    dtype: PolarsDataType,
    expected_dtype: PolarsDataType,
) -> None:
    expected = pl.Series("repeat", [value] * n).cast(expected_dtype)

    result_eager = pl.repeat(value, n=n, dtype=dtype, eager=True)
    assert_series_equal(result_eager, expected)

    result_lazy = pl.select(pl.repeat(value, n=n, dtype=dtype, eager=False)).to_series()
    assert_series_equal(result_lazy, expected)


def test_repeat_expr_input_eager() -> None:
    result = pl.select(pl.repeat(1, n=pl.lit(3), eager=True)).to_series()
    expected = pl.Series("repeat", [1, 1, 1], dtype=pl.Int32)
    assert_series_equal(result, expected)


def test_repeat_expr_input_lazy() -> None:
    df = pl.DataFrame({"a": [3, 2, 1]})
    result = df.select(pl.repeat(1, n=pl.col("a").first())).to_series()
    expected = pl.Series("repeat", [1, 1, 1], dtype=pl.Int32)
    assert_series_equal(result, expected)

    df = pl.DataFrame({"a": [3, 2, 1]})
    assert df.select(pl.repeat(pl.sum("a"), n=2)).to_series().to_list() == [6, 6]


def test_repeat_n_zero() -> None:
    assert pl.repeat(1, n=0, eager=True).len() == 0


@pytest.mark.parametrize(
    "n",
    [1.5, 2.0, date(1971, 1, 2), "hello"],
)
def test_repeat_n_non_integer(n: Any) -> None:
    with pytest.raises(SchemaError, match="expected expression of dtype 'integer'"):
        pl.repeat(1, n=pl.lit(n), eager=True)


def test_repeat_n_empty() -> None:
    df = pl.DataFrame(schema={"a": pl.Int32})
    with pytest.raises(ShapeError, match="'n' must be a scalar value"):
        df.select(pl.repeat(1, n=pl.col("a")))


def test_repeat_n_negative() -> None:
    with pytest.raises(ComputeError, match="could not parse value '-1' as a size"):
        pl.repeat(1, n=-1, eager=True)


@pytest.mark.parametrize(
    ("n", "value", "dtype"),
    [
        (2, 1, pl.UInt32),
        (0, 1, pl.Int16),
        (3, 1, pl.Float32),
        (1, "1", pl.Utf8),
        (2, ["1"], pl.List(pl.Utf8)),
        (4, True, pl.Boolean),
        (2, [True], pl.List(pl.Boolean)),
        (2, [1], pl.Array(pl.Int16, shape=1)),
        (2, [1, 1, 1], pl.Array(pl.Int8, shape=3)),
        (1, [1], pl.List(pl.UInt32)),
    ],
)
def test_ones(
    n: int,
    value: Any,
    dtype: PolarsDataType,
) -> None:
    expected = pl.Series("ones", [value] * n, dtype=dtype)

    result_eager = pl.ones(n=n, dtype=dtype, eager=True)
    assert_series_equal(result_eager, expected)

    result_lazy = pl.select(pl.ones(n=n, dtype=dtype, eager=False)).to_series()
    assert_series_equal(result_lazy, expected)


@pytest.mark.parametrize(
    ("n", "value", "dtype"),
    [
        (2, 0, pl.UInt8),
        (0, 0, pl.Int32),
        (3, 0, pl.Float32),
        (1, "0", pl.Utf8),
        (2, ["0"], pl.List(pl.Utf8)),
        (4, False, pl.Boolean),
        (2, [False], pl.List(pl.Boolean)),
        (3, [0], pl.Array(pl.UInt32, shape=1)),
        (2, [0, 0, 0], pl.Array(pl.UInt32, shape=3)),
        (1, [0], pl.List(pl.UInt32)),
    ],
)
def test_zeros(
    n: int,
    value: Any,
    dtype: PolarsDataType,
) -> None:
    expected = pl.Series("zeros", [value] * n, dtype=dtype)

    result_eager = pl.zeros(n=n, dtype=dtype, eager=True)
    assert_series_equal(result_eager, expected)

    result_lazy = pl.select(pl.zeros(n=n, dtype=dtype, eager=False)).to_series()
    assert_series_equal(result_lazy, expected)


def test_ones_zeros_misc() -> None:
    # check we default to f64 if dtype is unspecified
    s_ones = pl.ones(n=2, eager=True)
    s_zeros = pl.zeros(n=2, eager=True)

    assert s_ones.dtype == s_zeros.dtype == pl.Float64

    # confirm that we raise a suitable error if dtype is invalid
    with pytest.raises(TypeError, match="invalid dtype for `ones`"):
        pl.ones(n=2, dtype=pl.Struct({"x": pl.Date, "y": pl.Duration}), eager=True)

    with pytest.raises(TypeError, match="invalid dtype for `zeros`"):
        pl.zeros(n=2, dtype=pl.Struct({"x": pl.Date, "y": pl.Duration}), eager=True)


def test_repeat_by_logical_dtype() -> None:
    df = pl.DataFrame(
        {
            "repeat": [1, 2, 3],
            "date": [date(2021, 1, 1)] * 3,
            "cat": ["a", "b", "c"],
        },
        schema={"repeat": pl.Int32, "date": pl.Date, "cat": pl.Categorical},
    )
    out = df.select(
        pl.col("date").repeat_by("repeat"), pl.col("cat").repeat_by("repeat")
    )

    expected_df = pl.DataFrame(
        {
            "date": [
                [date(2021, 1, 1)],
                [date(2021, 1, 1), date(2021, 1, 1)],
                [date(2021, 1, 1), date(2021, 1, 1), date(2021, 1, 1)],
            ],
            "cat": [["a"], ["b", "b"], ["c", "c", "c"]],
        },
        schema={"date": pl.List(pl.Date), "cat": pl.List(pl.Categorical)},
    )

    assert_frame_equal(out, expected_df)


def test_repeat_by_list() -> None:
    df = pl.DataFrame(
        {
            "repeat": [1, 2, 3, None],
            "value": [None, [1, 2, 3], [4, None], [1, 2]],
        },
        schema={"repeat": pl.UInt32, "value": pl.List(pl.UInt8)},
    )
    out = df.select(pl.col("value").repeat_by("repeat"))

    expected_df = pl.DataFrame(
        {
            "value": [
                [None],
                [[1, 2, 3], [1, 2, 3]],
                [[4, None], [4, None], [4, None]],
                None,
            ],
        },
        schema={"value": pl.List(pl.List(pl.UInt8))},
    )

    assert_frame_equal(out, expected_df)


def test_repeat_by_nested_list() -> None:
    df = pl.DataFrame(
        {
            "repeat": [1, 2, 3],
            "value": [None, [[1], [2, 2]], [[3, 3], None, [4, None]]],
        },
        schema={"repeat": pl.UInt32, "value": pl.List(pl.List(pl.Int16))},
    )
    out = df.select(pl.col("value").repeat_by("repeat"))

    expected_df = pl.DataFrame(
        {
            "value": [
                [None],
                [[[1], [2, 2]], [[1], [2, 2]]],
                [
                    [[3, 3], None, [4, None]],
                    [[3, 3], None, [4, None]],
                    [[3, 3], None, [4, None]],
                ],
            ],
        },
        schema={"value": pl.List(pl.List(pl.List(pl.Int16)))},
    )

    assert_frame_equal(out, expected_df)


def test_repeat_by_struct() -> None:
    df = pl.DataFrame(
        {
            "repeat": [1, 2, 3],
            "value": [None, {"a": 1, "b": 2}, {"a": 3, "b": None}],
        },
        schema={"repeat": pl.UInt32, "value": pl.Struct({"a": pl.Int8, "b": pl.Int32})},
    )
    out = df.select(pl.col("value").repeat_by("repeat"))

    expected_df = pl.DataFrame(
        {
            "value": [
                [None],
                [{"a": 1, "b": 2}, {"a": 1, "b": 2}],
                [{"a": 3, "b": None}, {"a": 3, "b": None}, {"a": 3, "b": None}],
            ],
        },
        schema={"value": pl.List(pl.Struct({"a": pl.Int8, "b": pl.Int32}))},
    )

    assert_frame_equal(out, expected_df)


def test_repeat_by_nested_struct() -> None:
    df = pl.DataFrame(
        {
            "repeat": [1, 2, 3],
            "value": [
                None,
                {"a": {"x": 1, "y": 1}, "b": 2},
                {"a": {"x": None, "y": 3}, "b": None},
            ],
        },
        schema={
            "repeat": pl.UInt32,
            "value": pl.Struct(
                {"a": pl.Struct({"x": pl.Int64, "y": pl.Int128}), "b": pl.Int32}
            ),
        },
    )
    out = df.select(pl.col("value").repeat_by("repeat"))

    expected_df = pl.DataFrame(
        {
            "value": [
                [None],
                [{"a": {"x": 1, "y": 1}, "b": 2}, {"a": {"x": 1, "y": 1}, "b": 2}],
                [
                    {"a": {"x": None, "y": 3}, "b": None},
                    {"a": {"x": None, "y": 3}, "b": None},
                    {"a": {"x": None, "y": 3}, "b": None},
                ],
            ],
        },
        schema={
            "value": pl.List(
                pl.Struct(
                    {"a": pl.Struct({"x": pl.Int64, "y": pl.Int128}), "b": pl.Int32}
                )
            )
        },
    )

    assert_frame_equal(out, expected_df)


def test_repeat_by_struct_in_list() -> None:
    df = pl.DataFrame(
        {
            "repeat": [1, 2, 3],
            "value": [
                None,
                [{"a": "foo", "b": "A"}, None],
                [{"a": None, "b": "B"}, {"a": "test", "b": "B"}],
            ],
        },
        schema={
            "repeat": pl.UInt32,
            "value": pl.List(pl.Struct({"a": pl.String, "b": pl.Enum(["A", "B"])})),
        },
    )
    out = df.select(pl.col("value").repeat_by("repeat"))

    expected_df = pl.DataFrame(
        {
            "value": [
                [None],
                [[{"a": "foo", "b": "A"}, None], [{"a": "foo", "b": "A"}, None]],
                [
                    [{"a": None, "b": "B"}, {"a": "test", "b": "B"}],
                    [{"a": None, "b": "B"}, {"a": "test", "b": "B"}],
                    [{"a": None, "b": "B"}, {"a": "test", "b": "B"}],
                ],
            ],
        },
        schema={
            "value": pl.List(
                pl.List(pl.Struct({"a": pl.String, "b": pl.Enum(["A", "B"])}))
            )
        },
    )

    assert_frame_equal(out, expected_df)


def test_repeat_by_list_in_struct() -> None:
    df = pl.DataFrame(
        {
            "repeat": [1, 2, 3],
            "value": [
                None,
                {"a": [1, 2, 3], "b": ["x", "y", None]},
                {"a": [None, 5, 6], "b": None},
            ],
        },
        schema={
            "repeat": pl.UInt32,
            "value": pl.Struct({"a": pl.List(pl.Int8), "b": pl.List(pl.String)}),
        },
    )
    out = df.select(pl.col("value").repeat_by("repeat"))

    expected_df = pl.DataFrame(
        {
            "value": [
                [None],
                [
                    {"a": [1, 2, 3], "b": ["x", "y", None]},
                    {"a": [1, 2, 3], "b": ["x", "y", None]},
                ],
                [
                    {"a": [None, 5, 6], "b": None},
                    {"a": [None, 5, 6], "b": None},
                    {"a": [None, 5, 6], "b": None},
                ],
            ],
        },
        schema={
            "value": pl.List(
                pl.Struct({"a": pl.List(pl.Int8), "b": pl.List(pl.String)})
            )
        },
    )

    assert_frame_equal(out, expected_df)


@pytest.mark.parametrize(
    ("data", "expected_data"),
    [
        (["a", "b", None], [["a", "a"], None, [None, None, None]]),
        ([1, 2, None], [[1, 1], None, [None, None, None]]),
        ([1.1, 2.2, None], [[1.1, 1.1], None, [None, None, None]]),
        ([True, False, None], [[True, True], None, [None, None, None]]),
    ],
)
def test_repeat_by_none_13053(data: list[Any], expected_data: list[list[Any]]) -> None:
    df = pl.DataFrame({"x": data, "by": [2, None, 3]})
    res = df.select(repeat=pl.col("x").repeat_by("by"))
    expected = pl.Series("repeat", expected_data)
    assert_series_equal(res.to_series(), expected)


def test_repeat_by_literal_none_20268() -> None:
    df = pl.DataFrame({"x": ["a", "b"]})
    expected = pl.Series("repeat", [None, None], dtype=pl.List(pl.String))

    res = df.select(repeat=pl.col("x").repeat_by(pl.lit(None)))
    assert_series_equal(res.to_series(), expected)

    res = df.select(repeat=pl.col("x").repeat_by(None))  # type: ignore[arg-type]
    assert_series_equal(res.to_series(), expected)


@pytest.mark.parametrize("value", [pl.Series([]), pl.Series([1, 2])])
def test_repeat_nonscalar_value(value: pl.Series) -> None:
    with pytest.raises(ShapeError, match="'value' must be a scalar value"):
        pl.select(pl.repeat(pl.Series(value), n=1))


@pytest.mark.parametrize("n", [[], [1, 2]])
def test_repeat_nonscalar_n(n: list[int]) -> None:
    df = pl.DataFrame({"n": n})
    with pytest.raises(ShapeError, match="'n' must be a scalar value"):
        df.select(pl.repeat("a", pl.col("n")))


def test_repeat_value_first() -> None:
    df = pl.DataFrame({"a": ["a", "b", "c"], "n": [4, 5, 6]})
    result = df.select(rep=pl.repeat(pl.col("a").first(), n=pl.col("n").first()))
    expected = pl.DataFrame({"rep": ["a", "a", "a", "a"]})
    assert_frame_equal(result, expected)


def test_repeat_by_arr() -> None:
    assert_series_equal(
        pl.Series([["a", "b"], ["a", "c"]], dtype=pl.Array(pl.String, 2)).repeat_by(2),
        pl.Series(
            [[["a", "b"], ["a", "b"]], [["a", "c"], ["a", "c"]]],
            dtype=pl.List(pl.Array(pl.String, 2)),
        ),
    )


def test_repeat_by_null() -> None:
    assert_series_equal(
        pl.Series([None, None], dtype=pl.Null).repeat_by(2),
        pl.Series([[None, None], [None, None]], dtype=pl.List(pl.Null)),
    )
