"""Dump/restore behavior: round trips, deep trees, races, ACLs, protection."""

import pytest

from app import zk as zk_mod
from conftest import FakeZK, seed_tree, tree_shape

pytestmark = pytest.mark.usefixtures("_restore_settings")


class TestRoundTrip:
    def test_dump_serialize_deserialize_restore_round_trip(self):
        settings = zk_mod.settings
        settings.zk_root = "/"
        settings.restore_acls = False

        source = FakeZK()
        seed_tree(source)
        document = zk_mod.dump_tree(source, "/")

        blob = zk_mod.serialize(document)
        assert blob[:2] == b"\x1f\x8b", "expected gzip payload"
        assert zk_mod.deserialize(blob) == document

        target = FakeZK()
        seed_tree(target)
        target.create("/app/stale", b"to-be-wiped")
        stats = zk_mod.restore_tree(target, document, wipe=True)

        assert stats["deleted_on_wipe"] == 1
        assert stats["created"] == 5 and stats["updated"] == 1
        assert "/app/stale" not in target.nodes
        assert target.nodes["/zookeeper/quota"][0] == b"reserved"
        assert tree_shape(target, zkmod=zk_mod) == tree_shape(source, zkmod=zk_mod)

    def test_restore_into_empty_cluster(self):
        source = FakeZK()
        seed_tree(source)
        document = zk_mod.dump_tree(source, "/")

        fresh = FakeZK()
        stats = zk_mod.restore_tree(fresh, document)
        assert stats["created"] == 5 and stats["updated"] == 1
        assert fresh.nodes["/app/blob"][0] == bytes(range(256))
        assert tree_shape(fresh, zkmod=zk_mod) == tree_shape(source, zkmod=zk_mod)

    def test_restore_in_place_updates_without_wipe(self):
        source = FakeZK()
        seed_tree(source)
        document = zk_mod.dump_tree(source, "/")

        inplace = FakeZK()
        seed_tree(inplace)
        inplace.create("/app/stale", b"must-survive")
        stats = zk_mod.restore_tree(inplace, document, wipe=False)

        assert stats["created"] == 0 and stats["updated"] == 6
        assert inplace.nodes["/app/stale"][0] == b"must-survive"

    def test_very_deep_tree_rejected_with_clear_error(self):
        """Depth beyond the configured rail fails the DUMP with a clear
        message (json encoding itself cannot handle unbounded nesting)."""
        source = FakeZK()
        path = "/"
        for i in range(3000):
            source.create(f"{path}n{i}", b"x")
            path = f"{path}n{i}/"

        with pytest.raises(zk_mod.errors.ZooKeeperError, match="deeper than"):
            zk_mod.dump_tree(source, "/")

    def test_deep_tree_within_rail_round_trips(self):
        """Depths inside the rail work end-to-end without Python recursion."""
        depth = 200  # well below default rail (256) and json's comfort zone
        source = FakeZK()
        path = "/"
        for i in range(depth):
            source.create(f"{path}n{i}", f"data-{i}".encode())
            path = f"{path}n{i}/"

        document = zk_mod.deserialize(zk_mod.serialize(zk_mod.dump_tree(source, "/")))
        fresh = FakeZK()
        stats = zk_mod.restore_tree(fresh, document)
        assert stats["data_bytes"] > 0

        node, seen_depth = document["tree"], 0
        while node.get("children"):
            node = node["children"][0]
            seen_depth += 1
        assert seen_depth == depth


class TestDumpEdgeCases:
    def test_root_missing(self):
        with pytest.raises(zk_mod.errors.ZooKeeperError, match="does not exist"):
            zk_mod.dump_tree(FakeZK(), "/nope")

    def test_protected_root_refused(self):
        with pytest.raises(ValueError, match="reserved"):
            zk_mod.dump_tree(FakeZK(), "/zookeeper")

    def test_vanished_nodes_are_skipped_with_warning(self, caplog):
        source = FakeZK(vanish_after_list={"/app/blob"})
        seed_tree(source)
        with caplog.at_level("WARNING", logger="zbs.zk"):
            document = zk_mod.dump_tree(source, "/")

        paths = []

        def collect(node):
            paths.append(node["path"])
            for child in node["children"]:
                collect(child)

        collect(document["tree"])
        assert "/app/blob" not in paths
        assert any("vanished" in r.message for r in caplog.records)

    def test_zookeeper_subtree_never_dumped(self):
        source = FakeZK()
        seed_tree(source)
        document = zk_mod.dump_tree(source, "/")

        def paths(node):
            yield node["path"]
            for child in node["children"]:
                yield from paths(child)

        assert all(not p.startswith("/zookeeper") for p in paths(document["tree"]))


class TestRestoreEdgeCases:
    def test_wipe_keeps_only_protected_children(self):
        target = FakeZK()
        seed_tree(target)
        # A backup of an empty root: after wipe only protected nodes survive.
        empty_source = FakeZK()
        document = zk_mod.dump_tree(empty_source, "/")

        stats = zk_mod.restore_tree(target, document, wipe=True)
        # The whole /zookeeper subtree (including quota) survives untouched.
        survivors = sorted(k for k in target.nodes if k != "/")
        assert survivors == ["/zookeeper", "/zookeeper/quota"]
        assert stats["deleted_on_wipe"] == 1  # /app counted; /zookeeper protected

    def test_acl_replay_when_enabled(self):
        source = FakeZK()
        seed_tree(source)
        document = zk_mod.dump_tree(source, "/app")

        # Fresh target: nodes are created carrying their ACLs (no set_acls),
        # except the root itself which ensure_path pre-created -> updated path.
        target = FakeZK()
        zk_mod.settings.restore_acls = True
        stats = zk_mod.restore_tree(target, document)
        assert [p for p, _ in target.set_acls_log] == ["/app"]
        assert stats["created"] == 4

        # Pre-existing nodes must get ACLs replayed via set_acls.
        target2 = FakeZK()
        seed_tree(target2)
        zk_mod.restore_tree(target2, document)
        assert len(target2.set_acls_log) >= 1

    def test_per_node_failure_raises_contextual_error(self):
        source = FakeZK()
        seed_tree(source)
        document = zk_mod.dump_tree(source, "/")

        from kazoo.exceptions import NoAuthError

        broken = FakeZK(fail_on={"set_data": NoAuthError()})
        seed_tree(broken)
        with pytest.raises(zk_mod.errors.ZooKeeperError, match="denied"):
            zk_mod.restore_tree(broken, document)


class TestConnect:
    def test_unavailable_translates_to_typed_error(self, monkeypatch):
        import app.zk as zk_module

        class Boom:
            def __init__(self, hosts=None, timeout=None):
                pass

            def start(self, timeout=None):
                raise TimeoutError("too slow")

        monkeypatch.setattr(zk_module, "KazooClient", Boom)
        zk_module.settings.zk_hosts = "nowhere:2181"
        with pytest.raises(zk_mod.errors.ZooKeeperUnavailableError, match="cannot reach ZooKeeper"):
            zk_module.connect()

    def test_auth_applied_when_configured(self, monkeypatch):
        import app.zk as zk_module

        created = {}

        class Stub:
            def __init__(self, hosts=None, timeout=None):
                created["hosts"] = hosts
                self._event_called = False

            def start(self, timeout=None):
                pass

            def add_auth(self, scheme, credential):
                self.credential = credential

                class Event:
                    @staticmethod
                    def wait(timeout=None):
                        return True

                return Event()

        monkeypatch.setattr(zk_module, "KazooClient", Stub)
        zk_module.settings.zk_username = "admin"
        zk_module.settings.zk_password = "s3cret"
        client = zk_module.connect()
        assert created["hosts"] == zk_module.settings.zk_hosts
        assert client.credential == "admin:s3cret"
