<template>
  <n-form-item :label="label" class="outline-field-textarea">
    <n-input
      :value="modelValue"
      type="textarea"
      :rows="3"
      :placeholder="placeholder"
      :disabled="disabled"
      @update:value="$emit('update:modelValue', $event)"
    />
  </n-form-item>
</template>

<script setup lang="ts">
defineProps<{
  modelValue: string
  label: string
  placeholder?: string
  disabled?: boolean
}>()

defineEmits<{ 'update:modelValue': [value: string] }>()
</script>
