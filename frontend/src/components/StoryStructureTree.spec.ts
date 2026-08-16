import { describe, expect, it, vi } from 'vitest'
import { createVNode, defineComponent, h } from 'vue'
import { renderToString } from 'vue/server-renderer'

const mocks = vi.hoisted(() => ({
  getTree: vi.fn(),
  createNode: vi.fn(),
  updateNode: vi.fn(),
  deleteNode: vi.fn(),
  getStatus: vi.fn(),
  selectChapter: vi.fn(),
  messageError: vi.fn(),
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
    useDialog: () => ({ warning: vi.fn() }),
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
  chapterApi: { listChapters: vi.fn().mockResolvedValue([]) },
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
})
