import assert from "node:assert/strict";
import { test } from "node:test";
import { buildTree, chosenLabel, describeCounts, isWithin, labelFor, pruneTags, withTag } from "./fileTree.js";

const PATHS = [
  "set/real/a.png",
  "set/real/b.png",
  "set/edits/c.png",
  "set/edits/deep/d.png",
  "set/e.png",
  "lone.png",
];

const names = (folder) => folder.children.map((child) => child.name);
const find = (folder, ...route) => route.reduce((node, name) => node.children.find((child) => child.name === name), folder);

test("the tree puts folders before images, sorted by name, with counts", () => {
  const tree = buildTree(PATHS, {});
  assert.deepEqual(names(tree), ["set", "lone.png"]);
  const set = find(tree, "set");
  assert.deepEqual(names(set), ["edits", "real", "e.png"]);
  assert.equal(set.count, 5);
  assert.equal(find(set, "edits").count, 2);
  assert.equal(find(set, "edits", "deep").count, 1);
  assert.equal(tree.count, 6);
});

test("names sort the way people read them", () => {
  const tree = buildTree(["x/img10.png", "x/img2.png", "x/img1.png"], {});
  assert.deepEqual(names(find(tree, "x")), ["img1.png", "img2.png", "img10.png"]);
});

test("a folder named real labels its images, and shows as untampered", () => {
  const tree = buildTree(PATHS, {});
  const a = find(tree, "set", "real", "a.png");
  assert.deepEqual([a.label, a.source], ["real", "name"]);
  assert.deepEqual(find(tree, "set", "real").labels, { untampered: 2 });
  assert.deepEqual(tree.labels, { untampered: 2, unlabeled: 4 });
});

test("a tag on a folder reaches every image inside it, however deep", () => {
  const tags = { "set/edits/": "tampered" };
  assert.deepEqual(labelFor("set/edits/c.png", tags), { label: "tampered", source: "folder" });
  assert.deepEqual(labelFor("set/edits/deep/d.png", tags), { label: "tampered", source: "folder" });
  assert.deepEqual(labelFor("set/e.png", tags), { label: null, source: null });
  assert.deepEqual(find(buildTree(PATHS, tags), "set", "edits").labels, { tampered: 2 });
});

test("a tag on an image beats its folder's tag, and the nearest folder wins", () => {
  const tags = { "set/": "tampered", "set/edits/": "untampered", "set/edits/c.png": "tampered" };
  assert.equal(labelFor("set/edits/c.png", tags).label, "tampered");
  assert.equal(labelFor("set/edits/c.png", tags).source, "file");
  assert.equal(labelFor("set/edits/deep/d.png", tags).label, "real");
  assert.equal(labelFor("set/e.png", tags).label, "tampered");
});

test("a tag beats a folder name, even a different one", () => {
  assert.equal(labelFor("set/real/a.png", { "set/real/a.png": "tampered" }).label, "tampered");
  assert.equal(labelFor("set/real/a.png", { "set/": "tampered" }).label, "tampered");
});

test("only what a person chose is sent to the server", () => {
  const tags = { "set/edits/": "tampered", "set/e.png": "untampered" };
  assert.equal(chosenLabel("set/edits/c.png", tags), "tampered");
  assert.equal(chosenLabel("set/e.png", tags), "real");
  assert.equal(chosenLabel("set/real/a.png", tags), undefined, "its folder name already says real");
  assert.equal(chosenLabel("lone.png", tags), undefined);
});

test("tagging a folder drops tags set inside it and leaves the rest", () => {
  const before = { "set/edits/c.png": "untampered", "set/edits/deep/": "untampered", "set/e.png": "tampered" };
  const after = withTag(before, "set/edits/", "tampered");
  assert.deepEqual(after, { "set/e.png": "tampered", "set/edits/": "tampered" });
  assert.deepEqual(before, { "set/edits/c.png": "untampered", "set/edits/deep/": "untampered", "set/e.png": "tampered" }, "input is untouched");
});

test("clearing a tag removes only that tag", () => {
  const tags = { "set/": "tampered", "set/e.png": "untampered" };
  assert.deepEqual(withTag(tags, "set/", null), { "set/e.png": "untampered" });
  assert.deepEqual(withTag(tags, "set/e.png", null), { "set/": "tampered" });
});

test("retagging an image replaces its tag", () => {
  assert.deepEqual(withTag({ "a.png": "tampered" }, "a.png", "untampered"), { "a.png": "untampered" });
});

test("isWithin tells a folder's contents from a lookalike", () => {
  assert.equal(isWithin("set/", "set/real/a.png"), true);
  assert.equal(isWithin("set/", "set2/a.png"), false);
  assert.equal(isWithin("set/real/a.png", "set/real/a.png"), true);
  assert.equal(isWithin("set/real/a.png", "set/real/a.png.bak"), false);
});

test("removing images prunes the tags that no longer point at anything", () => {
  const tags = { "set/edits/": "tampered", "set/edits/c.png": "untampered", "set/e.png": "tampered", "set/real/": "untampered" };
  const left = PATHS.filter((path) => !isWithin("set/edits/", path));
  assert.deepEqual(pruneTags(left, tags), { "set/e.png": "tampered", "set/real/": "untampered" });
});

test("a folder emptied one image at a time loses its tag too", () => {
  const tags = { "set/real/": "tampered" };
  const left = PATHS.filter((path) => path !== "set/real/a.png" && path !== "set/real/b.png");
  assert.deepEqual(pruneTags(left, tags), {});
  assert.deepEqual(pruneTags(PATHS.filter((path) => path !== "set/real/a.png"), tags), tags);
});

test("counts read in a fixed order", () => {
  assert.equal(describeCounts({ unlabeled: 1, untampered: 5, tampered: 3 }), "3 tampered, 5 untampered, 1 unlabeled");
  assert.equal(describeCounts({}), "");
});

test("an empty selection is an empty tree", () => {
  const tree = buildTree([], {});
  assert.deepEqual([tree.count, tree.children, tree.labels], [0, [], {}]);
});
