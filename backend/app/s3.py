"""S3 storage backend for backup artifacts (AWS S3 or S3-compatible endpoints).

Multi-folder layout: each ZooKeeper cluster's backups live under a top-level
"folder" prefix, conventionally named ENVIRONMENT-NAMESPACE-ZOOKEEPER_NAME
(e.g. ``dev-test-my-zookeeper``), producing keys like::

    dev-test-my-zookeeper/zbs-20240501T120000Z.json.gz

Folders are auto-discovered via delimiter listings at the bucket root and can
be constrained with ZBS_S3_FOLDERS. This instance uploads to its own cluster
folder (ZBS_CLUSTER_FOLDER); legacy deployments without it keep writing to the
old ZBS_S3_PREFIX (default zbs/) which simply shows up as one more folder.

Every boto call is translated into the ZBS error taxonomy so callers can
distinguish "endpoint unreachable" from "bad credentials" from "no such
backup", and every operation is logged at a level that tells the story.
"""

import logging
import re
from contextlib import contextmanager
from datetime import datetime, timezone

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import (
    BotoCoreError,
    ClientError,
    ConnectionClosedError as BotoConnectionClosed,
    EndpointConnectionError,
    NoCredentialsError,
    PartialCredentialsError,
)

from . import errors
from .config import settings

log = logging.getLogger("zbs.s3")

_client = None

# Any single-segment prefix is acceptable when folders are auto-discovered;
# explicit configuration narrows this further.
_LOOSE_FOLDER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$")


def client():
    global _client
    if _client is None:
        if not settings.s3_bucket:
            raise errors.ConfigurationError("ZBS_S3_BUCKET is not configured")
        log.debug(
            "creating s3 client (endpoint=%s, region=%s)",
            settings.s3_endpoint or "AWS default",
            settings.s3_region,
        )
        session = boto3.session.Session(
            aws_access_key_id=settings.s3_access_key_id,
            aws_secret_access_key=settings.s3_secret_access_key,
            region_name=settings.s3_region,
        )
        _client = session.client(
            "s3",
            endpoint_url=settings.s3_endpoint,
            config=BotoConfig(
                retries={"max_attempts": 5, "mode": "standard"},
                connect_timeout=10,
                read_timeout=120,
            ),
        )
    return _client


def reset_client() -> None:
    global _client
    _client = None


@contextmanager
def _translate(operation: str):
    """Map botocore failures onto the ZBS taxonomy with useful context."""
    try:
        yield
    except errors.ZbsError:
        raise
    except EndpointConnectionError as exc:
        raise errors.S3UnavailableError(
            f"cannot reach S3 endpoint ({settings.s3_endpoint or 'AWS'}) during {operation}: {exc}"
        ) from exc
    except (BotoConnectionClosed,) as exc:
        raise errors.S3UnavailableError(f"S3 connection dropped during {operation}: {exc}") from exc
    except (NoCredentialsError, PartialCredentialsError) as exc:
        raise errors.ConfigurationError(f"S3 credentials missing/incomplete during {operation}: {exc}") from exc
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        message = exc.response.get("Error", {}).get("Message", str(exc))
        if code in ("NoSuchKey", "404"):
            raise errors.BackupNotFoundError(f"backup not found in bucket during {operation}") from exc
        if code == "NoSuchBucket":
            raise errors.ConfigurationError(f"bucket {settings.s3_bucket!r} does not exist") from exc
        if code in ("AccessDenied", "403", "InvalidAccessKeyId", "SignatureDoesNotMatch"):
            raise errors.ConfigurationError(
                f"S3 access denied during {operation} (check credentials/policy): {code}"
            ) from exc
        raise errors.S3Error(f"S3 {operation} failed [{code or 'unknown'}]: {message}") from exc
    except BotoCoreError as exc:
        raise errors.S3UnavailableError(f"S3 client error during {operation}: {exc}") from exc


# --------------------------------------------------------------------------
# folders
# --------------------------------------------------------------------------

def parse_folder_name(name: str) -> dict | None:
    """Split ENVIRONMENT-NAMESPACE-ZOOKEEPER_NAME into parts (None if not matching)."""
    parts = name.split("-", 2)
    if len(parts) < 3 or not all(parts):
        return None
    return {"environment": parts[0], "namespace": parts[1], "zkName": parts[2]}


def discover_folders() -> list[dict]:
    """List cluster folders at the bucket root.

    Returns one entry per folder: name, parsed env/ns/zk parts, and whether it
    is this instance's backup target. Explicit ZBS_S3_FOLDERS always appear
    (even when still empty in S3); with no allow-list, everything discovered
    at the root is shown - including legacy prefixes like `zbs`.
    """
    discovered: set[str] = set()
    with _translate("folder discovery"):
        paginator = client().get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=settings.s3_bucket, Delimiter="/"):
            for prefix_entry in page.get("CommonPrefixes", []):
                name = (prefix_entry.get("Prefix") or "").rstrip("/")
                if name and settings._FOLDER_NAME_RE.match(name) and ".." not in name:
                    discovered.add(name)

    known = set(settings.s3_folders) | ({settings.cluster_folder} if settings.cluster_folder else set())
    merged = sorted(discovered | known)
    target = settings.backup_target_folder
    folders = []
    for name in merged:
        info = parse_folder_name(name) or {}
        folders.append(
            {
                "name": name,
                "environment": info.get("environment"),
                "namespace": info.get("namespace"),
                "zkName": info.get("zkName"),
                "isBackupTarget": name == target,
            }
        )
    log.debug(
        "folder discovery: %d folder(s) (%d discovered, %d configured)",
        len(folders),
        len(discovered),
        len(settings.s3_folders),
    )
    return folders


def validate_folder(folder: str) -> None:
    """Reject malformed/unknown folder query parameters early."""
    if not folder or ".." in folder or not _LOOSE_FOLDER_RE.match(folder):
        raise ValueError(f"invalid folder name: {folder!r}")


def folder_prefix(folder: str | None = None) -> str:
    """Effective key prefix for a folder (defaults to the backup target)."""
    return f"{folder or settings.backup_target_folder}/"


def _allowed_prefixes() -> list[str]:
    """Static prefixes a restore key may live under (sync, no network)."""
    prefixes = [f"{name}/" for name in settings.s3_folders]
    prefixes.append(f"{settings.backup_target_folder}/")
    prefixes.append(settings.s3_prefix)
    return sorted(set(prefixes))


# --------------------------------------------------------------------------
# keys & objects
# --------------------------------------------------------------------------

def validate_key(key: str) -> None:
    """Refuse keys outside any managed folder/prefix (defense against misuse)."""
    if not key:
        raise ValueError("backup key must not be empty")
    if not key.endswith(".json.gz") or key.endswith("/"):
        raise ValueError(f"key {key!r} is not a ZBS backup artifact")
    allowed = _allowed_prefixes()
    # When folders are auto-discovered (no allow-list), accept well-formed
    # single-segment prefixes so newly dropped-in clusters are usable.
    if not settings.s3_folders:
        first, sep, _rest = key.partition("/")
        if sep and _LOOSE_FOLDER_RE.match(first):
            return
    if not any(key.startswith(p) for p in allowed):
        raise ValueError(f"backup key must live under one of the managed prefixes {allowed}")


def new_backup_key() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{folder_prefix()}zbs-{stamp}.json.gz"


def upload_backup(payload: bytes, key: str | None = None) -> str:
    key = key or new_backup_key()
    with _translate(f"upload of {key!r}"):
        client().put_object(
            Bucket=settings.s3_bucket, Key=key, Body=payload, ContentType="application/gzip"
        )
    log.info(
        "uploaded s3://%s/%s (%d bytes compressed)", settings.s3_bucket, key, len(payload)
    )
    return key


def list_backups(folder: str | None = None) -> list[dict]:
    """List backup artifacts under one folder's prefix (newest first)."""
    if folder is not None:
        validate_folder(folder)
    prefix = folder_prefix(folder)
    items: list[dict] = []
    pages = 0
    with _translate(f"list of {prefix!r}"):
        paginator = client().get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=settings.s3_bucket, Prefix=prefix):
            pages += 1
            for obj in page.get("Contents", []):
                if not obj["Key"].endswith(".json.gz"):
                    continue
                items.append(
                    {
                        "key": obj["Key"],
                        "size": obj["Size"],
                        "last_modified": obj["LastModified"]
                        .astimezone(timezone.utc)
                        .isoformat(),
                    }
                )
    items.sort(key=lambda item: item["last_modified"], reverse=True)
    log.debug("listed %d backup(s) under %s (%d page(s))", len(items), prefix, pages)
    return items


def download_backup(key: str) -> bytes:
    validate_key(key)
    limit = settings.restore_max_bytes
    with _translate(f"download of {key!r}"):
        body = client().get_object(Bucket=settings.s3_bucket, Key=key)["Body"]
        try:
            if limit <= 0:
                payload = body.read()
            else:
                # Stream with a hard ceiling so a huge object cannot exhaust
                # memory. The small allowance covers gzip framing overhead
                # (a backup at exactly the uncompressed cap may compress to
                # slightly more than the cap).
                ceiling = limit + (1 << 16)
                chunks: list[bytes] = []
                total = 0
                while True:
                    chunk = body.read(1 << 20)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > ceiling:
                        raise errors.BackupValidationError(
                            f"backup exceeds maximum size ({limit} bytes); refusing download"
                        )
                    chunks.append(chunk)
                payload = b"".join(chunks)
        finally:
            body.close()
    log.info("downloaded s3://%s/%s (%d bytes)", settings.s3_bucket, key, len(payload))
    return payload


def delete_backups(keys: list[str]) -> list[str]:
    """Batch-delete backup objects; returns the keys that were deleted."""
    for key in keys:
        validate_key(key)
    if not keys:
        return []
    deleted: list[str] = []
    for start in range(0, len(keys), 1000):
        chunk = keys[start : start + 1000]
        with _translate(f"delete of {len(chunk)} object(s)"):
            response = client().delete_objects(
                Bucket=settings.s3_bucket,
                Delete={"Objects": [{"Key": key} for key in chunk], "Quiet": True},
            )
        failed = {error["Key"] for error in response.get("Errors", [])}
        ok = [key for key in chunk if key not in failed]
        deleted.extend(ok)
        if failed:
            for key in sorted(failed):
                log.warning("S3 refused to delete %s", key)
    if deleted:
        log.info("deleted %d backup(s) from s3://%s", len(deleted), settings.s3_bucket)
        for key in deleted:
            log.debug("deleted %s", key)
    return deleted


def check_connection() -> None:
    with _translate("connectivity check"):
        client().head_bucket(Bucket=settings.s3_bucket)
