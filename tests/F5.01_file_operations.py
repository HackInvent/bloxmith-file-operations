"""FB1/FB2/FB3: explicit plans, no overwrites, durable admission and partial receipts."""

from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import json
import os
import sqlite3
import sys
from tempfile import TemporaryDirectory
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]
from blocs.file_operations import logic


def rejected(call):
    """Require a refusal; all fixtures are disposable and explicitly in scope."""
    try:
        call()
    except (ValueError, OSError):
        return
    raise AssertionError("Unsafe operation accepted")


def main():
    """Test only files created by this suite, never any developer directory."""
    with TemporaryDirectory(prefix="file-plans-") as temporary:
        root = Path(temporary).resolve()
        tree, storage = root / "tree", root / "storage"
        tree.mkdir()
        storage.mkdir()
        (tree / "target").mkdir()
        cfg = {"allowed_root": str(tree)}

        def event(value, port="request", override=None):
            return logic.process(value, port, {**cfg, **(override or {})}, storage, root)

        def plan(ident, op="copy", source="one.txt", target=None, revision=1):
            operations = [{"op": op, "source": source}]
            if op != "quarantine":
                operations[0]["target"] = target or f"target/{ident}.txt"
            return event({"id": ident, "revision": revision, "operations": operations})["plan"]

        def decision(proposal, action="approve", **overrides):
            return {"id": proposal["id"], "revision": proposal["revision"], "plan_hash": proposal["plan_hash"],
                    "decision": action, **overrides}

        (tree / "one.txt").write_text("Original fixture", encoding="utf-8")
        proposal = plan("copy")
        assert not (tree / "target/copy.txt").exists() and (tree / "one.txt").exists()
        rejected(lambda: event(decision(proposal, plan_hash="0" * 64), "decision"))
        result = event(decision(proposal), "decision")
        assert result["receipt"]["status"] == "applied" and not result["issues"]
        assert (tree / "one.txt").read_bytes() == (tree / "target/copy.txt").read_bytes()
        assert (tree / "target/copy.txt").stat().st_mode & 0o777 == 0o600
        repeated = event(decision(proposal), "decision")
        assert repeated["issues"][0]["code"] == "already_processed"
        rejected(lambda: plan("collision", target="target/copy.txt"))
        moved = plan("move", op="move")
        assert event(decision(moved), "decision")["receipt"]["operations"][0]["source_removed"]
        assert not (tree / "one.txt").exists() and (tree / "target/move.txt").exists()
        quarantine = plan("quarantine", op="quarantine", source="target/move.txt")
        assert not (tree / logic.QUARANTINE).exists()
        assert event(decision(quarantine), "decision")["receipt"]["status"] == "applied"
        assert (tree / quarantine["operations"][0]["target"]).read_text() == "Original fixture"
        assert not (tree / "target/move.txt").exists()
        (tree / "one.txt").write_text("Original fixture", encoding="utf-8")
        refused = plan("reject")
        assert event(decision(refused, "reject"), "decision")["receipt"]["status"] == "rejected"
        assert not (tree / "target/reject.txt").exists()
        stale = plan("stale")
        (tree / "one.txt").write_text("Changed", encoding="utf-8")
        assert event(decision(stale), "decision")["receipt"]["status"] == "refused"
        assert not (tree / "target/stale.txt").exists()
        old = plan("revision")
        newer = plan("revision", revision=2)
        rejected(lambda: event(decision(old), "decision"))
        assert event(decision(newer), "decision")["receipt"]["status"] == "applied"
        expired = plan("expired")
        with patch.object(logic.time, "time", return_value=expired["expires_at"] + 1):
            assert event(decision(expired), "decision")["receipt"]["status"] == "expired"
        crash = plan("crash")
        with sqlite3.connect(storage / "file-plans.sqlite3") as db:
            db.execute("UPDATE plans SET status='applying' WHERE id='crash'")
        uncertain = event(decision(crash), "decision")
        assert uncertain["receipt"]["status"] == "applying" and uncertain["issues"][0]["retry_allowed"] is False
        rejected(lambda: plan("crash", revision=2))
        assert not (tree / "target/crash.txt").exists()
        (root / "outside.txt").write_text("Outside", encoding="utf-8")
        (tree / "escape.txt").symlink_to(root / "outside.txt")
        rejected(lambda: plan("escape", source="escape.txt"))
        rejected(lambda: plan("escape-parent", source="../outside.txt"))
        (tree / "linked").symlink_to(root, target_is_directory=True)
        rejected(lambda: plan("escape-target", target="linked/should-not-exist.txt"))
        rejected(lambda: event({"id": "duplicate", "revision": 1, "operations": [
            {"op": "copy", "source": "one.txt", "target": "target/new.txt"},
            {"op": "move", "source": "target/new.txt", "target": "target/again.txt"}]}))
        os.link(tree / "one.txt", tree / "hardlink.txt")
        rejected(lambda: event({"id": "same-inode", "revision": 1, "operations": [
            {"op": "copy", "source": "one.txt", "target": "target/new.txt"},
            {"op": "copy", "source": "hardlink.txt", "target": "target/again.txt"}]}))
        (tree / "two.txt").write_text("Second fixture", encoding="utf-8")
        partial = event({"id": "partial", "revision": 1, "operations": [
            {"op": "copy", "source": "one.txt", "target": "target/first.txt"},
            {"op": "copy", "source": "two.txt", "target": "target/second.txt"}]})["plan"]
        original = logic.apply_one

        def fail_second(*args):
            if args[1]["source"] == "two.txt":
                raise OSError("simulated failure")
            return original(*args)

        with patch.object(logic, "apply_one", side_effect=fail_second):
            outcome = event(decision(partial), "decision")
        assert outcome["receipt"]["status"] == "partial"
        assert (tree / "target/first.txt").exists() and not (tree / "target/second.txt").exists()
        assert event(decision(partial), "decision")["issues"][0]["code"] == "already_processed"
        assert not list((tree / "target").glob(".blox-copy-*"))
        rejected(lambda: event({"id": "empty", "revision": 1, "operations": []}))
        rejected(lambda: logic.configuration({"max_operations": True}))
        rejected(lambda: logic.configuration({"allowed_operations": ["delete"]}))
        rejected(lambda: logic.process({}, "request", {}, storage, root, cancel=lambda: True))
        policy = plan("policy")
        assert event(decision(policy), "decision", {"allowed_operations": ["quarantine"]})["receipt"]["status"] == "refused"
        concurrent = plan("concurrent")
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(lambda _: event(decision(concurrent), "decision"), range(2)))
        assert sum(not result["issues"] for result in outcomes) == 1
        assert (tree / "target/concurrent.txt").read_text() == "Changed"
    print("[ok] File Operations plans, exact approval, persistence and safe partial outcomes")


if __name__ == "__main__":
    main()
