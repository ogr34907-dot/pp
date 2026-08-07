import { afterEach, describe, expect, it, vi } from 'vitest'
import { autopilotApi } from './autopilot'

describe('autopilotApi.retryCanonicalAftermath', () => {
  afterEach(() => {
    vi.restoreAllMocks()
    vi.unstubAllGlobals()
  })

  it('posts to the server-side recovery route and returns its commit result', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          success: true,
          chapter_number: 8,
          remains_paused: true,
        }),
        { status: 200, headers: { 'Content-Type': 'application/json' } },
      ),
    )
    vi.stubGlobal('fetch', fetchMock)

    const result = await autopilotApi.retryCanonicalAftermath('novel-1')

    expect(result).toMatchObject({
      success: true,
      chapter_number: 8,
      remains_paused: true,
    })
    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringMatching(
        /\/api\/v1\/autopilot\/novel-1\/canonical-aftermath\/retry$/,
      ),
      expect.objectContaining({ method: 'POST' }),
    )
  })
})
