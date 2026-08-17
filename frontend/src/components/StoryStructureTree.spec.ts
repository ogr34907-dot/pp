import { describe, expect, it, vi } from 'vitest'
import { createVNode, defineComponent, h } from 'vue'
import { renderToString } from 'vue/server-renderer'

const mocks = vi.hoisted(() => ({
  getTree: vi.fn(),
  createNode: vi.fn(),
  updateNode: vi.fn(),
  deleteNode: vi.fn(),
  listChapters: vi.fn(),
  getStatus: vi.fn(),
  selectChapter: vi.fn(),
  messageError: vi.fn(),
  dialogWarning: vi.fn(),
}))

vi.mock('naive-ui', () => {
  const component = { render: () => null }
  return {
    NTree: component,
    NEmpty: component,
    NSpin: component,
    NTag: component,
    NSpace: component,
    NButton: component,
    NDropdown: component,
    NModal: component,
    NInput: component,
    useMessage: () => ({ error: mocks.messageError, success: vi.fn() }),
    useDialog: () => ({ warning: mocks.dialogWarning }),
  }
})

vi.mock('@/api/structure', () => ({
  structureApi: {
    getTree: mocks.getTree,
    createNode: mocks.createNode,
    updateNode: mocks.updateNode,
    deleteNode: mocks.deleteNode,
  },
}))

vi.mock('@/api/chapter', () => ({
  chapterApi: { listChapters: mocks.listChapters },
}))

vi.mock('@/api/autopilot', () => ({
  autopilotApi: { getStatus: mocks.getStatus },
  isAutopilotHttpError: () => false,
}))

vi.mock('@/api/planning', () => ({
  planningApi: { getMacroProgress: vi.fn() },
  watchMacroPlanProgress: vi.fn(() => new AbortController()),
}))

vi.mock('@/composables/useAdaptivePolling', () => ({
  useAdaptivePolling: () => ({ start: vi.fn(), stop: vi.fn() }),
}))

import StoryStructureTree from './StoryStructureTree.vue'

async function setupTree() {
  let releaseRender!: () => void
  const renderGate = new Promise<void>((resolve) => { releaseRender = resolve })
  const blocker = defineComponent({
    async setup() {
      await renderGate
      return () => null
    },
  })
  const target = createVNode(StoryStructureTree, {
    slug: 'novel-1',
    onSelectChapter: mocks.selectChapter,
  })
  const root = createVNode(defineComponent({
    render: () => [target, h(blocker)],
  }))
  const rendering = renderToString(root, {})
  for (let attempt = 0; attempt < 20 && !target.component; attempt++) await Promise.resolve()
  if (!target.component) throw new Error('StoryStructureTree setup did not start')
  return {
    state: (target.component as any).setupState as Record<string, any>,
    finish: async () => {
      releaseRender()
      await rendering
    },
  }
}

describe('StoryStructureTree manifest authority', () => {
  it('keeps chapter navigation but removes every legacy write menu entry', async () => {
    const chapter = {
      id: 'chapter-1',
      novel_id: 'novel-1',
      parent_id: 'act-1',
      node_type: 'chapter',
      number: 1,
      title: 'Opening',
      order_index: 0,
      chapter_count: 0,
      metadata: {},
      created_at: '',
      updated_at: '',
      level: 4,
      icon: '',
      display_name: '',
      children: [],
    }
    const act = {
      ...chapter,
      id: 'act-1',
      parent_id: null,
      node_type: 'act',
      title: 'Act one',
      number: 1,
      level: 1,
      children: [chapter],
    }
    mocks.getTree.mockResolvedValue({
      novel_id: 'novel-1',
      tree: { nodes: [act] },
      manifest_authority: true,
    })

    const mounted = await setupTree()
    try {
      await mounted.state.loadTree()
      const props = mounted.state.nodeProps({ option: chapter })
      mounted.state.menuTargetNode = chapter

      expect(props.onContextmenu).toBeUndefined()
      expect(mounted.state.menuOptions).toEqual([])

      mounted.state.handleSelect(['chapter-1'])
      expect(mocks.selectChapter).toHaveBeenCalledWith(1, 'Opening')
      expect(mocks.createNode).not.toHaveBeenCalled()
      expect(mocks.updateNode).not.toHaveBeenCalled()
      expect(mocks.deleteNode).not.toHaveBeenCalled()
    } finally {
      await mounted.finish()
    }
  })

  it('invalidates legacy write interactions already opened before manifest cutover', async () => {
    const chapter = {
      id: 'chapter-1',
      novel_id: 'novel-1',
      parent_id: null,
      node_type: 'chapter',
      number: 1,
      title: 'Opening',
      order_index: 0,
      chapter_count: 0,
      metadata: {},
      created_at: '',
      updated_at: '',
      level: 1,
      icon: '',
      display_name: '',
      children: [],
    }
    const legacyTree = {
      novel_id: 'novel-1',
      tree: { nodes: [chapter] },
      manifest_authority: false,
    }
    mocks.getTree.mockReset()
      .mockResolvedValueOnce(legacyTree)
      .mockResolvedValue({ ...legacyTree, manifest_authority: true })
    mocks.deleteNode.mockReset().mockResolvedValue(true)
    mocks.dialogWarning.mockClear()

    const mounted = await setupTree()
    try {
      await mounted.state.loadTree()
      mounted.state.menuTargetNode = chapter
      mounted.state.handleMenuSelect('delete')
      const pendingDelete = mocks.dialogWarning.mock.calls[0][0]
      mounted.state.menuVisible = true
      mounted.state.showRename = true
      mounted.state.showAddChild = true

      await mounted.state.loadTree()

      expect(mounted.state.menuVisible).toBe(false)
      expect(mounted.state.showRename).toBe(false)
      expect(mounted.state.showAddChild).toBe(false)
      await pendingDelete.onPositiveClick()
      expect(mocks.deleteNode).not.toHaveBeenCalled()
    } finally {
      await mounted.finish()
    }
  })

  it('does not create a legacy child when manifest authority changes during chapter lookup', async () => {
    const act = {
      id: 'act-1',
      novel_id: 'novel-1',
      parent_id: null,
      node_type: 'act',
      number: 1,
      title: 'Act one',
      order_index: 0,
      chapter_count: 0,
      metadata: {},
      created_at: '',
      updated_at: '',
      level: 1,
      icon: '',
      display_name: '',
      children: [],
    }
    const legacyTree = {
      novel_id: 'novel-1',
      tree: { nodes: [act] },
      manifest_authority: false,
    }
    let releaseChapterLookup!: (chapters: unknown[]) => void
    mocks.getTree.mockReset()
      .mockResolvedValueOnce(legacyTree)
      .mockResolvedValue({ ...legacyTree, manifest_authority: true })
    mocks.listChapters.mockReset().mockImplementation(
      () => new Promise((resolve) => { releaseChapterLookup = resolve })
    )
    mocks.createNode.mockReset().mockResolvedValue(true)

    const mounted = await setupTree()
    try {
      await mounted.state.loadTree()
      mounted.state.menuTargetNode = act
      mounted.state.addChildValue = 'Chapter one'
      const pendingAdd = mounted.state.doAddChild()
      await Promise.resolve()

      await mounted.state.loadTree()
      releaseChapterLookup([])
      await pendingAdd

      expect(mocks.createNode).not.toHaveBeenCalled()
    } finally {
      await mounted.finish()
    }
  })
})
