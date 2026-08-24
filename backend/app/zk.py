"""Dump and restore the ZooKeeper znode tree.

Backup artifacts are gzipped JSON envelopes carrying a SHA-256 checksum of
the embedded document:

{
  "format": "zbs-backup-v1",
  "checksum": "<sha256 of canonical JSON of 'document'>",
  "document": {
    "format": "zbs-backup-v1",
    "created_at": "...", "source_zk": "...", "source_root": "/",
    "tree": { "path": "/", "data_b64": "", "ephemeral": false,
              "acls": [...], "children": [ ... recursive ... ] }
  }
}

Restore safety contract: deserialize() verifies gzip + JSON + checksum, and
restore_tree() runs validate_document() (structure, paths, base64, ACLs,
depth/node caps) BEFORE touching ZooKeeper. A corrupted or malformed backup
never mutates the cluster.

Notes:
- /zookeeper is reserved by ZooKeeper and is never dumped, wiped or restored.
- Ephemeral nodes are recorded but restored as persistent nodes.
- Walks are iterative so pathological tree depth cannot exhaust the stack.
"""

import base64
import binascii
import gzip
import hashlib
import io
import json
import logging
import posixpath
import time
from datetime import datetime, timezone

from kazoo.client import KazooClient
from kazoo.exceptions import (
    AuthFailedError,
    ConnectionClosedError,
    ConnectionLoss,
    NoAuthError,
    NoNodeError,
    SessionExpiredError,
)
from kazoo.security import ACL, Id

from . import errors
from .config import settings

log = logging.getLogger("zbs.zk")

BACKUP_FORMAT = "zbs-backup-v1"
PROTECTED_PATHS = ("/zookeeper",)

_CONNECT_ERRORS = (TimeoutError, ConnectionLoss, SessionExpiredError, ConnectionClosedError)
_AUTH_ERRORS = (AuthFailedError, NoAuthError)


# --------------------------------------------------------------------------
# connection helpers
# --------------------------------------------------------------------------

def connect() -> KazooClient:
    """Open a session, authenticating with digest credentials when configured."""
    log.debug(
        "connecting to zookeeper at %s (session timeout %ss)",
        settings.zk_hosts,
        settings.zk_session_timeout,
    )
    zk = KazooClient(hosts=settings.zk_hosts, timeout=settings.zk_session_timeout)
    try:
        zk.start(timeout=settings.zk_session_timeout)
    except _CONNECT_ERRORS as exc:
        raise errors.ZooKeeperUnavailableError(
            f"cannot reach ZooKeeper at {settings.zk_hosts}: {exc}"
        ) from exc
    log.info("connected to zookeeper at %s", settings.zk_hosts)

    if settings.zk_auth_enabled:
        log.debug("sending digest credentials for user %r", settings.zk_username)
        event = zk.add_auth("digest", f"{settings.zk_username}:{settings.zk_password}")
        wait = getattr(event, "wait", None)
        try:
            if wait is not None and not wait(timeout=10):
                raise TimeoutError("authentication acknowledgement timed out")
        except _AUTH_ERRORS as exc:
            close(zk)
            raise errors.ZooKeeperError(
                f"ZooKeeper rejected credentials for user {settings.zk_username!r}: {exc}"
            ) from exc
        log.info("zookeeper digest auth applied for user %r", settings.zk_username)
    return zk


def close(zk: KazooClient) -> None:
    """Close a session without ever raising."""
    try:
        zk.stop()
        zk.close()
        log.debug("zookeeper session closed")
    except Exception:  # noqa: BLE001 - closing must not mask real errors
        log.warning("error while closing zookeeper session", exc_info=True)


def _translate_zk_op(path: str, operation: str):
    """Wrap per-node kazoo failures with path/operation context."""
    def wrap(exc: Exception) -> errors.ZooKeeperError:
        if isinstance(exc, NoAuthError):
            return errors.ZooKeeperError(
                f"{operation} on {path} denied (insufficient permissions or missing auth)"
            )
        return errors.ZooKeeperError(f"{operation} failed at {path}: {exc}")
    return wrap


# --------------------------------------------------------------------------
# path helpers
# --------------------------------------------------------------------------

def _join(parent: str, child: str) -> str:
    return f"/{child}" if parent == "/" else f"{parent}/{child}"


def _is_protected(path: str) -> bool:
    return any(path == p or path.startswith(p + "/") for p in PROTECTED_PATHS)


def _assert_not_protected(path: str) -> None:
    if _is_protected(path):
        raise ValueError(f"{path} is reserved by ZooKeeper and cannot be used")


# --------------------------------------------------------------------------
# dumping
# --------------------------------------------------------------------------

def _read_node(zk: KazooClient, path: str) -> dict:
    data, stat = zk.get(path)
    acls, _ = zk.get_acls(path)
    return {
        "path": path,
        "data_b64": base64.b64encode(data or b"").decode("ascii"),
        "ephemeral": bool(getattr(stat, "ephemeralOwner", 0)),
        "acls": [
            {"scheme": entry.id.scheme, "id": entry.id.id, "perms": entry.perms}
            for entry in acls
        ],
        "children": [],
    }


def dump_tree(zk: KazooClient, root: str) -> dict:
    """Snapshot every znode under `root` into a plain dict (iterative walk)."""
    _assert_not_protected(root)
    started = time.monotonic()
    if zk.exists(root) is None:
        raise errors.ZooKeeperError(f"root {root} does not exist on {settings.zk_hosts}")

    try:
        tree = _read_node(zk, root)
    except NoNodeError as exc:
        raise errors.ZooKeeperError(f"root {root} vanished during dump") from exc

    max_depth = settings.restore_max_depth
    stack = [(tree, 0)]
    while stack:
        node, depth = stack.pop()
        path = node["path"]
        if depth > max_depth:
            raise errors.ZooKeeperError(
                f"tree deeper than {max_depth} levels below {root}; "
                f"narrow ZBS_ZK_ROOT or raise ZBS_RESTORE_MAX_DEPTH"
            )
        try:
            children = sorted(zk.get_children(path))
        except NoNodeError:
            log.warning("node %s vanished during dump; skipping its children", path)
            continue
        child_depth = depth + 1
        for name in children:
            child_path = _join(path, name)
            if _is_protected(child_path):
                log.debug("skipping protected path %s", child_path)
                continue
            try:
                child_node = _read_node(zk, child_path)
            except NoNodeError:
                log.warning("node %s vanished during dump; skipping", child_path)
                continue
            node["children"].append(child_node)
            stack.append((child_node, child_depth))
        log.debug("dumped %s (%d children)", path, len(node["children"]))

    document = {
        "format": BACKUP_FORMAT,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source_zk": settings.zk_hosts,
        "source_root": root,
        "tree": tree,
    }
    nodes = count_nodes(tree)
    log.info("dump complete: %d nodes under %s in %.2fs", nodes, root, time.monotonic() - started)
    return document


# --------------------------------------------------------------------------
# serialization / integrity
# --------------------------------------------------------------------------

def serialize(document: dict) -> bytes:
    """Wrap the document in a checksummed envelope and gzip it.

    The envelope is assembled by splicing the canonical JSON of 'document'
    into the wrapper, so the document text exists only once in memory and
    the embedded bytes are exactly what the checksum covers.
    """
    payload = json.dumps(document, separators=(",", ":")).encode("utf-8")
    checksum = hashlib.sha256(payload).hexdigest()
    envelope_bytes = (
        b'{"format":'
        + json.dumps(BACKUP_FORMAT).encode("utf-8")
        + b',"checksum":"'
        + checksum.encode("ascii")
        + b'","document":'
        + payload
        + b"}"
    )
    blob = gzip.compress(envelope_bytes)
    log.debug(
        "serialized backup: %d nodes, %d bytes compressed",
        count_nodes(document.get("tree", {})),
        len(blob),
    )
    return blob


def _gunzip_bounded(raw: bytes, max_bytes: int) -> bytes:
    """Decompress a gzip stream with an uncompressed-size ceiling.

    Raises BackupValidationError when the stream expands beyond max_bytes,
    so a decompression bomb cannot exhaust memory. max_bytes <= 0 disables
    the cap.
    """
    if max_bytes <= 0:
        return gzip.decompress(raw)
    out = bytearray()
    chunk_size = 1 << 20  # 1 MiB
    with gzip.GzipFile(fileobj=io.BytesIO(raw)) as gz:
        while True:
            chunk = gz.read(chunk_size)
            if not chunk:
                break
            out.extend(chunk)
            if len(out) > max_bytes:
                raise errors.BackupValidationError(
                    f"backup exceeds maximum uncompressed size "
                    f"({max_bytes} bytes); refusing to restore"
                )
    return bytes(out)


def deserialize(raw: bytes) -> dict:
    """Verify and unwrap a backup artifact. Raises typed corruption errors."""
    if raw[:2] != b"\x1f\x8b":
        raise errors.BackupFormatError("backup is not a gzip stream (bad magic bytes)")
    try:
        text = _gunzip_bounded(raw, settings.restore_max_bytes).decode("utf-8")
    except (OSError, EOFError, UnicodeDecodeError) as exc:
        raise errors.BackupCorruptedError(f"backup archive is truncated or corrupt: {exc}") from exc

    try:
        envelope = json.loads(text)
    except json.JSONDecodeError as exc:
        raise errors.BackupCorruptedError(f"backup is not valid JSON: {exc}") from exc
    if not isinstance(envelope, dict):
        raise errors.BackupCorruptedError("backup envelope must be a JSON object")

    fmt = envelope.get("format")
    if fmt != BACKUP_FORMAT:
        raise errors.UnsupportedBackupError(f"unsupported backup format: {fmt!r}")

    if "document" not in envelope:
        # Backups produced before checksums existed were bare documents that
        # also carried the format field themselves.
        log.warning("legacy pre-checksum backup accepted; integrity cannot be verified")
        return envelope

    document = envelope["document"]
    expected = envelope.get("checksum")
    actual = hashlib.sha256(
        json.dumps(document, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    if not isinstance(expected, str) or expected.lower() != actual:
        raise errors.BackupCorruptedError(
            f"checksum mismatch: backup data is corrupted "
            f"(stored={str(expected)[:12]}..., computed={actual[:12]}...)"
        )
    log.debug("checksum verified (%s...)", actual[:12])
    return document


# --------------------------------------------------------------------------
# validation - runs entirely before any ZooKeeper mutation
# --------------------------------------------------------------------------

_MAX_PROBLEMS_REPORTED = 5


def validate_document(document: dict) -> int:
    """Deep-validate a backup document. Returns the node count.

    Raises BackupFormatError / UnsupportedBackupError / BackupValidationError.
    """
    if not isinstance(document, dict):
        raise errors.BackupFormatError("backup document must be a JSON object")
    if document.get("format") != BACKUP_FORMAT:
        raise errors.UnsupportedBackupError(
            f"unsupported backup format: {document.get('format')!r}"
        )

    problems: list[str] = []
    source_root = document.get("source_root")
    if not isinstance(source_root, str) or not source_root.startswith("/"):
        problems.append(f"source_root {source_root!r} is not an absolute path")
    elif _is_protected(source_root):
        problems.append(f"source_root {source_root!r} is a protected path")
    if not isinstance(document.get("tree"), dict):
        problems.append("'tree' must be an object")

    seen_paths: set[str] = set()
    node_count = 0

    def check_node(node: object, parent_path: str | None, depth: int) -> list:
        """Return child entries when the node itself is acceptable."""
        nonlocal node_count
        where = (
            parent_path if isinstance(node, dict) and not isinstance(node.get("path"), str)
            else node.get("path") if isinstance(node, dict)
            else f"<child of {parent_path}>"
        )
        if not isinstance(node, dict):
            problems.append(f"node {where!r} is not an object")
            return []
        node_count += 1

        path = node.get("path")
        if depth > settings.restore_max_depth:
            problems.append(f"tree deeper than {settings.restore_max_depth} levels at {where!r}")
        elif settings.restore_max_nodes and node_count > settings.restore_max_nodes:
            problems.append(
                f"more than {settings.restore_max_nodes} nodes (limit exceeded near {where!r})"
            )
        if isinstance(path, str):
            problem = _check_path(path, parent_path)
            if problem:
                problems.append(problem)
            else:
                if path in seen_paths:
                    problems.append(f"duplicate node path {path!r}")
                seen_paths.add(path)

        data = node.get("data_b64", "")
        if not isinstance(data, str):
            problems.append(f"data_b64 at {where!r} is not a string")
        else:
            try:
                base64.b64decode(data.encode("ascii"), validate=True)
            except (binascii.Error, UnicodeEncodeError, ValueError) as exc:
                problems.append(f"data_b64 at {where!r} is not valid base64: {exc}")

        acls = node.get("acls", [])
        if not isinstance(acls, list):
            problems.append(f"acls at {where!r} is not a list")
        else:
            for acl in acls:
                if not isinstance(acl, dict):
                    problems.append(f"acl entry at {where!r} is not an object")
                    continue
                perms = acl.get("perms")
                if not isinstance(perms, int) or not 0 <= perms <= 31:
                    problems.append(f"acl perms at {where!r} must be an integer 0..31")
                for field in ("scheme", "id"):
                    if not isinstance(acl.get(field), str):
                        problems.append(f"acl {field} at {where!r} is not a string")

        children = node.get("children", [])
        if not isinstance(children, list):
            problems.append(f"children at {where!r} is not a list")
            return []
        return [(child, path if isinstance(path, str) else parent_path, depth + 1) for child in children]

    stack = [(document.get("tree"), None, 0)] if isinstance(document.get("tree"), dict) else []
    while stack and len(problems) < 50:  # hard cap to bound validation work
        node, parent_path, depth = stack.pop()
        stack.extend(check_node(node, parent_path, depth))

    if problems:
        shown = "; ".join(problems[:_MAX_PROBLEMS_REPORTED])
        more = "" if len(problems) <= _MAX_PROBLEMS_REPORTED else f"; (+{len(problems) - _MAX_PROBLEMS_REPORTED} more)"
        raise errors.BackupValidationError(
            f"backup validation failed ({len(problems)} problem(s)): {shown}{more}"
        )
    log.debug("backup document validated: %d nodes under %s", node_count, source_root)
    return node_count


def _check_path(path: str, parent_path: str | None) -> str | None:
    """Return a problem description, or None when the path is sane."""
    if "\x00" in path:
        return f"path {path!r} contains a NUL byte"
    if not path.startswith("/"):
        return f"path {path!r} is not absolute"
    normalized = posixpath.normpath(path)
    if normalized != path:
        return f"path {path!r} is not normalized ('..'/'.'/double slashes?)"
    if parent_path is not None:
        prefix = "/" if parent_path == "/" else parent_path + "/"
        if not path.startswith(prefix):
            return f"path {path!r} is not nested under its parent {parent_path!r}"
        remainder = path[len(prefix):]
        if not remainder or "/" in remainder:
            return f"path {path!r} skips intermediate levels under {parent_path!r}"
    return None


def count_nodes(node: dict) -> int:
    """Iterative node count (safe for arbitrarily deep documents)."""
    total = 0
    stack = [node]
    while stack:
        current = stack.pop()
        total += 1
        children = current.get("children", []) if isinstance(current, dict) else []
        stack.extend(child for child in children if isinstance(child, dict))
    return total


# --------------------------------------------------------------------------
# restoring
# --------------------------------------------------------------------------

def _to_acls(entries: list | None) -> list[ACL]:
    return [
        ACL(
            perms=int(entry.get("perms", 31)),
            id=Id(scheme=entry.get("scheme", "world"), id=entry.get("id", "anyone")),
        )
        for entry in (entries or [])
    ]


def restore_tree(
    zk: KazooClient, document: dict, wipe: bool = False, restore_acls: bool | None = None
) -> dict:
    """Replay a validated backup into the live cluster.

    Validation runs FIRST: a corrupted or malformed document raises before
    any znode is created, updated or deleted. Missing nodes are created
    top-down, existing ones are overwritten. wipe=True deletes the current
    subtree first (except protected paths).
    """
    validate_document(document)
    root = document["source_root"]
    if restore_acls is None:
        restore_acls = settings.restore_acls
    started = time.monotonic()

    stats = {
        "created": 0,
        "updated": 0,
        "skipped_protected": 0,
        "deleted_on_wipe": 0,
        "data_bytes": 0,
    }

    try:
        if zk.exists(root) is None:
            log.info("root %s does not exist; creating it", root)
            zk.ensure_path(root)

        if wipe:
            for child in sorted(zk.get_children(root)):
                child_path = _join(root, child)
                if _is_protected(child_path):
                    continue
                zk.delete(child_path, recursive=True)
                stats["deleted_on_wipe"] += 1
            log.info("wipe removed %d top-level entr(ies)", stats["deleted_on_wipe"])
    except NoAuthError as exc:
        raise errors.ZooKeeperError(
            f"preparing {root} denied (insufficient permissions): {exc}"
        ) from exc

    stack = [document["tree"]]
    while stack:
        node = stack.pop()
        path = node["path"]
        if _is_protected(path):
            stats["skipped_protected"] += 1
            continue
        data = base64.b64decode(node.get("data_b64") or "")
        stats["data_bytes"] += len(data)
        acls = _to_acls(node.get("acls")) if restore_acls else []

        try:
            if zk.exists(path) is None:
                zk.create(path, data, acl=acls or None)
                stats["created"] += 1
                action = "created"
            else:
                zk.set_data(path, data)
                if acls:
                    zk.set_acls(path, acls, version=-1)
                stats["updated"] += 1
                action = "updated"
        except Exception as exc:
            raise _translate_zk_op(path, "restore")(exc) from exc

        children = node.get("children", [])
        stack.extend(reversed(children))
        if log.isEnabledFor(logging.DEBUG):
            log.debug("restored %s (%s, %d bytes)", path, action, len(data))

    stats["duration_seconds"] = round(time.monotonic() - started, 2)
    log.info(
        "restore complete: created=%(created)d updated=%(updated)d "
        "wiped=%(deleted_on_wipe)d skipped_protected=%(skipped_protected)d "
        "bytes=%(data_bytes)d in %(duration_seconds)ss",
        stats,
    )
    return stats
