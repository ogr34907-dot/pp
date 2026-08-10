import { defineStore } from 'pinia'
import { ref, computed, watch } from 'vue'
import { storageKeys } from '@/config/storageKeys'
import { readStorageString, writeStorageString } from '@/utils/storage'
import { normalizePlotPilotTheme, type PlotPilotTheme } from '@/utils/themeMode'

export type ThemeMode = PlotPilotTheme

function getStoredTheme(): ThemeMode {
  return normalizePlotPilotTheme(readStorageString(storageKeys.themeMode))
}

export const useThemeStore = defineStore('theme', () => {
  const mode = ref<ThemeMode>(getStoredTheme())

  const isDark = computed(() => mode.value === 'dark')

  /** 保留兼容读取面；旧 anchor 值已统一迁移为 dark。 */
  const isAnchor = computed(() => false)

  /** 实际生效的主题名，供 naive-ui / CSS 使用 */
  const effectiveTheme = computed<'light' | 'dark'>(() =>
    isDark.value ? 'dark' : 'light'
  )

  function setTheme(newMode: ThemeMode) {
    mode.value = newMode
    writeStorageString(storageKeys.themeMode, newMode)
  }

  function applyThemeToDOM() {
    const root = document.documentElement
    if (isDark.value) {
      root.classList.add('dark')
      root.setAttribute('data-theme', 'dark')
    } else {
      root.classList.remove('dark')
      root.setAttribute('data-theme', 'light')
    }
  }

  // 监听手动切换，同步 Naive UI 与 CSS 主题边界。
  watch([isDark, mode], applyThemeToDOM, { immediate: true })

  return { mode, isDark, isAnchor, effectiveTheme, setTheme }
})
