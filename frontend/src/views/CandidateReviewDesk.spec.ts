import { describe, expect, it, vi } from 'vitest'
import { createVNode, defineComponent, h } from 'vue'
import { renderToString } from 'vue/server-renderer'

const mocks = vi.hoisted(() => ({
  getRun: vi.fn(),
  listCandidateVersions: vi.fn(),
  approveAndCommit: vi.fn(),
  generateNext: vi.fn(),
  runContinuous: vi.fn(),
  messageSuccess: vi.fn(),
}))

vi.mock('vue-router', () => ({
  useRoute: () => ({ params: { slug: 'novel-1' } }),
  useRouter: () => ({ push: vi.fn() }),
}))

vi.mock('naive-ui', () => ({
  useMessage: () => ({ success: mocks.messageSuccess }),
}))

vi.mock('@/api/generation', () => ({
  getGenerationRunOrNull: mocks.getRun,
  generationApi: {
    listCandidateVersions: mocks.listCandidateVersions,
    approveAndCommit: mocks.approveAndCommit,
    generateNext: mocks.generateNext,
    runContinuous: mocks.runContinuous,
    saveCandidateContent: vi.fn(),
    reaudit: vi.fn(),
    saveCommitPlan: vi.fn(),
    regenerate: vi.fn(),
    retrySync: vi.fn(),
    reject: vi.fn(),
  },
}))

import CandidateReviewDesk from './CandidateReviewDesk.vue'

async function setupDesk() {
  let releaseRender!: () => void
  const renderGate = new Promise<void>((resolve) => { releaseRender = resolve })
  const blocker = defineComponent({
    async setup() {
      await renderGate
      return () => null
    },
  })
  const target = createVNode(CandidateReviewDesk)
  const root = createVNode(defineComponent({
    render: () => [target, h(blocker)],
  }))
  const rendering = renderToString(root, {})
  for (let attempt = 0; attempt < 20 && !target.component; attempt++) await Promise.resolve()
  if (!target.component) throw new Error('CandidateReviewDesk setup did not start')
  return {
    state: (target.component as any).setupState as Record<string, any>,
    finish: async () => {
      releaseRender()
      await rendering
    },
  }
}

function candidateRun() {
  return {
    novel_id: 'novel-1',
    run_mode: 'chapter_review',
    state: 'waiting_review',
    generation_epoch: 1,
    target_chapters: 10,
    current_formal_chapter: 0,
    current_candidate_id: 'candidate-1',
    current_candidate_chapter: 1,
    canonical_sync_status: 'ready',
    next_action: 'commit_candidate',
    last_error: '',
    max_pending_candidates: 1,
    prefetch: 0,
    candidate: {
      id: 'candidate-1',
      novel_id: 'novel-1',
      chapter_number: 1,
      title: 'Opening',
      generation_epoch: 1,
      status: 'awaiting_review',
      outline_chain: {},
      outline_chain_digest: 'outline-digest',
      llm_content: 'Draft',
      final_content: 'Draft',
      content_revision: 1,
      audit_revision: 1,
      audit_is_current: true,
      commit_plan_revision: 1,
      commit_plan_is_current: true,
      audit: {},
      commit_plan: {},
      feedback: '',
    },
  }
}

describe('CandidateReviewDesk approval advancement', () => {
  it('refreshes authoritative state and exposes a rejected post-approval advance', async () => {
    const initial = candidateRun()
    const authoritative = {
      ...initial,
      state: 'paused',
      current_candidate_id: null,
      current_candidate_chapter: null,
      next_action: 'retry_advance',
      last_error: 'advance failed',
      candidate: null,
    }
    mocks.getRun.mockResolvedValueOnce(initial).mockResolvedValueOnce(authoritative)
    mocks.listCandidateVersions.mockResolvedValue([])
    mocks.approveAndCommit.mockResolvedValue(undefined)
    const rejectedAdvance = Promise.reject(new Error('advance failed'))
    rejectedAdvance.catch(() => undefined)
    mocks.generateNext.mockReturnValue(rejectedAdvance)

    const mounted = await setupDesk()
    try {
      await mounted.state.refresh({ force: true })
      await mounted.state.approve(true)

      expect(mocks.approveAndCommit).toHaveBeenCalledWith('candidate-1', true)
      expect(mocks.generateNext).toHaveBeenCalledWith('novel-1')
      expect(mocks.getRun).toHaveBeenCalledTimes(2)
      expect(mounted.state.run.state).toBe('paused')
      expect(mounted.state.error).toBe('advance failed')
      expect(mounted.state.actionLoading).toBe(false)
      expect(mocks.messageSuccess).not.toHaveBeenCalled()
    } finally {
      await mounted.finish()
    }
  })
})
