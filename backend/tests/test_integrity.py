"""Corruption, tampering and validation matrix.

Guarantee under test: a corrupted or malformed backup NEVER mutates
ZooKeeper - every failure mode below raises before a single znode op.
"""

import base64
import gzip
import json

import pytest

from app import errors, zk as zk_mod
from conftest import FakeZK, build_document

pytestmark = pytest.mark.usefixtures("_restore_settings")


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def make_envelope(document, checksum=None):
    """Build a proper envelope; pass checksum to forge a mismatch."""
    import hashlib

    payload = json.dumps(document, separators=(",", ":")).encode()
    return {
        "format": "zbs-backup-v1",
        "checksum": checksum or hashlib.sha256(payload).hexdigest(),
        "document": document,
    }


class NeverConnect:
    def __init__(self, *a, **kw):
        raise AssertionError("ZooKeeper must not be contacted for corrupted backups")


class NeverMutate:
    """Full fake client whose every mutating call explodes if reached."""

    def __init__(self):
        self.calls = []

    def _boom(self, *args, **kwargs):
        self.calls.append(args[0] if args else "unknown")
        raise AssertionError("mutating call reached on corrupted backup")

    exists = get = get_children = create = set_data = delete = ensure_path = _boom
    set_acls = get_acls = start = stop = close = _boom


def assert_never_touches_zk(raw: bytes):
    """deserialize+validate raise, and restore_tree refuses pre-mutation."""
    with pytest.raises(errors.ZbsError):
        document = zk_mod.deserialize(raw)
        zk_mod.validate_document(document)
    zk = NeverMutate()
    with pytest.raises(errors.ZbsError):
        try:
            document = zk_mod.deserialize(raw)
            zk_mod.restore_tree(zk, document)
        except AssertionError:
            raise AssertionError("mutating call reached on corrupted backup")
    assert zk.calls == []


# --------------------------------------------------------------------------
# deserialize(): artifact-level corruption
# --------------------------------------------------------------------------

class TestDeserialize:
    def test_valid_envelope_round_trip(self):
        document = build_document()
        blob = zk_mod.serialize(document)
        assert zk_mod.deserialize(blob) == document

    def test_not_gzip_at_all(self):
        with pytest.raises(errors.BackupFormatError, match="not a gzip"):
            zk_mod.deserialize(b"hello world, definitely not gzip")

    def test_empty_bytes(self):
        with pytest.raises(errors.BackupFormatError):
            zk_mod.deserialize(b"")

    def test_truncated_gzip(self):
        good = zk_mod.serialize(build_document())
        for cut in (5, len(good) // 2, len(good) - 1):
            with pytest.raises((errors.BackupCorruptedError, errors.BackupFormatError)):
                zk_mod.deserialize(good[:cut])

    def test_bitflip_inside_archive(self):
        good = bytearray(zk_mod.serialize(build_document()))
        good[len(good) // 2] ^= 0xFF
        with pytest.raises((errors.BackupCorruptedError, errors.BackupFormatError)):
            zk_mod.deserialize(bytes(good))

    def test_gzip_of_non_json(self):
        with pytest.raises(errors.BackupCorruptedError, match="not valid JSON"):
            zk_mod.deserialize(gzip.compress(b"this is prose, not json"))

    def test_gzip_of_json_scalar_or_list(self):
        for scalar in [b"42", b'"text"', b"[1,2,3]", b"null"]:
            with pytest.raises(errors.BackupCorruptedError, match="JSON object"):
                zk_mod.deserialize(gzip.compress(scalar))

    def test_unsupported_format_tag(self):
        document = build_document()
        document["format"] = "zbs-backup-v99"
        raw = zk_mod.serialize(document)
        # The envelope is intact, so deserialize succeeds...
        assert zk_mod.deserialize(raw) == document
        # ...but validation refuses the incompatible document.
        with pytest.raises(errors.UnsupportedBackupError):
            zk_mod.validate_document(zk_mod.deserialize(raw))

    def test_checksum_mismatch_on_tampered_payload(self):
        """Any mismatch between stored checksum and payload must be caught."""
        document = build_document(
            tree={
                "path": "/",
                "data_b64": base64.b64encode(b"original data").decode(),
                "ephemeral": False,
                "acls": [],
                "children": [],
            }
        )
        # Envelope whose checksum does not match its content.
        envelope = {"format": "zbs-backup-v1", "checksum": "a" * 64, "document": document}
        with pytest.raises(errors.BackupCorruptedError, match="checksum mismatch"):
            zk_mod.deserialize(gzip.compress(json.dumps(envelope).encode()))

        # Real tamper: the checksum of the ORIGINAL document paired with
        # different content - content hash differs -> corrupted.
        stolen_checksum = make_envelope(document)["checksum"]
        other = build_document(
            tree={**build_document()["tree"], "data_b64": base64.b64encode(b"evil").decode()}
        )
        evil_envelope = {"format": "zbs-backup-v1", "checksum": stolen_checksum, "document": other}
        with pytest.raises(errors.BackupCorruptedError, match="corrupted"):
            zk_mod.deserialize(gzip.compress(json.dumps(evil_envelope).encode()))

    def test_legacy_pre_checksum_backup_accepted(self):
        legacy = build_document()  # bare document, no envelope
        raw = gzip.compress(json.dumps(legacy).encode())
        assert zk_mod.deserialize(raw) == legacy


# --------------------------------------------------------------------------
# validate_document(): structural corruption
# --------------------------------------------------------------------------

class TestValidateDocument:
    @pytest.mark.parametrize(
        ("mutation", "expected_match"),
        [
            ({"format": None}, "format"),
            ({"source_root": "relative"}, "not an absolute"),
            ({"source_root": "/zookeeper"}, "protected"),
            ({"tree": None}, "'tree' must be"),
            ({"extra_unknown_field": 1}, None),  # unknown fields are tolerated
        ],
    )
    def test_top_level(self, mutation, expected_match):
        document = build_document()
        document.update(mutation)
        if expected_match is None:
            zk_mod.validate_document(document)
        else:
            with pytest.raises(errors.ZbsError, match=expected_match):
                zk_mod.validate_document(document)

    def test_node_missing_required_fields_is_tolerated_with_defaults(self):
        # data_b64/acls/children default to empty - a sparse node is fine.
        document = build_document(tree={"path": "/"})
        assert zk_mod.validate_document(document) == 1

    @pytest.mark.parametrize("bad_path", ["relative/path", "", "/double//slash", "/dot/../escape", "/ends/", "/null\x00byte"])
    def test_invalid_child_paths_rejected(self, bad_path):
        tree = {**build_document()["tree"], "children": [{"path": bad_path}]}
        document = build_document(tree=tree)
        with pytest.raises(errors.BackupValidationError):
            zk_mod.validate_document(document)

    def test_child_path_not_under_parent_rejected(self):
        tree = {
            **build_document()["tree"],
            "children": [
                {"path": "/a", "data_b64": "", "acls": [], "children": [{"path": "/b"}]}
            ],
        }
        with pytest.raises(errors.BackupValidationError, match="nested under its parent"):
            zk_mod.validate_document(build_document(tree=tree))

    def test_duplicate_paths_rejected(self):
        child = {"path": "/dup"}
        tree = {**build_document()["tree"], "children": [child, dict(child)]}
        with pytest.raises(errors.BackupValidationError, match="duplicate"):
            zk_mod.validate_document(build_document(tree=tree))

    @pytest.mark.parametrize("bad_b64", ["not*base64!", "abcde", "aGVsbG8=extra"])
    def test_invalid_base64_rejected(self, bad_b64):
        tree = {**build_document()["tree"], "data_b64": bad_b64}
        with pytest.raises(errors.BackupValidationError, match="base64"):
            zk_mod.validate_document(build_document(tree=tree))

    def test_non_string_data_b64_rejected(self):
        tree = {**build_document()["tree"], "data_b64": 12345}
        with pytest.raises(errors.BackupValidationError, match="not a string"):
            zk_mod.validate_document(build_document(tree=tree))

    def test_bad_acl_entries_rejected(self):
        tree = {
            **build_document()["tree"],
            "acls": [{"scheme": 1, "id": "x", "perms": 31}, {"scheme": "digest", "id": "u", "perms": 999}],
        }
        with pytest.raises(errors.BackupValidationError):
            zk_mod.validate_document(build_document(tree=tree))

    def test_depth_limit_enforced(self):
        def chain(prefix, remaining):
            node = {"path": prefix or "/", "data_b64": "", "ephemeral": False, "acls": [], "children": []}
            if remaining:
                name = f"d{remaining}"
                child_prefix = f"/{name}" if prefix == "/" else f"{prefix}/{name}"
                node["children"] = [chain(child_prefix, remaining - 1)]
            return node

        document = build_document(tree=chain("/", 300))
        zk_mod.settings.restore_max_depth = 256
        with pytest.raises(errors.BackupValidationError, match="deeper than"):
            zk_mod.validate_document(document)

    def test_deep_tree_within_limit_is_accepted(self):
        def chain(prefix, remaining):
            node = {"path": prefix or "/", "data_b64": "", "ephemeral": False, "acls": [], "children": []}
            if remaining:
                name = f"d{remaining}"
                child_prefix = f"/{name}" if prefix == "/" else f"{prefix}/{name}"
                node["children"] = [chain(child_prefix, remaining - 1)]
            return node

        document = build_document(tree=chain("/", 300))
        zk_mod.settings.restore_max_depth = 512
        assert zk_mod.validate_document(document) == 301

    def test_node_count_limit_enforced(self):
        children = [{"path": f"/n{i}"} for i in range(10)]
        document = build_document(tree={**build_document()["tree"], "children": children})
        zk_mod.settings.restore_max_nodes = 3
        with pytest.raises(errors.BackupValidationError, match="more than 3 nodes"):
            zk_mod.validate_document(document)

    def test_unlimited_when_zero(self):
        children = [{"path": f"/n{i}"} for i in range(10)]
        document = build_document(tree={**build_document()["tree"], "children": children})
        zk_mod.settings.restore_max_nodes = 0
        assert zk_mod.validate_document(document) == 11

    def test_validation_problem_cap_still_raises(self):
        children = [{"path": f"bad{i}"} for i in range(100)]
        document = build_document(tree={**build_document()["tree"], "children": children})
        with pytest.raises(errors.BackupValidationError, match=r"\(\d+ problem"):
            zk_mod.validate_document(document)


# --------------------------------------------------------------------------
# size limits: decompression bombs must fail cleanly, never OOM
# --------------------------------------------------------------------------

class TestSizeLimits:
    def test_decompression_bomb_rejected(self):
        """A stream expanding beyond restore_max_bytes raises a typed error."""
        document = build_document()
        blob = zk_mod.serialize(document)
        zk_mod.settings.restore_max_bytes = 16  # tiny cap; document is larger
        with pytest.raises(errors.BackupValidationError, match="maximum uncompressed size"):
            zk_mod.deserialize(blob)

    def test_cap_disabled_still_works(self):
        document = build_document()
        blob = zk_mod.serialize(document)
        zk_mod.settings.restore_max_bytes = 0  # unlimited
        assert zk_mod.deserialize(blob) == document

    def test_oversize_error_never_touches_zookeeper(self):
        blob = zk_mod.serialize(build_document())
        zk_mod.settings.restore_max_bytes = 16
        with pytest.raises(errors.ZbsError):
            zk_mod.deserialize(blob)  # raises before validate/restore can run

    def test_envelope_splice_matches_reference_format(self):
        """serialize() output is the exact dict-based envelope format."""
        import hashlib

        document = build_document()
        blob = zk_mod.serialize(document)
        envelope = json.loads(gzip.decompress(blob))
        payload = json.dumps(document, separators=(",", ":")).encode()
        assert envelope["format"] == "zbs-backup-v1"
        assert envelope["checksum"] == hashlib.sha256(payload).hexdigest()
        assert envelope["document"] == document

    def test_old_style_envelope_still_accepted(self):
        """Envelopes built as one big JSON dict deserialize identically."""
        import hashlib

        document = build_document()
        payload = json.dumps(document, separators=(",", ":")).encode()
        old_style_blob = gzip.compress(
            json.dumps(
                {
                    "format": "zbs-backup-v1",
                    "checksum": hashlib.sha256(payload).hexdigest(),
                    "document": document,
                },
                separators=(",", ":"),
            ).encode()
        )
        assert zk_mod.deserialize(old_style_blob) == document


# --------------------------------------------------------------------------
# end-to-end guarantee: corrupt restore never contacts zookeeper
# --------------------------------------------------------------------------

class TestCorruptedRestoreNeverTouchesZookeeper:
    def test_every_corruption_mode(self):
        good_document = build_document(
            tree={
                "path": "/",
                "data_b64": base64.b64encode(b"x").decode(),
                "ephemeral": False,
                "acls": [],
                "children": [{"path": "/kid", "data_b64": "", "acls": [], "children": []}],
            }
        )

        cases = [
            b"garbage",
            gzip.compress(b"{}"),                       # envelope without format/document/tree
            gzip.compress(json.dumps([1]).encode()),    # JSON array
            zk_mod.serialize({**good_document, "format": "other/vX"}),
            gzip.compress(                              # valid checksum, broken structure
                json.dumps(
                    make_envelope({"format": "zbs-backup-v1", "source_root": "/", "tree": {"path": "oops"}})
                ).encode()
            ),
        ]
        for raw in cases:
            assert_never_touches_zk(raw)

    def test_valid_backup_does_reach_zookeeper(self):
        """Control: the same pipeline with a GOOD document does mutate ZK."""
        source = FakeZK()
        from conftest import seed_tree

        seed_tree(source)
        document = zk_mod.dump_tree(source, "/")
        raw = zk_mod.serialize(document)

        target = FakeZK()
        stats = zk_mod.restore_tree(target, zk_mod.deserialize(raw), wipe=True)
        assert stats["created"] + stats["updated"] > 0

    def test_perform_restore_refuses_before_connect(self, monkeypatch):
        from app import jobs as jobs_module
        from app import s3 as s3_module
        from app import zk as zk_module

        called = {"connect": 0}

        def sentinel_connect():
            called["connect"] += 1
            return FakeZK()

        monkeypatch.setattr(s3_module, "download_backup", lambda key: b"definitely not gzip")
        monkeypatch.setattr(zk_module, "connect", sentinel_connect)

        with pytest.raises(errors.ZbsError):
            jobs_module.perform_restore("zbs/x.json.gz")
        assert called["connect"] == 0, "connect() must not run for corrupted artifacts"
