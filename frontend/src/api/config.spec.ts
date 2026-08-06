import { afterEach, describe, expect, it, vi } from 'vitest'

type TauriWindowShape = {
  __TAURI__?: unknown
  __TAURI_INTERNALS__?: {
    invoke?: unknown
  }
}

async function loadApiConfig(windowShape: TauriWindowShape, invoke: ReturnType<typeof vi.fn>) {
  vi.stubGlobal('window', windowShape)
  vi.doMock('@tauri-apps/api/core', () => ({ invoke }))
  vi.doMock('../support/feedbackNotifier', () => ({
    emitAxiosFeedbackIncident: vi.fn(),
  }))
  return import('./config')
}

afterEach(() => {
  vi.doUnmock('@tauri-apps/api/core')
  vi.doUnmock('../support/feedbackNotifier')
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
  vi.resetModules()
})

describe('initApiClient', () => {
  it('keeps a false Tauri marker on the browser HTTP path', async () => {
    const invoke = vi.fn().mockResolvedValue(45123)
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true }))
    const { apiAxios, initApiClient } = await loadApiConfig(
      {
        __TAURI__: {},
        __TAURI_INTERNALS__: {},
      },
      invoke,
    )

    await initApiClient()

    expect(invoke).not.toHaveBeenCalled()

    const result = await apiAxios.get<string[]>('/novels', {
      adapter: async config => ({
        data: ['browser-api-fallback'],
        status: 200,
        statusText: 'OK',
        headers: {},
        config,
      }),
    })

    expect(result).toEqual(['browser-api-fallback'])
    expect(apiAxios.defaults.baseURL).not.toBe('http://127.0.0.1:45123/api/v1')
  })

  it('keeps the Tauri backend port path when IPC is callable', async () => {
    const invoke = vi.fn().mockResolvedValue(45123)
    const healthCheck = vi.fn().mockResolvedValue({ ok: true })
    vi.stubGlobal('fetch', healthCheck)
    const { apiAxios, initApiClient } = await loadApiConfig(
      {
        __TAURI_INTERNALS__: {
          invoke: () => undefined,
        },
      },
      invoke,
    )

    await initApiClient()

    expect(invoke).toHaveBeenCalledWith('get_backend_port')
    expect(healthCheck).toHaveBeenCalledWith(
      'http://127.0.0.1:45123/health',
      expect.objectContaining({ method: 'GET' }),
    )
    expect(apiAxios.defaults.baseURL).toBe('http://127.0.0.1:45123/api/v1')
  })
})
