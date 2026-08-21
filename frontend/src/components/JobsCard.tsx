import { fmtDuration, fmtWhen, summarizeJob } from "../format";
import type { Job } from "../types";

export function JobsCard({ jobs }: { jobs: Job[] | null }) {
  return (
    <section className="card">
      <h2 style={{ marginBottom: 12 }}>Recent jobs</h2>
      <ul className="jobs">
        {jobs === null ? (
          <li className="muted">Loading&hellip;</li>
        ) : jobs.length === 0 ? (
          <li className="muted">No jobs yet.</li>
        ) : (
          jobs.map((job) => (
            <JobRow key={job.id} job={job} />
          ))
        )}
      </ul>
    </section>
  );
}

function JobRow({ job }: { job: Job }) {
  return (
    <li className={`job ${job.status}`}>
      <span className="pill">{job.status}</span>
      <span className="kind">{job.kind}</span>
      <span className="desc mono">{job.description}</span>
      <span className="meta">
        {fmtWhen(job.started_at)}
        {job.finished_at ? ` \u00b7 ${fmtDuration(job)}` : ""}
      </span>
      {job.status === "success" && job.result && (
        <span className="result">{summarizeJob(job)}</span>
      )}
      {job.error && <span className="err">{job.error}</span>}
    </li>
  );
}
