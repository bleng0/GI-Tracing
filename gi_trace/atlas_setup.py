"""One-time MongoDB Atlas setup:  docker compose run --rm web python -m gi_trace.atlas_setup

Reads ATLAS_PROJECT_ID, ATLAS_PUBLIC_KEY and ATLAS_PRIVATE_KEY from .env, then:
  1. allows this PC's public IP on the project's network access list
  2. creates a custom role (find / insert / update; delete only on certificates and documents) and the users
     gi_writer (pipeline) and gi_reader (dashboard), with random passwords
  3. creates the gi_trace collections and indexes through a temporary gi_setup user
  4. checks the permissions, then deletes gi_setup
  5. appends ATLAS_WRITE_URI and ATLAS_READ_URI to .env

Secrets are never printed. It refuses to run if gi_writer or gi_reader already exist,
or if .env already has the two URIs, so nothing is overwritten.
"""
from __future__ import annotations

import ipaddress
import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from ._docker import require_container

API_ROOT = "https://cloud.mongodb.com/api/atlas/v2"
DB = "gi_trace"
WRITER, READER, SETUP = "gi_writer", "gi_reader", "gi_setup"
ROLE = "gi_append_only"
ENV_FILE = Path(__file__).resolve().parent.parent / ".env"
TIMEOUT_MS = 30000

# name -> [(keys, options)]. The ledger_* collections are what the ledger step will use:
# unique (jurisdiction, index) stops two blocks at one position, and the unique
# (jurisdiction, dedupe_key) stops a batch being attested or anchored twice. Every block
# carries a dedupe_key ("ATTESTATION:<batch>:<cert_type>", "BATCH_ANCHOR:<batch>", "BLOCK:<index>").
COLLECTIONS = {
    "certificates": [([("batch_id", 1)], {}), ([("cert_type", 1), ("compliance_status", 1)], {})],
    "documents": [([("batch_id", 1)], {})],
    "audit_log": [([("ts", -1)], {})],
    "ledger_blocks": [
        ([("jurisdiction", 1), ("index", 1)], {"unique": True}),
        ([("jurisdiction", 1), ("dedupe_key", 1)], {"unique": True}),
    ],
    "ledger_heads": [([("jurisdiction", 1)], {"unique": True})],
    "pending_signatures": [([("shipment_id", 1), ("role", 1)], {"unique": True})],
}


class AtlasError(Exception):
    def __init__(self, status: int, body: str):
        super().__init__(f"Atlas API returned {status}: {body[:300]}")
        self.status = status


class SetupError(Exception):
    pass


class AtlasAPI:
    """Minimal Atlas Admin API client (HTTP digest auth, standard library only)."""

    def __init__(self, project_id: str, public_key: str, private_key: str):
        manager = urllib.request.HTTPPasswordMgrWithDefaultRealm()
        manager.add_password(None, "https://cloud.mongodb.com", public_key, private_key)
        self._opener = urllib.request.build_opener(urllib.request.HTTPDigestAuthHandler(manager))
        self._base = f"{API_ROOT}/groups/{project_id}"

    def call(self, method: str, path: str, body=None):
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(
            self._base + path, data=data, method=method,
            headers={"Accept": "application/vnd.atlas.2023-01-01+json", "Content-Type": "application/json"},
        )
        try:
            with self._opener.open(request, timeout=30) as response:
                text = response.read().decode()
                return json.loads(text) if text else {}
        except urllib.error.HTTPError as e:
            raise AtlasError(e.code, e.read().decode(errors="replace")) from None


def new_password() -> str:
    alphabet = "abcdefghijkmnopqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(24))


def public_ip() -> str:
    with urllib.request.urlopen("https://api.ipify.org", timeout=10) as response:
        return response.read().decode().strip()


def real_connect(uri: str):
    import pymongo
    return pymongo.MongoClient(uri, serverSelectionTimeoutMS=TIMEOUT_MS)


def uri_for(user: str, password: str, host: str) -> str:
    return f"mongodb+srv://{user}:{password}@{host}/{DB}?retryWrites=true&w=majority&appName=gi-trace"


def find_cluster(api, wanted: str | None) -> str:
    clusters = api.call("GET", "/clusters").get("results", [])
    if wanted:
        clusters = [c for c in clusters if c.get("name") == wanted]
    if len(clusters) != 1:
        raise SetupError(f"expected exactly one cluster, found {len(clusters)}; "
                         "set ATLAS_CLUSTER_NAME in .env to pick one")
    cluster = clusters[0]
    if cluster.get("stateName") != "IDLE":
        raise SetupError(f"cluster {cluster.get('name')} is {cluster.get('stateName')}, not ready (paused?)")
    srv = cluster.get("connectionStrings", {}).get("standardSrv")
    if not srv:
        raise SetupError("the cluster has no SRV connection string yet; wait a minute and retry")
    return srv.removeprefix("mongodb+srv://")


def user_exists(api, name: str) -> bool:
    try:
        api.call("GET", f"/databaseUsers/admin/{name}")
        return True
    except AtlasError as e:
        if e.status == 404:
            return False
        raise


def create_user(api, name: str, password: str, roles: list) -> None:
    api.call("POST", "/databaseUsers",
             {"databaseName": "admin", "username": name, "password": password, "roles": roles})


def create_append_only_role(api) -> bool:
    actions = [{"action": a, "resources": [{"db": DB, "collection": ""}]}
               for a in ("FIND", "INSERT", "UPDATE", "LIST_COLLECTIONS", "LIST_INDEXES")]
    # The pipeline may clear its own regenerable data; the ledger and audit log stay delete-proof.
    actions.append({"action": "REMOVE", "resources": [{"db": DB, "collection": c} for c in ("certificates", "documents")]})
    try:
        api.call("POST", "/customDBRoles/roles", {"roleName": ROLE, "actions": actions})
        return True
    except AtlasError as e:
        if e.status == 409:  # already exists from an earlier run
            return True
        print(f"  WARNING: could not create the no-delete role ({e}). "
              "gi_writer will get the built-in readWrite role instead.")
        return False


def wait_for_login(connect, uri: str, sleep):
    for _ in range(24):  # about 2 minutes: new Atlas users take a little while to appear
        try:
            client = connect(uri)
            client[DB].list_collection_names()
            return client
        except Exception:
            sleep(5)
    raise SetupError("the temporary setup user could not log in after about 2 minutes")


def build_schema(client) -> None:
    db = client[DB]
    existing = set(db.list_collection_names())
    for name, indexes in COLLECTIONS.items():
        if name not in existing:
            db.create_collection(name)
        for keys, options in indexes:
            db[name].create_index(keys, **options)


def check_permissions(connect, host: str, writer_uri: str, reader_uri: str, setup_client, delete_blocked: bool):
    """Returns [(label, ok, required)]."""
    from pymongo.errors import OperationFailure

    def refused(fn) -> bool:
        try:
            fn()
            return False
        except OperationFailure:
            return True

    results = []
    anon = connect(f"mongodb+srv://{host}/{DB}")
    results.append(("anonymous access refused", refused(lambda: anon[DB].list_collection_names()), True))
    reader = connect(reader_uri)
    results.append(("reader can read", "certificates" in reader[DB].list_collection_names(), True))
    results.append(("reader cannot write", refused(lambda: reader[DB]["audit_log"].insert_one({"_check": True})), True))
    writer = connect(writer_uri)
    try:
        writer[DB]["audit_log"].insert_one({"_check": True})
        wrote = True
    except OperationFailure:
        wrote = False
    results.append(("writer can write", wrote, True))
    results.append(("writer cannot delete", refused(lambda: writer[DB]["audit_log"].delete_many({"_check": True})), delete_blocked))
    setup_client[DB]["audit_log"].delete_many({"_check": True})
    return results


def run(env: dict, env_path: Path, make_api, connect, get_ip, sleep) -> int:
    for key in ("ATLAS_PROJECT_ID", "ATLAS_PUBLIC_KEY", "ATLAS_PRIVATE_KEY"):
        if not env.get(key):
            print(f"ERROR: {key} is missing from .env")
            return 1
    existing = env_path.read_text() if env_path.exists() else ""
    if "ATLAS_WRITE_URI=" in existing or "ATLAS_READ_URI=" in existing:
        print("ERROR: .env already has ATLAS_WRITE_URI or ATLAS_READ_URI. Nothing was changed.")
        return 1

    api = make_api(env["ATLAS_PROJECT_ID"], env["ATLAS_PUBLIC_KEY"], env["ATLAS_PRIVATE_KEY"])
    created: list[str] = []
    succeeded = False
    try:
        print("1/6 Finding the cluster")
        host = find_cluster(api, env.get("ATLAS_CLUSTER_NAME"))
        for name in (WRITER, READER):
            if user_exists(api, name):
                print(f"ERROR: database user {name} already exists, so its password is unknown. "
                      "Delete it in Atlas (Database Access) and run this again. Nothing was changed.")
                return 1

        print("2/6 Allowing this PC's IP address")
        ip = env.get("ATLAS_ALLOW_IP") or get_ip()
        ipaddress.ip_address(ip)
        try:
            api.call("POST", "/accessList", [{"ipAddress": ip, "comment": "gi-trace demo"}])
        except AtlasError as e:
            if e.status != 409:
                raise

        print("3/6 Creating the role and users")
        role_ok = create_append_only_role(api)
        passwords = {WRITER: new_password(), READER: new_password(), SETUP: new_password()}
        if user_exists(api, SETUP):
            api.call("DELETE", f"/databaseUsers/admin/{SETUP}")
        writer_roles = ([{"roleName": ROLE, "databaseName": "admin"}] if role_ok
                        else [{"roleName": "readWrite", "databaseName": DB}])
        create_user(api, WRITER, passwords[WRITER], writer_roles)
        created.append(WRITER)
        create_user(api, READER, passwords[READER], [{"roleName": "read", "databaseName": DB}])
        created.append(READER)
        create_user(api, SETUP, passwords[SETUP],
                    [{"roleName": "readWrite", "databaseName": DB}, {"roleName": "dbAdmin", "databaseName": DB}])
        created.append(SETUP)

        print("4/6 Creating collections and indexes (waits up to 2 minutes for the new user)")
        setup_client = wait_for_login(connect, uri_for(SETUP, passwords[SETUP], host), sleep)
        build_schema(setup_client)

        print("5/6 Checking permissions")
        writer_uri, reader_uri = uri_for(WRITER, passwords[WRITER], host), uri_for(READER, passwords[READER], host)
        checks = check_permissions(connect, host, writer_uri, reader_uri, setup_client, delete_blocked=role_ok)
        failed = False
        for label, ok, required in checks:
            status = "ok" if ok else ("FAILED" if required else "NOT ENFORCED (built-in role in use)")
            print(f"  {label}: {status}")
            failed = failed or (required and not ok)
        if failed:
            print("ERROR: a required check failed. .env was not changed.")
            return 1

        print("6/6 Writing .env")
        with open(env_path, "a", encoding="ascii") as f:
            if existing and not existing.endswith("\n"):
                f.write("\n")
            f.write(f"ATLAS_WRITE_URI={writer_uri}\nATLAS_READ_URI={reader_uri}\n")
        succeeded = True
        return 0
    except Exception as e:  # report and fall through to the cleanup below
        print(f"ERROR: {e}")
        return 1
    finally:
        # gi_setup is always removed; the other two only if the run failed (their passwords are lost).
        for name in created:
            if name == SETUP or not succeeded:
                try:
                    api.call("DELETE", f"/databaseUsers/admin/{name}")
                    print(f"Removed temporary user {name}.")
                except Exception as e:
                    print(f"WARNING: could not delete {name} ({e}). Delete it in Atlas, Database Access.")


def main() -> int:
    require_container()
    from dotenv import dotenv_values
    env = {**dotenv_values(ENV_FILE), **os.environ}
    code = run(env, ENV_FILE, AtlasAPI, real_connect, public_ip, time.sleep)
    if code == 0:
        print("\nDONE. ATLAS_WRITE_URI and ATLAS_READ_URI are in .env.")
        print("Now delete the API key in Atlas (Access Manager, API Keys) and remove the three ATLAS_PROJECT/KEY lines.")
    return code


if __name__ == "__main__":
    sys.exit(main())
