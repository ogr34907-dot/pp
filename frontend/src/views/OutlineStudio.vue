<template>
  <main class="outline-studio" aria-labelledby="outline-studio-title">
    <header class="outline-studio__header">
      <div>
        <p class="outline-studio__eyebrow">计划层 · 已发布版本才会进入正文提示词</p>
        <h1 id="outline-studio-title">五级大纲工作室</h1>
        <p>总纲 → 部纲 → 卷纲 → 幕纲 → 章纲。先发布并同步父级，才可生成下一层。</p>
      </div>
      <div class="outline-studio__header-actions">
        <n-button secondary @click="loadTree" :loading="loading">刷新状态</n-button>
        <n-button type="primary" @click="router.push(`/book/${novelId}/workbench`)">
          <template #icon><n-icon :component="CreateOutline" /></template>
          返回工作台
        </n-button>
      </div>
    </header>

    <n-alert v-if="error" type="error" :show-icon="true" role="alert" class="outline-studio__alert">
      {{ error }}
    </n-alert>

    <div class="outline-studio__grid">
      <aside class="outline-panel outline-tree-panel" aria-label="五级大纲树">
        <div class="outline-panel__head">
          <div>
            <span class="outline-panel__kicker">结构</span>
            <h2>{{ workingTree ? '审核中的 Working Tree' : '当前计划树' }}</h2>
          </div>
          <span class="outline-count">{{ flattenedTree.length }} 节点</span>
        </div>
        <div v-if="loading && !displayTree" class="outline-tree-empty">正在读取大纲…</div>
        <div v-else-if="!displayTree" class="outline-tree-empty">尚未建立规划结构。</div>
        <nav v-else class="outline-tree" aria-label="大纲节点">
          <button
            v-for="item in flattenedTree"
            :key="nodeKey(item.node)"
            type="button"
            class="outline-tree__node"
            :class="{ 'is-selected': selectedNode && nodeKey(selectedNode) === nodeKey(item.node) }"
            :style="{ '--tree-depth': item.depth }"
            :aria-current="selectedNode && nodeKey(selectedNode) === nodeKey(item.node) ? 'page' : undefined"
            @click="selectNode(item.node)"
          >
            <span class="outline-tree__level">{{ levelLabel(item.node.node_type) }}</span>
            <span class="outline-tree__copy">
              <strong>{{ item.node.title || defaultTitle(item.node.node_type) }}</strong>
            <small>{{ nodeStatusLabel(item.node) }}</small>
            </span>
            <span class="outline-tree__state" :class="`is-${nodeStatus(item.node)}`" aria-hidden="true" />
          </button>
        </nav>
      </aside>

      <section class="outline-panel outline-editor-panel" aria-label="大纲编辑器">
        <div class="outline-panel__head outline-editor-head">
          <div>
            <span class="outline-panel__kicker">{{ selectedNode ? levelLabel(selectedNode.node_type) : '选择节点' }}</span>
            <h2>{{ selectedNode?.title || '大纲编辑器' }}</h2>
          </div>
          <StatusPill v-if="selectedContract" :status="selectedContract.active?.status || selectedContract.draft?.status || 'missing'" />
        </div>

        <div v-if="!selectedNode" class="outline-editor-empty">
          <n-icon :component="GitNetworkOutline" :size="36" aria-hidden="true" />
          <p>从左侧选择一个大纲节点，开始编辑其计划契约。</p>
        </div>

        <div v-else-if="!selectedContract" class="outline-editor-empty">
          <n-icon :component="LockClosedOutline" :size="32" aria-hidden="true" />
          <h3>该层尚未开放</h3>
          <p>先让父级处于“已同步”状态，再建立本层契约。作者已锁定或人工修改过的子纲不会被静默覆盖。</p>
          <n-button v-if="selectedNode.node_type !== 'outline'" type="primary" :loading="binding" @click="bindSelectedNode">
            建立本层契约
          </n-button>
        </div>

        <form v-else class="outline-editor" @submit.prevent="saveDraft">
          <div class="outline-editor__summary">
            <n-input v-model:value="form.title" :disabled="!canEditSelectedNode" placeholder="本层标题" aria-label="大纲标题" />
            <n-input v-model:value="form.creative_goal" :disabled="!canEditSelectedNode" placeholder="创作目标：这一层需要完成什么？" aria-label="创作目标" />
          </div>
          <n-input
            v-model:value="form.narrative_text"
            type="textarea"
            :rows="5"
            :disabled="!canEditSelectedNode"
            placeholder="叙述性计划：给作者和下一层 AI 的清晰说明。"
            aria-label="叙述性计划"
          />
          <div class="outline-editor__grid">
            <FieldTextarea v-model="form.entry_state" :disabled="!canEditSelectedNode" label="进入状态" placeholder="人物、关系、地点与世界从何处开始" />
            <FieldTextarea v-model="form.exit_state" :disabled="!canEditSelectedNode" label="结束状态" placeholder="这一层结束时必须抵达的状态" />
            <FieldTextarea v-model="form.requiredEventsText" :disabled="!canEditSelectedNode" label="必须发生" placeholder="每行一条事件" />
            <FieldTextarea v-model="form.forbiddenEventsText" :disabled="!canEditSelectedNode" label="禁止发生" placeholder="每行一条禁止项" />
            <FieldTextarea v-model="form.handoffText" :disabled="!canEditSelectedNode" label="向下交接" placeholder="每行一条必须留给下层的条件" />
            <FieldTextarea v-model="form.foreshadowText" :disabled="!canEditSelectedNode" label="伏笔要求" placeholder="每行一条：设置 / 推进 / 兑现" />
          </div>
          <div class="outline-editor__grid outline-editor__grid--numbers">
            <n-form-item label="起始章节"><n-input-number v-model:value="form.chapter_start" :disabled="!canEditSelectedNode" :min="1" clearable /></n-form-item>
            <n-form-item label="结束章节"><n-input-number v-model:value="form.chapter_end" :disabled="!canEditSelectedNode" :min="1" clearable /></n-form-item>
            <n-form-item label="篇幅预算（字）"><n-input-number v-model:value="form.word_budget" :disabled="!canEditSelectedNode" :min="0" :step="1000" clearable /></n-form-item>
          </div>
          <div v-if="selectedContract.level === 'chapter'" class="outline-editor__chapter-fields">
            <n-input v-model:value="form.pov" :disabled="!canEditSelectedNode" placeholder="POV / 叙事视角" aria-label="POV" />
            <FieldTextarea v-model="form.scenesText" :disabled="!canEditSelectedNode" label="场景" placeholder="每行一个场景" />
            <FieldTextarea v-model="form.beatsText" :disabled="!canEditSelectedNode" label="节拍" placeholder="每行一个节拍" />
            <FieldTextarea v-model="form.conflictsText" :disabled="!canEditSelectedNode" label="冲突" placeholder="每行一个冲突" />
            <n-input v-model:value="form.ending_hook" :disabled="!canEditSelectedNode" placeholder="结尾钩子" aria-label="结尾钩子" />
          </div>
          <div class="outline-editor__actions">
            <n-button v-if="canEditSelectedNode" type="primary" attr-type="submit" :loading="saving">{{ isWorkingNode(selectedNode) ? '保存 Working 草稿' : '保存草稿' }}</n-button>
            <n-button v-if="canEditSelectedNode" secondary :loading="streaming" @click.prevent="generateDraftStream">
              <template #icon><n-icon :component="SparklesOutline" /></template>
              {{ draftButtonLabel }}
            </n-button>
            <n-button
              v-if="canEditSelectedNode"
              :disabled="isWorkingNode(selectedNode) ? !cohortAttemptId : !selectedContract.draft"
              :loading="publishing"
              @click.prevent="publishAndSync"
            >
              <template #icon><n-icon :component="CloudUploadOutline" /></template>
              {{ isWorkingNode(selectedNode) ? '作者发布规划' : '发布并同步' }}
            </n-button>
            <n-button
              v-if="nextLevel && canGenerateNextCohort"
              secondary
              :loading="cohortLoading"
              :disabled="cohortLoading"
              @click.prevent="generateNextCohort"
            >
              <template #icon><n-icon :component="SparklesOutline" /></template>
              {{ cohortButtonLabel }}
            </n-button>
          </div>
          <p class="outline-editor__note">草稿不会进入正文提示词；发布后会切换计划投影，并使受影响的未锁定子纲过期。</p>
        </form>
      </section>

      <aside class="outline-panel outline-inspector" aria-label="计划约束与影响">
        <div class="outline-panel__head">
          <div>
            <span class="outline-panel__kicker">检查器</span>
            <h2>约束与影响</h2>
          </div>
        </div>
        <template v-if="selectedNode">
          <section class="inspector-block">
            <h3>父级约束</h3>
            <p v-if="selectedParent">{{ selectedParent.title || defaultTitle(selectedParent.node_type) }} · {{ nodeStatusLabel(selectedParent) }}</p>
            <p v-else>总纲是此书唯一的计划根节点。</p>
          </section>
          <section class="inspector-block">
            <h3>当前版本</h3>
            <dl v-if="selectedContract" class="inspector-facts">
              <div><dt>已发布</dt><dd>r{{ selectedContract.active?.revision ?? '—' }}</dd></div>
              <div><dt>草稿</dt><dd>r{{ selectedContract.draft?.revision ?? '—' }}</dd></div>
              <div><dt>作者锁定</dt><dd>{{ selectedContract.author_locked ? '是' : '否' }}</dd></div>
              <div><dt>人工修改</dt><dd>{{ selectedContract.has_author_edits ? '是' : '否' }}</dd></div>
            </dl>
            <p v-else>父级发布并同步后才允许建立该层。</p>
          </section>
          <section class="inspector-block inspector-block--impact">
            <h3>发布影响</h3>
            <p>未来章节将使用新版本；已经发生的正文事实不会被覆盖。若新计划与历史冲突，请从冲突章节创建世界线重生成。</p>
            <n-button text type="primary" :disabled="!selectedContract" @click="router.push(`/book/${novelId}/worldline`)">
              打开世界线重生成
            </n-button>
          </section>
          <section v-if="streamText || streaming || streamAttempt" class="inspector-block inspector-block--stream" aria-live="polite">
            <h3>AI 草稿流</h3>
            <p>{{ streamStatus }}</p>
            <pre>{{ streamText || '正在准备…' }}</pre>
            <n-button v-if="streaming && streamAttempt" secondary size="small" @click="cancelDraftStream">取消本次生成</n-button>
            <n-button v-else-if="streamAttempt?.status === 'failed' || streamAttempt?.status === 'cancelled'" secondary size="small" @click="retryDraftStream">复用上下文重试</n-button>
          </section>
        </template>
        <div v-else class="outline-inspector__empty">选择一个节点后，可在此查看父级链、版本和发布影响。</div>
      </aside>
    </div>
  </main>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useMessage } from 'naive-ui'
import {
  CloudUploadOutline,
  CreateOutline,
  GitNetworkOutline,
  LockClosedOutline,
  SparklesOutline,
} from '@vicons/ionicons5'
import FieldTextarea from '@/components/outline/OutlineFieldTextarea.vue'
import StatusPill from '@/components/outline/OutlineStatusPill.vue'
import {
  consumeOutlineDraftStream,
  outlineApi,
  type OutlineContract,
  type OutlineGenerationAttempt,
  type OutlinePayload,
  type OutlineTreeNode,
} from '@/api/generation'
import { outlineLines, outlineStringLists, outlineText } from '@/domain/outlinePresentation'

type FlattenedNode = { node: OutlineTreeNode; depth: number; parent?: OutlineTreeNode }
type FormState = {
  title: string; narrative_text: string; creative_goal: string; entry_state: string; exit_state: string
  requiredEventsText: string; forbiddenEventsText: string; handoffText: string; foreshadowText: string
  chapter_start: number | null; chapter_end: number | null; word_budget: number | null
  pov: string; scenesText: string; beatsText: string; conflictsText: string; ending_hook: string
}
type OutlineLevel = 'outline' | 'part' | 'volume' | 'act' | 'chapter'
type CohortLevel = Exclude<OutlineLevel, 'outline'>

const route = useRoute()
const router = useRouter()
const message = useMessage()
const novelId = computed(() => String(route.params.slug || ''))
const tree = ref<OutlineTreeNode | null>(null)
const workingTree = ref<OutlineTreeNode | null>(null)
const workingTreeLoadFailed = ref(false)
const selectedNode = ref<OutlineTreeNode | null>(null)
const selectedParent = ref<OutlineTreeNode | undefined>()
const selectedContract = ref<OutlineContract | null>(null)
const loading = ref(false)
const saving = ref(false)
const publishing = ref(false)
const binding = ref(false)
const streaming = ref(false)
const cohortLoading = ref(false)
const streamText = ref('')
const streamAttempt = ref<OutlineGenerationAttempt | null>(null)
const error = ref('')
const cohortAttemptId = ref<string | null>(null)
let streamController: AbortController | null = null
let selectionEpoch = 0
let streamEpoch = 0
let payloadSnapshot: OutlinePayload | null = null
let formSnapshot: FormState | null = null

const form = reactive<FormState>({
  title: '', narrative_text: '', creative_goal: '', entry_state: '', exit_state: '',
  requiredEventsText: '', forbiddenEventsText: '', handoffText: '', foreshadowText: '',
  chapter_start: null, chapter_end: null, word_budget: null, pov: '', scenesText: '', beatsText: '', conflictsText: '', ending_hook: '',
})

const flattenedTree = computed<FlattenedNode[]>(() => {
  const result: FlattenedNode[] = []
  const visit = (node: OutlineTreeNode, depth: number, parent?: OutlineTreeNode) => {
    result.push({ node, depth, parent })
    for (const child of node.children || []) visit(child, depth + 1, node)
  }
  if (displayTree.value) visit(displayTree.value, 0)
  return result
})

const displayTree = computed(() => workingTree.value || tree.value)
const canEditSelectedNode = computed(() => Boolean(selectedNode.value && !isManifestActiveNode(selectedNode.value) && !workingTreeLoadFailed.value))

const nextLevel = computed<CohortLevel | null>(() => {
  const node = selectedNode.value
  if (!node || nodeStatus(node) !== 'synced') return null
  return ({
    outline: 'part',
    part: 'volume',
    volume: 'act',
    act: 'chapter',
  } as Partial<Record<OutlineLevel, CohortLevel>>)[String(node.node_type) as OutlineLevel] || null
})

const cohortButtonLabel = computed(() => ({
  part: 'AI 一键生成全部部纲',
  volume: 'AI 一键生成该部全部卷纲',
  act: 'AI 一键生成该卷全部幕纲',
  chapter: 'AI 一键生成该幕全部章纲',
} as Record<string, string>)[nextLevel.value || ''] || '')

const draftButtonLabel = computed(() => (
  String(selectedNode.value?.node_type || '') === 'outline'
    ? 'AI 一键生成总纲草稿'
    : 'AI 流式生成草稿'
))

const canGenerateNextCohort = computed(() => Boolean(
  selectedNode.value
  && nextLevel.value
  && !isWorkingNode(selectedNode.value)
  && !flattenedTree.value.some(item => (
    item.parent && nodeKey(item.parent) === nodeKey(selectedNode.value as OutlineTreeNode)
    && String(item.node.node_type) === nextLevel.value
  )),
))

function levelLabel(level?: string) {
  return ({ outline: '总纲', part: '部纲', volume: '卷纲', act: '幕纲', chapter: '章纲' } as Record<string, string>)[String(level)] || '计划'
}
function defaultTitle(level?: string) { return `${levelLabel(level)}（未命名）` }
function nodeKey(node: OutlineTreeNode) {
  if (node.logical_node_id) return String(node.logical_node_id)
  if (node.story_node_id) return `story:${node.story_node_id}`
  return String(node.id)
}
function treeMode(node?: OutlineTreeNode | null) {
  if (node?.tree_mode) return node.tree_mode
  return node?.status === 'draft' && node.plan_revision_id && node.logical_node_id
    ? 'MANIFEST_WORKING'
    : 'LEGACY_ACTIVE'
}
function isManifestActiveNode(node?: OutlineTreeNode | null) {
  return treeMode(node) === 'MANIFEST_ACTIVE'
}
function isWorkingNode(node?: OutlineTreeNode | null) {
  return Boolean(node && treeMode(node) === 'MANIFEST_WORKING' && node.status === 'draft' && node.plan_revision_id && node.logical_node_id)
}
function requireLogicalNodeId(node: OutlineTreeNode): string {
  const logicalNodeId = String(node.logical_node_id || '').trim()
  if (!logicalNodeId) throw new Error('当前节点缺少 logical_node_id，已阻止生成 Cohort')
  return logicalNodeId
}
function ensureWorkingTreeReadable(): boolean {
  if (!workingTreeLoadFailed.value) return true
  error.value = 'Working Tree 读取失败，已阻止规划写操作；请刷新并确认后端状态。'
  return false
}
function findFirstChild(
  root: OutlineTreeNode | null,
  parentKey: string,
  level: CohortLevel,
): OutlineTreeNode | null {
  if (!root) return null
  if (nodeKey(root) === parentKey) {
    return (root.children || []).find(child => String(child.node_type) === level) || null
  }
  for (const child of root.children || []) {
    const match = findFirstChild(child, parentKey, level)
    if (match) return match
  }
  return null
}
function nodeStatus(node: OutlineTreeNode) { return String(node.status || node.outline_contract?.status || 'missing') }
function nodeStatusLabel(node: OutlineTreeNode) {
  return ({ synced: '已发布 · 已同步', syncing: '发布同步中', published: '已发布', draft: '草稿待发布', stale: '需要重新校验', conflict: '需要处理冲突', missing: '等待父级开放' } as Record<string, string>)[nodeStatus(node)] || '等待配置'
}
function toLines(value: unknown) { return outlineLines(value).join('\n') }
function fromLines(value: string) { return value.split(/\r?\n/).map(item => item.trim()).filter(Boolean) }
function payloadToForm(payload?: OutlinePayload | null) {
  const data = payload || {}
  form.title = outlineText(data.title)
  form.narrative_text = outlineText(data.narrative_text)
  form.creative_goal = outlineText(data.creative_goal)
  form.entry_state = outlineText(data.entry_state)
  form.exit_state = outlineText(data.exit_state)
  form.requiredEventsText = toLines(data.required_events)
  form.forbiddenEventsText = toLines(data.forbidden_events)
  form.handoffText = toLines(data.handoff_conditions)
  form.foreshadowText = toLines(outlineStringLists(data.foreshadowing).author_notes)
  form.chapter_start = data.chapter_start ?? null
  form.chapter_end = data.chapter_end ?? null
  form.word_budget = data.word_budget ?? null
  form.pov = outlineText(data.pov)
  form.scenesText = toLines(data.scenes)
  form.beatsText = toLines(data.beats)
  form.conflictsText = toLines(data.conflicts)
  form.ending_hook = outlineText(data.ending_hook)
  payloadSnapshot = payload ? { ...payload } : null
  formSnapshot = { ...form }
}

function formChanged(field: keyof FormState) {
  return !formSnapshot || form[field] !== formSnapshot[field]
}

function formToPayload(): OutlinePayload {
  const changes: Partial<OutlinePayload> = {}
  if (formChanged('title')) changes.title = form.title.trim()
  if (formChanged('narrative_text')) changes.narrative_text = form.narrative_text.trim()
  if (formChanged('creative_goal')) changes.creative_goal = form.creative_goal.trim()
  if (formChanged('entry_state')) changes.entry_state = form.entry_state.trim()
  if (formChanged('exit_state')) changes.exit_state = form.exit_state.trim()
  if (formChanged('requiredEventsText')) changes.required_events = fromLines(form.requiredEventsText)
  if (formChanged('forbiddenEventsText')) changes.forbidden_events = fromLines(form.forbiddenEventsText)
  if (formChanged('handoffText')) changes.handoff_conditions = fromLines(form.handoffText)
  if (formChanged('chapter_start')) changes.chapter_start = form.chapter_start
  if (formChanged('chapter_end')) changes.chapter_end = form.chapter_end
  if (formChanged('word_budget')) changes.word_budget = form.word_budget
  if (formChanged('pov')) changes.pov = form.pov.trim()
  if (formChanged('scenesText')) changes.scenes = fromLines(form.scenesText)
  if (formChanged('beatsText')) changes.beats = fromLines(form.beatsText)
  if (formChanged('conflictsText')) changes.conflicts = fromLines(form.conflictsText)
  if (formChanged('ending_hook')) changes.ending_hook = form.ending_hook.trim()
  if (formChanged('foreshadowText')) {
    changes.foreshadowing = {
      ...(payloadSnapshot?.foreshadowing || {}),
      author_notes: fromLines(form.foreshadowText),
    }
  }
  return { ...(payloadSnapshot || {}), ...changes }
}

type ContractContext = { selection: number; nodeId: string; contractId: string }

function isCurrentNode(selection: number, nodeId: string) {
  return selection === selectionEpoch && selectedNode.value && nodeKey(selectedNode.value) === nodeId
}

function captureContractContext(contract: OutlineContract): ContractContext | null {
  const nodeId = selectedNode.value ? nodeKey(selectedNode.value) : undefined
  if (!nodeId || selectedContract.value?.id !== contract.id) return null
  return { selection: selectionEpoch, nodeId, contractId: contract.id }
}

function isCurrentContract(context: ContractContext) {
  return isCurrentNode(context.selection, context.nodeId)
    && selectedContract.value?.id === context.contractId
}
function idempotencyKey(prefix: string) { return `${prefix}-${crypto.randomUUID()}` }
const streamStatus = computed(() => {
  const attempt = streamAttempt.value
  if (streaming.value) return attempt ? `正在接收流式草稿（尝试 ${attempt.id.slice(-8)}）…` : '正在建立流式草稿尝试…'
  if (attempt?.status === 'completed') return '草稿已通过结构校验并写入当前节点的草稿版本。'
  if (attempt?.status === 'cancelled') return '本次流式草稿已取消；可复用原上下文重试。'
  if (attempt?.status === 'failed') return attempt.error || '本次流式草稿失败；可复用原上下文重试。'
  return '正在恢复已持久化的草稿流…'
})

async function loadTree() {
  if (!novelId.value) return
  loading.value = true
  error.value = ''
  workingTreeLoadFailed.value = false
  try {
    tree.value = await outlineApi.getTree(novelId.value)
    try {
      workingTree.value = await outlineApi.getWorkingTree(novelId.value)
    } catch (cause) {
      workingTree.value = null
      workingTreeLoadFailed.value = true
      throw cause
    }
    if (!selectedNode.value && displayTree.value) await selectNode(displayTree.value)
    else if (selectedNode.value) {
      const currentKey = nodeKey(selectedNode.value)
      const replacement = flattenedTree.value.find(item => nodeKey(item.node) === currentKey)?.node
      if (replacement) selectedNode.value = replacement
    }
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : '读取五级大纲失败'
  } finally { loading.value = false }
}

async function selectNode(node: OutlineTreeNode) {
  const requestEpoch = ++selectionEpoch
  ++streamEpoch
  streamController?.abort()
  streamController = null
  selectedNode.value = node
  selectedParent.value = flattenedTree.value.find(item => nodeKey(item.node) === nodeKey(node))?.parent
  selectedContract.value = null
  streamText.value = ''
  streamAttempt.value = null
  streaming.value = false
  saving.value = false
  publishing.value = false
  binding.value = false
  cohortLoading.value = false
  cohortAttemptId.value = node.cohort_attempt_id || null
  error.value = ''
  payloadToForm(null)
  const contractId = node.outline_contract?.contract_id
  if (!contractId) return
  let contract: OutlineContract
  try {
    contract = await outlineApi.getContract(contractId)
  } catch (cause) {
    if (!isCurrentNode(requestEpoch, nodeKey(node))) return
    selectedContract.value = null
    error.value = cause instanceof Error ? cause.message : '读取大纲节点失败'
    return
  }
  if (!isCurrentNode(requestEpoch, nodeKey(node)) || contract.id !== contractId) return
  selectedContract.value = contract
  payloadToForm(isWorkingNode(node) ? node.payload : (contract.draft?.payload || contract.active?.payload))
  const context = captureContractContext(contract)
  if (!context) return
  const recoveryStreamEpoch = streamEpoch
  try {
    await recoverDraftAttempt(contractId, context, recoveryStreamEpoch)
  } catch (cause) {
    if (isCurrentRecovery(context, recoveryStreamEpoch)) {
      error.value = cause instanceof Error ? cause.message : '恢复草稿流失败'
    }
  }
}

async function bindSelectedNode() {
  if (!ensureWorkingTreeReadable()) return
  const node = selectedNode.value
  const requestEpoch = selectionEpoch
  if (!node || node.node_type === 'outline') return
  binding.value = true
  error.value = ''
  try {
    const contract = await outlineApi.bindNode(novelId.value, node.id)
    if (!isCurrentNode(requestEpoch, nodeKey(node))) return
    selectedContract.value = contract
    payloadToForm(contract.draft?.payload || contract.active?.payload)
    await loadTree()
    if (isCurrentNode(requestEpoch, nodeKey(node))) message.success('本层契约已建立；现在可以生成或编辑草稿。')
  } catch (cause) {
    if (isCurrentNode(requestEpoch, nodeKey(node))) error.value = cause instanceof Error ? cause.message : '建立本层契约失败'
  } finally {
    if (isCurrentNode(requestEpoch, nodeKey(node))) binding.value = false
  }
}

async function saveDraft() {
  if (!ensureWorkingTreeReadable() || !canEditSelectedNode.value) {
    if (!canEditSelectedNode.value && !workingTreeLoadFailed.value) {
      error.value = 'Active Manifest 只读，不能保存 Legacy 草稿。'
    }
    return
  }
  const contract = selectedContract.value
  if (!contract) return
  const context = captureContractContext(contract)
  if (!context) return
  saving.value = true
  error.value = ''
  try {
    const node = selectedNode.value
    if (node && isWorkingNode(node)) {
      if (!node.plan_revision_id || !node.logical_node_id || !node.plan_digest || !node.version_digest) {
        throw new Error('Working 草稿缺少版本校验信息')
      }
      const saved = await outlineApi.saveWorkingItem(node.plan_revision_id, node.logical_node_id, {
        payload: formToPayload(),
        expected_plan_digest: node.plan_digest,
        expected_version_digest: node.version_digest,
      })
      if (!isCurrentContract(context) || saved.logical_node_id !== node.logical_node_id) return
      payloadToForm(saved.payload)
      await loadTree()
      if (isCurrentContract(context)) message.success('Working 草稿已保存，尚未进入正文上下文。')
      return
    }
    const saved = await outlineApi.saveDraft(contract.id, formToPayload())
    if (!isCurrentContract(context) || saved.id !== contract.id) return
    selectedContract.value = saved
    payloadToForm(saved.draft?.payload)
    await loadTree()
    if (isCurrentContract(context)) message.success('草稿已保存，尚未进入正文上下文。')
  } catch (cause) {
    if (isCurrentContract(context)) error.value = cause instanceof Error ? cause.message : '保存草稿失败'
  } finally {
    if (isCurrentContract(context)) saving.value = false
  }
}

async function publishAndSync() {
  if (!ensureWorkingTreeReadable() || !canEditSelectedNode.value) {
    if (!canEditSelectedNode.value && !workingTreeLoadFailed.value) {
      error.value = 'Active Manifest 只读，不能使用 Legacy 发布。'
    }
    return
  }
  const contract = selectedContract.value
  const node = selectedNode.value
  const draft = contract?.draft
  if (!contract || (!draft && !isWorkingNode(node))) return
  const context = captureContractContext(contract)
  if (!context) return
  publishing.value = true
  error.value = ''
  try {
    if (node && isWorkingNode(node)) {
      if (!cohortAttemptId.value) throw new Error('当前 Working 草稿没有可发布的 Cohort Attempt')
      await outlineApi.authorPublishCohort(cohortAttemptId.value)
      if (!isCurrentContract(context)) return
      workingTree.value = null
      cohortAttemptId.value = null
      await loadTree()
      if (isCurrentContract(context)) message.success('规划已由作者发布并切换为 Active Manifest。')
      return
    }
    if (!draft) return
    const published = await outlineApi.publish(contract.id, draft.revision, idempotencyKey('outline-publish'))
    if (!isCurrentContract(context) || published.id !== contract.id) return
    selectedContract.value = published
    await loadTree()
    if (isCurrentContract(context)) message.success('已发布并同步；下一级现在可按新的计划链生成。')
  } catch (cause) {
    if (isCurrentContract(context)) error.value = cause instanceof Error ? cause.message : '发布并同步失败'
  } finally {
    if (isCurrentContract(context)) publishing.value = false
  }
}

async function generateNextCohort() {
  if (!ensureWorkingTreeReadable()) return
  const node = selectedNode.value
  const level = nextLevel.value
  if (!node || !level || !canGenerateNextCohort.value) return
  const requestEpoch = selectionEpoch
  const selectedKey = nodeKey(node)
  cohortLoading.value = true
  error.value = ''
  try {
    const result = await outlineApi.expandCohort(novelId.value, {
      logical_node_id: requireLogicalNodeId(node),
      level,
      author_payloads: [],
    })
    if (!isCurrentNode(requestEpoch, selectedKey)) return
    const attemptId = String((result as any)?.attempt?.id || '') || null
    cohortAttemptId.value = attemptId
    const refreshedWorkingTree = await outlineApi.getWorkingTree(novelId.value)
    workingTree.value = refreshedWorkingTree
    if (!isCurrentNode(requestEpoch, selectedKey)) return
    const firstGeneratedChild = findFirstChild(refreshedWorkingTree, selectedKey, level)
    if (firstGeneratedChild) {
      await selectNode(firstGeneratedChild)
      if (attemptId && !cohortAttemptId.value) cohortAttemptId.value = attemptId
    }
    message.success('下一层 Cohort 已生成，当前仍处于 Working 草稿状态。')
  } catch (cause) {
    if (isCurrentNode(requestEpoch, selectedKey)) {
      error.value = cause instanceof Error ? cause.message : '生成下一层规划失败'
    }
  } finally {
    if (isCurrentNode(requestEpoch, selectedKey)) cohortLoading.value = false
  }
}

async function runDraftStream(retryAttemptId?: string) {
  if (!ensureWorkingTreeReadable() || !canEditSelectedNode.value) {
    if (!canEditSelectedNode.value && !workingTreeLoadFailed.value) {
      error.value = 'Active Manifest 只读，不能重生成当前层草稿。'
    }
    return
  }
  const contract = selectedContract.value
  if (!contract || streaming.value) return
  const context = captureContractContext(contract)
  if (!context) return
  streamController?.abort()
  const controller = new AbortController()
  streamController = controller
  const requestStreamEpoch = ++streamEpoch
  streaming.value = true
  streamText.value = ''
  streamAttempt.value = null
  error.value = ''
  const isCurrent = () => requestStreamEpoch === streamEpoch && isCurrentContract(context)
  try {
    await consumeOutlineDraftStream(contract.id, async event => {
      if (!isCurrent() || (event.contract_id && event.contract_id !== contract.id)) return
      if (event.type === 'started') streamAttempt.value = { id: event.attempt_id || '', contract_id: contract.id, status: 'running', retry_of_attempt_id: event.retry_of_attempt_id, accumulated_text: '', error: '', events: [] }
      if (event.type === 'delta') streamText.value += event.text || ''
      if (event.type === 'completed') {
        payloadToForm(event.payload)
        const refreshed = await outlineApi.getContract(contract.id)
        if (!isCurrent() || refreshed.id !== contract.id) return
        selectedContract.value = refreshed
        payloadToForm(refreshed.draft?.payload || refreshed.active?.payload || event.payload)
        await loadTree()
      }
      if (event.type === 'error') error.value = event.message || 'AI 大纲生成失败'
    }, controller.signal, retryAttemptId)
    if (isCurrent()) await recoverDraftAttempt(contract.id, context, requestStreamEpoch)
  } catch (cause) {
    if (isCurrent() && !(cause instanceof DOMException && cause.name === 'AbortError')) {
      error.value = cause instanceof Error ? cause.message : 'AI 大纲生成失败'
    }
  } finally {
    if (streamController === controller) streamController = null
    if (isCurrent()) streaming.value = false
  }
}

async function generateDraftStream() {
  await runDraftStream()
}

function isCurrentRecovery(context: ContractContext, expectedStream?: number) {
  return isCurrentContract(context)
    && (expectedStream === undefined || expectedStream === streamEpoch)
}

async function recoverDraftAttempt(
  contractId: string,
  context: ContractContext,
  expectedStream?: number,
) {
  const attempt = await outlineApi.getLatestGenerationAttempt(contractId)
  if (!isCurrentRecovery(context, expectedStream)) return null
  streamAttempt.value = attempt
  if (attempt) streamText.value = attempt.accumulated_text || streamText.value
  return attempt
}

async function cancelDraftStream() {
  const contract = selectedContract.value
  const attempt = streamAttempt.value
  if (!contract || !attempt || attempt.contract_id !== contract.id) return
  const context = captureContractContext(contract)
  if (!context) return
  const requestStreamEpoch = ++streamEpoch
  const controller = streamController
  controller?.abort()
  if (streamController === controller) streamController = null
  streaming.value = false
  try {
    const cancelled = await outlineApi.cancelGenerationAttempt(contract.id, attempt.id)
    if (requestStreamEpoch === streamEpoch && isCurrentContract(context)) streamAttempt.value = cancelled
  } catch (cause) {
    if (requestStreamEpoch === streamEpoch && isCurrentContract(context)) error.value = cause instanceof Error ? cause.message : '取消大纲生成失败'
  }
}

async function retryDraftStream() {
  const contract = selectedContract.value
  const previous = streamAttempt.value
  if (!contract || !previous || previous.contract_id !== contract.id || streaming.value) return
  await runDraftStream(previous.id)
}

onMounted(loadTree)
</script>

<style scoped>
.outline-studio { min-height: 100vh; padding: 28px; color: var(--app-text-primary); background: var(--app-page-bg); }
.outline-studio__header { display: flex; justify-content: space-between; gap: 24px; align-items: flex-start; max-width: 1540px; margin: 0 auto 20px; }
.outline-studio__eyebrow, .outline-panel__kicker { margin: 0 0 4px; color: var(--color-brand); font-size: 12px; font-weight: 700; letter-spacing: .06em; }
.outline-studio h1 { margin: 0; font: 700 clamp(24px, 3vw, 34px)/1.2 var(--app-font-serif, serif); }
.outline-studio__header p:not(.outline-studio__eyebrow) { margin: 8px 0 0; color: var(--app-text-secondary); }
.outline-studio__header-actions, .outline-editor__actions { display: flex; flex-wrap: wrap; gap: 8px; }
.outline-studio__alert { max-width: 1540px; margin: 0 auto 16px; }
.outline-studio__grid { display: grid; grid-template-columns: minmax(220px, .72fr) minmax(440px, 1.5fr) minmax(250px, .78fr); gap: 16px; max-width: 1540px; margin: 0 auto; align-items: start; }
.outline-panel { min-width: 0; border: 1px solid var(--app-border); border-radius: var(--app-radius-lg); background: var(--app-surface); box-shadow: var(--app-shadow-sm); }
.outline-panel__head { display: flex; align-items: start; justify-content: space-between; gap: 12px; padding: 16px; border-bottom: 1px solid var(--app-divider); }
.outline-panel__head h2 { margin: 0; font-size: 16px; }.outline-count { color: var(--app-text-muted); font-size: 12px; }
.outline-tree { padding: 8px; max-height: calc(100vh - 210px); overflow: auto; }.outline-tree-empty, .outline-editor-empty, .outline-inspector__empty { padding: 24px 18px; color: var(--app-text-secondary); line-height: 1.6; }
.outline-tree__node { width: 100%; display: grid; grid-template-columns: 36px minmax(0, 1fr) 8px; gap: 8px; align-items: center; padding: 10px 8px 10px calc(8px + var(--tree-depth) * 14px); text-align: left; color: inherit; border: 1px solid transparent; border-radius: var(--app-radius-sm); background: transparent; cursor: pointer; transition: background .18s ease, border-color .18s ease; }
.outline-tree__node:hover, .outline-tree__node.is-selected { border-color: var(--app-border); background: var(--app-surface-subtle); }.outline-tree__node.is-selected { box-shadow: inset 3px 0 var(--color-brand); }
.outline-tree__level { color: var(--color-brand); font-size: 11px; font-weight: 700; }.outline-tree__copy { min-width: 0; display: grid; gap: 2px; }.outline-tree__copy strong { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 13px; }.outline-tree__copy small { color: var(--app-text-muted); font-size: 11px; }.outline-tree__state { width: 7px; height: 7px; border-radius: 99px; background: var(--app-text-muted); }.outline-tree__state.is-synced { background: var(--color-success); }.outline-tree__state.is-draft, .outline-tree__state.is-published { background: var(--color-warning); }.outline-tree__state.is-conflict { background: var(--color-danger); }
.outline-editor { display: grid; gap: 14px; padding: 16px; }.outline-editor__summary, .outline-editor__grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }.outline-editor__grid--numbers { grid-template-columns: repeat(3, minmax(0, 1fr)); }.outline-editor__grid :deep(.n-form-item) { margin: 0; }.outline-editor__chapter-fields { display: grid; gap: 12px; padding: 14px; border: 1px solid var(--app-border); border-radius: var(--app-radius-md); background: var(--app-surface-subtle); }.outline-editor__note { margin: 0; color: var(--app-text-muted); font-size: 12px; line-height: 1.5; }.outline-editor-empty { min-height: 400px; display: grid; place-content: center; justify-items: start; gap: 10px; }.outline-editor-empty h3 { margin: 0; }.outline-editor-empty p { max-width: 46ch; margin: 0; }
.outline-inspector { position: sticky; top: 16px; }.inspector-block { padding: 15px 16px; border-bottom: 1px solid var(--app-divider); }.inspector-block:last-child { border-bottom: 0; }.inspector-block h3 { margin: 0 0 7px; font-size: 13px; }.inspector-block p { margin: 0; color: var(--app-text-secondary); font-size: 13px; line-height: 1.55; }.inspector-facts { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin: 0; }.inspector-facts div { display: grid; gap: 2px; }.inspector-facts dt { color: var(--app-text-muted); font-size: 11px; }.inspector-facts dd { margin: 0; font-size: 13px; }.inspector-block--stream pre { max-height: 260px; margin: 10px 0 0; overflow: auto; white-space: pre-wrap; color: var(--app-text-secondary); font: 12px/1.55 var(--app-font-mono, ui-monospace, monospace); }
@media (max-width: 1120px) { .outline-studio__grid { grid-template-columns: minmax(190px, .75fr) minmax(0, 1.5fr); }.outline-inspector { grid-column: 1 / -1; position: static; }.outline-inspector { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); }.outline-inspector .outline-panel__head { grid-column: 1 / -1; }.inspector-block { border-right: 1px solid var(--app-divider); border-bottom: 0; }.inspector-block--stream { grid-column: 1 / -1; border-right: 0; border-top: 1px solid var(--app-divider); } }
@media (max-width: 760px) { .outline-studio { padding: 16px; }.outline-studio__header { flex-direction: column; gap: 14px; }.outline-studio__grid { grid-template-columns: 1fr; }.outline-tree { max-height: 250px; }.outline-inspector { display: block; }.inspector-block { border-right: 0; border-bottom: 1px solid var(--app-divider); }.outline-editor__summary, .outline-editor__grid, .outline-editor__grid--numbers { grid-template-columns: 1fr; }.outline-studio__header-actions { width: 100%; }.outline-studio__header-actions :deep(.n-button) { flex: 1; } }
@media (prefers-reduced-motion: reduce) { .outline-tree__node { transition: none; } }
</style>
