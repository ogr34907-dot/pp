<template>
  <span
    class="plotpilot-mark"
    :class="`plotpilot-mark--${size}`"
    :role="label ? 'img' : undefined"
    :aria-label="label ? 'PlotPilot 墨枢' : undefined"
    :aria-hidden="label ? undefined : 'true'"
  >
    <span class="plotpilot-mark__symbol" aria-hidden="true">
      <svg viewBox="0 0 40 40" fill="none" xmlns="http://www.w3.org/2000/svg">
        <path
          d="M8.5 10.5c4.4-.9 8.2.1 11.5 3v18c-3.3-2.9-7.1-3.9-11.5-3V10.5Z"
          stroke="currentColor"
          stroke-width="2.4"
          stroke-linejoin="round"
        />
        <path
          d="M31.5 10.5c-4.4-.9-8.2.1-11.5 3v18c3.3-2.9 7.1-3.9 11.5-3V10.5Z"
          stroke="currentColor"
          stroke-width="2.4"
          stroke-linejoin="round"
        />
        <path
          d="m23.5 22.5 5-5m0 0h-3.8m3.8 0v3.8"
          stroke="currentColor"
          stroke-width="2.4"
          stroke-linecap="round"
          stroke-linejoin="round"
        />
      </svg>
    </span>
    <span v-if="label" class="plotpilot-mark__wordmark">PlotPilot</span>
  </span>
</template>

<script setup lang="ts">
withDefaults(defineProps<{
  size?: 'compact' | 'regular'
  label?: boolean
}>(), {
  size: 'regular',
  label: true,
})
</script>

<style scoped>
.plotpilot-mark {
  display: inline-flex;
  align-items: center;
  gap: var(--plotpilot-space-2, 8px);
  color: currentColor;
  line-height: 1;
}

.plotpilot-mark__symbol {
  display: inline-grid;
  flex: 0 0 auto;
  place-items: center;
}

.plotpilot-mark__symbol svg {
  display: block;
  width: 100%;
  height: 100%;
}

.plotpilot-mark--compact .plotpilot-mark__symbol {
  width: 32px;
  height: 32px;
}

.plotpilot-mark--regular .plotpilot-mark__symbol {
  width: 44px;
  height: 44px;
}

.plotpilot-mark__wordmark {
  font-family: var(--font-sans);
  font-size: 1.25rem;
  font-weight: 750;
  letter-spacing: -0.025em;
}
</style>
