<script setup>
import { computed, onBeforeUnmount, onMounted, ref } from "vue";

const props = defineProps({
  modelValue: { type: [String, Number], required: true },
  options: { type: Array, required: true },
  disabled: { type: Boolean, default: false },
  label: { type: String, default: "Choose an option" },
});

const emit = defineEmits(["update:modelValue"]);

const open = ref(false);
const root = ref(null);
const menuStyle = ref({});

const currentLabel = computed(() => {
  const match = props.options.find((option) => option.value === props.modelValue);
  return match ? match.label : props.modelValue;
});

function place() {
  const button = root.value?.querySelector(".choice-button");
  if (!button) return;
  const box = button.getBoundingClientRect();
  const menuHeight = Math.min(240, props.options.length * 40 + 12);
  const spaceBelow = window.innerHeight - box.bottom;
  const openUp = spaceBelow < menuHeight + 12 && box.top > spaceBelow;
  menuStyle.value = {
    left: `${box.left}px`,
    width: `${box.width}px`,
    ...(openUp
      ? { bottom: `${window.innerHeight - box.top + 6}px` }
      : { top: `${box.bottom + 6}px` }),
  };
}

function toggle() {
  if (props.disabled) return;
  open.value = !open.value;
  if (open.value) place();
}

function choose(value) {
  emit("update:modelValue", value);
  open.value = false;
}

function onPointerDown(event) {
  if (!root.value?.contains(event.target)) open.value = false;
}

function onKey(event) {
  if (event.key === "Escape") open.value = false;
}

function onReflow() {
  if (open.value) place();
}

onMounted(() => {
  document.addEventListener("pointerdown", onPointerDown);
  document.addEventListener("keydown", onKey);
  window.addEventListener("resize", onReflow);
  window.addEventListener("scroll", onReflow, true);
});

onBeforeUnmount(() => {
  document.removeEventListener("pointerdown", onPointerDown);
  document.removeEventListener("keydown", onKey);
  window.removeEventListener("resize", onReflow);
  window.removeEventListener("scroll", onReflow, true);
});
</script>

<template>
  <div ref="root" class="choice" :class="{ open, disabled }">
    <button
      type="button"
      class="choice-button"
      :aria-label="label"
      :aria-expanded="open"
      :disabled="disabled"
      @click="toggle"
    >
      <span>{{ currentLabel }}</span>
      <svg viewBox="0 0 12 8" aria-hidden="true">
        <path d="M1 1.5 6 6.5 11 1.5" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" />
      </svg>
    </button>
    <ul v-if="open" class="choice-menu" role="listbox" :aria-label="label" :style="menuStyle">
      <li v-for="option in options" :key="option.value">
        <button
          type="button"
          role="option"
          :aria-selected="option.value === modelValue"
          @click="choose(option.value)"
        >
          {{ option.label }}
        </button>
      </li>
    </ul>
  </div>
</template>
