const API_V1_ROOT = '/api/v1'

type QueryValue = string | number | boolean | null | undefined
type QueryParams = Record<string, QueryValue | QueryValue[]>

function encodeSegment(value: string | number | boolean): string {
  return encodeURIComponent(String(value))
}

function joinSegments(segments: Array<string | number | boolean>): string {
  return segments
    .filter(segment => String(segment).length > 0)
    .map(encodeSegment)
    .join('/')
}

function normalizeClientPath(path: string): string {
  return path.startsWith('/') ? path : `/${path}`
}

export function apiClientPath(...segments: Array<string | number | boolean>): string {
  return normalizeClientPath(joinSegments(segments))
}

export function apiRootPath(...segments: Array<string | number | boolean>): string {
  return `${API_V1_ROOT}${apiClientPath(...segments)}`
}

export function withQuery(path: string, params: QueryParams = {}): string {
  const query = new URLSearchParams()
  for (const [key, raw] of Object.entries(params)) {
    const values = Array.isArray(raw) ? raw : [raw]
    for (const value of values) {
      if (value === null || value === undefined || value === '') continue
      query.append(key, String(value))
    }
  }
  const qs = query.toString()
  return qs ? `${path}?${qs}` : path
}

export const apiRoutes = {
  novels: {
    root: () => apiClientPath('novels'),
    detail: (novelId: string) => apiClientPath('novels', novelId),
    stage: (novelId: string) => apiClientPath('novels', novelId, 'stage'),
    statistics: (novelId: string) => apiClientPath('novels', novelId, 'statistics'),
    autoApproveModeClient: (novelId: string) => apiClientPath('novels', novelId, 'auto-approve-mode'),
    autoApproveMode: (novelId: string) => apiRootPath('novels', novelId, 'auto-approve-mode'),
    chapters: (novelId: string, params?: QueryParams) =>
      withQuery(apiRootPath('novels', novelId, 'chapters'), params),
    chaptersClient: (novelId: string) => apiClientPath('novels', novelId, 'chapters'),
    chapterStream: (novelId: string) => apiRootPath('autopilot', novelId, 'chapter-stream'),
    exportNovel: (novelId: string) => apiClientPath('export', 'novel', novelId),
    exportChapter: (chapterId: string) => apiClientPath('export', 'chapter', chapterId),
  },
  autopilot: {
    root: (novelId: string) => apiRootPath('autopilot', novelId),
    status: (novelId: string) => apiRootPath('autopilot', novelId, 'status'),
    start: (novelId: string) => apiRootPath('autopilot', novelId, 'start'),
    stop: (novelId: string) => apiRootPath('autopilot', novelId, 'stop'),
    pause: (novelId: string) => apiRootPath('autopilot', novelId, 'pause'),
    terminate: (novelId: string) => apiRootPath('autopilot', novelId, 'terminate'),
    resume: (novelId: string) => apiRootPath('autopilot', novelId, 'resume'),
    canonicalAftermathRetry: (novelId: string) =>
      apiRootPath('autopilot', novelId, 'canonical-aftermath', 'retry'),
    canonicalAftermathResyncAll: (novelId: string) =>
      apiRootPath('autopilot', novelId, 'canonical-aftermath', 'resync-all'),
    stream: (novelId: string, params?: QueryParams) =>
      withQuery(apiRootPath('autopilot', novelId, 'stream'), params),
    logStream: (novelId: string) => apiRootPath('autopilot', novelId, 'log-stream'),
    circuitBreaker: (novelId: string) => apiRootPath('autopilot', novelId, 'circuit-breaker'),
    circuitBreakerReset: (novelId: string) => apiRootPath('autopilot', novelId, 'circuit-breaker', 'reset'),
  },
  generation: {
    run: (novelId: string) => apiRootPath('generation', 'novels', novelId),
    state: (novelId: string) => apiRootPath('generation', 'novels', novelId, 'state'),
    start: (novelId: string) => apiRootPath('generation', 'novels', novelId, 'start'),
    generateNext: (novelId: string) => apiRootPath('generation', 'novels', novelId, 'generate-next'),
    runContinuous: (novelId: string) => apiRootPath('generation', 'novels', novelId, 'run-continuous'),
    stop: (novelId: string) => apiRootPath('generation', 'novels', novelId, 'stop'),
    candidate: (candidateId: string) => apiRootPath('generation', 'candidates', candidateId),
    candidateVersions: (candidateId: string) => apiRootPath('generation', 'candidates', candidateId, 'versions'),
    candidateContent: (candidateId: string) => apiRootPath('generation', 'candidates', candidateId, 'content'),
    candidateCommitPlan: (candidateId: string) => apiRootPath('generation', 'candidates', candidateId, 'commit-plan'),
    candidateReaudit: (candidateId: string) => apiRootPath('generation', 'candidates', candidateId, 're-audit'),
    candidateRegenerate: (candidateId: string) => apiRootPath('generation', 'candidates', candidateId, 'regenerate'),
    candidateApprove: (candidateId: string) => apiRootPath('generation', 'candidates', candidateId, 'approve-and-commit'),
    candidateRetrySync: (candidateId: string) => apiRootPath('generation', 'candidates', candidateId, 'retry-sync'),
    candidateReject: (candidateId: string) => apiRootPath('generation', 'candidates', candidateId, 'reject'),
    candidateDagLatest: (candidateId: string, params?: QueryParams) =>
      withQuery(apiRootPath('generation', 'candidates', candidateId, 'dag-runs', 'latest'), params),
    candidateDagResume: (candidateId: string) =>
      apiRootPath('generation', 'candidates', candidateId, 'dag-runs', 'latest', 'resume'),
  },
  outline: {
    tree: (novelId: string) => apiRootPath('outline', 'novels', novelId, 'tree'),
    workingTree: (novelId: string) => apiRootPath('outline', 'novels', novelId, 'working-tree'),
    workingItem: (planRevisionId: string, logicalNodeId: string) =>
      apiRootPath('outline', 'plan-revisions', planRevisionId, 'items', logicalNodeId),
    cohortExpand: (novelId: string) => apiRootPath('outline', 'novels', novelId, 'cohorts', 'expand'),
    authorPublishCohort: (attemptId: string) =>
      apiRootPath('outline', 'cohort-attempts', attemptId, 'author-publish'),
    contract: (contractId: string) => apiRootPath('outline', 'contracts', contractId),
    draft: (contractId: string) => apiRootPath('outline', 'contracts', contractId, 'draft'),
    publish: (contractId: string) => apiRootPath('outline', 'contracts', contractId, 'publish'),
    generateDraft: (contractId: string) => apiRootPath('outline', 'contracts', contractId, 'generate-draft'),
    generateDraftStream: (contractId: string) => apiRootPath('outline', 'contracts', contractId, 'generate-draft-stream'),
    generationAttemptLatest: (contractId: string, params?: QueryParams) =>
      withQuery(apiRootPath('outline', 'contracts', contractId, 'generation-attempts', 'latest'), params),
    generationAttemptCancel: (contractId: string, attemptId: string) =>
      apiRootPath('outline', 'contracts', contractId, 'generation-attempts', attemptId, 'cancel'),
    chapterContext: (novelId: string, chapterNodeId: string) =>
      apiRootPath('outline', 'novels', novelId, 'chapters', chapterNodeId, 'published-context'),
    continuityReview: (planRevisionId: string, parentLogicalNodeId?: string) =>
      withQuery(apiRootPath('outline', 'plan-revisions', planRevisionId, 'continuity-review'), {
        parent_logical_node_id: parentLogicalNodeId,
      }),
    continuityReviewApply: (planRevisionId: string) =>
      apiRootPath('outline', 'plan-revisions', planRevisionId, 'continuity-review', 'acknowledge'),
    continuitySuggestionApply: (reviewId: string, suggestionId: string) =>
      apiRootPath('outline', 'continuity-reviews', reviewId, 'suggestions', suggestionId, 'apply'),
  },
  worldlineRegeneration: {
    preview: (novelId: string) => apiRootPath('worldline-regeneration', 'novels', novelId, 'preview'),
    execute: (novelId: string) => apiRootPath('worldline-regeneration', 'novels', novelId, 'execute'),
    archives: (novelId: string) => apiRootPath('worldline-regeneration', 'novels', novelId, 'archives'),
    restore: (novelId: string, archiveId: string) =>
      apiRootPath('worldline-regeneration', 'novels', novelId, 'archives', archiveId, 'restore'),
    rebuildStatus: (novelId: string) => apiRootPath('worldline-regeneration', 'novels', novelId, 'rebuild-status'),
    rebuild: (novelId: string) => apiRootPath('worldline-regeneration', 'novels', novelId, 'rebuild'),
    cancelRebuild: (novelId: string) => apiRootPath('worldline-regeneration', 'novels', novelId, 'rebuild', 'cancel'),
  },
  dag: {
    events: (novelId: string, afterEventId?: string) =>
      withQuery(apiRootPath('dag', 'events'), {
        novel_id: novelId,
        after_event_id: afterEventId,
      }),
  },
  monitor: {
    voiceDrift: (novelId: string) => apiRootPath('novels', novelId, 'monitor', 'voice-drift'),
    tensionCurve: (novelId: string) => apiClientPath('novels', novelId, 'monitor', 'tension-curve'),
  },
}
