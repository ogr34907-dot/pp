import { afterEach, describe, expect, it, vi } from 'vitest'

async function loadViteConfig(target?: string) {
  const env = (globalThis as unknown as {
    process: { env: Record<string, string | undefined> }
  }).process.env
  if (target === undefined) {
    delete env.PLOTPILOT_API_TARGET
  } else {
    env.PLOTPILOT_API_TARGET = target
  }
  vi.resetModules()
  return (await import('../vite.config')).default
}

afterEach(() => {
  const env = (globalThis as unknown as {
    process: { env: Record<string, string | undefined> }
  }).process.env
  delete env.PLOTPILOT_API_TARGET
})

describe('frontend dev proxy target', () => {
  it('allows the test frontend to target its isolated backend', async () => {
    const config = await loadViteConfig('http://127.0.0.1:8006')
    const proxy = config.server?.proxy?.['/api']

    expect(proxy).toMatchObject({ target: 'http://127.0.0.1:8006' })
  })

  it('keeps the formal backend as the default when no override is set', async () => {
    const config = await loadViteConfig()
    const proxy = config.server?.proxy?.['/api']

    expect(proxy).toMatchObject({ target: 'http://127.0.0.1:8005' })
  })
})
