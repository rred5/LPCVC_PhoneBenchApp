// The folder tree behind the Images pane: build it from the chosen paths,
// work out each image's label, and apply tags and removals. Plain functions
// with no React and no network, so they can be tested on their own.

// What a person can choose. The server stores an untampered image as "real".
export const TAGS = ["tampered", "untampered"];
const TAG_LABEL = { tampered: "tampered", untampered: "real" };
const NAME_LABELS = ["real", "synthetic", "tampered"];
const SHOWN_ORDER = ["tampered", "untampered", "synthetic", "unlabeled"];

// Tags are kept as { key: tag }. A folder's key ends in "/", so it can never
// equal an image's path.
export const folderKey = (path) => `${path}/`;

// Is `path` the file `key`, or inside the folder `key`?
export function isWithin(key, path) {
  return key.endsWith("/") ? path.startsWith(key) : path === key;
}

// A folder named real, synthetic or tampered labels the images directly in it.
export function nameLabel(path) {
  const parts = path.split("/");
  if (parts.length < 2) return null;
  const folder = parts[parts.length - 2].toLowerCase();
  return NAME_LABELS.includes(folder) ? folder : null;
}

// The tag in force for an image: its own, else the nearest tagged folder above it.
function tagFor(path, tags) {
  if (tags[path]) return { tag: tags[path], source: "file" };
  const parts = path.split("/");
  for (let depth = parts.length - 1; depth > 0; depth--) {
    const tag = tags[folderKey(parts.slice(0, depth).join("/"))];
    if (tag) return { tag, source: "folder" };
  }
  return null;
}

// The label the server will store, and where it came from: a tag on the file,
// a tag on a folder, or the folder's name. A tag always beats a folder name.
export function labelFor(path, tags) {
  const tagged = tagFor(path, tags);
  if (tagged) return { label: TAG_LABEL[tagged.tag], source: tagged.source };
  const label = nameLabel(path);
  return label ? { label, source: "name" } : { label: null, source: null };
}

// The label to send with an upload, only where a person chose it.
export function chosenLabel(path, tags) {
  const { label, source } = labelFor(path, tags);
  return source === "file" || source === "folder" ? label : undefined;
}

// How a label reads on screen: the server's "real" is an untampered image.
export const shownLabel = (label) => (label === "real" ? "untampered" : (label ?? "unlabeled"));

// "3 tampered, 5 untampered" from { tampered: 3, untampered: 5 }.
export function describeCounts(counts) {
  return Object.entries(counts)
    .sort(([a], [b]) => SHOWN_ORDER.indexOf(a) - SHOWN_ORDER.indexOf(b))
    .map(([label, count]) => `${count} ${label}`)
    .join(", ");
}

// Set a tag on a file or folder, or clear it with null. Tagging a folder tags
// everything inside it, so tags set earlier inside it are dropped.
export function withTag(tags, key, tag) {
  const next = {};
  for (const [other, value] of Object.entries(tags)) {
    if (other === key) continue;
    if (tag && key.endsWith("/") && isWithin(key, other)) continue;
    next[other] = value;
  }
  if (tag) next[key] = tag;
  return next;
}

// Keep only the tags that still point at a remaining image or a folder with one.
export function pruneTags(paths, tags) {
  const next = {};
  for (const [key, tag] of Object.entries(tags)) {
    if (paths.some((path) => isWithin(key, path))) next[key] = tag;
  }
  return next;
}

// The tree for `paths`: folders first, then images, each by name. Every folder
// has `count` images and `labels`, how many carry each shown label; every image
// has its `label` and where it came from. The top node is the whole selection.
export function buildTree(paths, tags) {
  const newFolder = (name, key) => ({ type: "folder", name, key, tag: tags[key] ?? null, count: 0, labels: {}, children: [] });
  const root = newFolder("", "");
  const folders = new Map();
  for (const path of paths) {
    const parts = path.split("/");
    const { label, source } = labelFor(path, tags);
    const shown = shownLabel(label);
    let parent = root;
    root.count++;
    root.labels[shown] = (root.labels[shown] ?? 0) + 1;
    for (let depth = 1; depth < parts.length; depth++) {
      const key = folderKey(parts.slice(0, depth).join("/"));
      let folder = folders.get(key);
      if (!folder) {
        folder = newFolder(parts[depth - 1], key);
        folders.set(key, folder);
        parent.children.push(folder);
      }
      folder.count++;
      folder.labels[shown] = (folder.labels[shown] ?? 0) + 1;
      parent = folder;
    }
    parent.children.push({ type: "file", name: parts[parts.length - 1], path, tag: tags[path] ?? null, label, source });
  }
  sortFolder(root);
  return root;
}

function sortFolder(folder) {
  const byName = (a, b) => a.name.localeCompare(b.name, undefined, { numeric: true });
  folder.children.sort((a, b) => (a.type === b.type ? byName(a, b) : a.type === "folder" ? -1 : 1));
  for (const child of folder.children) if (child.type === "folder") sortFolder(child);
}
