use polars_core::prelude::*;

use crate::prelude::*;

mod cache_states;
mod delay_rechunk;

mod cluster_with_columns;
mod collapse_and_project;
mod collapse_joins;
mod collect_members;
mod count_star;
#[cfg(feature = "cse")]
mod cse;
mod flatten_union;
#[cfg(feature = "fused")]
mod fused;
mod join_utils;
pub(crate) use join_utils::ExprOrigin;
mod expand_datasets;
#[cfg(feature = "python")]
pub use expand_datasets::ExpandedPythonScan;
mod predicate_pushdown;
mod projection_pushdown;
mod set_order;
mod simplify_expr;
mod slice_pushdown_expr;
mod slice_pushdown_lp;
mod stack_opt;

use collapse_and_project::SimpleProjectionAndCollapse;
#[cfg(feature = "cse")]
pub use cse::NaiveExprMerger;
use delay_rechunk::DelayRechunk;
pub use expand_datasets::ExpandedDataset;
use polars_core::config::verbose;
use polars_io::predicates::PhysicalIoExpr;
pub use predicate_pushdown::PredicatePushDown;
pub use projection_pushdown::ProjectionPushDown;
pub use simplify_expr::{SimplifyBooleanRule, SimplifyExprRule};
use slice_pushdown_lp::SlicePushDown;
pub use stack_opt::{OptimizationRule, OptimizeExprContext, StackOptimizer};

use self::flatten_union::FlattenUnionRule;
use self::set_order::set_order_flags;
pub use crate::frame::{AllowedOptimizations, OptFlags};
pub use crate::plans::conversion::type_coercion::TypeCoercionRule;
use crate::plans::optimizer::count_star::CountStar;
#[cfg(feature = "cse")]
use crate::plans::optimizer::cse::CommonSubExprOptimizer;
#[cfg(feature = "cse")]
use crate::plans::optimizer::cse::prune_unused_caches;
use crate::plans::optimizer::predicate_pushdown::ExprEval;
#[cfg(feature = "cse")]
use crate::plans::visitor::*;
use crate::prelude::optimizer::collect_members::MemberCollector;

pub trait Optimize {
    fn optimize(&self, logical_plan: DslPlan) -> PolarsResult<DslPlan>;
}

// arbitrary constant to reduce reallocation.
const HASHMAP_SIZE: usize = 16;

pub(crate) fn init_hashmap<K, V>(max_len: Option<usize>) -> PlHashMap<K, V> {
    PlHashMap::with_capacity(std::cmp::min(max_len.unwrap_or(HASHMAP_SIZE), HASHMAP_SIZE))
}

pub(crate) fn pushdown_maintain_errors() -> bool {
    std::env::var("POLARS_PUSHDOWN_OPT_MAINTAIN_ERRORS").as_deref() == Ok("1")
}

pub fn optimize(
    logical_plan: DslPlan,
    mut opt_flags: OptFlags,
    lp_arena: &mut Arena<IR>,
    expr_arena: &mut Arena<AExpr>,
    scratch: &mut Vec<Node>,
    expr_eval: ExprEval<'_>,
) -> PolarsResult<Node> {
    #[allow(dead_code)]
    let verbose = verbose();

    // Gradually fill the rules passed to the optimizer
    let opt = StackOptimizer {};
    let mut rules: Vec<Box<dyn OptimizationRule>> = Vec::with_capacity(8);

    // Unset CSE
    // This can be turned on again during ir-conversion.
    #[allow(clippy::eq_op)]
    #[cfg(feature = "cse")]
    if opt_flags.contains(OptFlags::EAGER) {
        opt_flags &= !(OptFlags::COMM_SUBEXPR_ELIM | OptFlags::COMM_SUBEXPR_ELIM);
    }
    let mut lp_top = to_alp(logical_plan, expr_arena, lp_arena, &mut opt_flags)?;

    // Don't run optimizations that don't make sense on a single node.
    // This keeps eager execution more snappy.
    #[cfg(feature = "cse")]
    let comm_subplan_elim = opt_flags.contains(OptFlags::COMM_SUBPLAN_ELIM);

    #[cfg(feature = "cse")]
    let comm_subexpr_elim = opt_flags.contains(OptFlags::COMM_SUBEXPR_ELIM);
    #[cfg(not(feature = "cse"))]
    let comm_subexpr_elim = false;

    // Note: This can be in opt_flags in the future if needed.
    let pushdown_maintain_errors = pushdown_maintain_errors();

    // During debug we check if the optimizations have not modified the final schema.
    #[cfg(debug_assertions)]
    let prev_schema = lp_arena.get(lp_top).schema(lp_arena).into_owned();

    let mut _opt_members = &mut None;

    macro_rules! get_or_init_members {
        () => {
            _get_or_init_members(_opt_members, lp_top, lp_arena, expr_arena)
        };
    }

    macro_rules! get_members_opt {
        () => {
            _opt_members.as_mut()
        };
    }

    // Run before slice pushdown
    if opt_flags.contains(OptFlags::CHECK_ORDER_OBSERVE) {
        let members = get_or_init_members!();
        if members.has_group_by | members.has_sort | members.has_distinct {
            set_order_flags(lp_top, lp_arena, expr_arena, scratch);
        }
    }

    if opt_flags.simplify_expr() {
        #[cfg(feature = "fused")]
        rules.push(Box::new(fused::FusedArithmetic {}));
    }

    #[cfg(feature = "cse")]
    let _cse_plan_changed = if comm_subplan_elim {
        let members = get_or_init_members!();
        if (members.has_sink_multiple || members.has_joins_or_unions)
            && members.has_duplicate_scans()
            && !members.has_cache
        {
            if verbose {
                eprintln!("found multiple sources; run comm_subplan_elim")
            }

            let (lp, changed, cid2c) = cse::elim_cmn_subplans(lp_top, lp_arena, expr_arena);

            prune_unused_caches(lp_arena, cid2c);

            lp_top = lp;
            members.has_cache |= changed;
            changed
        } else {
            false
        }
    } else {
        false
    };
    #[cfg(not(feature = "cse"))]
    let _cse_plan_changed = false;

    // Should be run before predicate pushdown.
    if opt_flags.projection_pushdown() {
        let mut projection_pushdown_opt = ProjectionPushDown::new();
        let alp = lp_arena.take(lp_top);
        let alp = projection_pushdown_opt.optimize(alp, lp_arena, expr_arena)?;
        lp_arena.replace(lp_top, alp);

        if projection_pushdown_opt.is_count_star {
            let mut count_star_opt = CountStar::new();
            count_star_opt.optimize_plan(lp_arena, expr_arena, lp_top)?;
        }
    }

    if opt_flags.predicate_pushdown() {
        let mut predicate_pushdown_opt = PredicatePushDown::new(
            expr_eval,
            pushdown_maintain_errors,
            opt_flags.new_streaming(),
        );
        let alp = lp_arena.take(lp_top);
        let alp = predicate_pushdown_opt.optimize(alp, lp_arena, expr_arena)?;
        lp_arena.replace(lp_top, alp);
    }

    // Make sure it is after predicate pushdown
    if opt_flags.collapse_joins() && get_or_init_members!().has_filter_with_join_input {
        collapse_joins::optimize(lp_top, lp_arena, expr_arena, opt_flags.new_streaming());
    }

    // Make sure its before slice pushdown.
    if opt_flags.fast_projection() {
        rules.push(Box::new(SimpleProjectionAndCollapse::new(
            opt_flags.eager(),
        )));
    }

    if !opt_flags.eager() {
        rules.push(Box::new(DelayRechunk::new()));
    }

    if opt_flags.slice_pushdown() {
        let mut slice_pushdown_opt = SlicePushDown::new(
            // We don't maintain errors on slice as the behavior is much more predictable that way.
            //
            // Even if we enable maintain_errors (thereby preventing the slice from being pushed),
            // the new-streaming engine still may not error due to early-stopping.
            false, // maintain_errors
            opt_flags.new_streaming(),
        );
        let alp = lp_arena.take(lp_top);
        let alp = slice_pushdown_opt.optimize(alp, lp_arena, expr_arena)?;

        lp_arena.replace(lp_top, alp);

        // Expressions use the stack optimizer.
        rules.push(Box::new(slice_pushdown_opt));
    }

    // This optimization removes branches, so we must do it when type coercion
    // is completed.
    if opt_flags.simplify_expr() {
        rules.push(Box::new(SimplifyBooleanRule {}));
    }

    if !opt_flags.eager() {
        rules.push(Box::new(FlattenUnionRule {}));
    }

    // Note: ExpandDatasets must run after slice and predicate pushdown.
    rules.push(Box::new(expand_datasets::ExpandDatasets {}) as Box<dyn OptimizationRule>);

    lp_top = opt.optimize_loop(&mut rules, expr_arena, lp_arena, lp_top)?;

    if opt_flags.cluster_with_columns() {
        cluster_with_columns::optimize(lp_top, lp_arena, expr_arena)
    }

    if _cse_plan_changed
        && get_members_opt!().is_some_and(|members| {
            (members.has_joins_or_unions | members.has_sink_multiple) && members.has_cache
        })
    {
        // We only want to run this on cse inserted caches
        cache_states::set_cache_states(
            lp_top,
            lp_arena,
            expr_arena,
            scratch,
            expr_eval,
            verbose,
            pushdown_maintain_errors,
            opt_flags.new_streaming(),
        )?;
    }

    // This one should run (nearly) last as this modifies the projections
    #[cfg(feature = "cse")]
    if comm_subexpr_elim && !get_or_init_members!().has_ext_context {
        let mut optimizer = CommonSubExprOptimizer::new();
        let alp_node = IRNode::new_mutate(lp_top);

        lp_top = try_with_ir_arena(lp_arena, expr_arena, |arena| {
            let rewritten = alp_node.rewrite(&mut optimizer, arena)?;
            Ok(rewritten.node())
        })?;
    }

    // During debug we check if the optimizations have not modified the final schema.
    #[cfg(debug_assertions)]
    {
        // only check by names because we may supercast types.
        assert_eq!(
            prev_schema.iter_names().collect::<Vec<_>>(),
            lp_arena
                .get(lp_top)
                .schema(lp_arena)
                .iter_names()
                .collect::<Vec<_>>()
        );
    };

    Ok(lp_top)
}

fn _get_or_init_members<'a>(
    opt_members: &'a mut Option<MemberCollector>,
    lp_top: Node,
    lp_arena: &mut Arena<IR>,
    expr_arena: &mut Arena<AExpr>,
) -> &'a mut MemberCollector {
    opt_members.get_or_insert_with(|| {
        let mut members = MemberCollector::new();
        members.collect(lp_top, lp_arena, expr_arena);

        members
    })
}
