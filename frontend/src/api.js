async function readError(response, fallback) {
  let data = null;
  try {
    data = await response.json();
  } catch {
    data = null;
  }
  const detail = data?.detail;
  if (typeof detail === "string") return new Error(detail);
  if (Array.isArray(detail)) {
    const text = detail.map((item) => item.msg || String(item)).join(" ");
    return new Error(text || fallback);
  }
  return new Error(fallback);
}

export async function fetchStats(signal) {
  const response = await fetch("/api/stats", { signal });
  if (!response.ok) throw await readError(response, "Gallery status is unavailable.");
  return response.json();
}

export async function fetchSamples(signal) {
  const response = await fetch("/api/samples?limit=8", { signal });
  if (!response.ok) throw await readError(response, "Sample portraits are unavailable.");
  return response.json();
}

export async function createSearch(file, { pageSize, minSimilarity, signal }) {
  const body = new FormData();
  body.append("file", file);
  const params = new URLSearchParams({
    page: "1",
    page_size: String(pageSize),
    min_similarity: String(minSimilarity),
  });
  const response = await fetch(`/api/search?${params}`, { method: "POST", body, signal });
  if (!response.ok) throw await readError(response, "Search failed.");
  return response.json();
}

export async function fetchPage(searchId, { page, pageSize, minSimilarity, signal }) {
  const params = new URLSearchParams({
    page: String(page),
    page_size: String(pageSize),
    min_similarity: String(minSimilarity),
  });
  const response = await fetch(`/api/search/${searchId}?${params}`, { signal });
  if (!response.ok) throw await readError(response, "Could not load that page.");
  return response.json();
}

export async function fetchFolders(path = "", signal) {
  const params = new URLSearchParams({ path });
  const response = await fetch(`/api/folders?${params}`, { signal });
  if (!response.ok) throw await readError(response, "Could not list that folder.");
  return response.json();
}

export async function uploadFolder(files, signal) {
  const body = new FormData();
  for (const file of files) {
    body.append("files", file, file.webkitRelativePath || file.name);
  }
  const response = await fetch("/api/folders/upload", { method: "POST", body, signal });
  if (!response.ok) throw await readError(response, "Could not upload that folder.");
  return response.json();
}

export async function replaceGallery(folder, signal) {
  const params = new URLSearchParams({ folder });
  const response = await fetch(`/api/gallery/replace?${params}`, { method: "POST", signal });
  if (!response.ok) throw await readError(response, "Could not replace the gallery.");
  return response.json();
}

export async function startFolderSearch(file, folder, minSimilarity, signal) {
  const body = new FormData();
  body.append("file", file);
  body.append("folder", folder);
  const params = new URLSearchParams({ min_similarity: String(minSimilarity) });
  const response = await fetch(`/api/folder-search?${params}`, { method: "POST", body, signal });
  if (!response.ok) throw await readError(response, "Folder search failed.");
  return response.json();
}

export async function previewVideo(video, signal) {
  const body = new FormData();
  body.append("video", video);
  const response = await fetch("/api/video/preview", { method: "POST", body, signal });
  if (!response.ok) throw await readError(response, "This video could not be prepared for preview.");
  return response.blob();
}

export async function startFaceVideo(file, video, { minSimilarity, sampleFps, signal }) {
  const body = new FormData();
  body.append("file", file);
  body.append("video", video);
  const params = new URLSearchParams({
    min_similarity: String(minSimilarity),
    sample_fps: String(sampleFps),
  });
  const response = await fetch(`/api/video/face?${params}`, { method: "POST", body, signal });
  if (!response.ok) throw await readError(response, "Video search failed.");
  return response.json();
}

export async function startPlateVideo(video, { plate, sampleFps, signal }) {
  const body = new FormData();
  body.append("video", video);
  body.append("plate", plate || "");
  const params = new URLSearchParams({ sample_fps: String(sampleFps) });
  const response = await fetch(`/api/video/plates?${params}`, { method: "POST", body, signal });
  if (!response.ok) throw await readError(response, "Plate search failed.");
  return response.json();
}

export async function fetchJob(jobId, signal) {
  const response = await fetch(`/api/jobs/${jobId}`, { signal });
  if (!response.ok) throw await readError(response, "Could not read scan progress.");
  return response.json();
}

export async function startLive({ source, watch, plate, file, minSimilarity, sampleFps, signal }) {
  const body = new FormData();
  body.append("source", source);
  body.append("watch", watch);
  body.append("plate", plate || "");
  if (file) body.append("file", file);
  const params = new URLSearchParams({
    min_similarity: String(minSimilarity),
    sample_fps: String(sampleFps),
  });
  const response = await fetch(`/api/live?${params}`, { method: "POST", body, signal });
  if (!response.ok) throw await readError(response, "The camera watch failed.");
  return response.json();
}

export async function fetchCurrentLive(signal) {
  const response = await fetch("/api/live/current", { signal });
  if (response.status === 404) return null;
  if (!response.ok) throw await readError(response, "Could not read the camera watch.");
  return response.json();
}

export async function fetchLive(sessionId, signal) {
  const response = await fetch(`/api/live/${sessionId}`, { signal });
  if (!response.ok) throw await readError(response, "Could not read the camera watch.");
  return response.json();
}

export async function stopLive(sessionId, signal) {
  const response = await fetch(`/api/live/${sessionId}/stop`, { method: "POST", signal });
  if (!response.ok) throw await readError(response, "Could not stop the camera.");
  return response.json();
}
