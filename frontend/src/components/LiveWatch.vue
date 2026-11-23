<script setup>
import { computed, onBeforeUnmount, onMounted, ref } from "vue";
import { fetchCurrentLive, fetchLive, startLive, stopLive } from "../api";
import ChoiceMenu from "./ChoiceMenu.vue";

const watchOptions = [
  { value: "both", label: "Faces and plates" },
  { value: "faces", label: "Faces only" },
  { value: "plates", label: "Plates only" },
];

const sampleOptions = [
  { value: 0.5, label: "1 every 2 seconds" },
  { value: 1, label: "1 per second" },
  { value: 2, label: "2 per second" },
  { value: 3, label: "3 per second" },
  { value: 4, label: "4 per second" },
  { value: 5, label: "5 per second" },
];

const source = ref("");
const watch = ref("both");
const faceFile = ref(null);
const faceUrl = ref("");
const faceDragging = ref(false);
const plate = ref("");
const sampleFps = ref(2);
const minPercent = ref(35);
const starting = ref(false);
const error = ref("");
const session = ref(null);
const previewSrc = ref("");
const faceInput = ref(null);

let shownRev = 0;
let generation = 0;
let pollAbort = null;
let leaving = false;

const wantsFace = computed(() => watch.value !== "plates");
const running = computed(() => {
  const status = session.value?.status;
  return starting.value || status === "starting" || status === "live";
});
const canStart = computed(() => {
  if (running.value || !source.value.trim()) return false;
  if (wantsFace.value && !faceFile.value) return false;
  return true;
});

function pickedFile(event) {
  return event.target?.files?.[0] || event.dataTransfer?.files?.[0] || null;
}

function isImage(file) {
  if (file.type) return file.type.startsWith("image/");
  return /\.(jpe?g|png|webp|gif|bmp|heic)$/i.test(file.name);
}

function setFace(event) {
  faceDragging.value = false;
  const file = pickedFile(event);
  if (!file || !isImage(file)) return;
  faceFile.value = file;
  if (faceUrl.value) URL.revokeObjectURL(faceUrl.value);
  faceUrl.value = URL.createObjectURL(file);
}

function syncPreview() {
  const rev = session.value?.preview_rev || 0;
  if (!rev || !session.value || rev === shownRev) return;
  shownRev = rev;
  previewSrc.value = `/api/live/${session.value.session_id}/preview?v=${rev}`;
}

async function wait(signal) {
  await new Promise((resolve, reject) => {
    const timer = setTimeout(resolve, 700);
    signal.addEventListener("abort", () => {
      clearTimeout(timer);
      reject(new DOMException("Aborted", "AbortError"));
    }, { once: true });
  });
}

async function follow(id) {
  const token = ++generation;
  pollAbort?.abort();
  const controller = new AbortController();
  pollAbort = controller;
  try {
    while (!controller.signal.aborted && token === generation) {
      const next = await fetchLive(id, controller.signal);
      if (token !== generation) return;
      session.value = next;
      if (next.error) error.value = next.error;
      syncPreview();
      if (next.status !== "starting" && next.status !== "live") return;
      await wait(controller.signal);
    }
  } catch (err) {
    if (controller.signal.aborted || err.name === "AbortError") return;
    if (token === generation) error.value = err.message || "The camera watch failed.";
  }
}

async function start() {
  if (!canStart.value) return;
  error.value = "";
  previewSrc.value = "";
  shownRev = 0;
  starting.value = true;
  try {
    const started = await startLive({
      source: source.value.trim(),
      watch: watch.value,
      plate: plate.value,
      file: wantsFace.value ? faceFile.value : null,
      minSimilarity: minPercent.value / 100,
      sampleFps: sampleFps.value,
    });
    if (leaving) {
      await stopLive(started.session_id);
      return;
    }
    session.value = started;
    starting.value = false;
    await follow(started.session_id);
  } catch (err) {
    if (leaving) return;
    error.value = err.message || "The camera watch failed.";
  } finally {
    starting.value = false;
  }
}

async function stop() {
  const id = session.value?.session_id;
  generation += 1;
  pollAbort?.abort();
  if (!id) return;
  try {
    session.value = await stopLive(id);
    if (session.value.error) error.value = session.value.error;
    syncPreview();
  } catch (err) {
    error.value = err.message || "Could not stop the camera.";
  }
}

function rangeLabel(hit) {
  if (hit.start_label === hit.end_label) return hit.start_label;
  return `${hit.start_label} – ${hit.end_label}`;
}

function band(value) {
  if (value >= 70) return "high";
  if (value >= 45) return "mid";
  return "low";
}

function stopQuiet() {
  const id = session.value?.session_id;
  const status = session.value?.status;
  if (!id || (status !== "live" && status !== "starting")) return;
  fetch(`/api/live/${id}/stop`, { method: "POST", keepalive: true }).catch(() => {});
}

onMounted(async () => {
  window.addEventListener("pagehide", stopQuiet);
  try {
    const current = await fetchCurrentLive();
    if (leaving || !current) return;
    session.value = current;
    if (current.error) error.value = current.error;
    syncPreview();
    if (current.status === "starting" || current.status === "live") await follow(current.session_id);
  } catch {
    /* no camera is open */
  }
});

onBeforeUnmount(() => {
  leaving = true;
  generation += 1;
  pollAbort?.abort();
  window.removeEventListener("pagehide", stopQuiet);
  const id = session.value?.session_id;
  const status = session.value?.status;
  if (id && (status === "live" || status === "starting")) stopLive(id).catch(() => {});
  if (faceUrl.value) URL.revokeObjectURL(faceUrl.value);
});
</script>

<template>
  <main class="layout">
    <section class="panel query">
      <label class="field">
        <span>Camera</span>
        <input
          v-model="source"
          class="text-input mono"
          type="text"
          spellcheck="false"
          autocapitalize="off"
          placeholder="rtsp://192.168.1.20:554/live"
          :disabled="running"
        />
      </label>
      <p class="face-meta">
        An RTSP, RTMP, HTTP, or UDP address, or a camera number such as 0.
        Drone FPV works when the receiver is restreamed to RTSP, or when its HDMI goes through a capture card.
      </p>

      <label class="field">
        <span>Look for</span>
        <ChoiceMenu v-model="watch" :options="watchOptions" :disabled="running" label="What to look for" />
      </label>

      <div
        v-if="wantsFace"
        class="drop short"
        :class="{ dragging: faceDragging }"
        @dragenter.prevent="faceDragging = true"
        @dragover.prevent="faceDragging = true"
        @dragleave.prevent="faceDragging = false"
        @drop.prevent="setFace"
      >
        <input
          ref="faceInput"
          type="file"
          accept="image/*"
          aria-label="Face to find"
          :disabled="running"
          @change="setFace"
        />
        <div v-if="faceUrl" class="preview">
          <img :src="faceUrl" alt="Face to find" />
        </div>
        <div v-else class="drop-copy">
          <strong>Drop one photo</strong>
          <p>or click to browse. This is the face to find.</p>
        </div>
      </div>
      <p v-if="wantsFace && faceFile" class="face-meta">{{ faceFile.name }}</p>

      <label v-if="watch !== 'faces'" class="field">
        <span>Plate to find</span>
        <input v-model="plate" class="text-input" type="text" maxlength="16" placeholder="Optional, for example AB12CDE" :disabled="running" />
      </label>

      <label class="field">
        <span>Samples per second</span>
        <ChoiceMenu v-model="sampleFps" :options="sampleOptions" :disabled="running" label="Samples per second" />
      </label>

      <div v-if="wantsFace" class="slider-row">
        <label for="live-floor">
          <span>Similarity floor</span>
          <strong>{{ minPercent }}%</strong>
        </label>
        <input id="live-floor" v-model.number="minPercent" type="range" min="0" max="90" step="1" :disabled="running" />
      </div>

      <div class="row-actions">
        <button v-if="wantsFace && faceUrl" type="button" class="ghost" :disabled="running" @click="faceInput?.click()">
          Change photo
        </button>
        <button v-if="!running" type="button" class="primary" :disabled="!canStart" @click="start">
          Start watching
        </button>
        <button v-else type="button" class="danger" @click="stop">Stop</button>
      </div>
      <p class="face-meta">
        The picture is sampled a few times a second. A face or plate that flashes between samples can be missed.
        Leaving this tab stops the watch.
      </p>
      <p class="status" role="status">{{ running ? (session?.message || "Opening the camera") : "" }}</p>
      <p v-if="error" class="error" role="alert">{{ error }}</p>
    </section>

    <section>
      <div class="results-head">
        <div>
          <p v-if="session?.status === 'live'" class="live-flag"><span class="live-dot"></span> Live</p>
          <h2>Live sightings</h2>
          <p v-if="session">
            {{ session.message }} · {{ session.elapsed_label }} on {{ session.source }}.
            {{ session.frames_checked.toLocaleString() }} frames checked.
            <template v-if="session.note"> {{ session.note }}</template>
          </p>
          <p v-else>Start a watch. Sightings are listed here, newest first. Times start when the watch starts.</p>
        </div>
      </div>

      <img v-if="previewSrc" class="live-view" :src="previewSrc" alt="Live camera" />

      <div v-if="session?.hits?.length" class="timeline">
        <article v-for="hit in session.hits" :key="`${hit.kind}-${hit.label}-${hit.start_seconds}`" class="shot">
          <img :src="hit.snapshot_url" :alt="hit.label || 'Sighting'" />
          <div>
            <div class="card-top">
              <p class="name">{{ hit.label || (hit.kind === "face" ? "Face" : "Plate") }}</p>
              <p class="percent" :data-band="band(hit.match_percent)">{{ hit.match_percent.toFixed(1) }}%</p>
            </div>
            <p class="credit">{{ rangeLabel(hit) }}</p>
          </div>
        </article>
      </div>
    </section>
  </main>
</template>
