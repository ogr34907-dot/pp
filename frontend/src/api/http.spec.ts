import { describe, expect, it, vi } from 'vitest'
import { fetchJson, HttpError } from './http'

describe('HTTP error details', () => {
  it('includes the server detail in the thrown message', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ detail: 'handoff:last_exit_state_mismatch' }), {
        status: 409,
        statusText: 'Conflict',
        headers: { 'Content-Type': 'application/json' },
      }),
    ))

    await expect(fetchJson('/api/test')).rejects.toMatchObject({
      name: 'HttpError',
      status: 409,
      detail: 'handoff:last_exit_state_mismatch',
      message: 'HTTP 409 Conflict: handoff:last_exit_state_mismatch',
    })
  })

  it('keeps the response body when the server returns a plain text error', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(
      new Response('working draft changed', { status: 409, statusText: 'Conflict' }),
    ))

    try {
      await fetchJson('/api/test')
      throw new Error('expected fetchJson to reject')
    } catch (error) {
      expect(error).toBeInstanceOf(HttpError)
      expect(error).toMatchObject({ detail: 'working draft changed' })
    }
  })
})
