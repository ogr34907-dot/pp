export type GenerationPresentationTone = 'brand' | 'success' | 'warning' | 'error' | 'neutral'

export interface GenerationPresentationInput {
  state?: string | null
  run_mode?: string | null
  current_candidate_chapter?: number | null
  current_formal_chapter?: number | null
  canonical_sync_status?: string | null
  next_action?: string | null
  last_error?: string | null
  candidate?: { status?: string | null; chapter_number?: number | null } | null
}

export interface GenerationPresentation {
  key: string
  label: string
  detail: string
  tone: GenerationPresentationTone
  isActive: boolean
}

function clean(value?: string | null): string {
  return String(value || '').trim().toLowerCase()
}

function candidateChapter(input: GenerationPresentationInput): number | null {
  const value = input.current_candidate_chapter ?? input.candidate?.chapter_number
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

/**
 * One presentation boundary for Home, the workbench and the review desk.
 * It deliberately trusts only the candidate-generation authority supplied by
 * the server; callers must not infer “writing” from a stale lifecycle stage.
 */
export function getGenerationPresentation(input: GenerationPresentationInput | null | undefined): GenerationPresentation | null {
  if (!input) return null
  const state = clean(input.state)
  const sync = clean(input.canonical_sync_status)
  const action = clean(input.next_action)
  const error = String(input.last_error || '').trim()
  const candidate = candidateChapter(input)
  const chapter = candidate ? `第 ${candidate} 章` : ''

  if (sync === 'failed') {
    return {
      key: 'sync_failed',
      label: `${chapter || '当前章节'}已入库、同步失败`,
      detail: error || '规范事实与长期记忆尚未就绪；不能开始下一章。',
      tone: 'error',
      isActive: false,
    }
  }
  if (sync === 'syncing' || sync === 'rebuilding') {
    return {
      key: sync,
      label: sync === 'rebuilding' ? '世界线正在重建事实记忆' : `${chapter || '当前章节'}正在正式同步`,
      detail: '正式进度会在规范事实、向量和长期记忆就绪后推进。',
      tone: 'brand',
      isActive: true,
    }
  }
  if (state === 'waiting_review') {
    return {
      key: 'waiting_review',
      label: `${chapter || '候选章节'}待审核`,
      detail: '未通过前不会生成下一章，也不会消耗下一章 Token。',
      tone: 'warning',
      isActive: false,
    }
  }
  if (state === 'running') {
    const candidateState = clean(input.candidate?.status)
    const phase = candidateState === 'auditing' ? '机器审校中' : candidateState === 'streaming' || candidateState === 'regenerating'
      ? '候选生成中' : action === 'commit_candidate' ? '正式写入中' : '自动驾驶运行中'
    return {
      key: candidateState || 'running',
      label: `${chapter ? `${chapter} ` : ''}${phase}`,
      detail: input.run_mode === 'chapter_review'
        ? '当前章完成后会停在审核台，不会预取后续章节。'
        : '软警告会自动采纳；硬性冲突或同步错误会立即暂停。',
      tone: 'brand',
      isActive: true,
    }
  }
  if (state === 'paused') {
    return {
      key: 'paused',
      label: action === 'select_run_mode' ? '世界线已重建，等待选择模式' : '已暂停',
      detail: error || '当前进度已持久化；恢复时会从最后稳定阶段继续。',
      tone: 'warning',
      isActive: false,
    }
  }
  if (state === 'error') {
    return {
      key: 'error', label: '生成异常暂停', detail: error || '修复当前错误后再继续。', tone: 'error', isActive: false,
    }
  }
  if (state === 'completed') {
    return {
      key: 'completed', label: '已完成目标章节', detail: '所有正式章节与规范记忆均已完成。', tone: 'success', isActive: false,
    }
  }
  if (state === 'stopped') {
    return {
      key: 'stopped', label: '已终止', detail: '运行已停止；不会继续写入或预取章节。', tone: 'neutral', isActive: false,
    }
  }
  return {
    key: state || 'idle', label: '等待开始', detail: '先确认五级大纲链，再选择自动驾驶模式。', tone: 'neutral', isActive: false,
  }
}
