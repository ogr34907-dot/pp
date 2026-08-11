<template>
  <n-form-item :label="label" class="outline-field-textarea">
    <n-input
      :value="modelValue"
      type="textarea"
      :rows="3"
      :placeholder="placeholder"
      @update:value="$emit('update:modelValue', $event)"
    />
  </n-form-item>
</template>

<script setup lang="ts">
defineProps<{
  modelValue: string
  label: string
  placeholder?: string
}>()

defineEmits<{ 'update:modelValue': [value: string] }>()
</script>
