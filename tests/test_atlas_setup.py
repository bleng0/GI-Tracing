import re

import pytest
from pymongo.errors import OperationFailure

from gi_trace import atlas_setup as a

ENV = {"ATLAS_PROJECT_ID": "a" * 24, "ATLAS_PUBLIC_KEY": "pub", "ATLAS_PRIVATE_KEY": "PRIVATE-SECRET"}


class FakeAtlas:
    def __init__(self, clusters=None, role_fails=False, existing_users=()):
        self.clusters = clusters if clusters is not None else [
            {"name": "C0", "stateName": "IDLE", "connectionStrings": {"standardSrv": "mongodb+srv://c0.abc.mongodb.net"}}]
        self.role_fails = role_fails
        self.users = {u: [] for u in existing_users}
        self.calls = []

    def call(self, method, path, body=None):
        self.calls.append((method, path, body))
        if path == "/clusters":
            return {"results": self.clusters}
        if path.startswith("/databaseUsers/admin/"):
            name = path.rsplit("/", 1)[1]
            if method == "GET" and name not in self.users:
                raise a.AtlasError(404, "no such user")
            if method == "DELETE":
                self.users.pop(name, None)
            return {}
        if path == "/databaseUsers":
            self.users[body["username"]] = body["roles"]
            return {}
        if path == "/customDBRoles/roles" and self.role_fails:
            raise a.AtlasError(400, "not supported")
        return {}


class FakeDB:
    """One shared database; permissions depend on which user logged in."""

    def __init__(self, fail_index=False):
        self.collections, self.indexes, self.docs = set(), {}, []
        self.fail_index = fail_index


class FakeClient:
    def __init__(self, world, user, can_delete, can_write, can_admin):
        self.world, self.user = world, user
        self.perm = dict(delete=can_delete, write=can_write, admin=can_admin)

    def __getitem__(self, name):
        return self if name == a.DB else FakeColl(self, name)

    def list_collection_names(self):
        if not self.user:
            raise OperationFailure("not authorized")
        return sorted(self.world.collections)

    def create_collection(self, name):
        self.world.collections.add(name)


class FakeColl:
    def __init__(self, client, name):
        self.c, self.name = client, name

    def create_index(self, keys, **options):
        if self.c.world.fail_index:
            raise RuntimeError("index build failed")
        self.c.world.indexes.setdefault(self.name, []).append((keys, options))

    def insert_one(self, doc):
        if not self.c.perm["write"]:
            raise OperationFailure("not authorized")
        self.c.world.docs.append(doc)

    def delete_many(self, query):
        if not self.c.perm["delete"]:
            raise OperationFailure("not authorized")
        self.c.world.docs.clear()


def make_connect(world):
    def connect(uri):
        m = re.match(r"mongodb\+srv://(?:([^:]+):[^@]+@)?", uri)
        user = m.group(1)
        return FakeClient(world, user,
                          can_delete=user == a.SETUP or (user == a.WRITER and world.writer_can_delete),
                          can_write=user in (a.SETUP, a.WRITER), can_admin=user == a.SETUP)
    return connect


def run(tmp_path, api, world=None, env=ENV, env_text=None, capsys=None):
    world = world or FakeDB()
    world.writer_can_delete = api.role_fails
    env_path = tmp_path / ".env"
    if env_text is not None:
        env_path.write_text(env_text)
    code = a.run(env, env_path, lambda *args: api, make_connect(world), lambda: "203.0.113.7", lambda s: None)
    return code, world, env_path


def test_happy_path_creates_everything_and_cleans_up(tmp_path, capsys):
    api = FakeAtlas()
    code, world, env_path = run(tmp_path, api, env_text="EXISTING=1")
    out = capsys.readouterr().out
    assert code == 0
    assert set(api.users) == {a.WRITER, a.READER}  # gi_setup removed
    assert api.users[a.WRITER] == [{"roleName": a.ROLE, "databaseName": "admin"}]
    assert api.users[a.READER] == [{"roleName": "read", "databaseName": a.DB}]
    assert ("POST", "/accessList", [{"ipAddress": "203.0.113.7", "comment": "gi-trace demo"}]) in api.calls
    role = next(b for m, path, b in api.calls if path == "/customDBRoles/roles")
    removable = [r["collection"] for act in role["actions"] if act["action"] == "REMOVE" for r in act["resources"]]
    assert removable == ["certificates", "documents"]
    assert world.collections == set(a.COLLECTIONS)
    assert world.docs == []  # the permission-check document was cleaned up
    text = env_path.read_text()
    assert text.startswith("EXISTING=1\n")
    assert re.search(r"^ATLAS_WRITE_URI=mongodb\+srv://gi_writer:\w{24}@c0\.abc\.mongodb\.net/gi_trace", text, re.M)
    assert re.search(r"^ATLAS_READ_URI=mongodb\+srv://gi_reader:\w{24}@", text, re.M)
    assert "PRIVATE-SECRET" not in out
    assert not any(p in out for p in re.findall(r"gi_\w+:(\w{24})@", text))  # no password printed


def test_refuses_when_a_user_already_exists_and_changes_nothing(tmp_path):
    api = FakeAtlas(existing_users=[a.WRITER])
    code, world, env_path = run(tmp_path, api)
    assert code == 1
    assert not env_path.exists()
    assert not any(m == "POST" for m, _, _ in api.calls)


def test_refuses_to_overwrite_existing_uris(tmp_path):
    api = FakeAtlas()
    code, _, env_path = run(tmp_path, api, env_text="ATLAS_WRITE_URI=keep\n")
    assert code == 1 and env_path.read_text() == "ATLAS_WRITE_URI=keep\n"
    assert api.calls == []


def test_missing_key_is_reported(tmp_path, capsys):
    code, _, _ = run(tmp_path, FakeAtlas(), env={"ATLAS_PROJECT_ID": "x"})
    assert code == 1 and "ATLAS_PUBLIC_KEY" in capsys.readouterr().out


@pytest.mark.parametrize("clusters", [[], [{"name": "A"}, {"name": "B"}]])
def test_needs_exactly_one_cluster(tmp_path, clusters):
    code, _, _ = run(tmp_path, FakeAtlas(clusters=clusters))
    assert code == 1


def test_paused_cluster_is_rejected(tmp_path):
    api = FakeAtlas(clusters=[{"name": "C0", "stateName": "PAUSED"}])
    assert run(tmp_path, api)[0] == 1


def test_falls_back_to_readwrite_when_custom_role_is_refused(tmp_path, capsys):
    api = FakeAtlas(role_fails=True)
    code, _, _ = run(tmp_path, api)
    out = capsys.readouterr().out
    assert code == 0
    assert api.users[a.WRITER] == [{"roleName": "readWrite", "databaseName": a.DB}]
    assert "NOT ENFORCED" in out


def test_temp_user_and_new_users_are_removed_when_setup_fails(tmp_path):
    api = FakeAtlas()
    code, _, env_path = run(tmp_path, api, world=FakeDB(fail_index=True))
    assert code == 1
    assert api.users == {}          # gi_setup, gi_writer and gi_reader are all gone
    assert not env_path.exists()
