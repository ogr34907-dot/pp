import { apiRootPath, apiRoutes } from './endpoints'
import { fetchJson, fetchUrl, HttpError } from './http'

export type RunMode = 'continuous' | 'chapter_review'
export type GenerationRunState =
  | 'idle' | 'running' | 'waiting_review' | 'paused' | 'stopped' | 'completed' | 'error'
export type CandidateStatus =
  | 'streaming' | 'auditing' | 'awaiting_review' | 'committing' | 'syncing' | 'committed'
  | 'stale' | 'regenerating' | 'rejected' | 'failed' | 'cancelled'

export interface OutlineRevision {
  revision: number
  status: string
  source: string
  digest: string
  parent_revision_digest?: string
  payload: OutlinePayload
}

export interface OutlinePayload {
  title?: string
  narrative_text?: string
  creative_goal?: string
  entry_state?: string
  exit_state?: string
  required_events?: string[]
  forbidden_events?: string[]
  state_changes?: Record<string, Array<Record<string, unknown>>>
  foreshadowing?: Record<string, string[]>
  chapter_start?: number | null
  chapter_end?: number | null
  word_budget?: number | null
  handoff_conditions?: string[]
  pov?: string
  scenes?: string[]
  beats?: string[]
  conflicts?: string[]
  ending_hook?: string
  extra?: Record<string, unknown>
}

export interface OutlineContract {
  id: string
  novel_id: string
  level: 'outline' | 'part' | 'volume' | 'act' | 'chapter'
  parent_contract_id?: string | null
  story_node_id?: string | null
  author_locked: boolean
  has_author_edits: boolean
  active?: OutlineRevision | null
  draft?: OutlineRevision | null
}

export interface OutlineTreeNode {
  id: string
  novel_id: string
  node_type: 'outline' | 'part' | 'volume' | 'act' | 'chapter' | string
  number?: number
  title?: string
  description?: string
  outline_contract?: {
    contract_id?: string | null
    level?: string | null
    status?: string
    active_revision?: number | null
    draft_revision?: number | null
    author_locked?: boolean
    has_author_edits?: boolean
  }
  children?: OutlineTreeNode[]
}

export interface ChapterCandidate {
  id: string
  novel_id: string
  chapter_number: number
  title: string
  generation_epoch: number
  status: CandidateStatus
  outline_chain: Record<string, { digest?: string; payload?: OutlinePayload; [key: string]: unknown }>
  outline_chain_digest: string
  llm_content: string
  author_content?: string | null
  final_content: string
  content_revision: number
  audit_revision: number
  audit_is_current: boolean
  commit_plan_revision: number
  commit_plan_is_current: boolean
  audit: Record<string, unknown>
  commit_plan: Record<string, unknown>
  feedback: string
  failure_reason: string
  continue_after_commit: boolean
  formal_chapter_id?: string | null
}

export interface CandidateVersion {
  id: string
  content_revision: number
  content: string
  source: 'llm' | 'author' | 'regenerated'
  feedback: string
  created_at: string
}

export interface GenerationRun {
  novel_id: string
  run_mode: RunMode
  state: GenerationRunState
  generation_epoch: number
  target_chapters: number
  current_formal_chapter: number
  current_candidate_id?: string | null
  current_candidate_chapter?: number | null
  canonical_sync_status: string
  next_action: string
  last_error: string
  max_pending_candidates: number
  prefetch: number
  candidate?: ChapterCandidate | null
}

export interface WorldlinePreview {
  token: string
  novel_id: string
  operation: 'continue' | 'regenerate'
  start_chapter: number
  target_chapters: number
  current_generated_chapters: number
  retained_through: number
  archive_from?: number | null
  archive_to?: number | null
  generation_epoch: number
  prefix_digest: string
  counts: Record<string, number>
}

export interface WorldlineArchive {
  id: string
  novel_id: string
  start_chapter: number
  end_chapter: number
  retained_through: number
  status: string
  target_chapters: number
  created_at: string
  restored_at?: string | null
  metadata?: Record<string, unknown>
}

export interface WorldlineResult {
  operation: 'continue' | 'regenerate' | 'restore'
  novel_id: string
  archive_id?: string | null
  generation_epoch: number
  retained_through: number
  next_action: string
}

interface Wrapped<T> { success: boolean; data: T }

async function unwrap<T>(path: string, options: Parameters<typeof fetchJson>[1] = {}): Promise<T> {
  const body = await fetchJson<Wrapped<T>>(path, options)
  return body.data
}

export const generationApi = {
  getState: (novelId: string) => unwrap<GenerationRun>(apiRoutes.generation.state(novelId)),
  start: (novelId: string, runMode: RunMode, targetChapters: number) =>
    unwrap<GenerationRun>(apiRoutes.generation.start(novelId), {
      method: 'POST', body: { run_mode: runMode, target_chapters: targetChapters },
    }),
  generateNext: (novelId: string) =>
    unwrap<ChapterCandidate | null>(apiRoutes.generation.generateNext(novelId), { method: 'POST' }),
  runContinuous: (novelId: string) =>
    unwrap<{ candidates: ChapterCandidate[]; state: GenerationRun }>(
      apiRoutes.generation.runContinuous(novelId), { method: 'POST' },
    ),
  stop: (novelId: string) => unwrap<GenerationRun>(apiRoutes.generation.stop(novelId), { method: 'POST' }),
  getCandidate: (candidateId: string) => unwrap<ChapterCandidate>(apiRoutes.generation.candidate(candidateId)),
  listCandidateVersions: (candidateId: string) =>
    unwrap<CandidateVersion[]>(apiRoutes.generation.candidateVersions(candidateId)),
  saveCandidateContent: (candidateId: string, content: string, feedback = '') =>
    unwrap<ChapterCandidate>(apiRoutes.generation.candidateContent(candidateId), {
      method: 'PATCH', body: { content, feedback },
    }),
  saveCommitPlan: (candidateId: string, commitPlan: Record<string, unknown>) =>
    unwrap<ChapterCandidate>(apiRoutes.generation.candidateCommitPlan(candidateId), {
      method: 'PATCH', body: { commit_plan: commitPlan },
    }),
  reaudit: (candidateId: string) =>
    unwrap<ChapterCandidate>(apiRoutes.generation.candidateReaudit(candidateId), { method: 'POST' }),
  regenerate: (candidateId: string, feedback = '') =>
    unwrap<ChapterCandidate>(apiRoutes.generation.candidateRegenerate(candidateId), {
      method: 'POST', body: { feedback },
    }),
  approveAndCommit: (candidateId: string, continueAfterCommit: boolean) =>
    unwrap<ChapterCandidate>(apiRoutes.generation.candidateApprove(candidateId), {
      method: 'POST', body: { continue_after_commit: continueAfterCommit },
    }),
  retrySync: (candidateId: string) =>
    unwrap<ChapterCandidate>(apiRoutes.generation.candidateRetrySync(candidateId), { method: 'POST' }),
  reject: (candidateId: string) =>
    unwrap<ChapterCandidate>(apiRoutes.generation.candidateReject(candidateId), { method: 'POST' }),
}

/** A novel has no generation run until the author explicitly starts one. */
export async function getGenerationRunOrNull(novelId: string): Promise<GenerationRun | null> {
  try {
    return await generationApi.getState(novelId)
  } catch (error) {
    if (error instanceof HttpError && error.status === 404) return null
    throw error
  }
}

export const outlineApi = {
  getTree: (novelId: string) => unwrap<OutlineTreeNode>(apiRoutes.outline.tree(novelId)),
  getContract: (contractId: string) => unwrap<OutlineContract>(apiRoutes.outline.contract(contractId)),
  saveDraft: (contractId: string, payload: OutlinePayload, source: 'author' | 'ai' | 'imported' = 'author') =>
    unwrap<OutlineContract>(apiRoutes.outline.draft(contractId), { method: 'POST', body: { payload, source } }),
  publish: (contractId: string, expectedRevision: number, idempotencyKey: string, authorLocked?: boolean) =>
    unwrap<OutlineContract & { stale_candidates: number }>(apiRoutes.outline.publish(contractId), {
      method: 'POST', body: {
        expected_revision: expectedRevision,
        idempotency_key: idempotencyKey,
        ...(authorLocked === undefined ? {} : { author_locked: authorLocked }),
      },
    }),
  generateDraft: (contractId: string) =>
    unwrap<OutlineContract>(apiRoutes.outline.generateDraft(contractId), { method: 'POST' }),
  bindNode: (novelId: string, nodeId: string) =>
    unwrap<OutlineContract>(apiRootPath('outline', 'novels', novelId, 'story-nodes', nodeId, 'contract'), { method: 'POST' }),
  draftStreamUrl: (contractId: string) => fetchUrl(apiRoutes.outline.generateDraftStream(contractId)),
}

export interface OutlineDraftStreamEvent {
  type: 'phase' | 'chunk' | 'done' | 'error'
  level?: string
  phase?: string
  text?: string
  contract_id?: string
  revision?: number
  payload?: OutlinePayload
  message?: string
}

/** Consume the existing outline SSE endpoint without introducing a second API. */
export async function consumeOutlineDraftStream(
  contractId: string,
  onEvent: (event: OutlineDraftStreamEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  const response = await fetch(outlineApi.draftStreamUrl(contractId), {
    headers: { Accept: 'text/event-stream', 'Cache-Control': 'no-cache' }, signal,
  })
  if (!response.ok || !response.body) {
    throw new Error(`大纲流式生成请求失败（${response.status}）`)
  }
  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  const dispatch = (frame: string) => {
    const raw = frame.split(/\r?\n/)
      .filter(line => line.startsWith('data:'))
      .map(line => line.slice(5).trimStart())
      .join('\n')
    if (!raw) return
    try { onEvent(JSON.parse(raw) as OutlineDraftStreamEvent) } catch { /* ignore malformed keepalive */ }
  }
  try {
    while (true) {
      const { done, value } = await reader.read()
      buffer += decoder.decode(value || new Uint8Array(), { stream: !done })
      let boundary = buffer.search(/\r?\n\r?\n/)
      while (boundary >= 0) {
        dispatch(buffer.slice(0, boundary))
        buffer = buffer.slice(boundary).replace(/^\r?\n\r?\n/, '')
        boundary = buffer.search(/\r?\n\r?\n/)
      }
      if (done) break
    }
    if (buffer.trim()) dispatch(buffer)
  } finally {
    reader.releaseLock()
  }
}

export const worldlineRegenerationApi = {
  preview: (novelId: string, startChapter: number, targetChapters: number) =>
    unwrap<WorldlinePreview>(apiRoutes.worldlineRegeneration.preview(novelId), {
      method: 'POST', body: { start_chapter: startChapter, target_chapters: targetChapters },
    }),
  execute: (novelId: string, previewToken: string, runMode: RunMode, idempotencyKey: string) =>
    unwrap<WorldlineResult>(apiRoutes.worldlineRegeneration.execute(novelId), {
      method: 'POST', body: {
        preview_token: previewToken, run_mode: runMode, idempotency_key: idempotencyKey,
      },
    }),
  listArchives: (novelId: string) =>
    unwrap<WorldlineArchive[]>(apiRoutes.worldlineRegeneration.archives(novelId)),
  restore: (novelId: string, archiveId: string, runMode: RunMode, idempotencyKey: string) =>
    unwrap<WorldlineResult>(apiRoutes.worldlineRegeneration.restore(novelId, archiveId), {
      method: 'POST', body: { run_mode: runMode, idempotency_key: idempotencyKey },
    }),
  rebuildStatus: (novelId: string) => unwrap<Record<string, unknown>>(apiRoutes.worldlineRegeneration.rebuildStatus(novelId)),
  rebuild: (novelId: string) => unwrap<Record<string, unknown>>(apiRoutes.worldlineRegeneration.rebuild(novelId), { method: 'POST' }),
  cancelRebuild: (novelId: string) =>
    unwrap<Record<string, unknown>>(apiRoutes.worldlineRegeneration.cancelRebuild(novelId), { method: 'POST' }),
}
