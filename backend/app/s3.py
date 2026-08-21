"""S3 storage backend for backup artifacts (AWS S3 or S3-compatible endpoints).

Every boto call is translated into the ZBS error taxonomy so callers can
distinguish "endpoint unreachable" from "bad credentials" from "no such
backup", and every operation is logged at a level that tells the story.
"""

import logging
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


def validate_key(key: str) -> None:
    """Refuse keys outside the configured prefix (defense against misuse)."""
    if not key or not key.startswith(settings.s3_prefix):
        raise ValueError(f"backup key must live under prefix {settings.s3_prefix!r}")


def new_backup_key() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{settings.s3_prefix}zbs-{stamp}.json.gz"


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


def list_backups() -> list[dict]:
    items: list[dict] = []
    pages = 0
    with _translate("list"):
        paginator = client().get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=settings.s3_bucket, Prefix=settings.s3_prefix):
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
    log.debug("listed %d backup(s) under %s (%d page(s))", len(items), settings.s3_prefix, pages)
    return items


def download_backup(key: str) -> bytes:
    validate_key(key)
    with _translate(f"download of {key!r}"):
        body = client().get_object(Bucket=settings.s3_bucket, Key=key)["Body"].read()
    log.info("downloaded s3://%s/%s (%d bytes)", settings.s3_bucket, key, len(body))
    return body


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
