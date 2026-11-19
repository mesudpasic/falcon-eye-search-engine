<script setup>
import { computed, onBeforeUnmount, onMounted, ref } from "vue";
import { fetchStats } from "./api";
import PhotoSearch from "./components/PhotoSearch.vue";
import VideoSearch from "./components/VideoSearch.vue";

const mode = ref("photos");
const stats = ref(null);
let statsTimer = 0;

const lede = computed(() => {
  if (mode.value === "video") {
    return "Choose one face and a CCTV video. The scan marks the stretches of the video where that face appears.";
  }
  if (mode.value === "plates") {
    return "Read license plates in a CCTV video. Leave the plate blank to list every plate, or type one to keep only close readings, each with its time range.";
  }
  return "Upload one portrait and compare it with the enrolled gallery or with a folder of photos. A mask over the nose and mouth still counts. The closest photos come back first.";
});

async function loadStats() {
  try {
    stats.value = await fetchStats();
  } catch {
    stats.value = stats.value || null;
  }
}

onMounted(() => {
  window.addEventListener("dragover", blockWindowDrop);
  window.addEventListener("drop", blockWindowDrop);
  loadStats();
  statsTimer = setInterval(loadStats, 20000);
});

onBeforeUnmount(() => {
  window.removeEventListener("dragover", blockWindowDrop);
  window.removeEventListener("drop", blockWindowDrop);
  clearInterval(statsTimer);
});

function blockWindowDrop(event) {
  event.preventDefault();
}
</script>

<template>
  <div class="shell">
    <header class="topbar">
      <div>
        <div class="brand">
          <img class="brand-mark" src="/logo.png" width="72" height="47" alt="" />
          <div>
            <p class="eyebrow">Search</p>
            <h1>Falcon Eye <em>Search Engine</em></h1>
          </div>
        </div>
        <p class="lede">{{ lede }}</p>
        <nav class="modes" aria-label="Search mode">
          <button type="button" :aria-current="mode === 'photos' ? 'page' : undefined" @click="mode = 'photos'">Search faces from photos</button>
          <button type="button" :aria-current="mode === 'video' ? 'page' : undefined" @click="mode = 'video'">Search faces from videos</button>
          <button type="button" :aria-current="mode === 'plates' ? 'page' : undefined" @click="mode = 'plates'">Search car plates from videos</button>
        </nav>
      </div>
      <div class="plaque">
        <strong>{{ stats ? stats.enrolled_faces.toLocaleString() : "—" }}</strong>
        <span>enrolled faces</span>
      </div>
    </header>

    <PhotoSearch v-if="mode === 'photos'" />
    <VideoSearch v-else-if="mode === 'video'" mode="face" />
    <VideoSearch v-else mode="plate" />

    <footer class="footnote">
      <p>
        A photo is compared with the enrolled gallery or with a folder under <span class="hash">data</span>.
        Gallery portraits come from Wikimedia Commons and keep the license stored with each photo.
        Video searches stay on this machine.
        The percentage on a face is cosine similarity. A plate percentage is how sure the reader was of the characters.
      </p>
      <p>Copyright 2026 <a href="https://www.setec.ba">SETEC d.o.o.</a></p>
    </footer>
  </div>
</template>
