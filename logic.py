"""Approved local file plans with exact fingerprints, no overwrite and durable receipts."""

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import sqlite3
import stat
import time

DEFAULTS = {"allowed_root": "", "allowed_operations": ["copy", "move", "quarantine"],
            "max_operations": 20, "max_bytes": 10485760, "plan_ttl_sec": 900,
            "max_plans": 1000, "timeout_sec": 30}
QUARANTINE = ".blox-quarantine"


class FileError(ValueError):
    """Expected failure without disclosing raw file data or subprocess output."""


def configuration(raw):
    """Validate policy independently of the eventual path and event."""
    if not isinstance(raw, dict) or set(raw) - set(DEFAULTS) - {"execution", "runtime_path", "runtime_path_label"}:
        raise FileError("Unknown setting.")
    result = {**DEFAULTS, **{k: v for k, v in raw.items() if k in DEFAULTS}}
    if not isinstance(result["allowed_root"], str) or len(result["allowed_root"]) > 4096 or "\x00" in result["allowed_root"]:
        raise FileError("allowed_root must be a directory path.")
    operations = result["allowed_operations"]
    if not isinstance(operations, list) or not operations or any(not isinstance(x, str) for x in operations):
        raise FileError("allowed_operations must be a non-empty array.")
    if not set(operations) <= {"copy", "move", "quarantine"} or len(set(operations)) != len(operations):
        raise FileError("Allowed operations are distinct copy, move and quarantine values.")
    for key, low, high in (("max_operations", 1, 100), ("max_bytes", 1024, 52428800),
                         ("plan_ttl_sec", 10, 86400), ("max_plans", 1, 10000), ("timeout_sec", 1, 120)):
        if type(result[key]) is not int or not low <= result[key] <= high:
            raise FileError(f"{key} must be an integer between {low} and {high}.")
    return result


def encoded(value):
    """Canonical finite JSON for request and plan identity."""
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True)


def message(value):
    """Only bounded objects may enter the planner or approval path."""
    if isinstance(value, str):
        if len(value.encode("utf-8")) > 65536:
            raise FileError("Input exceeds 64 KiB.")
        value = json.loads(value)
    if not isinstance(value, dict) or len(encoded(value).encode("utf-8")) > 65536:
        raise FileError("A JSON object of at most 64 KiB is required.")
    return value


def check(deadline, cancel):
    """Stop between bounded reads and mutations; never start an automatic retry."""
    if cancel():
        raise FileError("File operation cancelled.")
    if time.monotonic() >= deadline:
        raise FileError("File operation exceeded timeout_sec.")


def relative(value, *, internal=False):
    """Require explicit file names, not globs, absolute paths or symbolic traversals."""
    if not isinstance(value, str) or not value or len(value) > 1024 or "\x00" in value or "\\" in value:
        raise FileError("A bounded relative file path is required.")
    path = PurePosixPath(value)
    if path.is_absolute() or any(p in {"", ".", ".."} for p in value.split("/")):
        raise FileError("Paths must be relative without empty, dot or parent components.")
    if not internal and path.parts[0] == QUARANTINE:
        raise FileError("The quarantine directory is reserved; recovery requires a separately reviewed operation.")
    return path.parts


@contextmanager
def root_descriptor(config, runtime_root):
    """Pin an explicit root and reject platforms without descriptor-based no-follow operations."""
    if not config["allowed_root"].strip():
        raise FileError("Set an explicit allowed_root before planning file changes.")
    if os.name != "posix" or not hasattr(os, "O_NOFOLLOW"):
        raise FileError("Descriptor-based file operations require a POSIX host.")
    root = Path(config["allowed_root"]).expanduser()
    root = (root if root.is_absolute() else Path(runtime_root) / root).absolute()
    if root.is_symlink() or not root.is_dir() or root.resolve() != root:
        raise FileError("allowed_root must be an existing canonical directory without symbolic parents.")
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        yield root, fd
    finally:
        os.close(fd)


@contextmanager
def parent_descriptor(root_fd, path, *, quarantine=False):
    """Open each existing parent without following a symbolic link."""
    parts = relative(path, internal=quarantine)
    fd = os.dup(root_fd)
    try:
        for name in parts[:-1]:
            next_fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        yield fd, parts[-1]
    finally:
        os.close(fd)


def fingerprint(root_fd, path, config, deadline, cancel):
    """Read a stable regular file through a no-follow descriptor."""
    with parent_descriptor(root_fd, path) as (parent, name):
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        with os.fdopen(fd, "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_size > config["max_bytes"]:
                raise FileError("Sources must be bounded regular files.")
            digest, count = hashlib.sha256(), 0
            while chunk := stream.read(65536):
                check(deadline, cancel)
                count += len(chunk)
                if count > config["max_bytes"]:
                    raise FileError("Source exceeds max_bytes.")
                digest.update(chunk)
            after = os.fstat(stream.fileno())
            if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise FileError("A source changed while being read.")
            return {"sha256": digest.hexdigest(), "bytes": count, "device": before.st_dev,
                    "inode": before.st_ino, "mtime_ns": before.st_mtime_ns, "mode": stat.S_IMODE(before.st_mode)}


def absent(parent, name):
    """Any existing entry, including a dangling symlink, is a collision."""
    try:
        os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        return
    raise FileError("A destination already exists; no overwrite is allowed.")


@contextmanager
def ledger(directory):
    """Use only the framework-provided per-node storage directory."""
    directory = Path(directory)
    if not directory.is_absolute() or directory.is_symlink() or not directory.is_dir():
        raise FileError("Canonical persistent block storage is required.")
    path = directory / "file-plans.sqlite3"
    if path.is_symlink():
        raise FileError("The ledger must not be a symbolic link.")
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        if not path.is_file():
            raise FileError("The ledger must be a regular file.")
    else:
        os.close(fd)
    db = sqlite3.connect(path, timeout=2)
    try:
        db.execute("PRAGMA busy_timeout=2000")
        db.execute("PRAGMA synchronous=FULL")
        db.execute("PRAGMA max_page_count=32768")
        if db.execute("PRAGMA user_version").fetchone()[0] not in (0, 1):
            raise FileError("Unsupported ledger schema; no reset or migration is performed.")
        db.execute("CREATE TABLE IF NOT EXISTS plans (id TEXT PRIMARY KEY, revision INTEGER, request_hash TEXT, plan TEXT, status TEXT, receipt TEXT)")
        db.execute("PRAGMA user_version=1")
        db.commit()
        yield db
    finally:
        db.close()


def build_plan(value, config, runtime_root, db, deadline, cancel):
    """Store an expiring approval proposal, without changing the managed file tree."""
    request = message(value)
    if set(request) != {"id", "revision", "operations"} or not isinstance(request["id"], str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", request["id"]):
        raise FileError("Request requires id, revision and operations.")
    if type(request["revision"]) is not int or not 1 <= request["revision"] <= 1000000000:
        raise FileError("revision must be a positive integer.")
    operations = request["operations"]
    if not isinstance(operations, list) or not 1 <= len(operations) <= config["max_operations"]:
        raise FileError("operations must be a non-empty bounded array.")
    request_hash = hashlib.sha256(encoded(request).encode()).hexdigest()
    db.execute("BEGIN IMMEDIATE")
    previous = db.execute("SELECT revision,request_hash,plan,status FROM plans WHERE id=?", (request["id"],)).fetchone()
    if previous:
        if request["revision"] <= previous[0]:
            if request["revision"] == previous[0] and request_hash == previous[1] and previous[3] == "pending":
                return {"plan": json.loads(previous[2])}
            raise FileError("This plan id/revision was already used; a newer revision is required.")
        if previous[3] == "applying":
            raise FileError("The previous apply has an uncertain outcome; inspect its files and ledger first.")
    elif db.execute("SELECT COUNT(*) FROM plans").fetchone()[0] >= config["max_plans"]:
        raise FileError("Plan ledger is full; no automatic removal is performed.")
    with root_descriptor(config, runtime_root) as (root, root_fd):
        total, items, paths, inodes = 0, [], set(), set()
        nonce = secrets.token_hex(12)
        for index, operation in enumerate(operations):
            check(deadline, cancel)
            if not isinstance(operation, dict) or operation.get("op") not in config["allowed_operations"]:
                raise FileError("An operation is invalid or disabled by policy.")
            op = operation["op"]
            if set(operation) != ({"op", "source"} if op == "quarantine" else {"op", "source", "target"}):
                raise FileError("Operations require op/source and target except for quarantine.")
            parts = relative(operation["source"])
            target = f"{QUARANTINE}/{nonce}-{index}-{parts[-1]}" if op == "quarantine" else operation["target"]
            relative(target, internal=op == "quarantine")
            if operation["source"] in paths or target in paths or operation["source"] == target:
                raise FileError("Source and target paths must be distinct across all operations.")
            paths.update((operation["source"], target))
            source = fingerprint(root_fd, operation["source"], config, deadline, cancel)
            inode = (source["device"], source["inode"])
            if inode in inodes:
                raise FileError("Multiple source names reference the same inode.")
            inodes.add(inode)
            total += source["bytes"]
            if total > config["max_bytes"]:
                raise FileError("Combined source bytes exceed max_bytes.")
            try:
                with parent_descriptor(root_fd, target, quarantine=op == "quarantine") as (parent, name):
                    absent(parent, name)
                    if op != "copy" and os.fstat(parent).st_dev != source["device"]:
                        raise FileError("Moves and quarantine require the same filesystem; use copy otherwise.")
            except FileNotFoundError:
                if op != "quarantine":
                    raise FileError("Destination parent directory must already exist.")
                # Only the fixed quarantine directory may be created, after approval.
                if os.fstat(root_fd).st_dev != source["device"]:
                    raise FileError("Quarantine requires the same filesystem.")
            items.append({"op": op, "source": operation["source"], "target": target, "fingerprint": source})
        now = time.time()
        plan = {"id": request["id"], "revision": request["revision"], "action": "file_operations",
                "created_at": now, "expires_at": now + config["plan_ttl_sec"], "nonce": nonce,
                "root": str(root), "root_device": os.fstat(root_fd).st_dev, "root_inode": os.fstat(root_fd).st_ino,
                "operations": items, "bytes": total}
        plan["plan_hash"] = hashlib.sha256(encoded(plan).encode()).hexdigest()
        if len(encoded(plan).encode()) > 131072:
            raise FileError("Plan exceeds 128 KiB.")
        db.execute("INSERT INTO plans VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET revision=excluded.revision,request_hash=excluded.request_hash,plan=excluded.plan,status=excluded.status,receipt=NULL",
                   (plan["id"], plan["revision"], request_hash, encoded(plan), "pending", None))
        db.commit()
        return {"plan": plan}


def apply_one(root_fd, operation, config, deadline, cancel, receipt):
    """Create targets exclusively; move/quarantine use a same-filesystem link then unlink."""
    source = operation["source"]
    target = operation["target"]
    with parent_descriptor(root_fd, source) as (src_parent, src_name), parent_descriptor(
            root_fd, target, quarantine=operation["op"] == "quarantine") as (dst_parent, dst_name):
        absent(dst_parent, dst_name)
        check(deadline, cancel)
        expected = operation["fingerprint"]
        actual = os.stat(src_name, dir_fd=src_parent, follow_symlinks=False)
        if (actual.st_dev, actual.st_ino, actual.st_size, actual.st_mtime_ns) != (
                expected["device"], expected["inode"], expected["bytes"], expected["mtime_ns"]):
            raise FileError("A source changed since approval.")
        if operation["op"] == "copy":
            temporary = ".blox-copy-" + secrets.token_hex(12)
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=dst_parent)
            try:
                digest, count = hashlib.sha256(), 0
                with os.fdopen(fd, "wb") as output:
                    read_fd = os.open(src_name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=src_parent)
                    with os.fdopen(read_fd, "rb") as incoming:
                        if not stat.S_ISREG(os.fstat(incoming.fileno()).st_mode):
                            raise FileError("The source is no longer a regular file.")
                        while chunk := incoming.read(65536):
                            check(deadline, cancel)
                            count += len(chunk)
                            if count > expected["bytes"]:
                                raise FileError("Source changed during copy.")
                            output.write(chunk)
                            digest.update(chunk)
                    if count != expected["bytes"] or digest.hexdigest() != expected["sha256"]:
                        raise FileError("Source content changed during copy.")
                    output.flush()
                    os.fsync(output.fileno())
                os.link(temporary, dst_name, src_dir_fd=dst_parent, dst_dir_fd=dst_parent, follow_symlinks=False)
                receipt["target_created"] = True
            finally:
                os.unlink(temporary, dir_fd=dst_parent)
        else:
            os.link(src_name, dst_name, src_dir_fd=src_parent, dst_dir_fd=dst_parent, follow_symlinks=False)
            receipt["target_created"] = True
            linked = os.stat(dst_name, dir_fd=dst_parent, follow_symlinks=False)
            current = os.stat(src_name, dir_fd=src_parent, follow_symlinks=False)
            if (linked.st_dev, linked.st_ino, current.st_dev, current.st_ino) != (
                    expected["device"], expected["inode"], expected["device"], expected["inode"]):
                raise FileError("Source identity changed during move; inspect the partial receipt.")
            os.unlink(src_name, dir_fd=src_parent)
            receipt["source_removed"] = True
            os.fsync(src_parent)
        os.fsync(dst_parent)
        receipt["status"] = "applied"


def decide(value, config, runtime_root, db, deadline, cancel):
    """Approve only a fresh exact plan, journal admission before touching source files."""
    decision = message(value)
    if set(decision) != {"id", "revision", "plan_hash", "decision"} or decision["decision"] not in {"approve", "reject"}:
        raise FileError("Decision requires id, revision, plan_hash and approve/reject.")
    if not isinstance(decision["id"], str) or type(decision["revision"]) is not int or not isinstance(decision["plan_hash"], str):
        raise FileError("Decision identity is invalid.")
    db.execute("BEGIN IMMEDIATE")
    row = db.execute("SELECT plan,status,receipt FROM plans WHERE id=?", (decision["id"],)).fetchone()
    if not row:
        raise FileError("The referenced plan does not exist in this block instance.")
    plan, state = json.loads(row[0]), row[1]
    if decision["revision"] != plan["revision"] or not secrets.compare_digest(decision["plan_hash"], plan["plan_hash"]):
        raise FileError("Approval does not match the exact current plan.")
    if state != "pending":
        return {"receipt": json.loads(row[2]) if row[2] else {"id": plan["id"], "status": state},
                "issues": [{"code": "already_processed", "status": state, "retry_allowed": False}]}
    receipt = {"id": plan["id"], "revision": plan["revision"], "plan_hash": plan["plan_hash"],
               "status": "rejected" if decision["decision"] == "reject" else "applying", "operations": []}
    if time.time() >= plan["expires_at"]:
        receipt["status"] = "expired"
    if receipt["status"] != "applying":
        db.execute("UPDATE plans SET status=?,receipt=? WHERE id=?", (receipt["status"], encoded(receipt), plan["id"]))
        db.commit()
        return {"receipt": receipt, "issues": [{"code": receipt["status"]}]}
    # A committed applying state prevents automatic re-execution after a crash or lost result.
    db.execute("UPDATE plans SET status='applying',receipt=? WHERE id=?", (encoded(receipt), plan["id"]))
    db.commit()
    issues = []
    try:
        if any(op["op"] not in config["allowed_operations"] for op in plan["operations"]) or plan["bytes"] > config["max_bytes"] or len(plan["operations"]) > config["max_operations"]:
            raise FileError("The current policy no longer permits this plan.")
        with root_descriptor(config, runtime_root) as (root, root_fd):
            identity = os.fstat(root_fd)
            if str(root) != plan["root"] or (identity.st_dev, identity.st_ino) != (plan["root_device"], plan["root_inode"]):
                raise FileError("The configured root changed since planning.")
            # Recheck every source and destination before the first intended mutation.
            for operation in plan["operations"]:
                if fingerprint(root_fd, operation["source"], config, deadline, cancel) != operation["fingerprint"]:
                    raise FileError("Source content or identity changed since planning.")
                try:
                    with parent_descriptor(root_fd, operation["target"], quarantine=operation["op"] == "quarantine") as (parent, name):
                        absent(parent, name)
                except FileNotFoundError:
                    if operation["op"] != "quarantine":
                        raise FileError("A destination parent is missing.")
            if any(op["op"] == "quarantine" for op in plan["operations"]):
                try:
                    os.mkdir(QUARANTINE, mode=0o700, dir_fd=root_fd)
                    receipt["quarantine_directory_created"] = True
                    os.fsync(root_fd)
                except FileExistsError:
                    pass
            for operation in plan["operations"]:
                item = {"op": operation["op"], "source": operation["source"], "target": operation["target"],
                        "sha256": operation["fingerprint"]["sha256"], "bytes": operation["fingerprint"]["bytes"],
                        "status": "started", "target_created": False, "source_removed": False}
                receipt["operations"].append(item)
                apply_one(root_fd, operation, config, deadline, cancel, item)
                db.execute("UPDATE plans SET receipt=? WHERE id=?", (encoded(receipt), plan["id"]))
                db.commit()
            receipt["status"] = "applied"
    except (OSError, FileError) as exc:
        mutated = receipt.get("quarantine_directory_created") or any(x["target_created"] for x in receipt["operations"])
        receipt["status"] = "partial" if mutated else "refused"
        issues.append({"code": "apply_incomplete", "detail": str(exc) if isinstance(exc, FileError) else "Filesystem operation failed; inspect the receipt before any new plan.", "retry_allowed": False})
    receipt["completed_at"] = time.time()
    db.execute("UPDATE plans SET status=?,receipt=? WHERE id=?", (receipt["status"], encoded(receipt), plan["id"]))
    db.commit()
    return {"receipt": receipt, "issues": issues}


def process(value, port, raw_config, storage, runtime_root, cancel=lambda: False):
    """One event, one bounded transaction/plan; persistence never implies permission."""
    config = configuration(raw_config)
    deadline = time.monotonic() + config["timeout_sec"]
    check(deadline, cancel)
    with ledger(storage) as db:
        if port == "request":
            return build_plan(value, config, runtime_root, db, deadline, cancel)
        if port == "decision":
            return decide(value, config, runtime_root, db, deadline, cancel)
        raise FileError("Unknown input port.")
