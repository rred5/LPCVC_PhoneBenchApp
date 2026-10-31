import { useEffect, useMemo, useState } from "react";
import { api } from "./api.js";
import FileTree from "./FileTree.jsx";
import { buildTree, chosenLabel, describeCounts, isWithin, pruneTags, withTag } from "./fileTree.js";
import { ProfileBar, useProfiles } from "./Profiles.jsx";
import { BACKENDS, cleanSettings, resolveProfile } from "./profiles.js";
import { DEFAULT_MAX_TURNS, tidyPrompts, turnsOf } from "./prompts.js";
import { filePath, isImage, uploadImages } from "./upload.js";

const JOB_NAME = /^[\w .()-]{1,80}$/;
const MB = 1024 * 1024;

export default function NewJob({ user, onSubmitted }) {
  const store = useProfiles(user.email);
  const [options, setOptions] = useState(null);
  const [form, setForm] = useState(null);
  const [files, setFiles] = useState([]);
  const [tags, setTags] = useState({});
  const [paneOpen, setPaneOpen] = useState(false);
  const [skipped, setSkipped] = useState(0);
  const [busy, setBusy] = useState(false);
  const [progress, setProgress] = useState(null);
  const [message, setMessage] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    api("/api/v1/options")
      .then((loaded) => {
        // Start from the profile chosen last time, or the server's defaults. This
        // runs once, so `store` is what it was when the page opened.
        const start = resolveProfile(store.profiles, store.selectedId, loaded);
        setOptions(loaded);
        setForm({ name: "", ...cleanSettings(start.settings, loaded) });
      })
      .catch((failure) => setError(`Could not load the job options: ${failure.message}`));
  }, []);

  const allowedBackends = useMemo(() => {
    if (!options || !form) return BACKENDS;
    const chosen = options.models.filter((model) => form.models.includes(model.id));
    return BACKENDS.filter((backend) => chosen.every((model) => model.backends.includes(backend)));
  }, [options, form]);

  const tree = useMemo(() => buildTree(files.map(filePath), tags), [files, tags]);

  if (!form) {
    return <section className="card">{error ? <p className="error">{error}</p> : <p className="muted">Loading…</p>}</section>;
  }
  const limits = options.limits;
  const set = (key, value) => setForm((current) => ({ ...current, [key]: value }));
  const apply = (settings) => setForm((current) => ({ ...current, ...settings }));
  const toggle = (key, value) =>
    set(key, form[key].includes(value) ? form[key].filter((item) => item !== value) : [...form[key], value]);
  const chooseModels = (id) => {
    const models = form.models.includes(id) ? form.models.filter((item) => item !== id) : [...form.models, id];
    const chosen = options.models.filter((model) => models.includes(model.id));
    setForm((current) => ({
      ...current,
      models,
      backends: current.backends.filter((backend) => chosen.every((model) => model.backends.includes(backend))),
    }));
  };

  const pick = (event) => {
    const all = Array.from(event.target.files ?? []);
    const images = all.filter(isImage).sort((a, b) => filePath(a).localeCompare(filePath(b)));
    setFiles(images);
    setTags({});
    setSkipped(all.length - images.length);
    setMessage(null);
    setError(null);
    event.target.value = "";
  };
  // `key` is an image's path, or a folder's path with a trailing "/".
  const remove = (key) => {
    const kept = files.filter((file) => !isWithin(key, filePath(file)));
    setFiles(kept);
    setTags(pruneTags(kept.map(filePath), tags));
  };
  const tag = (key, value) => setTags(withTag(tags, key, value));
  const tooBig = files.filter((file) => file.size > limits.max_upload_bytes);
  const prompts = tidyPrompts(form.prompts);
  const maxTurns = limits.max_turns ?? DEFAULT_MAX_TURNS;
  // Replace prompt `index`'s turns; a prompt left with one turn goes back to plain text.
  const setTurns = (index, turns) =>
    set("prompts", form.prompts.map((item, i) => (i === index ? (turns.length === 1 ? turns[0] : turns) : item)));
  const runs = form.models.length * form.backends.length * files.length * prompts.length * form.repetitions;

  const problems = [];
  if (!JOB_NAME.test(form.name)) problems.push("Give the job a name (letters, digits, spaces, . - _ ( ))");
  if (!form.models.length) problems.push("Choose at least one model");
  if (!form.backends.length) problems.push("Choose at least one compute unit");
  if (!prompts.length) problems.push("Write a prompt");
  if (!files.length) problems.push("Choose a folder or files");
  if (files.length > limits.max_images) problems.push(`At most ${limits.max_images} images per job`);
  if (tooBig.length) problems.push(`${tooBig.length} image(s) are over ${limits.max_upload_bytes / MB} MB`);
  if (runs > limits.max_runs_per_job) problems.push(`${runs} runs; the limit is ${limits.max_runs_per_job}`);

  const submit = async (event) => {
    event.preventDefault();
    if (problems.length || busy) return;
    setBusy(true);
    setError(null);
    setMessage(null);
    try {
      setProgress({ done: 0, total: files.length });
      const batch = Math.min(limits.max_unfinished_uploads, 20);
      const { accepted, rejected } = await uploadImages(
        files,
        form.maxEdge,
        batch,
        (done, total) => setProgress({ done, total }),
        (file) => chosenLabel(filePath(file), tags),
      );
      if (!accepted.length) throw new Error(`No images were accepted. ${describe(rejected)}`);
      const job = await api("/api/v1/jobs", {
        method: "POST",
        body: {
          name: form.name.trim(),
          settings: {
            run: {
              models: form.models,
              backends: form.backends,
              prompts,
              images: accepted.map((upload) => upload.upload_id),
              mode: form.mode,
              repetitions: form.repetitions,
              rest_seconds: form.restSeconds,
              max_tokens: form.maxTokens,
            },
            preprocessing: { max_edge: form.maxEdge },
          },
        },
      });
      setMessage(
        `Submitted "${job.name}": ${job.runs.total} runs, about ${minutes(job.estimated_seconds)}.` +
          (rejected.length ? ` Skipped ${rejected.length}: ${describe(rejected)}` : ""),
      );
      setFiles([]);
      setTags({});
      setSkipped(0);
      onSubmitted();
    } catch (failure) {
      setError(failure.message);
    } finally {
      setBusy(false);
      setProgress(null);
    }
  };

  return (
    <div className="new-job">
      <ProfileBar store={store} options={options} form={form} onApply={apply} />
      <section className="card">
        <h2>New job</h2>
        <form onSubmit={submit} className="job-form">
          <label className="wide">
            Name
            <input value={form.name} onChange={(e) => set("name", e.target.value)} placeholder="e.g. 2B vs 4B on NPU" maxLength={80} />
          </label>

          <fieldset className="wide">
            <legend>Images</legend>
            <div className="row">
              <label className="button">
                Choose folder
                <input type="file" webkitdirectory="" multiple onChange={pick} hidden />
              </label>
              <label className="button">
                Choose files
                <input type="file" accept=".png,.jpg,.jpeg,.webp" multiple onChange={pick} hidden />
              </label>
              {files.length > 0 && (
                <button type="button" aria-expanded={paneOpen} onClick={() => setPaneOpen(!paneOpen)}>
                  {paneOpen ? "Hide files" : "Review files"}
                </button>
              )}
              <span className="muted">
                {files.length
                  ? `${files.length} image(s): ${describeCounts(tree.labels)}`
                  : "Subfolders named real, synthetic or tampered label their images for accuracy scoring. Once chosen, you can tag or remove folders and images."}
                {skipped > 0 && ` · ${skipped} other file(s) skipped`}
              </span>
            </div>
            {paneOpen && files.length > 0 && <FileTree tree={tree} onTag={tag} onRemove={remove} />}
          </fieldset>

          <fieldset>
            <legend>Models</legend>
            {options.models.map((model) => (
              <label key={model.id} className="check">
                <input type="checkbox" checked={form.models.includes(model.id)} onChange={() => chooseModels(model.id)} />
                {model.display_name} <span className="muted">({model.backends.join(", ")})</span>
              </label>
            ))}
          </fieldset>

          <fieldset>
            <legend>Compute units</legend>
            {BACKENDS.map((backend) => (
              <label key={backend} className="check">
                <input
                  type="checkbox"
                  checked={form.backends.includes(backend)}
                  disabled={!allowedBackends.includes(backend)}
                  onChange={() => toggle("backends", backend)}
                />
                {backend}
              </label>
            ))}
            <label>
              Mode
              <select value={form.mode} onChange={(e) => set("mode", e.target.value)}>
                <option value="warm">Warm: load once, one warm-up</option>
                <option value="cold">Cold: load and unload every run</option>
              </select>
            </label>
          </fieldset>

          <fieldset className="wide">
            <legend>Prompts</legend>
            {form.prompts.map((prompt, index) => {
              const turns = turnsOf(prompt);
              return (
                <div key={index} className="prompt-group">
                  {turns.map((turn, number) => (
                    <div key={number} className="prompt">
                      {turns.length > 1 && <span className="small muted">Turn {number + 1}</span>}
                      <textarea
                        rows={2}
                        value={turn}
                        maxLength={limits.max_prompt_chars}
                        onChange={(e) => setTurns(index, turns.map((item, t) => (t === number ? e.target.value : item)))}
                      />
                      {number > 0 && (
                        <button type="button" className="link" onClick={() => setTurns(index, turns.filter((_, t) => t !== number))}>
                          Remove turn
                        </button>
                      )}
                      {number === 0 && form.prompts.length > 1 && (
                        <button type="button" className="link" onClick={() => set("prompts", form.prompts.filter((_, i) => i !== index))}>
                          Remove
                        </button>
                      )}
                    </div>
                  ))}
                  {turns.length < maxTurns && (
                    <button type="button" className="link small" onClick={() => setTurns(index, [...turns, ""])}>
                      + Add a follow-up turn (same conversation; {"{width}"} and {"{height}"} become the image size)
                    </button>
                  )}
                </div>
              );
            })}
            {form.prompts.length < limits.max_prompts && (
              <button type="button" className="link" onClick={() => set("prompts", [...form.prompts, ""])}>
                + Add a prompt to compare
              </button>
            )}
          </fieldset>

          <fieldset className="wide numbers">
            <legend>Settings</legend>
            <NumberField label="Repetitions" value={form.repetitions} range={limits.repetitions} onChange={(v) => set("repetitions", v)} />
            <NumberField label="Rest between runs (s)" value={form.restSeconds} range={limits.rest_seconds} onChange={(v) => set("restSeconds", v)} />
            <NumberField label="Max tokens" value={form.maxTokens} range={limits.max_tokens} onChange={(v) => set("maxTokens", v)} />
            <NumberField label="Image long edge (px)" value={form.maxEdge} range={limits.max_edge} onChange={(v) => set("maxEdge", v)} />
          </fieldset>

          <div className="wide submit-row">
            <button className="primary" type="submit" disabled={busy || problems.length > 0}>
              {busy ? "Submitting…" : "Submit job"}
            </button>
            <span className="muted">
              {progress
                ? `Uploading and preparing images: ${progress.done} of ${progress.total}`
                : problems.length
                  ? problems[0]
                  : `${runs} runs`}
            </span>
          </div>
          {message && <p className="wide ok">{message}</p>}
          {error && <p className="wide error">{error}</p>}
        </form>
      </section>
    </div>
  );
}

function NumberField({ label, value, range, onChange }) {
  const [low, high] = range;
  return (
    <label>
      {label}
      <input
        type="number"
        min={low}
        max={high}
        value={value}
        onChange={(e) => {
          const number = Number(e.target.value);
          if (Number.isFinite(number)) onChange(Math.min(high, Math.max(low, Math.round(number))));
        }}
      />
      <span className="muted small">
        {low}–{high}
      </span>
    </label>
  );
}

function minutes(seconds) {
  const total = Math.round(seconds / 60);
  return total < 60 ? `${total} min` : `${Math.floor(total / 60)} h ${total % 60} min`;
}

function describe(rejected) {
  return rejected
    .slice(0, 3)
    .map((item) => `${item.name} (${item.reason})`)
    .join("; ") + (rejected.length > 3 ? `; and ${rejected.length - 3} more` : "");
}
