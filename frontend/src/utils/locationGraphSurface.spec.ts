import { describe, expect, it } from 'vitest'
import { resolveLocationGraphSurface } from './locationGraphSurface'

describe('resolveLocationGraphSurface', () => {
  it('shows an empty state when global knowledge exists but no location nodes were built', () => {
    expect(resolveLocationGraphSurface({ loading: false, nodeCount: 0 })).toBe('empty')
  })

  it('shows loading only before the first location graph is available', () => {
    expect(resolveLocationGraphSurface({ loading: true, nodeCount: 0 })).toBe('loading')
    expect(resolveLocationGraphSurface({ loading: true, nodeCount: 2 })).toBe('graph')
  })

  it('mounts the graph when location nodes exist', () => {
    expect(resolveLocationGraphSurface({ loading: false, nodeCount: 2 })).toBe('graph')
  })
})
