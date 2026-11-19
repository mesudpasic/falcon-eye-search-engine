<script setup>
import { onBeforeUnmount, ref } from "vue";
import { fetchJob, previewVideo, startFaceVideo, startPlateVideo } from "../api";
import ChoiceMenu from "./ChoiceMenu.vue";

const sampleOptions = [
  { value: 0.5, label: "1 every 2 seconds" },
  { value: 1, label: "1 per second" },
  { value: 2, label: "2 per second" },
  { value: 3, label: "3 per second" },
  { value: 4, label: "4 per second" },
  { value: 5, label: "5 per second" },
];

const props = defineProps({
  mode: { type: String, required: true },
});

const faceFile = ref(null);
const faceUrl = ref("");
const faceDragging = ref(false);
const videoFile = ref(null);
const playUrl = ref("");
const videoPoster = ref("");
const preparing = ref(false);
const videoDragging = ref(false);
const plate = ref("");
const sampleFps = ref(2);
const minPercent = ref(35);
const busy = ref(false);
const error = ref("");
const job = ref(null);
const player = ref(null);
const faceInput = ref(null);
const videoInput = ref(null);

let abortController = null;
let previewAbort = null;
let previewToken = 0;

function browserCanPlay(file) {
  const probe = document.createElement("video");
  const type = file.type && file.type !== "application/octet-stream" ? file.type : "";
  if (type && probe.canPlayType(type)) return true;
  const ext = file.name.split(".").pop()?.toLowerCase();
  const byExt = { mp4: "video/mp4", m4v: "video/mp4", webm: "video/webm" };
  return Boolean(byExt[ext] && probe.canPlayType(byExt[ext]));
}

function begin() {
  abortController?.abort();
  abortController = new AbortController();
  return abortController.signal;
}

function pickedFile(event) {
  return event.target?.files?.[0] || event.dataTransfer?.files?.[0] || null;
}

function isImage(file) {
  if (file.type) return file.type.startsWith("image/");
  return /\.(jpe?g|png|webp|gif|bmp|heic)$/i.test(file.name);
}

function isVideo(file) {
  if (file.type?.startsWith("video/")) return true;
  return /\.(mp4|mov|avi|mkv|webm|m4v|mpeg|mpg)$/i.test(file.name);
}

function framePoster(url) {
  return new Promise((resolve) => {
    const video = document.createElement("video");
    let settled = false;
    const finish = (value) => {
      if (settled) return;
      settled = true;
      video.removeAttribute("src");
      video.load();
      resolve(value);
    };
    const draw = () => {
      if (!video.videoWidth) return finish("");
      const canvas = document.createElement("canvas");
      const scale = Math.min(1, 960 / video.videoWidth);
      canvas.width = Math.max(1, Math.round(video.videoWidth * scale));
      canvas.height = Math.max(1, Math.round(video.videoHeight * scale));
      canvas.getContext("2d").drawImage(video, 0, 0, canvas.width, canvas.height);
      canvas.toBlob((blob) => {
        finish(blob ? URL.createObjectURL(blob) : "");
      }, "image/jpeg", 0.85);
    };
    video.muted = true;
    video.playsInline = true;
    video.preload = "auto";
    video.onerror = () => finish("");
    video.onloadeddata = () => {
      const duration = video.duration;
      const target = Number.isFinite(duration) && duration > 0.2 ? Math.min(0.2, duration * 0.05) : 0;
      if (target > 0) {
        video.onseeked = draw;
        try {
          video.currentTime = target;
        } catch {
          draw();
        }
      } else {
        draw();
      }
    };
    setTimeout(() => finish(""), 4000);
    video.src = url;
  });
}

function setFace(event) {
  faceDragging.value = false;
  const file = pickedFile(event);
  if (!file || !isImage(file)) return;
  faceFile.value = file;
  if (faceUrl.value) URL.revokeObjectURL(faceUrl.value);
  faceUrl.value = URL.createObjectURL(file);
}

function releasePlayUrl() {
  if (playUrl.value) URL.revokeObjectURL(playUrl.value);
  playUrl.value = "";
}

async function setVideo(event) {
  videoDragging.value = false;
  const file = pickedFile(event);
  if (!file || !isVideo(file)) return;
  previewAbort?.abort();
  previewAbort = new AbortController();
  const token = ++previewToken;
  const signal = previewAbort.signal;
  videoFile.value = file;
  releasePlayUrl();
  if (videoPoster.value) URL.revokeObjectURL(videoPoster.value);
  videoPoster.value = "";
  job.value = null;
  error.value = "";
  preparing.value = !browserCanPlay(file);
  try {
    let url = "";
    if (!preparing.value) {
      url = URL.createObjectURL(file);
    } else {
      const blob = await previewVideo(file, signal);
      if (token !== previewToken) return;
      url = URL.createObjectURL(new File([blob], "preview.mp4", { type: "video/mp4" }));
    }
    if (token !== previewToken) {
      URL.revokeObjectURL(url);
      return;
    }
    playUrl.value = url;
    const poster = await framePoster(url);
    if (token !== previewToken) {
      if (poster) URL.revokeObjectURL(poster);
      return;
    }
    videoPoster.value = poster;
  } catch (err) {
    if (signal.aborted || token !== previewToken) return;
    error.value = err.message || "This video could not be prepared for preview.";
  } finally {
    if (token === previewToken) preparing.value = false;
  }
}

async function wait(signal) {
  await new Promise((resolve, reject) => {
    const timer = setTimeout(resolve, 1000);
    signal.addEventListener("abort", () => {
      clearTimeout(timer);
      reject(new DOMException("Aborted", "AbortError"));
    }, { once: true });
  });
}

async function search() {
  if (!videoFile.value) return;
  if (props.mode === "face" && !faceFile.value) return;
  const signal = begin();
  busy.value = true;
  error.value = "";
  job.value = null;
  try {
    job.value = props.mode === "face"
      ? await startFaceVideo(faceFile.value, videoFile.value, {
          minSimilarity: minPercent.value / 100,
          sampleFps: sampleFps.value,
          signal,
        })
      : await startPlateVideo(videoFile.value, {
          plate: plate.value,
          sampleFps: sampleFps.value,
          signal,
        });
    while (job.value.status !== "done" && job.value.status !== "error") {
      await wait(signal);
      job.value = await fetchJob(job.value.job_id, signal);
    }
    if (job.value.error) error.value = job.value.error;
  } catch (err) {
    if (signal.aborted || err.name === "AbortError") return;
    error.value = err.message || "Video search failed.";
  } finally {
    if (!signal.aborted) busy.value = false;
  }
}

function playFrom(seconds) {
  const element = player.value;
  if (!element) return;
  element.currentTime = seconds;
  element.play();
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

onBeforeUnmount(() => {
  abortController?.abort();
  if (faceUrl.value) URL.revokeObjectURL(faceUrl.value);
  previewAbort?.abort();
  releasePlayUrl();
  if (videoPoster.value) URL.revokeObjectURL(videoPoster.value);
});
</script>

<template>
  <main class="layout">
    <section class="panel query">
      <div
        v-if="mode === 'face'"
        class="drop"
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
          :disabled="busy"
          @change="setFace"
        />
        <div v-if="faceUrl" class="preview">
          <img :src="faceUrl" alt="Face to find" />
        </div>
        <div v-else class="drop-copy">
          <strong>Drop one photo</strong>
          <p>or click to browse. This is the face to find in the video.</p>
        </div>
      </div>
      <p v-if="faceFile" class="face-meta">{{ faceFile.name }}</p>

      <div
        class="drop"
        :class="{ dragging: videoDragging, 'drop-follow': mode === 'face' }"
        @dragenter.prevent="videoDragging = true"
        @dragover.prevent="videoDragging = true"
        @dragleave.prevent="videoDragging = false"
        @drop.prevent="setVideo"
      >
        <input
          ref="videoInput"
          type="file"
          accept="video/*,.mp4,.mov,.avi,.mkv,.webm"
          aria-label="CCTV video"
          :disabled="busy"
          @change="setVideo"
        />
        <div v-if="videoFile" class="preview">
          <img v-if="videoPoster" :src="videoPoster" alt="Preview of the selected video" />
          <video v-else-if="playUrl" :src="playUrl" muted playsinline preload="auto"></video>
          <div v-else class="drop-copy">
            <strong>{{ preparing ? "Preparing preview…" : "Preview unavailable" }}</strong>
            <p v-if="preparing">AVI files are converted so the browser can play them.</p>
          </div>
        </div>
        <div v-else class="drop-copy">
          <strong>Drop a video</strong>
          <p>or click to browse. MP4, MOV, AVI, MKV, or WebM.</p>
        </div>
      </div>
      <p v-if="videoFile" class="face-meta">{{ videoFile.name }}</p>

      <label v-if="mode === 'plate'" class="field">
        <span>Plate to find</span>
        <input v-model="plate" class="text-input" type="text" maxlength="16" placeholder="Optional, for example AB12CDE" :disabled="busy" />
      </label>

      <label class="field">
        <span>Samples per second</span>
        <ChoiceMenu v-model="sampleFps" :options="sampleOptions" :disabled="busy" label="Samples per second" />
      </label>

      <div v-if="mode === 'face'" class="slider-row">
        <label for="video-floor">
          <span>Similarity floor</span>
          <strong>{{ minPercent }}%</strong>
        </label>
        <input id="video-floor" v-model.number="minPercent" type="range" min="0" max="90" step="1" :disabled="busy" />
      </div>

      <div class="row-actions">
        <button
          v-if="mode === 'face' && faceUrl"
          type="button"
          class="ghost"
          :disabled="busy"
          @click="faceInput?.click()"
        >
          Change photo
        </button>
        <button v-if="videoFile" type="button" class="ghost" :disabled="busy" @click="videoInput?.click()">
          Change video
        </button>
        <button
          type="button"
          class="primary"
          :disabled="busy || !videoFile || (mode === 'face' && !faceFile)"
          @click="search"
        >
          {{ mode === "face" ? "Search video" : "Read plates" }}
        </button>
      </div>
      <p class="face-meta">
        The video is sampled a few times a second. A face or plate that flashes between samples can be missed.
      </p>
      <p class="status" role="status">{{ busy ? (job?.message || "Working…") : "" }}</p>
      <div v-if="busy && job" class="meter" aria-hidden="true">
        <span :style="{ width: `${Math.round((job.progress || 0) * 100)}%` }"></span>
      </div>
      <p v-if="error" class="error" role="alert">{{ error }}</p>
    </section>

    <section>
      <div class="results-head">
        <div>
          <h2>{{ mode === "face" ? "Face time ranges" : "Plate time ranges" }}</h2>
          <p v-if="job?.scan">
            {{ job.scan.frames_checked.toLocaleString() }} frames checked.
            <template v-if="job.scan.note"> {{ job.scan.note }}</template>
          </p>
          <p v-else>Matches are listed with the stretch of video where they were seen.</p>
        </div>
      </div>

      <video
        v-if="playUrl"
        ref="player"
        class="video-player"
        :src="playUrl"
        :poster="videoPoster || undefined"
        controls
        playsinline
        preload="auto"
      ></video>

      <div v-if="job?.scan?.hits?.length" class="timeline">
        <article v-for="(hit, index) in job.scan.hits" :key="`${hit.start_seconds}-${hit.label}-${index}`" class="shot">
          <img :src="hit.snapshot_url" :alt="hit.label || 'Match frame'" />
          <div>
            <div class="card-top">
              <p class="name">{{ hit.label || (mode === "face" ? "Face" : "Plate") }}</p>
              <p class="percent" :data-band="band(hit.match_percent)">{{ hit.match_percent.toFixed(1) }}%</p>
            </div>
            <p class="credit">{{ rangeLabel(hit) }}</p>
            <button type="button" class="ghost" @click="playFrom(hit.start_seconds)">Play from here</button>
          </div>
        </article>
      </div>
    </section>
  </main>
</template>
