import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createVNode, nextTick } from 'vue'
import { renderToString } from 'vue/server-renderer'
import type { OutlineTreeNode } from '@/api/generation'

const mocks = vi.hoisted(() => ({
  getTree: vi.fn(),
  getContract: vi.fn(),
  saveDraft: vi.fn(),
  publish: vi.fn(),
  bindNode: vi.fn(),
  getLatestGenerationAttempt: vi.fn(),
  cancelGenerationAttempt: vi.fn(),
  consumeOutlineDraftStream: vi.fn(),
  messageSuccess: vi.fn(),
}))

vi.mock('vue-router', () => ({
  useRoute: () => ({ params: { slug: 'novel-1' } }),
  useRouter: () => ({ push: vi.fn() }),
}))

vi.mock('naive-ui', () => ({
  useMessage: () => ({ success: mocks.messageSuccess }),
}))

vi.mock('@/components/outline/OutlineFieldTextarea.vue', () => ({
  default: { render: () => null },
}))

vi.mock('@/components/outline/OutlineStatusPill.vue', () => ({
  default: { render: () => null },
}))

vi.mock('@/api/generation', () => ({
  outlineApi: {
    getTree: mocks.getTree,
    getContract: mocks.getContract,
    saveDraft: mocks.saveDraft,
    publish: mocks.publish,
    bindNode: mocks.bindNode,
    getLatestGenerationAttempt: mocks.getLatestGenerationAttempt,
    cancelGenerationAttempt: mocks.cancelGenerationAttempt,
  },
  consumeOutlineDraftStream: mocks.consumeOutlineDraftStream,
}))

import OutlineStudio from './OutlineStudio.vue'

async function setupStudio() {
  const vnode = createVNode(OutlineStudio)
  await renderToString(vnode, {})
  return (vnode.component as any).setupState as Record<string, any>
}

async function flushAsyncWork() {
  for (let attempt = 0; attempt < 12; attempt++) {
    await Promise.resolve()
    await nextTick()
  }
}

function node(id: string, contractId?: string): OutlineTreeNode {
  return {
    id,
    novel_id: 'novel-1',
    node_type: id === 'root' ? 'outline' : 'part',
    title: id.toUpperCase(),
    outline_contract: contractId ? { contract_id: contractId, status: 'draft' } : undefined,
    children: [],
  }
}

function contract(id: string, payload: Record<string, unknown>) {
  return {
    id,
    novel_id: 'novel-1',
    level: id === 'contract-root' ? 'outline' : 'part',
    author_locked: false,
    has_author_edits: false,
    active: null,
    draft: {
      revision: 1,
      status: 'draft',
      source: 'author',
      digest: `${id}-digest`,
      payload,
    },
  }
}

describe('OutlineStudio request and payload isolation', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mocks.getLatestGenerationAttempt.mockResolvedValue(null)
    mocks.consumeOutlineDraftStream.mockResolvedValue(undefined)
  })

  it('preserves a rich payload byte-for-byte when the form is saved without edits', async () => {
    const richPayload = {
      title: 'Root title',
      narrative_text: 'Root narrative',
      creative_goal: 'Reach the ending',
      entry_state: 'Before',
      exit_state: 'After',
      required_events: ['Event one'],
      forbidden_events: ['Forbidden one'],
      state_changes: { characters: [{ id: 'hero', field: 'trust', to: 'earned' }] },
      foreshadowing: { setup: ['Hidden key'], payoff: ['Opened gate'] },
      chapter_start: 1,
      chapter_end: 10,
      word_budget: 25000,
      handoff_conditions: ['Pass the key'],
      pov: 'Hero',
      scenes: ['Arrival'],
      beats: ['Choice'],
      conflicts: ['Trust versus fear'],
      ending_hook: 'The gate opens',
      extra: {
        field_provenance: { title: 'manifest-import' },
        field_locks: { creative_goal: true },
        nested_future_data: { version: 3 },
      },
      future_schema_field: { retained: true },
    }
    const root = node('root', 'contract-root')
    const current = contract('contract-root', richPayload)
    mocks.getTree.mockResolvedValue(root)
    mocks.getContract.mockResolvedValue(current)
    mocks.saveDraft.mockResolvedValue(current)

    const state = await setupStudio()
    await state.loadTree()
    await state.saveDraft()

    expect(mocks.saveDraft).toHaveBeenCalledWith('contract-root', richPayload)
  })

  it('overlays only the field edited by the author', async () => {
    const richPayload = {
      title: 'Original title',
      narrative_text: 'Keep narrative',
      state_changes: { world: [{ field: 'weather', to: 'storm' }] },
      foreshadowing: { setup: ['Keep setup'] },
      extra: { field_locks: { narrative_text: true } },
      future_schema_field: { retained: true },
    }
    const root = node('root', 'contract-root')
    const current = contract('contract-root', richPayload)
    mocks.getTree.mockResolvedValue(root)
    mocks.getContract.mockResolvedValue(current)
    mocks.saveDraft.mockResolvedValue(current)

    const state = await setupStudio()
    await state.loadTree()
    state.form.title = 'Edited title'
    await state.saveDraft()

    expect(mocks.saveDraft).toHaveBeenCalledWith('contract-root', {
      ...richPayload,
      title: 'Edited title',
    })
  })

  it('does not let a slow A selection overwrite a newer B selection', async () => {
    const root = node('root')
    const a = node('a', 'contract-a')
    const b = node('b', 'contract-b')
    root.children = [a, b]
    let resolveA!: (value: ReturnType<typeof contract>) => void
    const slowA = new Promise<ReturnType<typeof contract>>((resolve) => { resolveA = resolve })
    const contractA = contract('contract-a', { title: 'A payload' })
    const contractB = contract('contract-b', { title: 'B payload' })
    mocks.getTree.mockResolvedValue(root)
    mocks.getContract.mockImplementation((id: string) => id === 'contract-a' ? slowA : Promise.resolve(contractB))

    const state = await setupStudio()
    await state.loadTree()
    const selectingA = state.selectNode(a)
    await Promise.resolve()
    await state.selectNode(b)
    resolveA(contractA)
    await selectingA

    expect(state.selectedContract.id).toBe('contract-b')
    expect(state.form.title).toBe('B payload')
  })

  it('aborts A and ignores its late deltas and completion after selecting B', async () => {
    const a = node('a', 'contract-a')
    const b = node('b', 'contract-b')
    const root = node('root')
    root.children = [a, b]
    const contractA = contract('contract-a', { title: 'A payload' })
    const contractB = contract('contract-b', { title: 'B payload' })
    mocks.getTree.mockResolvedValue(root)
    mocks.getContract.mockImplementation((id: string) => Promise.resolve(id === 'contract-a' ? contractA : contractB))

    let streamCallback!: (event: Record<string, unknown>) => Promise<void> | void
    let streamSignal!: AbortSignal
    let finishStream!: () => void
    const streamGate = new Promise<void>((resolve) => { finishStream = resolve })
    mocks.consumeOutlineDraftStream.mockImplementation(async (_id, callback, signal) => {
      streamCallback = callback
      streamSignal = signal
      await streamGate
    })

    const state = await setupStudio()
    await state.loadTree()
    await state.selectNode(a)
    const generatingA = state.generateDraftStream()
    await flushAsyncWork()
    await state.selectNode(b)

    await streamCallback({ type: 'delta', text: 'late A delta' })
    await streamCallback({ type: 'completed', payload: { title: 'late A completion' } })

    expect(streamSignal.aborted).toBe(true)
    expect(state.selectedContract.id).toBe('contract-b')
    expect(state.form.title).toBe('B payload')
    expect(state.streamText).toBe('')

    finishStream()
    await generatingA
  })

  it('does not let an initial recovery overwrite a newer stream for the same node', async () => {
    const root = node('root', 'contract-root')
    const current = contract('contract-root', { title: 'Root payload' })
    let resolveInitialRecovery!: (value: Record<string, unknown>) => void
    const initialRecovery = new Promise<Record<string, unknown>>((resolve) => { resolveInitialRecovery = resolve })
    let finishStream!: () => void
    const streamGate = new Promise<void>((resolve) => { finishStream = resolve })

    mocks.getTree.mockResolvedValue(root)
    mocks.getContract.mockResolvedValue(current)
    mocks.getLatestGenerationAttempt
      .mockImplementationOnce(() => initialRecovery)
      .mockResolvedValueOnce(null)
    mocks.consumeOutlineDraftStream.mockImplementation(async (_id, callback) => {
      await callback({ type: 'started', attempt_id: 'new-attempt' })
      await streamGate
    })

    const state = await setupStudio()
    const loading = state.loadTree()
    await flushAsyncWork()
    const generating = state.generateDraftStream()
    await flushAsyncWork()

    resolveInitialRecovery({
      id: 'stale-attempt', contract_id: 'contract-root', status: 'running',
      accumulated_text: 'stale recovery text', error: '', events: [],
    })
    await flushAsyncWork()

    expect(state.streamAttempt.id).toBe('new-attempt')
    expect(state.streamText).toBe('')

    finishStream()
    await generating
    await loading
  })

  it('keeps a loaded contract when its initial stream recovery fails', async () => {
    const root = node('root', 'contract-root')
    const current = contract('contract-root', { title: 'Root payload' })
    mocks.getTree.mockResolvedValue(root)
    mocks.getContract.mockResolvedValue(current)
    mocks.getLatestGenerationAttempt.mockRejectedValue(new Error('recovery unavailable'))

    const state = await setupStudio()
    await state.loadTree()

    expect(state.selectedContract.id).toBe('contract-root')
    expect(state.error).toContain('recovery unavailable')
  })

  it('edits only author notes while preserving structured foreshadowing buckets', async () => {
    const payload = {
      title: 'Root title',
      foreshadowing: {
        setup: ['Keep setup'],
        payoff: ['Keep payoff'],
        author_notes: ['Old author note'],
      },
    }
    const root = node('root', 'contract-root')
    const current = contract('contract-root', payload)
    mocks.getTree.mockResolvedValue(root)
    mocks.getContract.mockResolvedValue(current)
    mocks.saveDraft.mockResolvedValue(current)

    const state = await setupStudio()
    await state.loadTree()
    expect(state.form.foreshadowText).toBe('Old author note')

    state.form.foreshadowText = 'New author note'
    await state.saveDraft()

    expect(mocks.saveDraft).toHaveBeenCalledWith('contract-root', {
      ...payload,
      foreshadowing: {
        setup: ['Keep setup'],
        payoff: ['Keep payoff'],
        author_notes: ['New author note'],
      },
    })
  })
})
