mod args;
#[cfg(feature = "asof_join")]
mod asof;
mod cross_join;
mod dispatch_left_right;
mod general;
mod hash_join;
#[cfg(feature = "iejoin")]
mod iejoin;
#[cfg(feature = "merge_sorted")]
mod merge_sorted;

use std::borrow::Cow;
use std::fmt::{Debug, Display, Formatter};
use std::hash::Hash;

pub use args::*;
use arrow::trusted_len::TrustedLen;
#[cfg(feature = "asof_join")]
pub use asof::{AsOfOptions, AsofJoin, AsofJoinBy, AsofStrategy};
pub use cross_join::CrossJoin;
#[cfg(feature = "chunked_ids")]
use either::Either;
#[cfg(feature = "chunked_ids")]
use general::create_chunked_index_mapping;
pub use general::{_coalesce_full_join, _finish_join, _join_suffix_name};
pub use hash_join::*;
use hashbrown::hash_map::{Entry, RawEntryMut};
#[cfg(feature = "iejoin")]
pub use iejoin::{IEJoinOptions, InequalityOperator};
#[cfg(feature = "merge_sorted")]
pub use merge_sorted::_merge_sorted_dfs;
use polars_core::POOL;
#[allow(unused_imports)]
use polars_core::chunked_array::ops::row_encode::{
    encode_rows_vertical_par_unordered, encode_rows_vertical_par_unordered_broadcast_nulls,
};
use polars_core::hashing::_HASHMAP_INIT_SIZE;
use polars_core::prelude::*;
pub(super) use polars_core::series::IsSorted;
use polars_core::utils::slice_offsets;
#[allow(unused_imports)]
use polars_core::utils::slice_slice;
use polars_utils::hashing::BytesHash;
use rayon::prelude::*;

use self::cross_join::fused_cross_filter;
use super::IntoDf;

pub trait DataFrameJoinOps: IntoDf {
    /// Generic join method. Can be used to join on multiple columns.
    ///
    /// # Example
    ///
    /// ```no_run
    /// # use polars_core::prelude::*;
    /// # use polars_ops::prelude::*;
    /// let df1: DataFrame = df!("Fruit" => &["Apple", "Banana", "Pear"],
    ///                          "Phosphorus (mg/100g)" => &[11, 22, 12])?;
    /// let df2: DataFrame = df!("Name" => &["Apple", "Banana", "Pear"],
    ///                          "Potassium (mg/100g)" => &[107, 358, 115])?;
    ///
    /// let df3: DataFrame = df1.join(&df2, ["Fruit"], ["Name"], JoinArgs::new(JoinType::Inner),
    /// None)?;
    /// assert_eq!(df3.shape(), (3, 3));
    /// println!("{}", df3);
    /// # Ok::<(), PolarsError>(())
    /// ```
    ///
    /// Output:
    ///
    /// ```text
    /// shape: (3, 3)
    /// +--------+----------------------+---------------------+
    /// | Fruit  | Phosphorus (mg/100g) | Potassium (mg/100g) |
    /// | ---    | ---                  | ---                 |
    /// | str    | i32                  | i32                 |
    /// +========+======================+=====================+
    /// | Apple  | 11                   | 107                 |
    /// +--------+----------------------+---------------------+
    /// | Banana | 22                   | 358                 |
    /// +--------+----------------------+---------------------+
    /// | Pear   | 12                   | 115                 |
    /// +--------+----------------------+---------------------+
    /// ```
    fn join(
        &self,
        other: &DataFrame,
        left_on: impl IntoIterator<Item = impl Into<PlSmallStr>>,
        right_on: impl IntoIterator<Item = impl Into<PlSmallStr>>,
        args: JoinArgs,
        options: Option<JoinTypeOptions>,
    ) -> PolarsResult<DataFrame> {
        let df_left = self.to_df();
        let selected_left = df_left.select_columns(left_on)?;
        let selected_right = other.select_columns(right_on)?;

        let selected_left = selected_left
            .into_iter()
            .map(Column::take_materialized_series)
            .collect::<Vec<_>>();
        let selected_right = selected_right
            .into_iter()
            .map(Column::take_materialized_series)
            .collect::<Vec<_>>();

        self._join_impl(
            other,
            selected_left,
            selected_right,
            args,
            options,
            true,
            false,
        )
    }

    #[doc(hidden)]
    #[allow(clippy::too_many_arguments)]
    #[allow(unused_mut)]
    fn _join_impl(
        &self,
        other: &DataFrame,
        mut selected_left: Vec<Series>,
        mut selected_right: Vec<Series>,
        mut args: JoinArgs,
        options: Option<JoinTypeOptions>,
        _check_rechunk: bool,
        _verbose: bool,
    ) -> PolarsResult<DataFrame> {
        let left_df = self.to_df();

        #[cfg(feature = "cross_join")]
        if let JoinType::Cross = args.how {
            if let Some(JoinTypeOptions::Cross(cross_options)) = &options {
                assert!(args.slice.is_none());
                return fused_cross_filter(left_df, other, args.suffix.clone(), cross_options);
            }
            return left_df.cross_join(other, args.suffix.clone(), args.slice);
        }

        // Clear literals if a frame is empty. Otherwise we could get an oob
        fn clear(s: &mut [Series]) {
            for s in s.iter_mut() {
                if s.len() == 1 {
                    *s = s.clear()
                }
            }
        }
        if left_df.is_empty() {
            clear(&mut selected_left);
        }
        if other.is_empty() {
            clear(&mut selected_right);
        }

        let should_coalesce = args.should_coalesce();
        assert_eq!(selected_left.len(), selected_right.len());

        #[cfg(feature = "chunked_ids")]
        {
            // a left join create chunked-ids
            // the others not yet.
            // TODO! change this to other join types once they support chunked-id joins
            if _check_rechunk
                && !(matches!(args.how, JoinType::Left)
                    || std::env::var("POLARS_NO_CHUNKED_JOIN").is_ok())
            {
                let mut left = Cow::Borrowed(left_df);
                let mut right = Cow::Borrowed(other);
                if left_df.should_rechunk() {
                    if _verbose {
                        eprintln!(
                            "{:?} join triggered a rechunk of the left DataFrame: {} columns are affected",
                            args.how,
                            left_df.width()
                        );
                    }

                    let mut tmp_left = left_df.clone();
                    tmp_left.as_single_chunk_par();
                    left = Cow::Owned(tmp_left);
                }
                if other.should_rechunk() {
                    if _verbose {
                        eprintln!(
                            "{:?} join triggered a rechunk of the right DataFrame: {} columns are affected",
                            args.how,
                            other.width()
                        );
                    }
                    let mut tmp_right = other.clone();
                    tmp_right.as_single_chunk_par();
                    right = Cow::Owned(tmp_right);
                }
                return left._join_impl(
                    &right,
                    selected_left,
                    selected_right,
                    args,
                    options,
                    false,
                    _verbose,
                );
            }
        }

        if let Some((l, r)) = selected_left
            .iter()
            .zip(&selected_right)
            .find(|(l, r)| l.dtype() != r.dtype())
        {
            polars_bail!(
                ComputeError:
                    format!(
                        "datatypes of join keys don't match - `{}`: {} on left does not match `{}`: {} on right",
                        l.name(), l.dtype(), r.name(), r.dtype()
                    )
            );
        };

        #[cfg(feature = "iejoin")]
        if let JoinType::IEJoin = args.how {
            let Some(JoinTypeOptions::IEJoin(options)) = options else {
                unreachable!()
            };
            let func = if POOL.current_num_threads() > 1 && !left_df.is_empty() && !other.is_empty()
            {
                iejoin::iejoin_par
            } else {
                iejoin::iejoin
            };
            return func(
                left_df,
                other,
                selected_left,
                selected_right,
                &options,
                args.suffix,
                args.slice,
            );
        }

        // Single keys.
        if selected_left.len() == 1 {
            let s_left = &selected_left[0];
            let s_right = &selected_right[0];
            let drop_names: Option<Vec<PlSmallStr>> =
                if should_coalesce { None } else { Some(vec![]) };
            return match args.how {
                JoinType::Inner => left_df
                    ._inner_join_from_series(other, s_left, s_right, args, _verbose, drop_names),
                JoinType::Left => dispatch_left_right::left_join_from_series(
                    self.to_df().clone(),
                    other,
                    s_left,
                    s_right,
                    args,
                    _verbose,
                    drop_names,
                ),
                JoinType::Right => dispatch_left_right::right_join_from_series(
                    self.to_df(),
                    other.clone(),
                    s_left,
                    s_right,
                    args,
                    _verbose,
                    drop_names,
                ),
                JoinType::Full => left_df._full_join_from_series(other, s_left, s_right, args),
                #[cfg(feature = "semi_anti_join")]
                JoinType::Anti => left_df._semi_anti_join_from_series(
                    s_left,
                    s_right,
                    args.slice,
                    true,
                    args.nulls_equal,
                ),
                #[cfg(feature = "semi_anti_join")]
                JoinType::Semi => left_df._semi_anti_join_from_series(
                    s_left,
                    s_right,
                    args.slice,
                    false,
                    args.nulls_equal,
                ),
                #[cfg(feature = "asof_join")]
                JoinType::AsOf(options) => match (options.left_by, options.right_by) {
                    (Some(left_by), Some(right_by)) => left_df._join_asof_by(
                        other,
                        s_left,
                        s_right,
                        left_by,
                        right_by,
                        options.strategy,
                        options.tolerance.map(|v| v.into_value()),
                        args.suffix.clone(),
                        args.slice,
                        should_coalesce,
                        options.allow_eq,
                        options.check_sortedness,
                    ),
                    (None, None) => left_df._join_asof(
                        other,
                        s_left,
                        s_right,
                        options.strategy,
                        options.tolerance.map(|v| v.into_value()),
                        args.suffix,
                        args.slice,
                        should_coalesce,
                        options.allow_eq,
                        options.check_sortedness,
                    ),
                    _ => {
                        panic!("expected by arguments on both sides")
                    },
                },
                #[cfg(feature = "iejoin")]
                JoinType::IEJoin => {
                    unreachable!()
                },
                JoinType::Cross => {
                    unreachable!()
                },
            };
        }
        let (lhs_keys, rhs_keys) =
            if (left_df.is_empty() || other.is_empty()) && matches!(&args.how, JoinType::Inner) {
                // Fast path for empty inner joins.
                // Return 2 dummies so that we don't row-encode.
                let a = Series::full_null("".into(), 0, &DataType::Null);
                (a.clone(), a)
            } else {
                // Row encode the keys.
                (
                    prepare_keys_multiple(&selected_left, args.nulls_equal)?.into_series(),
                    prepare_keys_multiple(&selected_right, args.nulls_equal)?.into_series(),
                )
            };

        let drop_names = if should_coalesce {
            if args.how == JoinType::Right {
                selected_left
                    .iter()
                    .map(|s| s.name().clone())
                    .collect::<Vec<_>>()
            } else {
                selected_right
                    .iter()
                    .map(|s| s.name().clone())
                    .collect::<Vec<_>>()
            }
        } else {
            vec![]
        };

        // Multiple keys.
        match args.how {
            #[cfg(feature = "asof_join")]
            JoinType::AsOf(_) => polars_bail!(
                ComputeError: "asof join not supported for join on multiple keys"
            ),
            #[cfg(feature = "iejoin")]
            JoinType::IEJoin => {
                unreachable!()
            },
            JoinType::Cross => {
                unreachable!()
            },
            JoinType::Full => {
                let names_left = selected_left
                    .iter()
                    .map(|s| s.name().clone())
                    .collect::<Vec<_>>();
                args.coalesce = JoinCoalesce::KeepColumns;
                let suffix = args.suffix.clone();
                let out = left_df._full_join_from_series(other, &lhs_keys, &rhs_keys, args);

                if should_coalesce {
                    Ok(_coalesce_full_join(
                        out?,
                        names_left.as_slice(),
                        drop_names.as_slice(),
                        suffix,
                        left_df,
                    ))
                } else {
                    out
                }
            },
            JoinType::Inner => left_df._inner_join_from_series(
                other,
                &lhs_keys,
                &rhs_keys,
                args,
                _verbose,
                Some(drop_names),
            ),
            JoinType::Left => dispatch_left_right::left_join_from_series(
                left_df.clone(),
                other,
                &lhs_keys,
                &rhs_keys,
                args,
                _verbose,
                Some(drop_names),
            ),
            JoinType::Right => dispatch_left_right::right_join_from_series(
                left_df,
                other.clone(),
                &lhs_keys,
                &rhs_keys,
                args,
                _verbose,
                Some(drop_names),
            ),
            #[cfg(feature = "semi_anti_join")]
            JoinType::Anti | JoinType::Semi => self._join_impl(
                other,
                vec![lhs_keys],
                vec![rhs_keys],
                args,
                options,
                _check_rechunk,
                _verbose,
            ),
        }
    }

    /// Perform an inner join on two DataFrames.
    ///
    /// # Example
    ///
    /// ```
    /// # use polars_core::prelude::*;
    /// # use polars_ops::prelude::*;
    /// fn join_dfs(left: &DataFrame, right: &DataFrame) -> PolarsResult<DataFrame> {
    ///     left.inner_join(right, ["join_column_left"], ["join_column_right"])
    /// }
    /// ```
    fn inner_join(
        &self,
        other: &DataFrame,
        left_on: impl IntoIterator<Item = impl Into<PlSmallStr>>,
        right_on: impl IntoIterator<Item = impl Into<PlSmallStr>>,
    ) -> PolarsResult<DataFrame> {
        self.join(
            other,
            left_on,
            right_on,
            JoinArgs::new(JoinType::Inner),
            None,
        )
    }

    /// Perform a left outer join on two DataFrames
    /// # Example
    ///
    /// ```no_run
    /// # use polars_core::prelude::*;
    /// # use polars_ops::prelude::*;
    /// let df1: DataFrame = df!("Wavelength (nm)" => &[480.0, 650.0, 577.0, 1201.0, 100.0])?;
    /// let df2: DataFrame = df!("Color" => &["Blue", "Yellow", "Red"],
    ///                          "Wavelength nm" => &[480.0, 577.0, 650.0])?;
    ///
    /// let df3: DataFrame = df1.left_join(&df2, ["Wavelength (nm)"], ["Wavelength nm"])?;
    /// println!("{:?}", df3);
    /// # Ok::<(), PolarsError>(())
    /// ```
    ///
    /// Output:
    ///
    /// ```text
    /// shape: (5, 2)
    /// +-----------------+--------+
    /// | Wavelength (nm) | Color  |
    /// | ---             | ---    |
    /// | f64             | str    |
    /// +=================+========+
    /// | 480             | Blue   |
    /// +-----------------+--------+
    /// | 650             | Red    |
    /// +-----------------+--------+
    /// | 577             | Yellow |
    /// +-----------------+--------+
    /// | 1201            | null   |
    /// +-----------------+--------+
    /// | 100             | null   |
    /// +-----------------+--------+
    /// ```
    fn left_join(
        &self,
        other: &DataFrame,
        left_on: impl IntoIterator<Item = impl Into<PlSmallStr>>,
        right_on: impl IntoIterator<Item = impl Into<PlSmallStr>>,
    ) -> PolarsResult<DataFrame> {
        self.join(
            other,
            left_on,
            right_on,
            JoinArgs::new(JoinType::Left),
            None,
        )
    }

    /// Perform a full outer join on two DataFrames
    /// # Example
    ///
    /// ```
    /// # use polars_core::prelude::*;
    /// # use polars_ops::prelude::*;
    /// fn join_dfs(left: &DataFrame, right: &DataFrame) -> PolarsResult<DataFrame> {
    ///     left.full_join(right, ["join_column_left"], ["join_column_right"])
    /// }
    /// ```
    fn full_join(
        &self,
        other: &DataFrame,
        left_on: impl IntoIterator<Item = impl Into<PlSmallStr>>,
        right_on: impl IntoIterator<Item = impl Into<PlSmallStr>>,
    ) -> PolarsResult<DataFrame> {
        self.join(
            other,
            left_on,
            right_on,
            JoinArgs::new(JoinType::Full),
            None,
        )
    }
}

trait DataFrameJoinOpsPrivate: IntoDf {
    fn _inner_join_from_series(
        &self,
        other: &DataFrame,
        s_left: &Series,
        s_right: &Series,
        args: JoinArgs,
        verbose: bool,
        drop_names: Option<Vec<PlSmallStr>>,
    ) -> PolarsResult<DataFrame> {
        let left_df = self.to_df();
        let ((join_tuples_left, join_tuples_right), sorted) =
            _sort_or_hash_inner(s_left, s_right, verbose, args.validation, args.nulls_equal)?;

        let mut join_tuples_left = &*join_tuples_left;
        let mut join_tuples_right = &*join_tuples_right;

        if let Some((offset, len)) = args.slice {
            join_tuples_left = slice_slice(join_tuples_left, offset, len);
            join_tuples_right = slice_slice(join_tuples_right, offset, len);
        }

        let other = if let Some(drop_names) = drop_names {
            other.drop_many(drop_names)
        } else {
            other.drop(s_right.name()).unwrap()
        };

        let mut left = unsafe { IdxCa::mmap_slice("a".into(), join_tuples_left) };
        if sorted {
            left.set_sorted_flag(IsSorted::Ascending);
        }
        let right = unsafe { IdxCa::mmap_slice("b".into(), join_tuples_right) };

        let already_left_sorted = sorted
            && matches!(
                args.maintain_order,
                MaintainOrderJoin::Left | MaintainOrderJoin::LeftRight
            );
        try_raise_keyboard_interrupt();
        let (df_left, df_right) =
            if args.maintain_order != MaintainOrderJoin::None && !already_left_sorted {
                let mut df =
                    DataFrame::new(vec![left.into_series().into(), right.into_series().into()])?;

                let columns = match args.maintain_order {
                    MaintainOrderJoin::Left | MaintainOrderJoin::LeftRight => vec!["a"],
                    MaintainOrderJoin::Right | MaintainOrderJoin::RightLeft => vec!["b"],
                    _ => unreachable!(),
                };

                let options = SortMultipleOptions::new()
                    .with_order_descending(false)
                    .with_maintain_order(true);

                df.sort_in_place(columns, options)?;

                let [mut a, b]: [Column; 2] = df.take_columns().try_into().unwrap();
                if matches!(
                    args.maintain_order,
                    MaintainOrderJoin::Left | MaintainOrderJoin::LeftRight
                ) {
                    a.set_sorted_flag(IsSorted::Ascending);
                }

                POOL.join(
                    // SAFETY: join indices are known to be in bounds
                    || unsafe { left_df.take_unchecked(a.idx().unwrap()) },
                    || unsafe { other.take_unchecked(b.idx().unwrap()) },
                )
            } else {
                POOL.join(
                    // SAFETY: join indices are known to be in bounds
                    || unsafe { left_df.take_unchecked(left.into_series().idx().unwrap()) },
                    || unsafe { other.take_unchecked(right.into_series().idx().unwrap()) },
                )
            };

        _finish_join(df_left, df_right, args.suffix)
    }
}

impl DataFrameJoinOps for DataFrame {}
impl DataFrameJoinOpsPrivate for DataFrame {}

fn prepare_keys_multiple(s: &[Series], nulls_equal: bool) -> PolarsResult<BinaryOffsetChunked> {
    let keys = s
        .iter()
        .map(|s| {
            let phys = s.to_physical_repr();
            match phys.dtype() {
                DataType::Float32 => phys.f32().unwrap().to_canonical().into_column(),
                DataType::Float64 => phys.f64().unwrap().to_canonical().into_column(),
                _ => phys.into_owned().into_column(),
            }
        })
        .collect::<Vec<_>>();

    if nulls_equal {
        encode_rows_vertical_par_unordered(&keys)
    } else {
        encode_rows_vertical_par_unordered_broadcast_nulls(&keys)
    }
}
pub fn private_left_join_multiple_keys(
    a: &DataFrame,
    b: &DataFrame,
    nulls_equal: bool,
) -> PolarsResult<LeftJoinIds> {
    // @scalar-opt
    let a_cols = a
        .get_columns()
        .iter()
        .map(|c| c.as_materialized_series().clone())
        .collect::<Vec<_>>();
    let b_cols = b
        .get_columns()
        .iter()
        .map(|c| c.as_materialized_series().clone())
        .collect::<Vec<_>>();

    let a = prepare_keys_multiple(&a_cols, nulls_equal)?.into_series();
    let b = prepare_keys_multiple(&b_cols, nulls_equal)?.into_series();
    sort_or_hash_left(&a, &b, false, JoinValidation::ManyToMany, nulls_equal)
}
