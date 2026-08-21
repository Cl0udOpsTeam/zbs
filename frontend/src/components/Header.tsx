import { fmtInterval, humanizeSeconds } from "../format";
import type { AppConfig } from "../types";

export function Header({ config }: { config: AppConfig | null }) {
  const chips: Array<[string, string]> = config
    ? [
        [
          "ZooKeeper",
          `${config.zk_hosts} @ ${config.zk_root}${config.zk_auth_enabled ? " (auth)" : ""}`,
        ],
        ["S3", `${config.s3_endpoint} \u00b7 ${config.s3_bucket}/${config.s3_prefix}`],
        ["Schedule", `every ${fmtInterval(config.backup_interval_seconds)}`],
        [
          "Retention",
          config.retention?.enabled
            ? `keep ${humanizeSeconds(config.retention.max_age_seconds)}, sweep ${fmtInterval(config.retention.interval_seconds)}`
            : "off",
        ],
      ]
    : [];

  return (
    <header>
      <div className="brand">
        <div className="logo">Z</div>
        <div>
          <h1>ZBS</h1>
          <p>ZooKeeper Backup System</p>
        </div>
      </div>
      <div className="chips">
        {chips.map(([name, value]) => (
          <span key={name} className="chip">
            <b>{name}</b>
            {value}
          </span>
        ))}
      </div>
    </header>
  );
}
