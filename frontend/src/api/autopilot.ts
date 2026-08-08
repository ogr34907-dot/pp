import { apiRoutes } from './endpoints'
import { fetchJson, fetchOk, fetchUrl, HttpError, type FetchJsonOptions } from './http'

export type AutopilotStatus = Record<string, any> & {
  active_pipeline_step?: string
  active_pipeline_run_id?: string
  last_stable_stage?: string
  autopilot_run_epoch?: number
  autopilot_pause_reason?: string
  autopilot_recovery_reason?: string
}

export interface AutopilotStartRequest {
  max_auto_chapters: number
  target_chapters: number
  target_words_per_chapter: number
}

export interface AutopilotResumeResponse {
  current_stage?: string
  message?: string
  [key: string]: unknown
}

export interface CanonicalAftermathRetryResponse {
  current_stage?: string
  chapter_number?: number
  message?: string
  status?: string
  [key: string]: unknown
}

export type CanonicalAftermathFullResyncEventType =
  | 'started' | 'chapter' | 'vector' | 'failed' | 'completed' | 'cancelled'

export interface CanonicalAftermathFullResyncEvent {
  type: CanonicalAftermathFullResyncEventType
  run_id?: string
  chapter_number?: number
  total?: number
  processed?: number
  synced?: number
  skipped?: number
  action?: string
  status?: string
  failure_reason?: string
  reason?: string
  message?: string
  remains_paused?: boolean
  vector_failed?: number[]
  [key: string]: unknown
}

export interface CanonicalAftermathFullResyncHandlers {
  onEvent?: (event: CanonicalAftermathFullResyncEvent) => void
  onStarted?: (event: CanonicalAftermathFullResyncEvent) => void
  onChapter?: (event: CanonicalAftermathFullResyncEvent) => void
  onVector?: (event: CanonicalAftermathFullResyncEvent) => void
  onFailed?: (event: CanonicalAftermathFullResyncEvent) => void
  onCompleted?: (event: CanonicalAftermathFullResyncEvent) => void
  onCancelled?: (event: CanonicalAftermathFullResyncEvent) => void
  signal?: AbortSignal
}

export interface AutopilotErrorRecord {
  message: string
  timestamp: string
  context?: string
}

export interface AutopilotCircuitBreakerData {
  status: 'closed' | 'open' | 'half_open'
  error_count: number
  max_errors: number
  last_error?: AutopilotErrorRecord
  error_history?: AutopilotErrorRecord[]
}

export const autopilotApi = {
  getStatus(novelId: string, options?: FetchJsonOptions): Promise<AutopilotStatus> {
    return fetchJson<AutopilotStatus>(apiRoutes.autopilot.status(novelId), options)
  },

  start(novelId: string, data: AutopilotStartRequest): Promise<Response> {
    return fetchOk(apiRoutes.autopilot.start(novelId), {
      method: 'POST',
      body: data,
    })
  },

  stop(novelId: string, timeoutMs?: number): Promise<Response> {
    return fetchOk(apiRoutes.autopilot.stop(novelId), {
      method: 'POST',
      timeoutMs,
    })
  },

  pause(novelId: string, timeoutMs?: number): Promise<Response> {
    return fetchOk(apiRoutes.autopilot.pause(novelId), {
      method: 'POST',
      timeoutMs,
    })
  },

  terminate(novelId: string, timeoutMs?: number): Promise<Response> {
    return fetchOk(apiRoutes.autopilot.terminate(novelId), {
      method: 'POST',
      timeoutMs,
    })
  },

  resume(novelId: string): Promise<AutopilotResumeResponse> {
    return fetchJson<AutopilotResumeResponse>(apiRoutes.autopilot.resume(novelId), {
      method: 'POST',
    })
  },

  getCircuitBreaker(novelId: string): Promise<AutopilotCircuitBreakerData> {
    return fetchJson<AutopilotCircuitBreakerData>(apiRoutes.autopilot.circuitBreaker(novelId))
  },

  resetCircuitBreaker(novelId: string): Promise<Response> {
    return fetchOk(apiRoutes.autopilot.circuitBreakerReset(novelId), {
      method: 'POST',
    })
  },

  streamUrl(novelId: string, afterSeq?: number, afterEventId?: string): string {
    const params = {
      ...(afterSeq && afterSeq > 0 ? { after_seq: afterSeq } : {}),
      ...(afterEventId ? { after_event_id: afterEventId } : {}),
    }
    return fetchUrl(apiRoutes.autopilot.stream(novelId, params))
  },

  retryCanonicalAftermath(novelId: string): Promise<CanonicalAftermathRetryResponse> {
    return fetchJson<CanonicalAftermathRetryResponse>(apiRoutes.autopilot.canonicalAftermathRetry(novelId), {
      method: 'POST',
    })
  },

  async consumeCanonicalAftermathFullResync(
    novelId: string,
    handlers: CanonicalAftermathFullResyncHandlers = {},
  ): Promise<void> {
    const response = await fetch(fetchUrl(apiRoutes.autopilot.canonicalAftermathResyncAll(novelId)), {
      method: 'POST',
      headers: { Accept: 'text/event-stream', 'Cache-Control': 'no-cache' },
      signal: handlers.signal,
    })
    if (!response.ok || !response.body) {
      const text = await response.text().catch(() => '')
      let body: unknown = text
      try { body = text ? JSON.parse(text) : undefined } catch { /* preserve text body */ }
      if (!response.ok) throw new HttpError(response, body)
      return
    }

    const reader = response.body.getReader()
    const decoder = new TextDecoder()
    let buffer = ''
    const dispatch = (raw: string) => {
      const dataLines: string[] = []
      for (const line of raw.split(/\r?\n/)) {
        if (line.startsWith('data:')) dataLines.push(line.slice(5).replace(/^\s/, ''))
      }
      if (!dataLines.length) return false
      let parsed: unknown
      try { parsed = JSON.parse(dataLines.join('\n')) } catch { return false }
      if (!parsed || typeof parsed !== 'object') return false
      const event = parsed as CanonicalAftermathFullResyncEvent
      const type = event.type
      handlers.onEvent?.(event)
      if (type === 'started') handlers.onStarted?.(event)
      else if (type === 'chapter') handlers.onChapter?.(event)
      else if (type === 'vector') handlers.onVector?.(event)
      else if (type === 'failed') handlers.onFailed?.(event)
      else if (type === 'completed') handlers.onCompleted?.(event)
      else if (type === 'cancelled') handlers.onCancelled?.(event)
      return type === 'completed' || type === 'failed' || type === 'cancelled'
    }
    try {
      let terminal = false
      while (!terminal) {
        const chunk = await reader.read()
        buffer += decoder.decode(chunk.value || new Uint8Array(), { stream: !chunk.done })
        let match: RegExpMatchArray | null
        while ((match = buffer.match(/^(.*?)(?:\r?\n){2}/s))) {
          buffer = buffer.slice(match[0].length)
          terminal = dispatch(match[1]) || terminal
          if (terminal) break
        }
        if (chunk.done) break
      }
      if (!terminal && buffer.trim()) dispatch(buffer)
    } finally {
      reader.releaseLock()
    }
  },

  logStreamUrl(novelId: string): string {
    return fetchUrl(apiRoutes.autopilot.logStream(novelId))
  },
}

export function isAutopilotNotFoundError(error: unknown): boolean {
  return error instanceof HttpError && error.status === 404
}

export function isAutopilotHttpError(error: unknown): boolean {
  return error instanceof HttpError
}

export function getAutopilotHttpStatus(error: unknown): number | null {
  return error instanceof HttpError ? error.status : null
}

export function getAutopilotErrorDetail(error: unknown): string {
  if (!(error instanceof HttpError)) return ''
  const body = error.body
  if (body && typeof body === 'object' && 'detail' in body) {
    const detail = (body as { detail?: unknown }).detail
    return typeof detail === 'string' ? detail : ''
  }
  return ''
}
