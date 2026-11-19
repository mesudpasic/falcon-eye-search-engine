<script setup>
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";
import {
  createSearch,
  fetchFolders,
  fetchJob,
  fetchPage,
  fetchSamples,
  fetchStats,
  replaceGallery,
  startFolderSearch,
  uploadFolder,
} from "../api";

const pageSize = 12;
const source = ref("gallery");
const stats = ref(null);
const samples = ref([]);
const queryFile = ref(null);
const previewUrl = ref("");
const dragging = ref(false);
const searching = ref(false);
const error = ref("");
const galleryResult = ref(null);
const minPercent = ref(35);
const active = ref(null);
const fileInput = ref(null);
const folderInput = ref(null);
const replaceInput = ref(null);
const dialog = ref(null);
const replaceDialog = ref(null);
const replaceOpen = ref(false);
const replaceTarget = ref(null);
const pendingReplace = ref(null);
const notice = ref("");
const resultsHead = ref(null);

const listing = ref(null);
const selected = ref(null);
const job = ref(null);
const folderPage = ref(1);

let abortController = null;
let debounceTimer = 0;
let statsTimer = 0;

const minSimilarity = computed(() => minPercent.value / 100);
const folderResult = computed(() => (source.value === "folder" ? job.value?.folder || null : null));
const shown = computed(() => (source.value === "gallery" ? galleryResult.value : folderResult.value));

const boxStyle = computed(() => {
  const box = shown.value?.face_box;
  if (!box || box.length !== 4) return null;
  const [x1, y1, x2, y2] = box;
  return {
    left: `${x1 * 100}%`,
    top: `${y1 * 100}%`,
    width: `${Math.max(0, x2 - x1) * 100}%`,
    height: `${Math.max(0, y2 - y1) * 100}%`,
  };
});

const rangeLabel = computed(() => {
  const result = galleryResult.value;
  if (source.value !== "gallery" || !result?.results?.length) return "";
  const start = (result.page - 1) * result.page_size + 1;
  const end = start + result.results.length - 1;
  return `Showing ${start}–${end} of ${result.total}`;
});

const pageWindow = computed(() => {
  const result = galleryResult.value;
  const current = result?.page || 1;
  const pages = result?.pages || 0;
  if (source.value !== "gallery" || !pages) return [];
  const start = Math.max(1, Math.min(current - 2, pages - 4));
  const end = Math.min(pages, start + 4);
  return Array.from({ length: end - start + 1 }, (_, index) => start + index);
});

const folderPageCount = computed(() => Math.ceil((folderResult.value?.results?.length || 0) / pageSize));
const folderPageItems = computed(() => {
  const rows = folderResult.value?.results || [];
  const start = (folderPage.value - 1) * pageSize;
  return rows.slice(start, start + pageSize);
});

const resultTitle = computed(() => {
  const result = shown.value;
  if (!result) return "Results";
  const count = result.total;
  const label = count === 1 ? "similar face" : "similar faces";
  return `${count.toLocaleString()} ${label}`;
});

const resultHint = computed(() => {
  if (source.value === "folder") {
    const result = folderResult.value;
    if (!result) {
      return selected.value
        ? "Matches appear here after a photo is compared with this folder."
        : "Choose a folder, then a photo. Each detectable face in the folder is compared.";
    }
    let text = `${result.examined.toLocaleString()} images checked, ${result.with_faces.toLocaleString()} with a face.`;
    if (result.total > result.results.length) text += ` Showing the strongest ${result.results.length}.`;
    return text;
  }
  if (!galleryResult.value) return "Matches appear here after a photo is uploaded.";
  return rangeLabel.value || "Ranked by face similarity.";
});

const emptyCopy = computed(() => {
  const result = shown.value;
  if (!result || result.total > 0 || searching.value) return "";
  if (source.value === "folder") return "No face in that folder reached this similarity. Lower the floor to see nearer faces.";
  if ((stats.value?.enrolled_faces || 0) === 0) {
    return "The gallery is empty. Replace it with a folder of photos.";
  }
  return "No enrolled face reached this similarity. Lower the floor to see nearer faces.";
});

const currentLabel = computed(() => {
  if (!listing.value) return "…";
  return listing.value.path || "data";
});

const statusText = computed(() => {
  if (searching.value) {
    if (job.value?.kind === "gallery-replace") return job.value.message || "Replacing the gallery…";
    if (source.value === "folder") return job.value?.message || "Reading faces in the folder…";
    return "Reading the face and comparing it with the gallery…";
  }
  if (notice.value) return notice.value;
  if (source.value === "folder" && queryFile.value && !selected.value) {
    return "Choose a folder to compare this photo with.";
  }
  return "";
});

function formatPercent(value) {
  return `${Number(value).toFixed(1)}%`;
}

function band(value) {
  if (value >= 70) return "high";
  if (value >= 45) return "mid";
  return "low";
}

function credit(item) {
  return [item.author, item.license].filter(Boolean).join(" · ");
}

function faceNote() {
  const found = shown.value?.faces_found || 0;
  if (!found) return "";
  if (found === 1) return "1 face used";
  return `Largest of ${found} faces`;
}

function beginRequest() {
  abortController?.abort();
  abortController = new AbortController();
  return abortController.signal;
}

function setPreview(file) {
  if (previewUrl.value) URL.revokeObjectURL(previewUrl.value);
  previewUrl.value = URL.createObjectURL(file);
}

function takePhoto(file) {
  if (!file) return;
  if (file.type && !file.type.startsWith("image/")) return;
  queryFile.value = file;
  setPreview(file);
  error.value = "";
  folderPage.value = 1;
  runSearch();
}

async function searchGallery() {
  if (!queryFile.value) return;
  const signal = beginRequest();
  searching.value = true;
  error.value = "";
  try {
    galleryResult.value = await createSearch(queryFile.value, {
      pageSize,
      minSimilarity: minSimilarity.value,
      signal,
    });
  } catch (err) {
    if (signal.aborted || err.name === "AbortError") return;
    galleryResult.value = null;
    error.value = err.message || "Search failed.";
  } finally {
    if (!signal.aborted) searching.value = false;
  }
}

async function loadPage(page, { scroll = false } = {}) {
  if (!galleryResult.value?.search_id) return;
  const signal = beginRequest();
  searching.value = true;
  error.value = "";
  try {
    galleryResult.value = await fetchPage(galleryResult.value.search_id, {
      page,
      pageSize,
      minSimilarity: minSimilarity.value,
      signal,
    });
    if (scroll) resultsHead.value?.scrollIntoView({ block: "start" });
  } catch (err) {
    if (signal.aborted || err.name === "AbortError") return;
    error.value = err.message || "Could not load that page.";
  } finally {
    if (!signal.aborted) searching.value = false;
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

async function searchFolder() {
  if (!queryFile.value || !selected.value) return;
  const signal = beginRequest();
  searching.value = true;
  error.value = "";
  folderPage.value = 1;
  try {
    job.value = await startFolderSearch(queryFile.value, selected.value.path, minSimilarity.value, signal);
    while (job.value.status !== "done" && job.value.status !== "error") {
      await wait(signal);
      job.value = await fetchJob(job.value.job_id, signal);
    }
    if (job.value.error) error.value = job.value.error;
  } catch (err) {
    if (signal.aborted || err.name === "AbortError") return;
    error.value = err.message || "Folder search failed.";
  } finally {
    if (!signal.aborted) searching.value = false;
  }
}

function runSearch() {
  if (!queryFile.value) return;
  if (source.value === "gallery") searchGallery();
  else if (selected.value) searchFolder();
}

function onInput(event) {
  const file = event.target.files?.[0];
  if (file) takePhoto(file);
}

function onDrop(event) {
  dragging.value = false;
  const file = event.dataTransfer?.files?.[0];
  if (file) takePhoto(file);
}

async function useSample(sample) {
  error.value = "";
  try {
    const response = await fetch(sample.photo_url);
    if (!response.ok) throw new Error("Could not load that gallery photo.");
    const blob = await response.blob();
    takePhoto(new File([blob], `${sample.file_sha1}.jpg`, { type: "image/jpeg" }));
  } catch (err) {
    if (err.name === "AbortError") return;
    error.value = err.message || "Search failed.";
    searching.value = false;
  }
}

function clearSearch() {
  abortController?.abort();
  searching.value = false;
  if (previewUrl.value) URL.revokeObjectURL(previewUrl.value);
  previewUrl.value = "";
  queryFile.value = null;
  galleryResult.value = null;
  job.value = null;
  error.value = "";
  active.value = null;
  folderPage.value = 1;
  if (fileInput.value) fileInput.value.value = "";
}

function setSource(next) {
  if (source.value === next) return;
  abortController?.abort();
  searching.value = false;
  error.value = "";
  source.value = next;
  folderPage.value = 1;
  if (next === "folder" && !listing.value) {
    loadFolders("").catch((err) => {
      error.value = err.message || "Could not list folders.";
    });
  }
  runSearch();
}

function openMatch(item) {
  active.value = item;
  if (dialog.value && !dialog.value.open) dialog.value.showModal();
}

function closeMatch() {
  dialog.value?.close();
  active.value = null;
}

async function loadStats() {
  try {
    stats.value = await fetchStats();
    if (!samples.value.length && stats.value.enrolled_faces > 0) {
      samples.value = await fetchSamples();
    }
  } catch {
    stats.value = stats.value || null;
  }
}

async function loadFolders(path = "") {
  error.value = "";
  replaceTarget.value = null;
  listing.value = await fetchFolders(path);
}

function openReplace() {
  replaceOpen.value = !replaceOpen.value;
  notice.value = "";
  if (replaceOpen.value && !listing.value) {
    loadFolders("").catch((err) => {
      error.value = err.message || "Could not list folders.";
    });
  }
}

function askReplace() {
  const target = replaceTarget.value || (
    listing.value?.path
      ? {
          path: listing.value.path,
          imageCount: listing.value.image_count,
          label: listing.value.path,
        }
      : null
  );
  if (!target?.path) return;
  pendingReplace.value = target;
  replaceDialog.value?.showModal();
}

async function confirmReplace() {
  const target = pendingReplace.value;
  replaceDialog.value?.close();
  if (!target?.path) return;
  const signal = beginRequest();
  searching.value = true;
  error.value = "";
  notice.value = "";
  galleryResult.value = null;
  job.value = null;
  try {
    job.value = await replaceGallery(target.path, signal);
    while (job.value.status !== "done" && job.value.status !== "error") {
      await wait(signal);
      job.value = await fetchJob(job.value.job_id, signal);
    }
    if (job.value.error) error.value = job.value.error;
    else {
      notice.value = job.value.message;
      samples.value = [];
      await loadStats();
    }
  } catch (err) {
    if (signal.aborted || err.name === "AbortError") return;
    error.value = err.message || "Could not replace the gallery.";
  } finally {
    if (!signal.aborted) searching.value = false;
  }
}

function chooseCurrent() {
  if (!listing.value) return;
  selected.value = {
    path: listing.value.path,
    imageCount: listing.value.image_count,
    label: listing.value.path || "data",
  };
  folderPage.value = 1;
  if (queryFile.value) searchFolder();
}

async function onFolder(event) {
  const files = [...(event.target.files || [])];
  if (!files.length) return;
  const signal = beginRequest();
  searching.value = true;
  error.value = "";
  try {
    const uploaded = await uploadFolder(files, signal);
    const picked = {
      path: uploaded.path,
      imageCount: uploaded.image_count,
      label: uploaded.path,
    };
    if (source.value === "gallery") {
      replaceTarget.value = picked;
    } else {
      selected.value = picked;
      if (queryFile.value) await searchFolder();
    }
  } catch (err) {
    if (signal.aborted || err.name === "AbortError") return;
    error.value = err.message || "Could not upload that folder.";
  } finally {
    if (!signal.aborted) searching.value = false;
    if (folderInput.value) folderInput.value.value = "";
    if (replaceInput.value) replaceInput.value.value = "";
  }
}

watch(minPercent, () => {
  clearTimeout(debounceTimer);
  debounceTimer = setTimeout(() => {
    if (source.value === "gallery" && galleryResult.value?.search_id) loadPage(1);
    else if (source.value === "folder" && queryFile.value && selected.value && job.value) searchFolder();
  }, 250);
});

onMounted(() => {
  loadStats();
  statsTimer = setInterval(loadStats, 20000);
});

onBeforeUnmount(() => {
  clearTimeout(debounceTimer);
  clearInterval(statsTimer);
  abortController?.abort();
  if (previewUrl.value) URL.revokeObjectURL(previewUrl.value);
});
</script>

<template>
  <main class="layout">
    <section class="panel query">
      <div class="sources" role="group" aria-label="Where to search">
        <button type="button" :aria-pressed="source === 'gallery'" @click="setSource('gallery')">Gallery</button>
        <button type="button" :aria-pressed="source === 'folder'" @click="setSource('folder')">Folder</button>
      </div>

      <div v-if="source === 'gallery'" class="replace-box">
        <button type="button" class="ghost" :disabled="searching" @click="openReplace">
          {{ replaceOpen ? "Hide gallery replace" : "Replace gallery" }}
        </button>
        <template v-if="replaceOpen">
          <p class="face-meta">
            Delete the enrolled gallery and build a new one from a folder of photos. A photo with no detectable face is skipped. The current gallery stays until a new face is found.
          </p>
          <p class="path-line">Now browsing {{ currentLabel }}</p>
          <div class="row-actions">
            <button
              v-if="listing?.parent !== null && listing?.parent !== undefined"
              type="button"
              class="ghost"
              :disabled="searching"
              @click="loadFolders(listing.parent)"
            >
              Up
            </button>
          </div>
          <div v-if="listing?.directories?.length" class="folder-list">
            <button
              v-for="entry in listing.directories"
              :key="entry.path"
              type="button"
              class="ghost"
              :disabled="searching"
              @click="loadFolders(entry.path)"
            >
              {{ entry.name }}
            </button>
          </div>
          <p v-if="listing" class="face-meta">{{ listing.image_count.toLocaleString() }} images here, including subfolders.</p>
          <p v-if="replaceTarget" class="path-line">Uploaded folder: {{ replaceTarget.label }} · {{ replaceTarget.imageCount.toLocaleString() }} images</p>
          <label class="upload-label">
            Upload a folder
            <input
              ref="replaceInput"
              type="file"
              webkitdirectory
              directory
              multiple
              aria-label="Upload a folder to enroll"
              :disabled="searching"
              @change="onFolder"
            />
          </label>
          <div class="row-actions">
            <button
              type="button"
              class="danger"
              :disabled="searching || !(replaceTarget?.path || listing?.path)"
              @click="askReplace"
            >
              Replace gallery with this folder
            </button>
          </div>
        </template>
      </div>

      <div
        class="drop"
        :class="{ dragging }"
        @dragenter.prevent="dragging = true"
        @dragover.prevent="dragging = true"
        @dragleave.prevent="dragging = false"
        @drop.prevent="onDrop"
      >
        <input
          ref="fileInput"
          type="file"
          accept="image/*"
          aria-label="Upload one portrait"
          :disabled="searching"
          @change="onInput"
        />
        <div v-if="previewUrl" class="preview">
          <img :src="previewUrl" alt="Uploaded portrait" />
          <div v-if="boxStyle" class="facebox" :style="boxStyle"></div>
        </div>
        <div v-else class="drop-copy">
          <strong>Drop one photo</strong>
          <p>or click to browse. A single portrait per search.</p>
        </div>
      </div>

      <p v-if="queryFile && source === 'folder'" class="face-meta">{{ queryFile.name }}</p>
      <p v-if="faceNote()" class="face-meta">{{ faceNote() }}</p>

      <template v-if="source === 'folder'">
        <p class="face-meta">
          Large sets belong in the project <strong>data</strong> folder. Smaller sets can be uploaded from this computer.
        </p>
        <p class="path-line">Now browsing {{ currentLabel }}</p>
        <div class="row-actions">
          <button
            v-if="listing?.parent !== null && listing?.parent !== undefined"
            type="button"
            class="ghost"
            :disabled="searching"
            @click="loadFolders(listing.parent)"
          >
            Up
          </button>
          <button
            type="button"
            class="primary"
            :disabled="searching || !listing"
            @click="chooseCurrent"
          >
            Use this folder
          </button>
        </div>
        <div v-if="listing?.directories?.length" class="folder-list">
          <button
            v-for="entry in listing.directories"
            :key="entry.path"
            type="button"
            class="ghost"
            :disabled="searching"
            @click="loadFolders(entry.path)"
          >
            {{ entry.name }}
          </button>
        </div>
        <p v-if="listing" class="face-meta">{{ listing.image_count.toLocaleString() }} images here, including subfolders.</p>
        <label class="upload-label">
          Upload a folder
          <input
            ref="folderInput"
            type="file"
            webkitdirectory
            directory
            multiple
            aria-label="Upload a folder"
            :disabled="searching"
            @change="onFolder"
          />
        </label>
        <p v-if="selected" class="path-line">Source: {{ selected.label }} · {{ selected.imageCount.toLocaleString() }} images</p>
      </template>

      <div class="slider-row">
        <label for="floor">
          <span>Similarity floor</span>
          <strong>{{ minPercent }}%</strong>
        </label>
        <input
          id="floor"
          v-model.number="minPercent"
          type="range"
          min="0"
          max="90"
          step="1"
          :disabled="searching && !shown"
        />
      </div>

      <div class="row-actions">
        <button v-if="previewUrl" type="button" class="ghost" :disabled="searching" @click="fileInput?.click()">
          Change photo
        </button>
        <button type="button" class="ghost" :disabled="!previewUrl && !shown" @click="clearSearch">
          Clear
        </button>
      </div>

      <div v-if="source === 'gallery' && !galleryResult && samples.length" class="samples">
        <p>Try a gallery portrait</p>
        <div class="sample-row">
          <button
            v-for="sample in samples"
            :key="sample.file_sha1"
            type="button"
            class="sample"
            :disabled="searching"
            @click="useSample(sample)"
          >
            <img :src="sample.photo_url" :alt="sample.person_name" />
          </button>
        </div>
      </div>

      <p class="status" role="status">{{ statusText }}</p>
      <div v-if="searching && job && (source === 'folder' || job.kind === 'gallery-replace')" class="meter" aria-hidden="true">
        <span :style="{ width: `${Math.round((job.progress || 0) * 100)}%` }"></span>
      </div>
      <p v-if="error" class="error" role="alert">{{ error }}</p>
    </section>

    <section>
      <div ref="resultsHead" class="results-head">
        <div>
          <h2>{{ resultTitle }}</h2>
          <p>{{ resultHint }}</p>
        </div>
      </div>

      <p v-if="emptyCopy" class="error">{{ emptyCopy }}</p>

      <div v-if="source === 'gallery' && galleryResult?.results?.length" class="grid">
        <article v-for="item in galleryResult.results" :key="item.file_sha1" class="card">
          <button type="button" class="photo-button" @click="openMatch(item)">
            <img :src="item.photo_url" :alt="item.person_name" />
          </button>
          <div class="card-copy">
            <div class="card-top">
              <button type="button" class="name" @click="openMatch(item)">{{ item.person_name }}</button>
              <p class="percent" :data-band="band(item.match_percent)">{{ formatPercent(item.match_percent) }}</p>
            </div>
            <div class="meter" aria-hidden="true">
              <span :style="{ width: `${Math.min(item.match_percent, 100)}%` }"></span>
            </div>
            <p class="credit">
              <a
                v-if="item.source_url"
                :href="item.source_url"
                target="_blank"
                rel="noreferrer"
              >{{ credit(item) || "Source" }}</a>
              <template v-else>{{ credit(item) }}</template>
            </p>
          </div>
        </article>
      </div>

      <div v-if="source === 'folder' && folderPageItems.length" class="grid">
        <article v-for="item in folderPageItems" :key="item.relative_path" class="card">
          <button type="button" class="photo-button" @click="openMatch(item)">
            <img :src="item.photo_url" :alt="item.name" />
          </button>
          <div class="card-copy">
            <div class="card-top">
              <button type="button" class="name" @click="openMatch(item)">{{ item.name }}</button>
              <p class="percent" :data-band="band(item.match_percent)">{{ formatPercent(item.match_percent) }}</p>
            </div>
            <div class="meter" aria-hidden="true">
              <span :style="{ width: `${Math.min(item.match_percent, 100)}%` }"></span>
            </div>
            <p class="credit">{{ item.relative_path }}</p>
          </div>
        </article>
      </div>

      <nav v-if="source === 'gallery' && galleryResult?.pages > 1" class="pager" aria-label="Result pages">
        <button type="button" class="page" :disabled="!galleryResult.has_previous || searching" @click="loadPage(galleryResult.page - 1, { scroll: true })">
          Previous
        </button>
        <button
          v-for="page in pageWindow"
          :key="page"
          type="button"
          class="page"
          :aria-current="page === galleryResult.page ? 'page' : undefined"
          :disabled="searching"
          @click="loadPage(page, { scroll: true })"
        >
          {{ page }}
        </button>
        <button type="button" class="page" :disabled="!galleryResult.has_next || searching" @click="loadPage(galleryResult.page + 1, { scroll: true })">
          Next
        </button>
      </nav>

      <nav v-if="source === 'folder' && folderPageCount > 1" class="pager" aria-label="Result pages">
        <button type="button" class="page" :disabled="folderPage <= 1" @click="folderPage -= 1">Previous</button>
        <button type="button" class="page" :disabled="folderPage >= folderPageCount" @click="folderPage += 1">Next</button>
      </nav>
    </section>
  </main>

  <dialog ref="replaceDialog" class="confirm">
    <div class="confirm-card">
      <h3>Replace the gallery?</h3>
      <p>
        This deletes {{ (stats?.enrolled_faces || 0).toLocaleString() }} enrolled faces.
        The new gallery is built from {{ pendingReplace?.label || "the chosen folder" }}
        ({{ (pendingReplace?.imageCount || 0).toLocaleString() }} images).
      </p>
      <div class="row-actions">
        <button type="button" class="ghost" @click="replaceDialog?.close()">Cancel</button>
        <button type="button" class="danger" @click="confirmReplace">Delete and rebuild</button>
      </div>
    </div>
  </dialog>

  <dialog ref="dialog" @close="active = null">
    <div v-if="active" class="dialog-card">
      <img :src="active.photo_url" :alt="active.person_name || active.name" />
      <div class="dialog-copy">
        <p class="percent" :data-band="band(active.match_percent)">{{ formatPercent(active.match_percent) }} match</p>
        <h3>{{ active.person_name || active.name }}</h3>
        <p v-if="active.relative_path" class="credit">{{ active.relative_path }}</p>
        <p v-else class="credit">{{ credit(active) }}</p>
        <p v-if="active.license_url">
          <a :href="active.license_url" target="_blank" rel="noreferrer">License</a>
        </p>
        <p v-if="active.wikidata_url">
          <a :href="active.wikidata_url" target="_blank" rel="noreferrer">Wikidata</a>
        </p>
        <p v-if="active.file_sha1" class="hash">File SHA-1 {{ active.file_sha1 }}</p>
        <button type="button" class="close" @click="closeMatch">Close</button>
      </div>
    </div>
  </dialog>
</template>
