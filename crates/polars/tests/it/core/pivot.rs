use chrono::NaiveDate;
use polars::prelude::*;
use polars_lazy::frame::pivot::PivotExpr;
use polars_ops::pivot::{PivotAgg, pivot, pivot_stable};

#[test]
#[cfg(feature = "dtype-date")]
fn test_pivot_date_() -> PolarsResult<()> {
    let mut df = df![
        "index" => [8, 2, 3, 6, 3, 6, 2, 2],
        "values1" => [1000, 1000, 1000, 1000, 1000, 1000, 1000, 1000],
        "values_2" => [1, 1, 1, 1, 1, 1, 1, 1],
    ]?;
    df.try_apply("values1", |s| s.cast(&DataType::Date))?;

    // Test with date as the `columns` input
    let out = pivot(
        &df,
        ["values1"],
        Some(["index"]),
        Some(["values_2"]),
        true,
        Some(PivotAgg(Arc::new(PivotExpr::from_expr(col("").count())))),
        None,
    )?;

    let first = 1 as IdxSize;
    let expected = df![
        "index" => [8i32, 2, 3, 6],
        "1972-09-27" => [first, 3, 2, 2]
    ]?;
    assert!(out.equals_missing(&expected));

    // Test with date as the `values` input.
    let mut out = pivot_stable(
        &df,
        ["values_2"],
        Some(["index"]),
        Some(["values1"]),
        true,
        Some(PivotAgg(Arc::new(PivotExpr::from_expr(col("").first())))),
        None,
    )?;
    out.try_apply("1", |s| {
        let ca = s.date()?;
        ca.to_string("%Y-%d-%m")
    })?;

    let expected = df![
        "index" => [8i32, 2, 3, 6],
        "1" => ["1972-27-09", "1972-27-09", "1972-27-09", "1972-27-09"]
    ]?;
    assert!(out.equals_missing(&expected));

    Ok(())
}

#[test]
fn test_pivot_old() {
    let s0 = Column::new("index".into(), ["A", "A", "B", "B", "C"].as_ref());
    let s2 = Column::new("columns".into(), ["k", "l", "m", "m", "l"].as_ref());
    let s1 = Column::new("values".into(), [1, 2, 2, 4, 2].as_ref());
    let df = DataFrame::new(vec![s0, s1, s2]).unwrap();

    let pvt = pivot(
        &df,
        ["columns"],
        Some(["index"]),
        Some(["values"]),
        false,
        Some(PivotAgg(Arc::new(PivotExpr::from_expr(col("").sum())))),
        None,
    )
    .unwrap();
    assert_eq!(pvt.get_column_names(), &["index", "k", "l", "m"]);
    assert_eq!(
        Vec::from(&pvt.column("m").unwrap().i32().unwrap().sort(false)),
        &[Some(0), Some(0), Some(6)]
    );
    let pvt = pivot(
        &df,
        ["columns"],
        Some(["index"]),
        Some(["values"]),
        false,
        Some(PivotAgg(Arc::new(PivotExpr::from_expr(col("").min())))),
        None,
    )
    .unwrap();
    assert_eq!(
        Vec::from(&pvt.column("m").unwrap().i32().unwrap().sort(false)),
        &[None, None, Some(2)]
    );
    let pvt = pivot(
        &df,
        ["columns"],
        Some(["index"]),
        Some(["values"]),
        false,
        Some(PivotAgg(Arc::new(PivotExpr::from_expr(col("").max())))),
        None,
    )
    .unwrap();
    assert_eq!(
        Vec::from(&pvt.column("m").unwrap().i32().unwrap().sort(false)),
        &[None, None, Some(4)]
    );
    let pvt = pivot(
        &df,
        ["columns"],
        Some(["index"]),
        Some(["values"]),
        false,
        Some(PivotAgg(Arc::new(PivotExpr::from_expr(col("").mean())))),
        None,
    )
    .unwrap();
    assert_eq!(
        Vec::from(&pvt.column("m").unwrap().f64().unwrap().sort(false)),
        &[None, None, Some(3.0)]
    );
    let pvt = pivot(
        &df,
        ["columns"],
        Some(["index"]),
        Some(["values"]),
        false,
        Some(PivotAgg(Arc::new(PivotExpr::from_expr(col("").len())))),
        None,
    )
    .unwrap();
    assert_eq!(
        Vec::from(&pvt.column("m").unwrap().idx().unwrap().sort(false)),
        &[Some(0), Some(0), Some(2)]
    );
}

#[test]
#[cfg(feature = "dtype-categorical")]
fn test_pivot_categorical() -> PolarsResult<()> {
    let mut df = df![
        "index" => [1, 1, 1, 1, 1, 1, 1, 1],
        "columns" => ["a", "b", "c", "a", "b", "c", "a", "b"],
        "values" => [8, 2, 3, 6, 3, 6, 2, 2],
    ]?;
    df.try_apply("columns", |s| {
        s.cast(&DataType::from_categories(Categories::global()))
    })?;

    let out = pivot(
        &df,
        ["columns"],
        Some(["index"]),
        Some(["values"]),
        true,
        Some(PivotAgg(Arc::new(PivotExpr::from_expr(col("").len())))),
        None,
    )?;
    assert_eq!(out.get_column_names(), &["index", "a", "b", "c"]);

    Ok(())
}

#[test]
fn test_pivot_new() -> PolarsResult<()> {
    let df = df![
        "index1"=> ["foo", "foo", "foo", "foo", "foo", "bar", "bar", "bar", "bar"],
        "index2"=> ["one", "one", "one", "two", "two", "one", "one", "two", "two"],
        "cols1"=> ["small", "large", "large", "small", "small", "large", "small", "small", "large"],
        "cols2"=> ["jam", "egg", "egg", "egg", "jam", "jam", "potato", "jam", "jam"],
        "values1"=> [1, 2, 2, 3, 3, 4, 5, 6, 7],
        "values_2"=> [2, 4, 5, 5, 6, 6, 8, 9, 9]
    ]?;

    let out = (pivot_stable(
        &df,
        ["cols1"],
        Some(["index1", "index2"]),
        Some(["values1"]),
        true,
        Some(PivotAgg(Arc::new(PivotExpr::from_expr(col("").sum())))),
        None,
    ))?;
    let expected = df![
        "index1" => ["foo", "foo", "bar", "bar"],
        "index2" => ["one", "two", "one", "two"],
        "large" => [Some(4), Some(0), Some(4), Some(7)],
        "small" => [1, 6, 5, 6],
    ]?;
    assert!(out.equals_missing(&expected));

    let out = pivot_stable(
        &df,
        ["cols1", "cols2"],
        Some(["index1", "index2"]),
        Some(["values1"]),
        true,
        Some(PivotAgg(Arc::new(PivotExpr::from_expr(col("").sum())))),
        None,
    )?;
    let expected = df![
        "index1" => ["foo", "foo", "bar", "bar"],
        "index2" => ["one", "two", "one", "two"],
        "{\"large\",\"egg\"}" => [Some(4), Some(0), Some(0), Some(0)],
        "{\"large\",\"jam\"}" => [Some(0), Some(0), Some(4), Some(7)],
        "{\"small\",\"egg\"}" => [Some(0), Some(3), Some(0), Some(0)],
        "{\"small\",\"jam\"}" => [Some(1), Some(3), Some(0), Some(6)],
        "{\"small\",\"potato\"}" => [Some(0), Some(0), Some(5), Some(0)],
    ]?;
    assert!(out.equals_missing(&expected));

    Ok(())
}

#[test]
fn test_pivot_2() -> PolarsResult<()> {
    let df = df![
        "index" => [Some("name1"), Some("name2"), None, Some("name1"), Some("name2")],
        "columns"=> ["avg", "avg", "act", "test", "test"],
        "values"=> [0.0, 0.1, 1.0, 0.4, 0.2]
    ]?;

    let out = pivot_stable(
        &df,
        ["columns"],
        Some(["index"]),
        Some(["values"]),
        false,
        Some(PivotAgg(Arc::new(PivotExpr::from_expr(col("").first())))),
        None,
    )?;
    let expected = df![
        "index" => [Some("name1"), Some("name2"), None],
        "avg" => [Some(0.0), Some(0.1), None],
        "act" => [None, None, Some(1.)],
        "test" => [Some(0.4), Some(0.2), None],
    ]?;
    assert!(out.equals_missing(&expected));

    Ok(())
}

#[test]
#[cfg(feature = "dtype-datetime")]
fn test_pivot_datetime() -> PolarsResult<()> {
    let dt = NaiveDate::from_ymd_opt(2021, 1, 1)
        .unwrap()
        .and_hms_opt(12, 15, 0)
        .unwrap();
    let df = df![
        "index" => [dt, dt, dt, dt],
        "columns" => ["x", "x", "y", "y"],
        "values" => [100, 50, 500, -80]
    ]?;

    let out = pivot(
        &df,
        ["columns"],
        Some(["index"]),
        Some(["values"]),
        false,
        Some(PivotAgg(Arc::new(PivotExpr::from_expr(col("").sum())))),
        None,
    )?;
    let expected = df![
        "index" => [dt],
        "x" => [150],
        "y" => [420]
    ]?;
    assert!(out.equals(&expected));

    Ok(())
}
