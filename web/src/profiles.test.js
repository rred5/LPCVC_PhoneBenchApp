// Run with `npm test` (node's built-in test runner; no browser needed).
import assert from "node:assert/strict";
import { test } from "node:test";
import {
  DEFAULT_ID,
  MAX_NAME,
  MAX_PROFILES,
  addProfile,
  builtInProfile,
  cleanSettings,
  defaultSettings,
  pickSettings,
  readState,
  removeProfile,
  resolveProfile,
  sameSettings,
  selectProfile,
  storageKey,
  summarize,
  updateProfile,
} from "./profiles.js";

class MemoryStorage {
  constructor() {
    this.data = new Map();
  }
  getItem(key) {
    return this.data.has(key) ? this.data.get(key) : null;
  }
  setItem(key, value) {
    this.data.set(key, String(value));
  }
  removeItem(key) {
    this.data.delete(key);
  }
}

class FailingStorage extends MemoryStorage {
  constructor(name = "QuotaExceededError") {
    super();
    this.name = name;
  }
  setItem() {
    throw Object.assign(new Error("nope"), { name: this.name });
  }
}

const EMAIL = "Alex@Example.com";

const options = {
  defaults: {
    run: {
      models: ["small"],
      backends: ["NPU"],
      prompts: ["Describe the image."],
      mode: "Warm",
      repetitions: 10,
      rest_seconds: 10,
      max_tokens: 2048,
    },
    preprocessing: { max_edge: 768 },
  },
  limits: {
    max_prompts: 3,
    max_prompt_chars: 20,
    repetitions: [1, 30],
    rest_seconds: [0, 60],
    max_tokens: [1, 4096],
    max_edge: [256, 2048],
  },
  models: [
    { id: "small", display_name: "Small", backends: ["CPU", "GPU", "NPU"] },
    { id: "big", display_name: "Big", backends: ["NPU"] },
  ],
};

const settings = (changes = {}) => ({ ...defaultSettings(options.defaults), ...changes });

// A counter instead of random ids keeps assertions readable.
const ids = () => {
  let count = 0;
  return () => `id-${++count}`;
};

test("a profile survives a round trip and a fresh read", () => {
  const storage = new MemoryStorage();
  const added = addProfile(storage, EMAIL, "  Fast NPU  ", settings({ repetitions: 3, backends: ["NPU"] }), ids());
  assert.equal(added.ok, true);
  assert.equal(added.profile.name, "Fast NPU");
  assert.equal(added.state.selected, added.profile.id);

  // A new read, as after a reload, sees the same thing.
  const again = readState(storage, EMAIL);
  assert.deepEqual(again, added.state);
  assert.equal(again.profiles[0].settings.repetitions, 3);
});

test("a profile keeps only the eight settings", () => {
  const storage = new MemoryStorage();
  const form = { ...settings(), name: "my job", files: ["a.png"], extra: 1 };
  const { profile } = addProfile(storage, EMAIL, "P", pickSettings(form), ids());
  assert.deepEqual(Object.keys(profile.settings).sort(), [
    "backends",
    "maxEdge",
    "maxTokens",
    "mode",
    "models",
    "prompts",
    "repetitions",
    "restSeconds",
  ]);
});

test("removing a profile removes it from storage and falls back to Default", () => {
  const storage = new MemoryStorage();
  const makeId = ids();
  const a = addProfile(storage, EMAIL, "A", settings(), makeId).profile;
  const b = addProfile(storage, EMAIL, "B", settings(), makeId).profile;
  assert.equal(readState(storage, EMAIL).selected, b.id);

  const removedSelected = removeProfile(storage, EMAIL, b.id);
  assert.equal(removedSelected.ok, true);
  assert.deepEqual(readState(storage, EMAIL).profiles.map((p) => p.id), [a.id]);
  assert.equal(readState(storage, EMAIL).selected, DEFAULT_ID);

  // Removing one that is not selected leaves the selection alone.
  selectProfile(storage, EMAIL, a.id);
  const c = addProfile(storage, EMAIL, "C", settings(), makeId).profile;
  selectProfile(storage, EMAIL, a.id);
  removeProfile(storage, EMAIL, c.id);
  assert.equal(readState(storage, EMAIL).selected, a.id);

  // Removing one that is already gone is not an error.
  assert.equal(removeProfile(storage, EMAIL, "never-existed").ok, true);
});

test("updating a profile replaces its settings and keeps its name, id and place", () => {
  const storage = new MemoryStorage();
  const makeId = ids();
  const first = addProfile(storage, EMAIL, "First", settings(), makeId).profile;
  const second = addProfile(storage, EMAIL, "Second", settings(), makeId).profile;
  addProfile(storage, EMAIL, "Third", settings(), makeId);

  const changed = settings({ repetitions: 7, mode: "cold", prompts: ["new prompt", "  ", "another"], maxEdge: 1024.4 });
  const result = updateProfile(storage, EMAIL, second.id, changed);
  assert.equal(result.ok, true);
  assert.equal(result.profile.name, "Second");
  assert.equal(result.profile.id, second.id);
  assert.equal(result.state.selected, second.id);

  // A fresh read, as after a reload, sees the new settings in the same place.
  const again = readState(storage, EMAIL);
  assert.deepEqual(again.profiles.map((p) => p.name), ["First", "Second", "Third"]);
  assert.deepEqual(again.profiles[1].settings, { ...changed, prompts: ["new prompt", "another"], maxEdge: 1024 });
  assert.equal(again.selected, second.id);
  // The others are untouched.
  assert.deepEqual(again.profiles[0].settings, first.settings);
  assert.equal(again.profiles[2].settings.repetitions, 10);
});

test("updating a profile that is gone, or with unusable settings, is refused", () => {
  const storage = new MemoryStorage();
  const { profile } = addProfile(storage, EMAIL, "Keep", settings(), ids());

  const missing = updateProfile(storage, EMAIL, "gone", settings());
  assert.equal(missing.ok, false);
  assert.equal(missing.found, false);
  assert.match(missing.error, /no longer saved/);

  for (const bad of [null, "text", { ...settings(), mode: "lukewarm" }, { ...settings(), maxTokens: "5" }]) {
    const result = updateProfile(storage, EMAIL, profile.id, bad);
    assert.equal(result.ok, false);
    assert.equal(result.found, true);
  }
  assert.deepEqual(readState(storage, EMAIL).profiles[0].settings, profile.settings);
  // Default is built in and can't be overwritten.
  assert.equal(updateProfile(storage, EMAIL, DEFAULT_ID, settings()).ok, false);
});

test("a failed update returns an error and leaves the old settings in place", () => {
  const storage = new MemoryStorage();
  const { profile } = addProfile(storage, EMAIL, "Keep", settings(), ids());
  const failing = Object.assign(new FailingStorage(), { data: storage.data });
  const result = updateProfile(failing, EMAIL, profile.id, settings({ repetitions: 2 }));
  assert.equal(result.ok, false);
  assert.match(result.error, /storage is full/);
  assert.equal(result.state.profiles[0].settings.repetitions, 10);
  assert.equal(readState(storage, EMAIL).profiles[0].settings.repetitions, 10);
});

test("an update re-reads storage so another tab's profiles are kept", () => {
  const storage = new MemoryStorage();
  const makeId = ids();
  const mine = addProfile(storage, EMAIL, "Mine", settings(), makeId).profile;
  // Another tab adds one and removes none after this page last looked.
  addProfile(storage, EMAIL, "Added elsewhere", settings(), makeId);
  const result = updateProfile(storage, EMAIL, mine.id, settings({ repetitions: 4 }));
  assert.deepEqual(result.state.profiles.map((p) => p.name), ["Mine", "Added elsewhere"]);
  assert.deepEqual(readState(storage, EMAIL).profiles.map((p) => p.name), ["Mine", "Added elsewhere"]);
  assert.equal(readState(storage, EMAIL).profiles[0].settings.repetitions, 4);
});

test("the last selected profile is remembered", () => {
  const storage = new MemoryStorage();
  const makeId = ids();
  const a = addProfile(storage, EMAIL, "A", settings(), makeId).profile;
  addProfile(storage, EMAIL, "B", settings(), makeId);
  assert.equal(selectProfile(storage, EMAIL, a.id).ok, true);
  assert.equal(readState(storage, EMAIL).selected, a.id);
  assert.equal(selectProfile(storage, EMAIL, DEFAULT_ID).ok, true);
  assert.equal(readState(storage, EMAIL).selected, DEFAULT_ID);

  const missing = selectProfile(storage, EMAIL, "gone");
  assert.equal(missing.ok, false);
  assert.equal(missing.found, false);
  assert.match(missing.error, /no longer saved/);
});

test("names are trimmed, limited, unique ignoring case, and never Default", () => {
  const storage = new MemoryStorage();
  const makeId = ids();
  assert.equal(addProfile(storage, EMAIL, "Alpha", settings(), makeId).ok, true);

  for (const [name, pattern] of [
    ["", /Give the profile a name/],
    ["   ", /Give the profile a name/],
    [undefined, /Give the profile a name/],
    ["x".repeat(MAX_NAME + 1), /at most 40/],
    ["alpha", /already exists/],
    ["  ALPHA ", /already exists/],
    ["Default", /built-in/],
    [" default ", /built-in/],
  ]) {
    const result = addProfile(storage, EMAIL, name, settings(), makeId);
    assert.equal(result.ok, false, `expected ${JSON.stringify(name)} to be refused`);
    assert.match(result.error, pattern);
  }

  assert.equal(addProfile(storage, EMAIL, "x".repeat(MAX_NAME), settings(), makeId).ok, true);
  assert.equal(readState(storage, EMAIL).profiles.length, 2);
});

test("at most 30 profiles", () => {
  const storage = new MemoryStorage();
  const makeId = ids();
  for (let i = 0; i < MAX_PROFILES; i++) assert.equal(addProfile(storage, EMAIL, `P${i}`, settings(), makeId).ok, true);
  const extra = addProfile(storage, EMAIL, "One too many", settings(), makeId);
  assert.equal(extra.ok, false);
  assert.match(extra.error, /at most 30/);
  assert.equal(readState(storage, EMAIL).profiles.length, MAX_PROFILES);
});

test("settings that can't be stored are refused", () => {
  const storage = new MemoryStorage();
  for (const bad of [null, "text", { ...settings(), mode: "lukewarm" }, { ...settings(), repetitions: "10" }, { ...settings(), models: "small" }]) {
    assert.equal(addProfile(storage, EMAIL, "Bad", bad, ids()).ok, false);
  }
  assert.deepEqual(readState(storage, EMAIL).profiles, []);
});

test("corrupt or odd storage reads as empty and never throws", () => {
  const key = storageKey(EMAIL);
  for (const stored of ["", "not json", "{", "null", "5", '"text"', "[]", '{"profiles":5}', '{"profiles":{"a":1}}']) {
    const storage = new MemoryStorage();
    storage.setItem(key, stored);
    assert.deepEqual(readState(storage, EMAIL), { profiles: [], selected: DEFAULT_ID }, stored);
  }
  assert.deepEqual(readState(null, EMAIL), { profiles: [], selected: DEFAULT_ID });
  assert.deepEqual(readState(undefined, EMAIL), { profiles: [], selected: DEFAULT_ID });
  const throwing = {
    getItem() {
      throw new Error("blocked");
    },
  };
  assert.deepEqual(readState(throwing, EMAIL), { profiles: [], selected: DEFAULT_ID });
});

test("junk entries are dropped and the good ones kept", () => {
  const storage = new MemoryStorage();
  const good = { id: "g1", name: "Good", settings: settings() };
  storage.setItem(
    storageKey(EMAIL),
    JSON.stringify({
      selected: "g1",
      profiles: [
        null,
        7,
        "x",
        [],
        { id: "", name: "No id", settings: settings() },
        { id: 5, name: "Numeric id", settings: settings() },
        { id: "n1", name: "", settings: settings() },
        { id: "n2", name: "x".repeat(MAX_NAME + 1), settings: settings() },
        { id: DEFAULT_ID, name: "Impostor", settings: settings() },
        { id: "d1", name: "default", settings: settings() },
        { id: "s1", name: "No settings" },
        { id: "s2", name: "Bad mode", settings: { ...settings(), mode: "hot" } },
        { id: "s3", name: "Bad number", settings: { ...settings(), maxEdge: "big" } },
        { id: "s4", name: "Infinite", settings: { ...settings(), maxTokens: null } },
        good,
        { id: "g1", name: "Same id", settings: settings() },
        { id: "g2", name: "GOOD", settings: settings() },
      ],
    }),
  );
  const state = readState(storage, EMAIL);
  assert.deepEqual(state.profiles.map((p) => p.id), ["g1"]);
  assert.equal(state.profiles[0].name, "Good");
  assert.equal(state.selected, "g1");
});

test("a selection that points at nothing falls back to Default, and the list is capped", () => {
  const storage = new MemoryStorage();
  const many = Array.from({ length: MAX_PROFILES + 5 }, (_, i) => ({ id: `p${i}`, name: `P${i}`, settings: settings() }));
  storage.setItem(storageKey(EMAIL), JSON.stringify({ selected: "p-missing", profiles: many }));
  const state = readState(storage, EMAIL);
  assert.equal(state.profiles.length, MAX_PROFILES);
  assert.equal(state.selected, DEFAULT_ID);
});

test("a failed write returns an error and changes nothing", () => {
  const full = new FailingStorage();
  const added = addProfile(full, EMAIL, "A", settings(), ids());
  assert.equal(added.ok, false);
  assert.match(added.error, /storage is full/);
  assert.deepEqual(added.state, { profiles: [], selected: DEFAULT_ID });

  const blocked = new FailingStorage("SecurityError");
  assert.match(addProfile(blocked, EMAIL, "A", settings(), ids()).error, /blocked or unavailable/);

  // No storage at all is also reported, not thrown.
  assert.match(addProfile(null, EMAIL, "A", settings(), ids()).error, /does not allow/);

  // A failed remove or select leaves the stored profile in place.
  const storage = new MemoryStorage();
  const { profile } = addProfile(storage, EMAIL, "A", settings(), ids());
  const failing = Object.assign(new FailingStorage(), { data: storage.data });
  const removed = removeProfile(failing, EMAIL, profile.id);
  assert.equal(removed.ok, false);
  assert.equal(removed.state.profiles.length, 1);
  assert.equal(readState(storage, EMAIL).profiles.length, 1);
  const selected = selectProfile(failing, EMAIL, DEFAULT_ID);
  assert.equal(selected.ok, false);
  assert.equal(selected.found, true);
});

test("a change re-reads storage so another tab's profiles are kept", () => {
  const storage = new MemoryStorage();
  const makeId = ids();
  addProfile(storage, EMAIL, "From tab 1", settings(), makeId);
  // Tab 2 added a profile after tab 1 last looked; tab 1 now adds another.
  const otherTab = addProfile(storage, EMAIL, "From tab 2", settings(), makeId).profile;
  const result = addProfile(storage, EMAIL, "From tab 1 again", settings(), makeId);
  assert.deepEqual(result.state.profiles.map((p) => p.name), ["From tab 1", "From tab 2", "From tab 1 again"]);

  // Tab 1 removing one profile doesn't bring back or drop the others.
  removeProfile(storage, EMAIL, result.profile.id);
  assert.deepEqual(readState(storage, EMAIL).profiles.map((p) => p.id), ["id-1", otherTab.id]);
});

test("each email has its own profiles, whatever its capitalisation", () => {
  const storage = new MemoryStorage();
  const makeId = ids();
  addProfile(storage, "alex@example.com", "Mine", settings(), makeId);
  addProfile(storage, "sam@example.com", "Theirs", settings(), makeId);
  assert.deepEqual(readState(storage, "ALEX@Example.com").profiles.map((p) => p.name), ["Mine"]);
  assert.deepEqual(readState(storage, " sam@example.com ").profiles.map((p) => p.name), ["Theirs"]);
  assert.deepEqual(readState(storage, "nobody@example.com").profiles, []);
  assert.notEqual(storageKey("alex@example.com"), storageKey("sam@example.com"));
  assert.equal(storageKey("Alex@Example.com"), "phonebench.profiles.v1:alex@example.com");
});

test("the built-in Default comes from the server's defaults", () => {
  const builtIn = builtInProfile(options);
  assert.equal(builtIn.id, DEFAULT_ID);
  assert.equal(builtIn.name, "Default");
  assert.deepEqual(builtIn.settings, {
    models: ["small"],
    backends: ["NPU"],
    prompts: ["Describe the image."],
    mode: "warm",
    repetitions: 10,
    restSeconds: 10,
    maxTokens: 2048,
    maxEdge: 768,
  });
  assert.equal(resolveProfile([], DEFAULT_ID, options).id, DEFAULT_ID);
  assert.equal(resolveProfile([], "unknown", options).id, DEFAULT_ID);
  const saved = { id: "x", name: "X", settings: settings() };
  assert.equal(resolveProfile([saved], "x", options), saved);
});

test("cleanSettings clamps a saved profile to the current limits", () => {
  const cleaned = cleanSettings(
    settings({
      repetitions: 500,
      restSeconds: -5,
      maxTokens: 99999,
      maxEdge: 100,
      prompts: ["  a very long prompt that is over the limit  ", "b", "", "c", "d"],
    }),
    options,
  );
  assert.equal(cleaned.repetitions, 30);
  assert.equal(cleaned.restSeconds, 0);
  assert.equal(cleaned.maxTokens, 4096);
  assert.equal(cleaned.maxEdge, 256);
  assert.deepEqual(cleaned.prompts, ["a very long prompt t", "b", "c"]);
});

test("cleanSettings drops models and compute units that are no longer allowed", () => {
  // "gone" is no longer offered; "big" can only run on the NPU.
  const cleaned = cleanSettings(settings({ models: ["small", "gone", "big", "big"], backends: ["CPU", "NPU", "TPU"] }), options);
  assert.deepEqual(cleaned.models, ["small", "big"]);
  assert.deepEqual(cleaned.backends, ["NPU"]);

  const onlySmall = cleanSettings(settings({ models: ["small"], backends: ["CPU", "GPU"] }), options);
  assert.deepEqual(onlySmall.backends, ["CPU", "GPU"]);
});

test("cleanSettings uses the server's defaults for what is missing or unusable", () => {
  const cleaned = cleanSettings({ mode: "hot", repetitions: "many", maxTokens: NaN, prompts: [], models: ["small"], backends: ["NPU"] }, options);
  assert.equal(cleaned.mode, "warm");
  assert.equal(cleaned.repetitions, 10);
  assert.equal(cleaned.maxTokens, 2048);
  assert.equal(cleaned.restSeconds, 10);
  assert.deepEqual(cleaned.prompts, [""]); // the form always shows one prompt box

  assert.deepEqual(cleanSettings(null, options), cleanSettings(builtInProfile(options).settings, options));
  assert.deepEqual(cleanSettings(undefined, options).models, ["small"]);
});

test("applying the built-in Default and comparing it agree", () => {
  const applied = cleanSettings(builtInProfile(options).settings, options);
  assert.equal(sameSettings(applied, builtInProfile(options).settings), true);
});

test("sameSettings ignores order and blank prompts", () => {
  const a = settings({ models: ["small", "big"], backends: ["NPU", "CPU"], prompts: ["one", "two"] });
  const b = settings({ models: ["big", "small"], backends: ["CPU", "NPU"], prompts: ["two", "", "one", "   "] });
  assert.equal(sameSettings(a, b), true);

  assert.equal(sameSettings(a, { ...a, mode: "cold" }), false);
  assert.equal(sameSettings(a, { ...a, repetitions: 11 }), false);
  assert.equal(sameSettings(a, { ...a, restSeconds: 11 }), false);
  assert.equal(sameSettings(a, { ...a, maxTokens: 1 }), false);
  assert.equal(sameSettings(a, { ...a, maxEdge: 1 }), false);
  assert.equal(sameSettings(a, { ...a, models: ["small"] }), false);
  assert.equal(sameSettings(a, { ...a, backends: ["NPU"] }), false);
  assert.equal(sameSettings(a, { ...a, prompts: ["one", "three"] }), false);
  // Having a prompt where there was none is a difference.
  assert.equal(sameSettings({ ...a, prompts: [""] }, { ...a, prompts: ["one"] }), false);
});

test("summarize gives one line using display names", () => {
  const line = summarize(settings({ models: ["small", "unknown-id"], prompts: ["a", "b", ""] }), options.models);
  assert.equal(line.includes("\n"), false);
  assert.match(line, /^Small, unknown-id · NPU · warm · 10 repetitions · 10 s rest · 2048 tokens · 768 px · 2 prompts$/);
  assert.match(summarize(settings({ repetitions: 1, prompts: ["a"] })), /1 repetition · .* 1 prompt$/);
  assert.match(summarize(settings({ models: [], backends: [] })), /^no models · no compute units/);
});
