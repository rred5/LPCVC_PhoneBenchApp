import { useState } from "react";
import { api, download } from "./api.js";
import { usePolling } from "./usePolling.js";
import { isMultiTurn } from "./prompts.js";

const ACTIVE = ["queued", "running"];

export default function Jobs({ me, version }) {
  const [jobs, setJobs] = useState(null);
  const [error, setError] = useState(null);
  const [selected, setSelected] = useState(null);

  usePolling(
    8_000,
    async () => {
      const { jobs: loaded } = await api("/api/v1/jobs");
      setJobs(loaded);
      setError(null);
    },
    [version],
    (failure) => setError(failure.message),
  );

  const cancel = async (job) => {
    if (!window.confirm(`Cancel "${job.name}"? Finished runs are kept.`)) return;
    try {
      const updated = await api(`/api/v1/jobs/${job.job_id}/cancel`, { method: "POST" });
      setJobs((current) => current.map((item) => (item.job_id === job.job_id ? updated : item)));
    } catch (failure) {
      setError(failure.message);
    }
  };

  return (
    <section className="card">
      <h2>Jobs</h2>
      {error && <p className="error">{error}</p>}
      {jobs === null ? (
        <p className="muted">Loading…</p>
      ) : jobs.length === 0 ? (
        <p className="muted">No jobs yet.</p>
      ) : (
        <div className="table-wrap">
          <table className="jobs">
            <thead>
              <tr>
                <th>Job</th>
                <th>Status</th>
                <th>Progress</th>
                <th>Models and units</th>
                <th>By</th>
                <th>Submitted</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {jobs.map((job) => {
                const finished = job.runs.completed + job.runs.error;
                const canCancel = ACTIVE.includes(job.status) && (job.submitted_by === me.email || me.is_admin);
                return (
                  <tr key={job.job_id} className={selected === job.job_id ? "selected" : ""}>
                    <td>
                      <button className="link" onClick={() => setSelected(selected === job.job_id ? null : job.job_id)}>
                        {job.name}
                      </button>
                    </td>
                    <td>
                      <span className={`badge ${job.status}`}>{job.status}</span>
                    </td>
                    <td>
                      <div className="bar" title={`${job.runs.completed} passed, ${job.runs.error} failed`}>
                        <span style={{ width: `${job.runs.total ? (100 * finished) / job.runs.total : 0}%` }} />
                      </div>
                      <span className="small muted">
                        {finished}/{job.runs.total}
                        {job.runs.error > 0 && ` · ${job.runs.error} failed`}
                      </span>
                    </td>
                    <td className="small">
                      {job.models.map(shortModel).join(", ")} · {job.backends.join(", ")}
                    </td>
                    <td className="small muted">{job.submitted_by}</td>
                    <td className="small muted">{when(job.created_at)}</td>
                    <td>
                      {canCancel && (
                        <button className="link danger" onClick={() => cancel(job)}>
                          Cancel
                        </button>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
      {selected && <JobDetail key={selected} jobId={selected} active={ACTIVE.includes(jobs?.find((job) => job.job_id === selected)?.status)} />}
    </section>
  );
}

function JobDetail({ jobId, active }) {
  const [job, setJob] = useState(null);
  const [runs, setRuns] = useState(null);
  const [open, setOpen] = useState(null);
  const [error, setError] = useState(null);

  usePolling(
    active ? 8_000 : 600_000,
    async () => {
      const [detail, list] = await Promise.all([api(`/api/v1/jobs/${jobId}`), api(`/api/v1/jobs/${jobId}/runs`)]);
      setJob(detail);
      setRuns(list.runs);
    },
    [jobId, active],
    (failure) => setError(failure.message),
  );

  const save = (name) => download(`/api/v1/jobs/${jobId}/outputs/${name}`, `${jobId}-${name}`).catch((failure) => setError(failure.message));

  if (error) return <p className="error">{error}</p>;
  if (!job || !runs) return <p className="muted">Loading job…</p>;
  const run = job.settings.run;
  const labeled = runs.some((row) => row.label);
  return (
    <div className="detail">
      <h3>{job.name}</h3>
      <p className="small muted">
        {run.mode} mode · {run.repetitions} repetition(s) · {run.rest_seconds} s rest · max {run.max_tokens} tokens · images at{" "}
        {job.settings.preprocessing.max_edge} px · {run.prompts.length} prompt(s)
        {job.error && <span className="error"> · {job.error}</span>}
      </p>
      <div className="row">
        <span className="muted small">Download:</span>
        <button className="link" onClick={() => save("runs.csv")}>runs.csv</button>
        {run.prompts.some(isMultiTurn) && <button className="link" onClick={() => save("turns.csv")}>turns.csv</button>}
        <button className="link" onClick={() => save("summary.json")}>summary.json</button>
        {labeled && <button className="link" onClick={() => save("accuracy.json")}>accuracy.json</button>}
        <button className="link" onClick={() => save("outputs.zip")}>everything (.zip)</button>
      </div>
      <div className="table-wrap">
        <table className="runs">
          <thead>
            <tr>
              <th>#</th>
              <th>Model</th>
              <th>Unit</th>
              <th>Image</th>
              <th>Rep</th>
              <th>Status</th>
              <th title="Time to first token">First token</th>
              <th>Decode</th>
              <th>Tokens</th>
              {labeled && <th>Verdict</th>}
              <th>Response</th>
            </tr>
          </thead>
          <tbody>
            {runs.map((row) => (
              <tr key={row.run_id}>
                <td className="muted">{row.sequence}</td>
                <td className="small">{shortModel(row.model)}</td>
                <td>{row.backend}</td>
                <td className="small" title={row.image}>
                  {row.image}
                  {row.label && <span className="muted"> ({row.label})</span>}
                </td>
                <td>{row.repetition}</td>
                <td>
                  <span className={`badge ${row.status}`}>{row.status}</span>
                </td>
                <td className="nowrap">{row.ttft_ms == null ? "" : `${(row.ttft_ms / 1000).toFixed(1)} s`}</td>
                <td className="nowrap">{row.decode_tok_s == null ? "" : `${row.decode_tok_s.toFixed(1)} tok/s`}</td>
                <td className="small nowrap">{row.prompt_tokens == null ? "" : `${row.prompt_tokens} + ${row.generated_tokens}`}</td>
                {labeled && (
                  <td className="small">
                    {row.diagnostic_label ?? ""}
                    {row.diagnostic_correct === true && " ✓"}
                    {row.diagnostic_correct === false && " ✗"}
                  </td>
                )}
                <td className="response">
                  {row.error ? (
                    <span className="error small">{row.error}</span>
                  ) : row.response ? (
                    <button className="link small" onClick={() => setOpen(open === row.run_id ? null : row.run_id)}>
                      {open === row.run_id ? row.response : `${row.response.slice(0, 80)}${row.response.length > 80 ? "…" : ""}`}
                    </button>
                  ) : null}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function shortModel(id) {
  return id.replace(/-Instruct/, "").replace(/-GGUF$/, "");
}

function when(seconds) {
  return new Date(seconds * 1000).toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
}
