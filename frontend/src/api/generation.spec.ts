import { afterEach, describe, expect, it, vi } from 'vitest'
import { getGenerationRunOrNull } from './generation'

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
})
