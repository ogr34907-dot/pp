# 12. Frontend UI Audit

## Functional changes accepted

| UI area | User-visible behavior | Evidence |
| --- | --- | --- |
| Empty story structure | non-autopilot empty state displays 生成叙事骨架, wired to the existing planning-modal event | source review, TypeScript build and browser route inspection |
| Plot-outline stage editor | start/end inputs and validation respect actual target chapters; AI review is not labeled durable before save | focused model/service tests and build |
| Embedding settings | saving a valid current configuration displays 嵌入配置已保存 | live isolated browser interaction and DOM toast assertion |

## Browser acceptance environment

The Vite server on 127.0.0.1:3000 used the test-only
VITE_API_BASE_URL=http://127.0.0.1:8015/api/v1. The inspected page loaded
existing isolated test records, opened the AI control panel, loaded the
embedding form and completed a save using its current values. No real provider
or production service was contacted.

## Recorded limitation

The plain-browser preview logs one recoverable Tauri IPC probe warning. The
configured HTTP API fallback works and the warning is not shown as an end-user
error. This is documented as P3/no-change rather than changing desktop runtime
detection without a desktop-shell regression.

frontend/package.json contains no independent lint or test command. The shared
configuration check, vue-tsc, Vite production build and browser acceptance are
the actual frontend evidence for this batch.
