"""Shared fixtures and fakes for the ZBS backend test suite."""

import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # backend/

from app import s3 as s3_module  # noqa: E402
from app.config import Settings, settings  # noqa: E402
from kazoo.exceptions import NoNodeError  # noqa: E402
from kazoo.security import ACL, Id  # noqa: E402

OPEN = [ACL(perms=31, id=Id("world", "anyone"))]


# --------------------------------------------------------------------------
# settings snapshot/restore so tests can tweak freely
# --------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _restore_settings():
    saved = {key: value for key, value in vars(settings).items()}
    yield
    vars(settings).clear()
    vars(settings).update(saved)


@pytest.fixture(autouse=True)
def _reset_s3_singleton():
    s3_module.reset_client()
    yield
    s3_module.reset_client()


@pytest.fixture
def make_settings(monkeypatch):
    """Build a fresh Settings from an env mapping; clears all ZBS_* vars so
    each call is independent of previous calls within the same test."""
    import os

    def factory(**env) -> Settings:
        for key in [k for k in os.environ if k.startswith("ZBS_")]:
            monkeypatch.delenv(key, raising=False)
        for key, value in env.items():
            monkeypatch.setenv(key, str(value))
        return Settings()

    return factory


# --------------------------------------------------------------------------
# Fake ZooKeeper client (duck-typed like kazoo.client.KazooClient)
# --------------------------------------------------------------------------

def make_stat(ephemeral=False):
    return types.SimpleNamespace(ephemeralOwner=1 if ephemeral else 0, ctime=0, mtime=0)


class FakeZK:
    """Duck-typed stand-in for KazooClient backed by a dict.

    Supports fault injection per method name via `fail_on={name: exception}`,
    and `vanish_after_list` paths whose get() raises NoNodeError (simulating a
    node deleted between get_children() and get()).
    """

    def __init__(self, fail_on=None, vanish_after_list=()):
        self.nodes = {
            "/": (b"", make_stat()),
            "/zookeeper": (b"", make_stat()),
        }
        self.auth: list[tuple[str, str]] = []
        self.started = False
        self.closed = False
        self.set_acls_log: list[tuple[str, list]] = []
        self.fail_on = dict(fail_on or {})
        self.vanish_after_list = set(vanish_after_list)

    # session ---------------------------------------------------------------
    def start(self, timeout=None):
        self.started = True

    def stop(self):
        self.closed = True

    def close(self):
        self.closed = True

    def add_auth(self, scheme, credential):
        self.auth.append((scheme, credential))

    # nodes -----------------------------------------------------------------
    def _maybe_fail(self, name):
        exc = self.fail_on.get(name)
        if exc is not None:
            raise exc

    def exists(self, path):
        self._maybe_fail("exists")
        return self.nodes.get(path)

    def get(self, path):
        self._maybe_fail("get")
        if path in self.vanish_after_list:
            raise NoNodeError()
        data, stat = self.nodes[path]
        return data, stat

    def get_children(self, path):
        self._maybe_fail("get_children")
        prefix = "/" if path == "/" else path + "/"
        return sorted(
            {
                key[len(prefix):].split("/")[0]
                for key in self.nodes
                if key != path and key.startswith(prefix)
            }
        )

    def create(self, path, data=b"", acl=None):
        self._maybe_fail("create")
        if path in self.nodes:
            raise AssertionError(f"duplicate create {path}")
        self.nodes[path] = (data, make_stat())

    def ensure_path(self, path, acl=None):
        current = ""
        for part in [p for p in path.split("/") if p]:
            current += "/" + part
            if current not in self.nodes:
                self.create(current)

    def set_data(self, path, data):
        self._maybe_fail("set_data")
        old, stat = self.nodes[path]
        self.nodes[path] = (data, stat)

    def get_acls(self, path):
        self._maybe_fail("get_acls")
        return list(OPEN), make_stat()

    def set_acls(self, path, acls, version=-1):
        self._maybe_fail("set_acls")
        self.set_acls_log.append((path, list(acls)))

    def delete(self, path, recursive=False):
        self._maybe_fail("delete")
        if recursive:
            for key in [k for k in self.nodes if k == path or k.startswith(path + "/")]:
                del self.nodes[key]
        else:
            self.nodes.pop(path)


def tree_shape(zk, root="/", zkmod=None):
    shape = {}

    def walk(path):
        if zkmod is not None and zkmod._is_protected(path):
            return
        data, _ = zk.get(path)
        shape[path] = data
        for child in sorted(zk.get_children(path)):
            walk(f"/{child}" if path == "/" else f"{path}/{child}")

    walk(root)
    return shape


def seed_tree(zk: FakeZK) -> None:
    zk.ensure_path("/app")
    zk.create("/app/config", b'{"replicas": 3}')
    zk.create("/app/config/users", b"alice,bob")
    zk.create("/app/queue-", b"item-0001")  # pseudo sequential node
    zk.create("/app/blob", bytes(range(256)))  # binary payload
    zk.create("/zookeeper/quota", b"reserved")


# --------------------------------------------------------------------------
# Fake S3 client (duck-typed subset of boto3 S3 client)
# --------------------------------------------------------------------------

class FakeS3:
    """Duck-typed boto3 S3 client with paginator support and fault injection."""

    def __init__(self, objects=None, fail_on=None, delete_errors=None):
        # objects: {key: {"body": bytes, "last_modified": datetime}}
        self.objects = objects or {}
        self.fail_on = dict(fail_on or {})
        self.delete_errors = list(delete_errors or [])
        self.deleted: list[str] = []
        self.uploaded: dict[str, bytes] = {}
        self.paginate_calls: list[dict] = []

    def _maybe_fail(self, name):
        exc = self.fail_on.get(name)
        if exc is not None:
            raise exc

    def put_object(self, Bucket, Key, Body, ContentType=None):
        self._maybe_fail("put_object")
        self.uploaded[Key] = bytes(Body)
        self.objects.setdefault(
            Key,
            {
                "body": bytes(Body),
                "last_modified": FAKE_NOW.replace(tzinfo=None),
            },
        )

    class _Paginator:
        def __init__(self, outer):
            self.outer = outer

        def paginate(self, Bucket=None, Prefix="", Delimiter=None):
            self.outer.paginate_calls.append(
                {"Bucket": Bucket, "Prefix": Prefix, "Delimiter": Delimiter}
            )
            if Delimiter:
                # group keys by their first path segment -> CommonPrefixes
                prefixes = sorted(
                    {key.split("/")[0] + "/" for key in self.outer.objects if "/" in key}
                )
                page_size = 2  # force multiple pages to exercise pagination
                for start in range(0, len(prefixes), page_size):
                    chunk = prefixes[start : start + page_size]
                    yield {
                        "CommonPrefixes": [{"Prefix": p} for p in chunk],
                        "KeyCount": len(chunk),
                    }
                return
            matching = [
                (key, meta)
                for key, meta in self.outer.objects.items()
                if key.startswith(Prefix)
            ]
            page_size = 2  # force multiple pages to exercise pagination
            for start in range(0, len(matching), page_size):
                chunk = matching[start : start + page_size]
                yield {
                    "Contents": [
                        {
                            "Key": key,
                            "Size": len(meta["body"]),
                            "LastModified": meta["last_modified"],
                        }
                        for key, meta in chunk
                    ]
                }

    def get_paginator(self, operation):
        assert operation == "list_objects_v2"
        return FakeS3._Paginator(self)

    def get_object(self, Bucket, Key):
        self._maybe_fail("get_object")
        if Key not in self.objects:
            import botocore.exceptions as botocore_errors

            raise botocore_errors.ClientError(
                {"Error": {"Code": "NoSuchKey", "Message": "no such key"}}, "GetObject"
            )
        return {"Body": _ReadableBytes(self.objects[Key]["body"])}

    def delete_objects(self, Bucket, Delete):
        self._maybe_fail("delete_objects")
        requested = [entry["Key"] for entry in Delete["Objects"]]
        error_set = set(self.delete_errors)
        errors_out = [
            {"Key": key, "Code": "InternalError", "Message": "nope"}
            for key in requested
            if key in error_set
        ]
        for key in requested:
            if key not in error_set:
                self.deleted.append(key)
                self.objects.pop(key, None)
        return {"Errors": errors_out}

    def head_bucket(self, Bucket):
        self._maybe_fail("head_bucket")


FAKE_NOW = None  # set below


class _ReadableBytes:
    """Mimics the botocore StreamingBody interface used by download_backup,
    including chunked reads with a size argument."""

    def __init__(self, payload: bytes):
        self._payload = payload
        self._pos = 0

    def read(self, size=-1):
        if size is None or size < 0:
            data = self._payload[self._pos:]
            self._pos = len(self._payload)
            return data
        data = self._payload[self._pos : self._pos + size]
        self._pos += len(data)
        return data

    def close(self) -> None:
        pass


from datetime import datetime as _dt  # noqa: E402

FAKE_NOW = _dt(2024, 5, 1, 12, 0, 0)


# --------------------------------------------------------------------------
# backup document builders
# --------------------------------------------------------------------------

def build_document(tree=None, source_root="/") -> dict:
    from app import zk as zk_mod

    document = {
        "format": zk_mod.BACKUP_FORMAT,
        "created_at": "2024-05-01T12:00:00+00:00",
        "source_zk": "fake:2181",
        "source_root": source_root,
        "tree": tree
        if tree is not None
        else {
            "path": source_root,
            "data_b64": "",
            "ephemeral": False,
            "acls": [{"scheme": "world", "id": "anyone", "perms": 31}],
            "children": [],
        },
    }
    return document
