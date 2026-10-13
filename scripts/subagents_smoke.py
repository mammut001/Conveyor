"""Env-free gate for background child isolation and durable reconciliation."""
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import subagents


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="conveyor-subagents-") as root:
        disabled = SimpleNamespace(codex_memory_root=Path(root), chat_tools_enabled=True,
                                   background_subagents_enabled=False)
        assert not subagents.enabled(disabled)
        assert not subagents.schemas(disabled)
        assert not (Path(root) / "subagents.db").exists()
        subagents.reconcile(disabled)
        with subagents._connect(disabled) as db:
            db.execute("INSERT INTO children "
                       "(id,parent,operator,channel,chat_id,goal,status,created_at) "
                       "VALUES (?,?,?,?,?,?,?,?)",
                       ("dead", "parent", "owner", "web", "chat", "task", "running", 1))
        subagents.reconcile(disabled, force=True)
        assert subagents.status(disabled, "dead")["status"] == "interrupted"
    print("subagents smoke: PASS")


if __name__ == "__main__":
    main()
