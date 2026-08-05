# 08. Autopilot State Machine Audit

## Guarded path

~~~
EngineDaemon -> StoryPipelineRunner -> BaseStoryPipeline
  -> canonical chapter save
  -> ChapterAftermathPipeline
  -> chapter-narrative-sync committed
  -> MemoryEngine committed
  -> optional vector indexing/retry state
  -> advancement only after required gate state
~~~

The implementation remains fail-closed for required canonical extraction and
memory stages. A vector failure is independently retryable after canonical
commit and does not downgrade that commit; it also cannot be silently treated
as a successful canonical write when a required stage fails.

## Tested recovery behavior

The 30 chapter regression verifies restart at chapter 9, vector failure at
chapter 12, three failed extraction attempts at chapter 17 followed by an
explicit recovery, a retained-prose rewrite at chapter 20, and auxiliary-stage
drain before subsequent context construction. The explicit 100 chapter test
repeats the real pipeline loop one hundred times.

No state-machine change was made outside established gate/retry/replay
interfaces.
