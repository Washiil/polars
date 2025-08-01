from __future__ import annotations

import sys
import urllib.parse
import warnings
from collections import OrderedDict
from datetime import date, datetime
from functools import partial
from pathlib import Path
from typing import Any, Callable

import pyarrow.parquet as pq
import pytest

import polars as pl
from polars.exceptions import ComputeError, SchemaFieldNotFoundError
from polars.testing import assert_frame_equal, assert_series_equal


def impl_test_hive_partitioned_predicate_pushdown(
    io_files_path: Path,
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    monkeypatch.setenv("POLARS_VERBOSE", "1")
    df = pl.read_ipc(io_files_path / "*.ipc")

    root = tmp_path / "partitioned_data"

    pq.write_to_dataset(
        df.to_arrow(),
        root_path=root,
        partition_cols=["category", "fats_g"],
    )
    q = pl.scan_parquet(root / "**/*.parquet", hive_partitioning=False)
    # checks schema
    assert q.collect_schema().names() == ["calories", "sugars_g"]
    # checks materialization
    assert q.collect().columns == ["calories", "sugars_g"]

    q = pl.scan_parquet(root / "**/*.parquet", hive_partitioning=True)
    assert q.collect_schema().names() == ["calories", "sugars_g", "category", "fats_g"]

    # Partitioning changes the order
    sort_by = ["fats_g", "category", "calories", "sugars_g"]

    # The hive partitioned columns are appended,
    # so we must ensure we assert in the proper order.
    df = df.select(["calories", "sugars_g", "category", "fats_g"])
    for streaming in [True, False]:
        for pred in [
            pl.col("category") == "vegetables",
            pl.col("category") != "vegetables",
            pl.col("fats_g") > 0.5,
            (pl.col("fats_g") == 0.5) & (pl.col("category") == "vegetables"),
        ]:
            assert_frame_equal(
                q.filter(pred)
                .sort(sort_by)
                .collect(engine="streaming" if streaming else "in-memory"),
                df.filter(pred).sort(sort_by),
            )

    # tests: 11536
    assert q.filter(pl.col("sugars_g") == 25).collect().shape == (1, 4)

    # tests: 12570
    assert q.filter(pl.col("fats_g") == 1225.0).select("category").collect().shape == (
        0,
        1,
    )


@pytest.mark.xdist_group("streaming")
@pytest.mark.write_disk
def test_hive_partitioned_predicate_pushdown(
    io_files_path: Path,
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    impl_test_hive_partitioned_predicate_pushdown(
        io_files_path,
        tmp_path,
        monkeypatch,
    )


@pytest.mark.xdist_group("streaming")
@pytest.mark.write_disk
def test_hive_partitioned_predicate_pushdown_single_threaded_async_17155(
    io_files_path: Path,
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    monkeypatch.setenv("POLARS_FORCE_ASYNC", "1")
    monkeypatch.setenv("POLARS_PREFETCH_SIZE", "1")

    impl_test_hive_partitioned_predicate_pushdown(
        io_files_path,
        tmp_path,
        monkeypatch,
    )


@pytest.mark.write_disk
@pytest.mark.may_fail_auto_streaming
def test_hive_partitioned_predicate_pushdown_skips_correct_number_of_files(
    tmp_path: Path, monkeypatch: Any, capfd: Any
) -> None:
    monkeypatch.setenv("POLARS_VERBOSE", "1")
    df = pl.DataFrame({"d": pl.arange(0, 5, eager=True)}).with_columns(
        a=pl.col("d") % 5
    )
    root = tmp_path / "test_int_partitions"
    df.write_parquet(
        root,
        use_pyarrow=True,
        pyarrow_options={"partition_cols": ["a"]},
    )

    q = pl.scan_parquet(root / "**/*.parquet", hive_partitioning=True)
    assert q.filter(pl.col("a").is_in([1, 4])).collect().shape == (2, 2)
    assert "allows skipping 3 / 5" in capfd.readouterr().err

    # Ensure the CSE can work with hive partitions.
    q = q.filter(pl.col("a").gt(2))
    result = q.join(q, on="a", how="left").collect(
        optimizations=pl.QueryOptFlags(comm_subplan_elim=True)
    )
    expected = {
        "a": [3, 4],
        "d": [3, 4],
        "d_right": [3, 4],
    }
    assert result.to_dict(as_series=False) == expected


@pytest.mark.write_disk
def test_hive_streaming_pushdown_is_in_22212(tmp_path: Path) -> None:
    (
        pl.DataFrame({"x": range(5)}).write_parquet(
            tmp_path,
            partition_by="x",
        )
    )

    lf = pl.scan_parquet(tmp_path, hive_partitioning=True).filter(
        pl.col("x").is_in([1, 4])
    )

    assert_frame_equal(
        lf.collect(
            engine="streaming", optimizations=pl.QueryOptFlags(predicate_pushdown=False)
        ),
        lf.collect(
            engine="streaming", optimizations=pl.QueryOptFlags(predicate_pushdown=True)
        ),
    )


@pytest.mark.xdist_group("streaming")
@pytest.mark.write_disk
@pytest.mark.parametrize("streaming", [True, False])
def test_hive_partitioned_slice_pushdown(
    io_files_path: Path, tmp_path: Path, streaming: bool
) -> None:
    df = pl.read_ipc(io_files_path / "*.ipc")

    root = tmp_path / "partitioned_data"

    # Ignore the pyarrow legacy warning until we can write properly with new settings.
    warnings.filterwarnings("ignore")
    pq.write_to_dataset(
        df.to_arrow(),
        root_path=root,
        partition_cols=["category", "fats_g"],
    )

    q = pl.scan_parquet(root / "**/*.parquet", hive_partitioning=True)
    schema = q.collect_schema()
    expect_count = pl.select(pl.lit(1, dtype=pl.UInt32).alias(x) for x in schema)

    assert_frame_equal(
        q.head(1)
        .collect(engine="streaming" if streaming else "in-memory")
        .select(pl.all().len()),
        expect_count,
    )
    assert q.head(0).collect(
        engine="streaming" if streaming else "in-memory"
    ).columns == [
        "calories",
        "sugars_g",
        "category",
        "fats_g",
    ]


@pytest.mark.xdist_group("streaming")
@pytest.mark.write_disk
def test_hive_partitioned_projection_pushdown(
    io_files_path: Path, tmp_path: Path
) -> None:
    df = pl.read_ipc(io_files_path / "*.ipc")

    root = tmp_path / "partitioned_data"

    # Ignore the pyarrow legacy warning until we can write properly with new settings.
    warnings.filterwarnings("ignore")
    pq.write_to_dataset(
        df.to_arrow(),
        root_path=root,
        partition_cols=["category", "fats_g"],
    )

    q = pl.scan_parquet(root / "**/*.parquet", hive_partitioning=True)
    columns = ["sugars_g", "category"]
    for streaming in [True, False]:
        assert (
            q.select(columns)
            .collect(engine="streaming" if streaming else "in-memory")
            .columns
            == columns
        )

    # test that hive partition columns are projected with the correct height when
    # the projection contains only hive partition columns (11796)
    for parallel in ("row_groups", "columns"):
        q = pl.scan_parquet(
            root / "**/*.parquet",
            hive_partitioning=True,
            parallel=parallel,
        )

        expected = q.collect().select("category")
        result = q.select("category").collect()

        assert_frame_equal(result, expected)


@pytest.mark.write_disk
def test_hive_partitioned_projection_skips_files(tmp_path: Path) -> None:
    # ensure that it makes hive columns even when . in dir value
    # and that it doesn't make hive columns from filename with =
    df = pl.DataFrame(
        {"sqlver": [10012.0, 10013.0], "namespace": ["eos", "fda"], "a": [1, 2]}
    )
    root = tmp_path / "partitioned_data"
    for dir_tuple, sub_df in df.partition_by(
        ["sqlver", "namespace"], include_key=False, as_dict=True
    ).items():
        new_path = root / f"sqlver={dir_tuple[0]}" / f"namespace={dir_tuple[1]}"
        new_path.mkdir(parents=True, exist_ok=True)
        sub_df.write_parquet(new_path / "file=8484.parquet")
    test_df = (
        pl.scan_parquet(str(root) + "/**/**/*.parquet", hive_partitioning=True)
        # don't care about column order
        .select("sqlver", "namespace", "a", pl.exclude("sqlver", "namespace", "a"))
        .collect()
    )
    assert_frame_equal(df, test_df)


@pytest.fixture
def dataset_path(tmp_path: Path) -> Path:
    tmp_path.mkdir(exist_ok=True)

    # Set up Hive partitioned Parquet file
    root = tmp_path / "dataset"
    part1 = root / "c=1"
    part2 = root / "c=2"
    root.mkdir()
    part1.mkdir()
    part2.mkdir()
    df1 = pl.DataFrame({"a": [1, 2], "b": [11.0, 12.0]})
    df2 = pl.DataFrame({"a": [3, 4], "b": [13.0, 14.0]})
    df3 = pl.DataFrame({"a": [5, 6], "b": [15.0, 16.0]})
    df1.write_parquet(part1 / "one.parquet")
    df2.write_parquet(part1 / "two.parquet")
    df3.write_parquet(part2 / "three.parquet")

    return root


@pytest.mark.write_disk
def test_scan_parquet_hive_schema(dataset_path: Path) -> None:
    result = pl.scan_parquet(dataset_path / "**/*.parquet", hive_partitioning=True)
    assert result.collect_schema() == OrderedDict(
        {"a": pl.Int64, "b": pl.Float64, "c": pl.Int64}
    )

    result = pl.scan_parquet(
        dataset_path / "**/*.parquet",
        hive_partitioning=True,
        hive_schema={"c": pl.Int32},
    )

    expected_schema = OrderedDict({"a": pl.Int64, "b": pl.Float64, "c": pl.Int32})
    assert result.collect_schema() == expected_schema
    assert result.collect().schema == expected_schema


@pytest.mark.write_disk
def test_read_parquet_invalid_hive_schema(dataset_path: Path) -> None:
    with pytest.raises(
        SchemaFieldNotFoundError,
        match='path contains column not present in the given Hive schema: "c"',
    ):
        pl.read_parquet(
            dataset_path / "**/*.parquet",
            hive_partitioning=True,
            hive_schema={"nonexistent": pl.Int32},
        )


def test_read_parquet_hive_schema_with_pyarrow() -> None:
    with pytest.raises(
        TypeError,
        match="cannot use `hive_partitions` with `use_pyarrow=True`",
    ):
        pl.read_parquet("test.parquet", hive_schema={"c": pl.Int32}, use_pyarrow=True)


@pytest.mark.parametrize(
    ("scan_func", "write_func"),
    [
        (pl.scan_parquet, pl.DataFrame.write_parquet),
        (pl.scan_ipc, pl.DataFrame.write_ipc),
    ],
)
@pytest.mark.parametrize(
    "glob",
    [True, False],
)
def test_hive_partition_directory_scan(
    tmp_path: Path,
    scan_func: Callable[..., pl.LazyFrame],
    write_func: Callable[[pl.DataFrame, Path], None],
    glob: bool,
) -> None:
    tmp_path.mkdir(exist_ok=True)

    dfs = [
        pl.DataFrame({'x': 5 * [1], 'a': 1, 'b': 1}),
        pl.DataFrame({'x': 5 * [2], 'a': 1, 'b': 2}),
        pl.DataFrame({'x': 5 * [3], 'a': 22, 'b': 1}),
        pl.DataFrame({'x': 5 * [4], 'a': 22, 'b': 2}),
    ]  # fmt: skip

    for df in dfs:
        a = df.item(0, "a")
        b = df.item(0, "b")
        path = tmp_path / f"a={a}/b={b}/data.bin"
        path.parent.mkdir(exist_ok=True, parents=True)
        write_func(df.drop("a", "b"), path)

    df = pl.concat(dfs)
    hive_schema = df.lazy().select("a", "b").collect_schema()

    scan = scan_func

    if scan_func is pl.scan_parquet:
        scan = partial(scan, glob=glob)

    scan_with_hive_schema = partial(scan_func, hive_schema=hive_schema)

    out = scan_with_hive_schema(
        tmp_path,
        hive_partitioning=True,
    ).collect()
    assert_frame_equal(out, df)

    out = scan(tmp_path, hive_partitioning=False).collect()
    assert_frame_equal(out, df.drop("a", "b"))

    out = scan_with_hive_schema(
        tmp_path / "a=1",
        hive_partitioning=True,
    ).collect()
    assert_frame_equal(out, df.filter(a=1).drop("a"))

    out = scan(tmp_path / "a=1", hive_partitioning=False).collect()
    assert_frame_equal(out, df.filter(a=1).drop("a", "b"))

    path = tmp_path / "a=1/b=1/data.bin"

    out = scan_with_hive_schema(path, hive_partitioning=True).collect()
    assert_frame_equal(out, dfs[0])

    out = scan(path, hive_partitioning=False).collect()
    assert_frame_equal(out, dfs[0].drop("a", "b"))

    # Test default behavior with `hive_partitioning=None`, which should only
    # enable hive partitioning when a single directory is passed:
    out = scan_with_hive_schema(tmp_path).collect()
    assert_frame_equal(out, df)

    # Otherwise, hive partitioning is not enabled automatically:
    out = scan(tmp_path / "a=1/b=1/data.bin").collect()
    assert out.columns == ["x"]

    out = scan([tmp_path / "a=1/", tmp_path / "a=22/"]).collect()
    assert out.columns == ["x"]

    out = scan([tmp_path / "a=1/", tmp_path / "a=22/b=1/data.bin"]).collect()
    assert out.columns == ["x"]

    if glob:
        out = scan(tmp_path / "a=1/**/*.bin").collect()
        assert out.columns == ["x"]

    # Test `hive_partitioning=True`
    out = scan_with_hive_schema(tmp_path, hive_partitioning=True).collect()
    assert_frame_equal(out, df)

    # Accept multiple directories from the same level
    out = scan_with_hive_schema(
        [tmp_path / "a=1", tmp_path / "a=22"], hive_partitioning=True
    ).collect()
    assert_frame_equal(out, df.drop("a"))

    with pytest.raises(
        pl.exceptions.InvalidOperationError,
        match="attempted to read from different directory levels with hive partitioning enabled:",
    ):
        scan_with_hive_schema(
            [tmp_path / "a=1", tmp_path / "a=22/b=1"], hive_partitioning=True
        ).collect()

    if glob:
        out = scan_with_hive_schema(
            tmp_path / "**/*.bin", hive_partitioning=True
        ).collect()
        assert_frame_equal(out, df)

        # Parse hive from full path for glob patterns
        out = scan_with_hive_schema(
            [tmp_path / "a=1/**/*.bin", tmp_path / "a=22/**/*.bin"],
            hive_partitioning=True,
        ).collect()
        assert_frame_equal(out, df)

    # Parse hive from full path for files
    out = scan_with_hive_schema(
        tmp_path / "a=1/b=1/data.bin", hive_partitioning=True
    ).collect()
    assert_frame_equal(out, df.filter(a=1, b=1))

    out = scan_with_hive_schema(
        [tmp_path / "a=1/b=1/data.bin", tmp_path / "a=22/b=1/data.bin"],
        hive_partitioning=True,
    ).collect()
    assert_frame_equal(
        out,
        df.filter(
            ((pl.col("a") == 1) & (pl.col("b") == 1))
            | ((pl.col("a") == 22) & (pl.col("b") == 1))
        ),
    )

    # Test `hive_partitioning=False`
    out = scan(tmp_path, hive_partitioning=False).collect()
    assert_frame_equal(out, df.drop("a", "b"))

    if glob:
        out = scan(tmp_path / "**/*.bin", hive_partitioning=False).collect()
        assert_frame_equal(out, df.drop("a", "b"))

    out = scan(tmp_path / "a=1/b=1/data.bin", hive_partitioning=False).collect()
    assert_frame_equal(out, df.filter(a=1, b=1).drop("a", "b"))


def test_hive_partition_schema_inference(tmp_path: Path) -> None:
    tmp_path.mkdir(exist_ok=True)

    dfs = [
        pl.DataFrame({"x": 1}),
        pl.DataFrame({"x": 2}),
        pl.DataFrame({"x": 3}),
    ]

    paths = [
        tmp_path / "a=1/data.bin",
        tmp_path / "a=1.5/data.bin",
        tmp_path / "a=polars/data.bin",
    ]

    expected = [
        pl.Series("a", [1], dtype=pl.Int64),
        pl.Series("a", [1.0, 1.5], dtype=pl.Float64),
        pl.Series("a", ["1", "1.5", "polars"], dtype=pl.String),
    ]

    for i in range(3):
        paths[i].parent.mkdir(exist_ok=True, parents=True)
        dfs[i].write_parquet(paths[i])
        out = pl.scan_parquet(tmp_path).collect()

        assert_series_equal(out["a"], expected[i])


@pytest.mark.write_disk
def test_hive_partition_force_async_17155(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("POLARS_FORCE_ASYNC", "1")
    monkeypatch.setenv("POLARS_PREFETCH_SIZE", "1")

    dfs = [
        pl.DataFrame({"x": 1}),
        pl.DataFrame({"x": 2}),
        pl.DataFrame({"x": 3}),
    ]

    paths = [
        tmp_path / "a=1/b=1/data.bin",
        tmp_path / "a=2/b=2/data.bin",
        tmp_path / "a=3/b=3/data.bin",
    ]

    for i in range(3):
        paths[i].parent.mkdir(exist_ok=True, parents=True)
        dfs[i].write_parquet(paths[i])

    lf = pl.scan_parquet(tmp_path)

    assert_frame_equal(
        lf.collect(), pl.DataFrame({k: [1, 2, 3] for k in ["x", "a", "b"]})
    )


@pytest.mark.parametrize(
    ("scan_func", "write_func"),
    [
        (partial(pl.scan_parquet, parallel="row_groups"), pl.DataFrame.write_parquet),
        (partial(pl.scan_parquet, parallel="columns"), pl.DataFrame.write_parquet),
        (partial(pl.scan_parquet, parallel="prefiltered"), pl.DataFrame.write_parquet),
        (
            lambda *a, **kw: pl.scan_parquet(*a, parallel="prefiltered", **kw).filter(
                pl.col("b") == pl.col("b")
            ),
            pl.DataFrame.write_parquet,
        ),
        (pl.scan_ipc, pl.DataFrame.write_ipc),
    ],
)
@pytest.mark.write_disk
@pytest.mark.slow
@pytest.mark.parametrize("projection_pushdown", [True, False])
def test_hive_partition_columns_contained_in_file(
    tmp_path: Path,
    scan_func: Callable[[Any], pl.LazyFrame],
    write_func: Callable[[pl.DataFrame, Path], None],
    projection_pushdown: bool,
) -> None:
    path = tmp_path / "a=1/b=2/data.bin"
    path.parent.mkdir(exist_ok=True, parents=True)
    df = pl.DataFrame(
        {"x": 1, "a": 1, "b": 2, "y": 1},
        schema={"x": pl.Int32, "a": pl.Int8, "b": pl.Int16, "y": pl.Int32},
    )
    write_func(df, path)

    def assert_with_projections(
        lf: pl.LazyFrame, df: pl.DataFrame, *, row_index: str | None = None
    ) -> None:
        row_index: list[str] = [row_index] if row_index is not None else []  # type: ignore[no-redef]

        from itertools import permutations

        cols = ["a", "b", "x", "y", *row_index]  # type: ignore[misc]

        for projection in (
            x for i in range(len(cols)) for x in permutations(cols[: 1 + i])
        ):
            assert_frame_equal(
                lf.select(projection).collect(
                    optimizations=pl.QueryOptFlags(
                        projection_pushdown=projection_pushdown
                    )
                ),
                df.select(projection),
            )

    lf = scan_func(path, hive_partitioning=True)  # type: ignore[call-arg]
    rhs = df
    assert_frame_equal(
        lf.collect(
            optimizations=pl.QueryOptFlags(projection_pushdown=projection_pushdown)
        ),
        rhs,
    )
    assert_with_projections(lf, rhs)

    lf = scan_func(  # type: ignore[call-arg]
        path,
        hive_schema={"a": pl.String, "b": pl.String},
        hive_partitioning=True,
    )
    rhs = df.with_columns(pl.col("a", "b").cast(pl.String))
    assert_frame_equal(
        lf.collect(
            optimizations=pl.QueryOptFlags(projection_pushdown=projection_pushdown)
        ),
        rhs,
    )
    assert_with_projections(lf, rhs)

    # partial cols in file
    partial_path = tmp_path / "a=1/b=2/partial_data.bin"
    df = pl.DataFrame(
        {"x": 1, "b": 2, "y": 1},
        schema={"x": pl.Int32, "b": pl.Int16, "y": pl.Int32},
    )
    write_func(df, partial_path)

    rhs = rhs.select(
        pl.col("x").cast(pl.Int32),
        pl.col("b").cast(pl.Int16),
        pl.col("y").cast(pl.Int32),
        pl.col("a").cast(pl.Int64),
    )

    lf = scan_func(partial_path, hive_partitioning=True)  # type: ignore[call-arg]
    assert_frame_equal(
        lf.collect(
            optimizations=pl.QueryOptFlags(projection_pushdown=projection_pushdown)
        ),
        rhs,
    )
    assert_with_projections(lf, rhs)

    assert_frame_equal(
        lf.with_row_index().collect(
            optimizations=pl.QueryOptFlags(projection_pushdown=projection_pushdown)
        ),
        rhs.with_row_index(),
    )
    assert_with_projections(
        lf.with_row_index(), rhs.with_row_index(), row_index="index"
    )

    assert_frame_equal(
        lf.with_row_index()
        .select(pl.exclude("index"), "index")
        .collect(
            optimizations=pl.QueryOptFlags(projection_pushdown=projection_pushdown)
        ),
        rhs.with_row_index().select(pl.exclude("index"), "index"),
    )
    assert_with_projections(
        lf.with_row_index().select(pl.exclude("index"), "index"),
        rhs.with_row_index().select(pl.exclude("index"), "index"),
        row_index="index",
    )

    lf = scan_func(  # type: ignore[call-arg]
        partial_path,
        hive_schema={"a": pl.String, "b": pl.String},
        hive_partitioning=True,
    )
    rhs = rhs.select(
        pl.col("x").cast(pl.Int32),
        pl.col("b").cast(pl.String),
        pl.col("y").cast(pl.Int32),
        pl.col("a").cast(pl.String),
    )
    assert_frame_equal(
        lf.collect(
            optimizations=pl.QueryOptFlags(projection_pushdown=projection_pushdown)
        ),
        rhs,
    )
    assert_with_projections(lf, rhs)


@pytest.mark.write_disk
def test_hive_partition_dates(tmp_path: Path) -> None:
    df = pl.DataFrame(
        {
            "date1": [
                datetime(2024, 1, 1),
                datetime(2024, 2, 1),
                datetime(2024, 3, 1),
                None,
            ],
            "date2": [
                datetime(2023, 1, 1),
                datetime(2023, 2, 1),
                None,
                datetime(2023, 3, 1),
            ],
            "x": [1, 2, 3, 4],
        },
        schema={"date1": pl.Date, "date2": pl.Datetime, "x": pl.Int32},
    )

    root = tmp_path / "pyarrow"
    pq.write_to_dataset(
        df.to_arrow(),
        root_path=root,
        partition_cols=["date1", "date2"],
    )

    lf = pl.scan_parquet(
        root, hive_schema=df.clear().select("date1", "date2").collect_schema()
    )
    assert_frame_equal(lf.collect(), df.select("x", "date1", "date2"))

    lf = pl.scan_parquet(root)
    assert_frame_equal(lf.collect(), df.select("x", "date1", "date2"))

    lf = pl.scan_parquet(root, try_parse_hive_dates=False)
    assert_frame_equal(
        lf.collect(),
        df.select("x", "date1", "date2").with_columns(
            pl.col("date1", "date2").cast(pl.String)
        ),
    )

    for perc_escape in [True, False] if sys.platform != "win32" else [True]:
        root = tmp_path / f"includes_hive_cols_in_file_{perc_escape}"
        for (date1, date2), part_df in df.group_by(
            pl.col("date1").cast(pl.String).fill_null("__HIVE_DEFAULT_PARTITION__"),
            pl.col("date2").cast(pl.String).fill_null("__HIVE_DEFAULT_PARTITION__"),
        ):
            if perc_escape:
                date2 = urllib.parse.quote(date2)  # type: ignore[call-overload]

            path = root / f"date1={date1}/date2={date2}/data.bin"
            path.parent.mkdir(exist_ok=True, parents=True)
            part_df.write_parquet(path)

        # The schema for the hive columns is included in the file, so it should
        # just work
        lf = pl.scan_parquet(root)
        assert_frame_equal(lf.collect(), df)

        lf = pl.scan_parquet(root, try_parse_hive_dates=False)
        assert_frame_equal(
            lf.collect(),
            df.with_columns(pl.col("date1", "date2").cast(pl.String)),
        )


@pytest.mark.write_disk
def test_hive_partition_filter_null_23005(tmp_path: Path) -> None:
    root = tmp_path

    df = pl.DataFrame(
        {
            "date1": [
                datetime(2024, 1, 1),
                datetime(2024, 2, 1),
                datetime(2024, 3, 1),
                None,
            ],
            "date2": [
                datetime(2023, 1, 1),
                datetime(2023, 2, 1),
                None,
                datetime(2023, 3, 1),
            ],
            "x": [1, 2, 3, 4],
        },
        schema={"date1": pl.Date, "date2": pl.Datetime, "x": pl.Int32},
    )

    df.write_parquet(root, partition_by=["date1", "date2"])

    q = pl.scan_parquet(root, include_file_paths="path")

    full = q.collect()

    assert (
        full.select(
            (
                pl.any_horizontal(pl.col("date1", "date2").is_null())
                & pl.col("path").str.contains("__HIVE_DEFAULT_PARTITION__")
            ).sum()
        ).item()
        == 2
    )

    lf = pl.scan_parquet(root).filter(pl.col("date1") == datetime(2024, 1, 1))
    assert_frame_equal(lf.collect(), df.head(1))


@pytest.mark.parametrize(
    ("scan_func", "write_func"),
    [
        (pl.scan_parquet, pl.DataFrame.write_parquet),
        (pl.scan_ipc, pl.DataFrame.write_ipc),
    ],
)
@pytest.mark.write_disk
def test_projection_only_hive_parts_gives_correct_number_of_rows(
    tmp_path: Path,
    scan_func: Callable[[Any], pl.LazyFrame],
    write_func: Callable[[pl.DataFrame, Path], None],
) -> None:
    # Check the number of rows projected when projecting only hive parts, which
    # should be the same as the number of rows in the file.
    path = tmp_path / "a=3/data.bin"
    path.parent.mkdir(exist_ok=True, parents=True)

    write_func(pl.DataFrame({"x": [1, 1, 1]}), path)

    assert_frame_equal(
        scan_func(path, hive_partitioning=True).select("a").collect(),  # type: ignore[call-arg]
        pl.DataFrame({"a": [3, 3, 3]}),
    )


@pytest.mark.parametrize(
    "df",
    [
        pl.select(
            pl.Series("a", [1, 2, 3, 4], dtype=pl.Int8),
            pl.Series("b", [1, 2, 3, 4], dtype=pl.Int8),
            pl.Series("x", [1, 2, 3, 4]),
        ),
        pl.select(
            pl.Series(
                "a",
                [1.2981275, 2.385974035, 3.1231892749185718397510, 4.129387128949156],
                dtype=pl.Float64,
            ),
            pl.Series("b", ["a", "b", " / c = : ", "d"]),
            pl.Series("x", [1, 2, 3, 4]),
        ),
    ],
)
@pytest.mark.write_disk
def test_hive_write(tmp_path: Path, df: pl.DataFrame) -> None:
    root = tmp_path
    df.write_parquet(root, partition_by=["a", "b"])

    lf = pl.scan_parquet(root)
    assert_frame_equal(lf.collect(), df)

    lf = pl.scan_parquet(root, hive_schema={"a": pl.String, "b": pl.String})
    assert_frame_equal(lf.collect(), df.with_columns(pl.col("a", "b").cast(pl.String)))


@pytest.mark.slow
@pytest.mark.write_disk
@pytest.mark.xfail
def test_hive_write_multiple_files(tmp_path: Path) -> None:
    chunk_size = 262_144
    n_rows = 100_000
    df = pl.select(a=pl.repeat(0, n_rows), b=pl.int_range(0, n_rows))

    n_files = int(df.estimated_size() / chunk_size)

    assert n_files > 1, "increase df size or decrease file size"

    root = tmp_path
    df.write_parquet(root, partition_by="a", partition_chunk_size_bytes=chunk_size)

    assert sum(1 for _ in (root / "a=0").iterdir()) == n_files
    assert_frame_equal(pl.scan_parquet(root).collect(), df)


@pytest.mark.write_disk
def test_hive_write_dates(tmp_path: Path) -> None:
    df = pl.DataFrame(
        {
            "date1": [
                datetime(2024, 1, 1),
                datetime(2024, 2, 1),
                datetime(2024, 3, 1),
                None,
            ],
            "date2": [
                datetime(2023, 1, 1),
                datetime(2023, 2, 1),
                None,
                datetime(2023, 3, 1, 1, 1, 1, 1),
            ],
            "x": [1, 2, 3, 4],
        },
        schema={"date1": pl.Date, "date2": pl.Datetime, "x": pl.Int32},
    )

    root = tmp_path
    df.write_parquet(root, partition_by=["date1", "date2"])

    lf = pl.scan_parquet(root)
    assert_frame_equal(lf.collect(), df)

    lf = pl.scan_parquet(root, try_parse_hive_dates=False)
    assert_frame_equal(
        lf.collect(),
        df.with_columns(pl.col("date1", "date2").cast(pl.String)),
    )


@pytest.mark.write_disk
@pytest.mark.may_fail_auto_streaming
def test_hive_predicate_dates_14712(
    tmp_path: Path, monkeypatch: Any, capfd: Any
) -> None:
    monkeypatch.setenv("POLARS_VERBOSE", "1")
    pl.DataFrame({"a": [datetime(2024, 1, 1)]}).write_parquet(
        tmp_path, partition_by="a"
    )
    pl.scan_parquet(tmp_path).filter(pl.col("a") != datetime(2024, 1, 1)).collect()
    assert "allows skipping 1 / 1" in capfd.readouterr().err


@pytest.mark.skipif(sys.platform != "win32", reason="Test is only for Windows paths")
@pytest.mark.write_disk
def test_hive_windows_splits_on_forward_slashes(tmp_path: Path) -> None:
    # Note: This needs to be an absolute path.
    tmp_path = tmp_path.resolve()
    path = f"{tmp_path}/a=1/b=1/c=1/d=1/e=1"
    Path(path).mkdir(exist_ok=True, parents=True)

    df = pl.DataFrame({"x": "x"})
    df.write_parquet(f"{path}/data.parquet")

    expect = pl.DataFrame(
        [
            s.new_from_index(0, 5)
            for s in pl.DataFrame(
                {
                    "x": "x",
                    "a": 1,
                    "b": 1,
                    "c": 1,
                    "d": 1,
                    "e": 1,
                }
            )
        ]
    )

    assert_frame_equal(
        pl.scan_parquet(
            [
                f"{tmp_path}/a=1/b=1/c=1/d=1/e=1/data.parquet",
                f"{tmp_path}\\a=1\\b=1\\c=1\\d=1\\e=1\\data.parquet",
                f"{tmp_path}\\a=1/b=1/c=1/d=1/**/*",
                f"{tmp_path}/a=1/b=1\\c=1/d=1/**/*",
                f"{tmp_path}/a=1/b=1/c=1/d=1\\e=1/*",
            ],
            hive_partitioning=True,
        ).collect(),
        expect,
    )


@pytest.mark.write_disk
def test_passing_hive_schema_with_hive_partitioning_disabled_raises(
    tmp_path: Path,
) -> None:
    with pytest.raises(
        ComputeError,
        match="a hive schema was given but hive_partitioning was disabled",
    ):
        pl.scan_parquet(
            tmp_path,
            schema={"x": pl.Int64},
            hive_schema={"h": pl.String},
            hive_partitioning=False,
        ).collect()


@pytest.mark.write_disk
def test_hive_auto_enables_when_unspecified_and_hive_schema_passed(
    tmp_path: Path,
) -> None:
    tmp_path.mkdir(exist_ok=True)
    (tmp_path / "a=1").mkdir(exist_ok=True)

    pl.DataFrame({"x": 1}).write_parquet(tmp_path / "a=1/1")

    for path in [tmp_path / "a=1/1", tmp_path / "**/*"]:
        lf = pl.scan_parquet(path, hive_schema={"a": pl.UInt8})

        assert_frame_equal(
            lf.collect(),
            pl.select(
                pl.Series("x", [1]),
                pl.Series("a", [1], dtype=pl.UInt8),
            ),
        )


@pytest.mark.write_disk
def test_hive_file_as_uri_with_hive_start_idx_23830(
    tmp_path: Path,
) -> None:
    tmp_path.mkdir(exist_ok=True)
    (tmp_path / "a=1").mkdir(exist_ok=True)

    pl.DataFrame({"x": 1}).write_parquet(tmp_path / "a=1/1")

    # ensure we have a trailing "/"
    uri = tmp_path.resolve().as_posix().rstrip("/") + "/"
    uri = "file://" + uri

    lf = pl.scan_parquet(uri, hive_schema={"a": pl.UInt8})

    assert_frame_equal(
        lf.collect(),
        pl.select(
            pl.Series("x", [1]),
            pl.Series("a", [1], dtype=pl.UInt8),
        ),
    )


@pytest.mark.write_disk
@pytest.mark.parametrize("force_single_thread", [True, False])
def test_hive_parquet_prefiltered_20894_21327(
    tmp_path: Path, force_single_thread: bool
) -> None:
    n_threads = 1 if force_single_thread else pl.thread_pool_size()

    file_path = tmp_path / "date=2025-01-01/00000000.parquet"
    file_path.parent.mkdir(exist_ok=True, parents=True)

    data = pl.DataFrame(
        {
            "date": [date(2025, 1, 1), date(2025, 1, 1)],
            "value": ["1", "2"],
        }
    )

    data.write_parquet(file_path)

    import base64
    import subprocess

    # For security, and for Windows backslashes.
    scan_path_b64 = base64.b64encode(str(file_path.absolute()).encode()).decode()

    # This is, the easiest way to control the threadpool size so that it is stable.
    out = subprocess.check_output(
        [
            sys.executable,
            "-c",
            f"""\
import os
os.environ["POLARS_MAX_THREADS"] = "{n_threads}"

import polars as pl
import datetime
import base64

from polars.testing import assert_frame_equal

assert pl.thread_pool_size() == {n_threads}

tmp_path = base64.b64decode("{scan_path_b64}").decode()
df = pl.scan_parquet(tmp_path, hive_partitioning=True).filter(pl.col("value") == "1").collect()
# We need the str() to trigger panic on invalid state
str(df)

assert_frame_equal(df, pl.DataFrame(
    [
        pl.Series('date', [datetime.date(2025, 1, 1)], dtype=pl.Date),
        pl.Series('value', ['1'], dtype=pl.String),
    ]
))

print("OK", end="")
""",
        ],
    )

    assert out == b"OK"
