import { afterEach, describe, expect, it, vi } from 'vitest'
import { consumeOutlineDraftStream, generationApi, getGenerationRunOrNull, outlineApi } from './generation'

describe('getGenerationRunOrNull', () => {
  afterEach(() => {
    vi.restoreAllMocks()
    vi.unstubAllGlobals()
  })

  it('treats a missing generation run as an empty authoring state', async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ detail: 'generation run not found' }), {
      status: 404,
      headers: { 'Content-Type': 'application/json' },
    }))
    vi.stubGlobal('fetch', fetchMock)

    await expect(getGenerationRunOrNull('novel-1')).resolves.toBeNull()
    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringMatching(/\/api\/v1\/generation\/novels\/novel-1\/state$/),
      expect.any(Object),
    )
  })

  it('keeps other generation-state failures visible to the caller', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({ detail: 'database unavailable' }), {
      status: 503,
      headers: { 'Content-Type': 'application/json' },
    })))

    await expect(getGenerationRunOrNull('novel-1')).rejects.toMatchObject({
      name: 'HttpError',
      status: 503,
    })
  })

  it('reads a candidate DAG trace with its durable cursor', async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({
      success: true,
      data: { id: 'dag-1', candidate_id: 'candidate-1', content_revision: 1, status: 'running', current_node_id: 'exec_writer', failure_reason: '', node_attempts: [], events: [] },
    }), { headers: { 'Content-Type': 'application/json' } }))
    vi.stubGlobal('fetch', fetchMock)

    await expect(generationApi.getLatestDagRun('candidate-1', 7)).resolves.toMatchObject({ id: 'dag-1' })
    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringMatching(/candidates\/candidate-1\/dag-runs\/latest\?after_sequence=7$/),
      expect.any(Object),
    )
  })

  it('starts a generation run without sending a client-side target chapter count', async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({
      success: true,
      data: { novel_id: 'novel-1', run_mode: 'chapter_review', state: 'running' },
    }), { headers: { 'Content-Type': 'application/json' } }))
    vi.stubGlobal('fetch', fetchMock)

    await generationApi.start('novel-1', 'chapter_review')

    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringMatching(/\/api\/v1\/generation\/novels\/novel-1\/start$/),
      expect.objectContaining({ body: JSON.stringify({ run_mode: 'chapter_review' }) }),
    )
  })

  it('recovers and cancels an outline stream attempt through its durable endpoints', async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({
        success: true,
        data: { id: 'attempt-1', contract_id: 'outline-1', status: 'running', events: [] },
      }), { headers: { 'Content-Type': 'application/json' } }))
      .mockResolvedValueOnce(new Response(JSON.stringify({
        success: true,
        data: { id: 'attempt-1', contract_id: 'outline-1', status: 'cancelled', events: [] },
      }), { headers: { 'Content-Type': 'application/json' } }))
    vi.stubGlobal('fetch', fetchMock)

    await expect(outlineApi.getLatestGenerationAttempt('outline-1', 3)).resolves.toMatchObject({ id: 'attempt-1' })
    await expect(outlineApi.cancelGenerationAttempt('outline-1', 'attempt-1')).resolves.toMatchObject({ status: 'cancelled' })
    expect(fetchMock.mock.calls[0][0]).toMatch(/outline\/contracts\/outline-1\/generation-attempts\/latest\?after_sequence=3$/)
    expect(fetchMock.mock.calls[1][0]).toMatch(/outline\/contracts\/outline-1\/generation-attempts\/attempt-1\/cancel$/)
  })

  it('waits for an asynchronous outline stream callback before completing', async () => {
    const encoder = new TextEncoder()
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(new ReadableStream({
      start(controller) {
        controller.enqueue(encoder.encode('data: {"type":"completed","contract_id":"outline-1"}\n\n'))
        controller.close()
      },
    }), { status: 200 })))

    let releaseCallback!: () => void
    const callbackGate = new Promise<void>((resolve) => { releaseCallback = resolve })
    let callbackStarted = false
    let callbackFinished = false
    let consumeFinished = false

    const consuming = consumeOutlineDraftStream('outline-1', async () => {
      callbackStarted = true
      await callbackGate
      callbackFinished = true
    }).then(() => { consumeFinished = true })

    for (let attempt = 0; attempt < 10 && !callbackStarted; attempt++) await Promise.resolve()
    expect(callbackStarted).toBe(true)
    await new Promise(resolve => setTimeout(resolve, 0))
    expect(consumeFinished).toBe(false)

    releaseCallback()
    await consuming
    expect(callbackFinished).toBe(true)
  })
})
