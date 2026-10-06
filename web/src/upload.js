// Image uploads: reserve slots, send each file, complete it (docs/API.md, Uploads).
import { api } from "./api.js";
import { getToken } from "./auth.js";

const IMAGE_TYPES = [".png", ".jpg", ".jpeg", ".webp"];
const PARALLEL_FILES = 4;

// The path within the chosen folder, as the server stores it.
export function filePath(file) {
  return (file.webkitRelativePath || file.name).replace(/\\/g, "/").trim();
}

export function isImage(file) {
  const path = filePath(file);
  return IMAGE_TYPES.some((type) => path.toLowerCase().endsWith(type)) && !path.split("/").some((part) => part.startsWith("."));
}

async function sendFile(slot, file) {
  const headers = { "Content-Type": slot.content_type };
  // A path on our own server needs the sign-in; a signed storage URL needs nothing.
  if (slot.upload_url.startsWith("/")) {
    const token = await getToken();
    if (token) headers.Authorization = `Bearer ${token}`;
  }
  const response = await fetch(slot.upload_url, { method: "PUT", headers, body: file });
  if (!response.ok) throw new Error(`upload failed (HTTP ${response.status})`);
}

async function inParallel(items, limit, work) {
  let next = 0;
  const workers = Array.from({ length: Math.min(limit, items.length) }, async () => {
    while (next < items.length) {
      const item = items[next++];
      await work(item);
    }
  });
  await Promise.all(workers);
}

// Upload `files` prepared for `maxEdge`; returns {accepted, rejected}, accepted in name order.
// `chosenLabel(file)` gives the label a person picked for a file, if any; the
// server takes the rest from folder names.
export async function uploadImages(files, maxEdge, batchSize, onProgress, chosenLabel = () => undefined) {
  const accepted = [];
  const rejected = [];
  let done = 0;
  const step = () => onProgress(++done, files.length);
  for (let start = 0; start < files.length; start += batchSize) {
    const batch = files.slice(start, start + batchSize);
    const byName = new Map(batch.map((file) => [filePath(file), file]));
    const body = await api("/api/v1/uploads", {
      method: "POST",
      body: { max_edge: maxEdge, files: batch.map((file) => ({ name: filePath(file), bytes: file.size, label: chosenLabel(file) })) },
    });
    body.rejected.forEach((item) => {
      rejected.push(item);
      step();
    });
    await inParallel(body.uploads, PARALLEL_FILES, async (slot) => {
      try {
        const file = byName.get(slot.name);
        if (!file) throw new Error("the server renamed this file");
        await sendFile(slot, file);
        accepted.push(await api(`/api/v1/uploads/${slot.upload_id}/complete`, { method: "POST" }));
      } catch (error) {
        rejected.push({ name: slot.name, reason: error.message });
        // Free the slot; it would otherwise count against the upload cap for hours.
        api(`/api/v1/uploads/${slot.upload_id}`, { method: "DELETE" }).catch(() => {});
      }
      step();
    });
  }
  accepted.sort((a, b) => a.name.localeCompare(b.name));
  return { accepted, rejected };
}
