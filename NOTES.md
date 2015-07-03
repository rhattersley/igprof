## IgProf structure
### Initialisation
* Set `LD_PRELOAD` so libigprof.so is loaded before the target.
* `profile-perf.cc: initialize`
    * `profile.cc: igprof_init`
    * `profile-perf.cc: enableSignalHandler`
        Sets `profile-perf.cc: profileSignalHandler` to receive signals.

### Sampling
* `profile-perf.cc: profileSignalHandler` runs on receipt of signal.
* Collects call stack (ignoring signal handling frames).
* Updates the `profile-trace.h: IgProfTrace` trace buffer.
    * Finds the `profile-trace.h: IgProfTrace::Stack` which corresponds
      to the end of the call stack.
    * Updates the `PERF_TICKS` counter: `value` += 1; `amount` += 1
    * (Also updates the stats about the profiler's own performance,
       stored in `profile-trace.h: IgProfTrace::perfStats_`)

### Exit
On the way out, `profile.cc: dumpAllProfiles` is called to write the
textual profile results file.

## Python innards
- http://tech.blog.aknin.name/2010/07/22/pythons-innards-interpreter-stacks/

    Including the tidbit, "In gdb, I used a rather crude method to
    look at the call stack (I dereferenced the global variable
    interp_head and went on from there)".

    This seems to suggest we don't need to grab bits and pieces from
    the C stack in order to find out what's going on. Interesting! :)

- http://tech.blog.aknin.name/2010/05/26/pythons-innards-pystate/

    This clarifies the relationship between `interp_head` and
    `_PyThreadState_Current`. The latter being specific to the
    currently executing thread.

    From the comments section (re: mod_wsgi) it seems clear that
    *any* thread can call *any* Python interpreter as long as it is
    careful to manage the thread state.

    If a thread has released the GIL (e.g. in NumPy) then
    `_PyThreadState_Current` can no longer be used to determine the
    current thread's state. One could traverse the thread states
    from `interp_head` and check for a matching thread_id, but
    without holding `head_mutex` there might be a risk of
    inconsistent values.

## Approach

### GSoC description:
From http://ph-dep-sft.web.cern.ch/article/175948:

> "the first objective is to identify and instrument the parts of the
python interpreter which are responsible for allocating,
deallocating and execute python stackframes, eventually extending
igprof instrumentation capabilities to deal with peculiarities of
the python interpreter. The second objective is to collect enough
information via the above mentioned instrumentation to be able to
show mixed python / C / C++ profiles, first for the performance
profiler and subsequently for the memory profiler."

### Hand-wavy...
* Hook Python Frame-related calls and associate current Frame to C
stack frames (`PyEval_EvalFrameEx`?).
* When recording a sample, check for associated Frame and translate
accordingly.

When `profileSignalHandler` captures a call stack it needs to capture the
corresponding(1) Python stack. With a normal build of Python a lot of
the local variables in `PyEval_EvalFrameEx` are optimised out so are not
available - including `tstate`. Unless we can get all the information we
need from a global (e.g. `interp_head`(2)), then we need to hook the
Python C API and squirrel away what we need.

*1) Take care with OS vs. Python threads, multiple interpreters, etc.
Could there be a mix of Python stacks within a single call stack?
e.g. Python calls C, which calls a different interpreter, which calls ...

*2) Ignoring interpreter/threading multiplicity, this is:

    frame = interp_head->tstate_head->frame
    line_number = frame->f_lineno
    name = (PyStringObject *)frame->f_code->co_name
    name_len = name->ob_size
    name_bytes = name->ob_sval

### KISS
Only consider single-threaded (and hence, single interpreter) `python ...`
invocations. That means we can trivially use `interp_head` if we want.

## Questions
* How to distinguish different states of `PyEval_EvalFrameEx` so they don't get
collapsed into a single node?
    * Use the address of a description of its state, instead of the
      frame address? We would need to cache the descriptions and re-use.
* How to determine when `PyEval_EvalFrameEx` is on the call stack?
    * Determine address of `PyEval_EvalFrameEx`. Use
      `sym-cache.h: IgProfSymCache::roundAddressToSymbol` (or perhaps
      just `walk-syms.h: IgHookTrace::symbol` as I'm not convinced any
      "rounding" is taking place in that other routine) to convert frame
      addresses to symbol addresses. Compare against known address of
      `PyEval_...`
    * Use `sym-cache.cc: IgProfSymCache::get` on each frame in the call
      stack and check the name and binary name match `PyEval...` and
      `libpython...`.
    * Hook `PyEval_EvalFrameEx` so it records (on a per-thread basis)
      the frame numbers where it occurs.
* How to deal with different CPython versions and any variances they
  have in their ABI? For example, changes to the layout of
  `PyInterpreterState`. Using accessor functions would help as long
  as they don't require the GIL (or some other Python mutex).
    * Might it help to do something similar to the `gdb` command
      `ptype`? (Although that sounds complicated!)

## Offset interpretation
Example output:

    C11 FN10=(F2+f874f N=(PyEval_EvalFrameEx))+5edf
    C12 FN10+5edf
    C13 FN11=(F2+f3fc2 N=(PyEval_EvalFrameEx))+1752

The function-offset is the offset from the start of the function to the
instruction being executed. Similarly, the file-offset is the offset
from the start of the shared object to the instruction being executed,
*not* the start of the function!

So, `FN10` and `FN11` are both refering to exactly the *same* function.

Having both offsets refering to the instruction seems somewhat
redundant. Having the file-offset just refer to the start of the
function would simplify the previous output to:

    C11 FN10=(F2+f2870 N=(PyEval_EvalFrameEx))+5edf
    C12 FN10+5edf
    C13 FN10+1752

## Archive
libunwind
    - full stack unwind vs. simple, fast unwind?
    - What information does it give you?
    - Do we need the slow version to read Python state?
    - Can we use a hybrid? I.e. Slow for Python frames, fast for others.
