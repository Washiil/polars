#![allow(clippy::disallowed_types)]

mod park_group;
mod task;

use std::cell::{Cell, UnsafeCell};
use std::collections::HashMap;
use std::future::Future;
use std::marker::PhantomData;
use std::panic::{AssertUnwindSafe, Location};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, LazyLock, OnceLock, Weak};
use std::time::Duration;

use crossbeam_deque::{Injector, Steal, Stealer, Worker as WorkQueue};
use crossbeam_utils::CachePadded;
use park_group::ParkGroup;
use parking_lot::Mutex;
use polars_utils::relaxed_cell::RelaxedCell;
use rand::rngs::SmallRng;
use rand::{Rng, SeedableRng};
use slotmap::SlotMap;
pub use task::{AbortOnDropHandle, JoinHandle};
use task::{CancelHandle, Runnable};

static NUM_EXECUTOR_THREADS: RelaxedCell<usize> = RelaxedCell::new_usize(0);
pub fn set_num_threads(t: usize) {
    NUM_EXECUTOR_THREADS.store(t);
}

static GLOBAL_SCHEDULER: OnceLock<Executor> = OnceLock::new();

thread_local!(
    /// Used to store which executor thread this is.
    static TLS_THREAD_ID: Cell<usize> = const { Cell::new(usize::MAX) };
);

static NS_SPENT_BLOCKED: LazyLock<Mutex<HashMap<&'static Location<'static>, u64>>> =
    LazyLock::new(Mutex::default);

static TRACK_WAIT_STATISTICS: RelaxedCell<bool> = RelaxedCell::new_bool(false);

pub fn track_task_wait_statistics(should_track: bool) {
    TRACK_WAIT_STATISTICS.store(should_track);
}

pub fn get_task_wait_statistics() -> Vec<(&'static Location<'static>, Duration)> {
    NS_SPENT_BLOCKED
        .lock()
        .iter()
        .map(|(l, ns)| (*l, Duration::from_nanos(*ns)))
        .collect()
}

pub fn clear_task_wait_statistics() {
    NS_SPENT_BLOCKED.lock().clear()
}

slotmap::new_key_type! {
    struct TaskKey;
}

/// High priority tasks are scheduled preferentially over low priority tasks.
#[derive(Copy, Clone, Debug, PartialEq, Eq, PartialOrd, Ord)]
pub enum TaskPriority {
    Low,
    High,
}

/// Metadata associated with a task to help schedule it and clean it up.
struct ScopedTaskMetadata {
    task_key: TaskKey,
    completed_tasks: Weak<Mutex<Vec<TaskKey>>>,
}

struct TaskMetadata {
    spawn_location: &'static Location<'static>,
    ns_spent_blocked: RelaxedCell<u64>,
    priority: TaskPriority,
    freshly_spawned: AtomicBool,
    scoped: Option<ScopedTaskMetadata>,
}

impl Drop for TaskMetadata {
    fn drop(&mut self) {
        *NS_SPENT_BLOCKED
            .lock()
            .entry(self.spawn_location)
            .or_default() += self.ns_spent_blocked.load();
        if let Some(scoped) = &self.scoped {
            if let Some(completed_tasks) = scoped.completed_tasks.upgrade() {
                completed_tasks.lock().push(scoped.task_key);
            }
        }
    }
}

/// A task ready to run.
type ReadyTask = Runnable<TaskMetadata>;

/// A per-thread task list.
struct ThreadLocalTaskList {
    // May be used from any thread.
    high_prio_tasks_stealer: Stealer<ReadyTask>,

    // SAFETY: these may only be used on the thread this task list belongs to.
    high_prio_tasks: WorkQueue<ReadyTask>,
    local_slot: UnsafeCell<Option<ReadyTask>>,
}

unsafe impl Sync for ThreadLocalTaskList {}

struct Executor {
    park_group: ParkGroup,
    thread_task_lists: Vec<CachePadded<ThreadLocalTaskList>>,
    global_high_prio_task_queue: Injector<ReadyTask>,
    global_low_prio_task_queue: Injector<ReadyTask>,
}

impl Executor {
    fn schedule_task(&self, task: ReadyTask) {
        let thread = TLS_THREAD_ID.get();
        let meta = task.metadata();
        let opt_ttl = self.thread_task_lists.get(thread);

        let mut use_global_queue = opt_ttl.is_none();
        if meta.freshly_spawned.load(Ordering::Relaxed) {
            use_global_queue = true;
            meta.freshly_spawned.store(false, Ordering::Relaxed);
        }

        if use_global_queue {
            // Scheduled from an unknown thread, add to global queue.
            if meta.priority == TaskPriority::High {
                self.global_high_prio_task_queue.push(task);
            } else {
                self.global_low_prio_task_queue.push(task);
            }
            self.park_group.unpark_one();
        } else {
            let ttl = opt_ttl.unwrap();
            // SAFETY: this slot may only be accessed from the local thread, which we are.
            let slot = unsafe { &mut *ttl.local_slot.get() };

            if meta.priority == TaskPriority::High {
                // Insert new task into thread local slot, taking out the old task.
                let Some(task) = slot.replace(task) else {
                    // We pushed a task into our local slot which was empty. Since
                    // we are already awake, no need to notify anyone.
                    return;
                };

                ttl.high_prio_tasks.push(task);
                self.park_group.unpark_one();
            } else {
                // Optimization: while this is a low priority task we have no
                // high priority tasks on this thread so we'll execute this one.
                if ttl.high_prio_tasks.is_empty() && slot.is_none() {
                    *slot = Some(task);
                } else {
                    self.global_low_prio_task_queue.push(task);
                    self.park_group.unpark_one();
                }
            }
        }
    }

    fn try_steal_task<R: Rng>(&self, thread: usize, rng: &mut R) -> Option<ReadyTask> {
        // Try to get a global task.
        loop {
            match self.global_high_prio_task_queue.steal() {
                Steal::Empty => break,
                Steal::Success(task) => return Some(task),
                Steal::Retry => std::hint::spin_loop(),
            }
        }

        loop {
            match self.global_low_prio_task_queue.steal() {
                Steal::Empty => break,
                Steal::Success(task) => return Some(task),
                Steal::Retry => std::hint::spin_loop(),
            }
        }

        // Try to steal tasks.
        let ttl = &self.thread_task_lists[thread];
        for _ in 0..4 {
            let mut retry = true;
            while retry {
                retry = false;

                for idx in random_permutation(self.thread_task_lists.len() as u32, rng) {
                    let foreign_ttl = &self.thread_task_lists[idx as usize];
                    match foreign_ttl
                        .high_prio_tasks_stealer
                        .steal_batch_and_pop(&ttl.high_prio_tasks)
                    {
                        Steal::Empty => {},
                        Steal::Success(task) => return Some(task),
                        Steal::Retry => retry = true,
                    }
                }

                std::hint::spin_loop()
            }
        }

        None
    }

    fn runner(&self, thread: usize) {
        TLS_THREAD_ID.set(thread);

        let mut rng = SmallRng::from_rng(&mut rand::rng());
        let mut worker = self.park_group.new_worker();
        let mut last_block_start = None;

        loop {
            let ttl = &self.thread_task_lists[thread];
            let task = (|| {
                // Try to get a task from LIFO slot.
                if let Some(task) = unsafe { (*ttl.local_slot.get()).take() } {
                    return Some(task);
                }

                // Try to get a local high-priority task.
                if let Some(task) = ttl.high_prio_tasks.pop() {
                    return Some(task);
                }

                // Try to steal a task.
                if let Some(task) = self.try_steal_task(thread, &mut rng) {
                    return Some(task);
                }

                // Prepare to park, then try one more steal attempt.
                let park = worker.prepare_park();
                if let Some(task) = self.try_steal_task(thread, &mut rng) {
                    return Some(task);
                }

                if last_block_start.is_none() && TRACK_WAIT_STATISTICS.load() {
                    last_block_start = Some(std::time::Instant::now());
                }
                park.park();
                None
            })();

            if let Some(task) = task {
                if let Some(t) = last_block_start.take() {
                    if TRACK_WAIT_STATISTICS.load() {
                        let ns: u64 = t.elapsed().as_nanos().try_into().unwrap();
                        task.metadata().ns_spent_blocked.fetch_add(ns);
                    }
                }
                worker.recruit_next();
                task.run();
            }
        }
    }

    fn global() -> &'static Executor {
        GLOBAL_SCHEDULER.get_or_init(|| {
            let mut n_threads = NUM_EXECUTOR_THREADS.load();
            if n_threads == 0 {
                n_threads = std::thread::available_parallelism()
                    .map(|n| n.get())
                    .unwrap_or(4);
            }

            let thread_task_lists = (0..n_threads)
                .map(|t| {
                    std::thread::Builder::new()
                        .name(format!("async-executor-{t}"))
                        .spawn(move || Self::global().runner(t))
                        .unwrap();

                    let high_prio_tasks = WorkQueue::new_lifo();
                    CachePadded::new(ThreadLocalTaskList {
                        high_prio_tasks_stealer: high_prio_tasks.stealer(),
                        high_prio_tasks,
                        local_slot: UnsafeCell::new(None),
                    })
                })
                .collect();
            Self {
                park_group: ParkGroup::new(),
                thread_task_lists,
                global_high_prio_task_queue: Injector::new(),
                global_low_prio_task_queue: Injector::new(),
            }
        })
    }
}

pub struct TaskScope<'scope, 'env: 'scope> {
    // Keep track of in-progress tasks so we can forcibly cancel them
    // when the scope ends, to ensure the lifetimes are respected.
    // Tasks add their own key to completed_tasks when done so we can
    // reclaim the memory used by the cancel_handles.
    cancel_handles: Mutex<SlotMap<TaskKey, CancelHandle>>,
    completed_tasks: Arc<Mutex<Vec<TaskKey>>>,

    // Copied from std::thread::scope. Necessary to prevent unsoundness.
    scope: PhantomData<&'scope mut &'scope ()>,
    env: PhantomData<&'env mut &'env ()>,
}

impl<'scope> TaskScope<'scope, '_> {
    // Not Drop because that extends lifetimes.
    fn destroy(&self) {
        // Make sure all tasks are cancelled.
        for (_, t) in self.cancel_handles.lock().drain() {
            t.cancel();
        }
    }

    fn clear_completed_tasks(&self) {
        let mut cancel_handles = self.cancel_handles.lock();
        for t in self.completed_tasks.lock().drain(..) {
            cancel_handles.remove(t);
        }
    }

    #[track_caller]
    pub fn spawn_task<F: Future + Send + 'scope>(
        &self,
        priority: TaskPriority,
        fut: F,
    ) -> JoinHandle<F::Output>
    where
        <F as Future>::Output: Send + 'static,
    {
        let spawn_location = Location::caller();
        self.clear_completed_tasks();

        let mut runnable = None;
        let mut join_handle = None;
        self.cancel_handles.lock().insert_with_key(|task_key| {
            let (run, jh) = unsafe {
                // SAFETY: we make sure to cancel this task before 'scope ends.
                let executor = Executor::global();
                let on_wake = move |task| executor.schedule_task(task);
                task::spawn_with_lifetime(
                    fut,
                    on_wake,
                    TaskMetadata {
                        spawn_location,
                        ns_spent_blocked: RelaxedCell::new_u64(0),
                        priority,
                        freshly_spawned: AtomicBool::new(true),
                        scoped: Some(ScopedTaskMetadata {
                            task_key,
                            completed_tasks: Arc::downgrade(&self.completed_tasks),
                        }),
                    },
                )
            };
            let cancel_handle = jh.cancel_handle();
            runnable = Some(run);
            join_handle = Some(jh);
            cancel_handle
        });
        runnable.unwrap().schedule();
        join_handle.unwrap()
    }
}

pub fn task_scope<'env, F, T>(f: F) -> T
where
    F: for<'scope> FnOnce(&'scope TaskScope<'scope, 'env>) -> T,
{
    // By having this local variable inaccessible to anyone we guarantee
    // that either abort is called killing the entire process, or that this
    // executor is properly destroyed.
    let scope = TaskScope {
        cancel_handles: Mutex::default(),
        completed_tasks: Arc::new(Mutex::default()),
        scope: PhantomData,
        env: PhantomData,
    };

    let result = std::panic::catch_unwind(AssertUnwindSafe(|| f(&scope)));

    // Make sure all tasks are properly destroyed.
    scope.destroy();

    match result {
        Err(e) => std::panic::resume_unwind(e),
        Ok(result) => result,
    }
}

#[track_caller]
pub fn spawn<F: Future + Send + 'static>(priority: TaskPriority, fut: F) -> JoinHandle<F::Output>
where
    <F as Future>::Output: Send + 'static,
{
    let spawn_location = Location::caller();
    let executor = Executor::global();
    let on_wake = move |task| executor.schedule_task(task);
    let (runnable, join_handle) = task::spawn(
        fut,
        on_wake,
        TaskMetadata {
            spawn_location,
            ns_spent_blocked: RelaxedCell::new_u64(0),
            priority,
            freshly_spawned: AtomicBool::new(true),
            scoped: None,
        },
    );
    runnable.schedule();
    join_handle
}

fn random_permutation<R: Rng>(len: u32, rng: &mut R) -> impl Iterator<Item = u32> {
    let modulus = len.next_power_of_two();
    let halfwidth = modulus.trailing_zeros() / 2;
    let mask = modulus - 1;
    let displace_zero = rng.random::<u32>();
    let odd1 = rng.random::<u32>() | 1;
    let odd2 = rng.random::<u32>() | 1;
    let uniform_first = ((rng.random::<u32>() as u64 * len as u64) >> 32) as u32;

    (0..modulus)
        .map(move |mut i| {
            // Invertible permutation on [0, modulus).
            i = i.wrapping_add(displace_zero);
            i = i.wrapping_mul(odd1);
            i ^= (i & mask) >> halfwidth;
            i = i.wrapping_mul(odd2);
            i & mask
        })
        .filter(move |i| *i < len)
        .map(move |mut i| {
            i += uniform_first;
            if i >= len {
                i -= len;
            }
            i
        })
}
