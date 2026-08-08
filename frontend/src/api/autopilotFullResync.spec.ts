import { afterEach, describe, expect, it, vi } from 'vitest'
import { autopilotApi } from './autopilot'

describe('autopilotApi.consumeCanonicalAftermathFullResync', () => {
  afterEach(() => {
    vi.restoreAllMocks()
    vi.unstubAllGlobals()
  })

  it('posts to the canonical full-resync route and parses typed SSE events', async () => {
    const encoder = new TextEncoder()
    const frames = [
      { type: 'started', run_id: 'run-1', total: 2, pending_chapters: 2 },
      { type: 'chapter', run_id: 'run-1', chapter_number: 7, action: 'synced', processed: 1, total: 2 },
      { type: 'vector', run_id: 'run-1', chapter_number: 7, status: 'failed', processed: 1, total: 2 },
      { type: 'completed', run_id: 'run-1', processed: 2, total: 2, remains_paused: true },
    ]
    const body = new ReadableStream({
      start(controller) {
        controller.enqueue(encoder.encode(frames.map(frame => `data: ${JSON.stringify(frame)}\n\n`).join('')))
        controller.close()
      },
    })
    const fetchMock = vi.fn().mockResolvedValue(new Response(body, {
      status: 200,
      headers: { 'Content-Type': 'text/event-stream' },
    }))
    vi.stubGlobal('fetch', fetchMock)
    const events: string[] = []
    await autopilotApi.consumeCanonicalAftermathFullResync('novel-1', {
      onEvent: event => events.push(event.type),
    })

    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringMatching(/\/api\/v1\/autopilot\/novel-1\/canonical-aftermath\/resync-all$/),
      expect.objectContaining({ method: 'POST', headers: expect.objectContaining({ Accept: 'text/event-stream' }) }),
    )
    expect(events).toEqual(['started', 'chapter', 'vector', 'completed'])
  })

  it('surfaces non-2xx responses as HttpError and passes abort signal', async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ detail: 'busy' }), { status: 409 }))
    vi.stubGlobal('fetch', fetchMock)
    const controller = new AbortController()
    await expect(autopilotApi.consumeCanonicalAftermathFullResync('novel-1', { signal: controller.signal }))
      .rejects.toMatchObject({ name: 'HttpError', status: 409 })
    expect(fetchMock.mock.calls[0][1]).toEqual(expect.objectContaining({ signal: controller.signal }))
  })
})
