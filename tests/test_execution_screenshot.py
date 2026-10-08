"""Screenshots follow the pinned execution desktop."""
from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from channel.types import InboundMessage
from desktop_computer_requests import computer_requests_path, get_computer_task
from desktop_observe_requests import create_observe_request, load_observe_requests
from handlers.tools.observe_tools import exec_desktop_observe_request
from handlers.workers import PhysicalOrigin, PhysicalOriginPort
from nodes.state import get_desktop_runtime, record_heartbeat, register_desktop_node


def _settings(root: Path, **overrides):
    from config import Settings

    base = Settings(
        telegram_bot_token="test-token",
        telegram_allowed_user_id=1,
        codex_workspace_root=root,
        codex_bin="codex",
        codex_task_root=root / "tasks",
        codex_model=None,
        codex_timeout_seconds=30,
        codex_retry_429_delays_seconds=(),
        telegram_progress_seconds=1,
        codex_memory_root=root,
        user_timezone="UTC",
        agents_enabled=True,
        agent_desktops_enabled=True,
        conveyor_computer_use_enabled=True,
        conveyor_computer_direct_enabled=True,
        conveyor_computer_always_direct=True,
        conveyor_desktop_upload_enabled=True,
        conveyor_desktop_node_enabled=True,
        conveyor_desktop_node_id="vps-desktop",
        conveyor_desktop_screenshot_helper="/usr/bin/true",
    )
    return replace(base, **overrides)


def _msg(chat_id: str, text: str = "截图", channel: str = "web") -> InboundMessage:
    return InboundMessage(
        channel=channel,  # type: ignore[arg-type]
        operator_id="web-console",
        chat_id=chat_id,
        message_id="m1",
        text=text,
    )


class _Images:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str | None]] = []

    async def send_image(self, chat_id: str, image_path: str, *, caption: str | None = None) -> None:
        self.sent.append((chat_id, image_path, caption))


def _install_loop(test: unittest.TestCase, settings):
    calls: list[tuple[str, str]] = []

    async def _loop(settings, goal, *, task_id, backend, **kwargs):
        task = get_computer_task(settings, task_id) or {}
        scope = str(task.get("takeover_scope") or "default")
        node = settings.conveyor_desktop_node_id if scope == "default" else f"x11:{scope}"
        from desktop_computer_requests import append_trajectory
        from desktop_screenshot import ensure_screenshot_dir

        screenshot_id = f"shot-{task_id}"
        directory = ensure_screenshot_dir(settings)
        image = directory / f"{screenshot_id}.png"
        from io import BytesIO
        from PIL import Image

        buffer = BytesIO()
        Image.new("RGB", (2, 2), (12, 24, 36)).save(buffer, format="PNG")
        image.write_bytes(buffer.getvalue())
        record = {
            "screenshot_id": screenshot_id,
            "path": str(image.resolve()),
            "sha256": "abc",
            "width": 1,
            "height": 1,
            "node_id": node,
            "bytes": image.stat().st_size,
        }
        (directory / f"{screenshot_id}.json").write_text(json.dumps(record), encoding="utf-8")
        append_trajectory(settings, task_id, {
            "action_type": "observe",
            "result_ok": True,
            "screenshot_id": screenshot_id,
        })
        calls.append((type(backend).__name__, scope))
        return {"ok": True, "task_id": task_id, "steps_used": 1}

    patch = mock.patch("desktop_computer_loop.run_computer_loop", _loop)
    test.addCleanup(patch.stop)
    patch.start()
    return calls


class ExecutionScreenshotTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.settings = _settings(self.root)
        self.calls = _install_loop(self, self.settings)
        from agents import AgentStore
        self.store = AgentStore(self.settings)

    def test_canonical_main_uses_host_computer_not_observe_queue(self) -> None:
        import asyncio
        text = asyncio.run(exec_desktop_observe_request(
            self.settings, _msg("agent-default"), "截图", port=_Images(),
        ))
        self.assertIn("VPS共享桌面", text)
        self.assertIn("Conveyor › 主会话", text)
        self.assertIn("执行节点：vps-desktop", text)
        self.assertNotIn("Mac", text)
        self.assertEqual(self.calls, [("HttpComputerBackend", "default")])
        self.assertEqual(load_observe_requests(self.settings), {})
        store = json.loads(computer_requests_path(self.settings).read_text(encoding="utf-8"))
        record = next(iter(store["tasks"].values()))
        self.assertEqual(record["chat_id"], "agent-default")
        self.assertEqual(record["takeover_scope"], "default")

    def test_secondary_session_shares_agent_desktop(self) -> None:
        import asyncio
        from worker_sessions import WorkerSessionStore
        agent = self.store.create({"name": "Alpha"})
        display = self.store.ensure_display(agent["id"])
        secondary = WorkerSessionStore(self.settings).create(agent["id"], title="设计")
        text = asyncio.run(exec_desktop_observe_request(
            self.settings, _msg(secondary["source_chat_id"]), "截屏", port=_Images(),
        ))
        self.assertIn(f"Agent独立桌面(:{display})", text)
        self.assertIn("Alpha › 设计", text)
        self.assertEqual(self.calls[0][0], "X11ComputerBackend")
        self.assertEqual(self.calls[0][1], f"agent:{agent['id']}")

    def test_other_agent_is_isolated(self) -> None:
        import asyncio
        alpha = self.store.create({"name": "Alpha"})
        beta = self.store.create({"name": "Beta"})
        self.store.ensure_display(alpha["id"])
        self.store.ensure_display(beta["id"])
        asyncio.run(exec_desktop_observe_request(
            self.settings, _msg(f"agent-{alpha['id']}"), "截图", port=_Images(),
        ))
        asyncio.run(exec_desktop_observe_request(
            self.settings, _msg(f"agent-{beta['id']}"), "截图", port=_Images(),
        ))
        self.assertEqual(self.calls[0][1], f"agent:{alpha['id']}")
        self.assertEqual(self.calls[1][1], f"agent:{beta['id']}")
        self.assertNotEqual(self.calls[0][1], self.calls[1][1])

    def test_archived_or_missing_desktop_fails_closed(self) -> None:
        import asyncio
        agent = self.store.create({"name": "Alpha"})
        self.store.ensure_display(agent["id"])
        self.store.archive(agent["id"])
        text = asyncio.run(exec_desktop_observe_request(
            self.settings, _msg(f"agent-{agent['id']}"), "截图", port=_Images(),
        ))
        self.assertIn("不会改截共享桌面", text)
        bare = self.store.create({"name": "Bare"})
        text = asyncio.run(exec_desktop_observe_request(
            self.settings, _msg(f"agent-{bare['id']}"), "截图", port=_Images(),
        ))
        self.assertIn("不会改截共享桌面", text)
        self.assertEqual(self.calls, [])
        self.assertEqual(load_observe_requests(self.settings), {})

    def test_physical_delivery_after_session_switch(self) -> None:
        import asyncio
        images = _Images()
        origin = PhysicalOrigin(channel="telegram", operator_id="7", chat_id="phys-9")
        port = PhysicalOriginPort(images, origin, self.settings)
        text = asyncio.run(exec_desktop_observe_request(
            self.settings, _msg("agent-default", channel="web"), "截图", port=port,
        ))
        self.assertIn("缩略图已发送", text)
        self.assertEqual(images.sent[0][0], "phys-9")
        self.assertTrue(images.sent[0][1].endswith(".png"))
        self.assertNotIn(self.root.as_posix(), text)

    def test_explicit_mac_does_not_capture_vps(self) -> None:
        import asyncio
        register_desktop_node(
            self.settings, "vps-desktop", "VPS desktop", "0.3.0",
            {"platform": "Linux"}, poll_observe=True,
        )
        before = list((self.root / "state").glob("desktop_computer_requests.json"))
        text = asyncio.run(exec_desktop_observe_request(
            self.settings, _msg("agent-default"), "帮我截一下 mac 屏幕", port=_Images(),
        ))
        self.assertIn("不会改为截取 VPS", text)
        self.assertEqual(self.calls, [])
        self.assertEqual(load_observe_requests(self.settings), {})
        self.assertEqual(before, list((self.root / "state").glob("desktop_computer_requests.json")))
        unknown = asyncio.run(exec_desktop_observe_request(
            self.settings, _msg("agent-default"), "截图 node:macbook-payton", port=_Images(),
        ))
        self.assertIn("未知桌面节点", unknown)
        self.assertEqual(self.calls, [])

    def test_heartbeat_without_observe_fails_and_real_client_succeeds(self) -> None:
        register_desktop_node(
            self.settings, "vps-desktop", "VPS desktop", "0.3.0", {"platform": "Linux"},
        )
        self.assertFalse(get_desktop_runtime(self.settings, "vps-desktop").get("poll_observe"))
        record_heartbeat(self.settings, "vps-desktop", "idle", "heartbeat", poll_computer=True)
        self.assertFalse(get_desktop_runtime(self.settings, "vps-desktop").get("poll_observe"))
        msg = _msg("agent-default", channel="feishu")
        denied = create_observe_request(self.settings, msg, "截图")
        self.assertEqual(denied.get("error"), "node_does_not_poll_observe")
        record_heartbeat(
            self.settings, "vps-desktop", "idle", "heartbeat",
            poll_computer=True, poll_observe=True,
        )
        allowed = create_observe_request(self.settings, msg, "截图")
        self.assertTrue(allowed.get("ok"))

    def test_agent_reports_its_poll_flag(self) -> None:
        from desktop_agent import send_heartbeat_once
        with mock.patch("desktop_agent.post_json", return_value={"ok": True}) as posted:
            send_heartbeat_once(self.settings, poll_computer=True, poll_observe=False)
        body = posted.call_args.args[2]
        self.assertIs(body["poll_observe"], False)
        self.assertIs(body["poll_computer"], True)

    def test_sharing_disabled_does_not_send(self) -> None:
        import asyncio
        settings = _settings(self.root, conveyor_desktop_upload_enabled=False)
        images = _Images()
        text = asyncio.run(exec_desktop_observe_request(
            settings, _msg("agent-default"), "截图", port=images,
        ))
        self.assertIn("外发已关闭", text)
        self.assertEqual(images.sent, [])

    def test_metadata_only_and_missing_port(self) -> None:
        import asyncio
        text = asyncio.run(exec_desktop_observe_request(
            self.settings, _msg("agent-default"), "截图 --metadata-only", port=_Images(),
        ))
        self.assertIn("仅元数据", text)
        bare = asyncio.run(exec_desktop_observe_request(
            self.settings, _msg("agent-default"), "截图", port=None,
        ))
        self.assertIn("仅元数据", bare)

    def test_default_secondary_uses_host(self) -> None:
        import asyncio
        from worker_sessions import WorkerSessionStore
        secondary = WorkerSessionStore(self.settings).create("default", title="草稿")
        text = asyncio.run(exec_desktop_observe_request(
            self.settings, _msg(secondary["source_chat_id"]), "截图", port=_Images(),
        ))
        self.assertIn("VPS共享桌面", text)
        self.assertIn("执行节点：vps-desktop", text)
        self.assertEqual(self.calls, [("HttpComputerBackend", "default")])

    def test_group_without_binding_has_no_fake_context(self) -> None:
        import asyncio
        from dataclasses import replace as replace_msg
        msg = replace_msg(_msg("group-1", channel="feishu"), chat_type="group")
        text = asyncio.run(exec_desktop_observe_request(
            self.settings, msg, "截图", port=_Images(),
        ))
        self.assertIn("VPS共享桌面", text)
        self.assertNotIn("主会话", text)
        self.assertNotIn("当前：", text)
        self.assertEqual(self.calls, [("HttpComputerBackend", "default")])

    def test_named_configured_host_does_not_override_agent(self) -> None:
        import asyncio
        text = asyncio.run(exec_desktop_observe_request(
            self.settings, _msg("agent-default"), "截图 node:vps-desktop", port=_Images(),
        ))
        self.assertIn("VPS共享桌面", text)
        self.assertEqual(self.calls, [("HttpComputerBackend", "default")])
        agent = self.store.create({"name": "Alpha"})
        self.store.ensure_display(agent["id"])
        refused = asyncio.run(exec_desktop_observe_request(
            self.settings, _msg(f"agent-{agent['id']}"), "screenshot node:vps-desktop", port=_Images(),
        ))
        self.assertIn("不会改截配置的主机桌面", refused)
        self.assertEqual(len(self.calls), 1)

    def test_english_remote_is_not_a_mac_request(self) -> None:
        import asyncio
        text = asyncio.run(exec_desktop_observe_request(
            self.settings, _msg("agent-default"), "take a screenshot of the remote desktop", port=_Images(),
        ))
        self.assertIn("VPS共享桌面", text)
        self.assertNotIn("Mac", text)
        self.assertEqual(self.calls, [("HttpComputerBackend", "default")])

    def test_changed_target_stops_before_capture(self) -> None:
        import asyncio
        import agents
        original = agents.computer_target_for_chat
        answers = iter([
            {"scope": "default"},
            {"scope": "agent:other", "agent_id": "other", "display": 101},
        ])

        def flipped(settings, channel, chat_id):
            try:
                return next(answers)
            except StopIteration:
                return original(settings, channel, chat_id)

        with mock.patch("agents.computer_target_for_chat", flipped):
            text = asyncio.run(exec_desktop_observe_request(
                self.settings, _msg("agent-default"), "截图", port=_Images(),
            ))
        self.assertIn("已停止任务", text)
        self.assertEqual(self.calls, [])
        store = json.loads(computer_requests_path(self.settings).read_text(encoding="utf-8"))
        record = next(iter(store["tasks"].values()))
        self.assertEqual(record["status"], "error")
        self.assertEqual(record["takeover_scope"], "agent:other")

    def test_thumbnail_linux_without_sips_and_oversize_stays_local(self) -> None:
        import asyncio
        from desktop_agent import generate_thumbnail

        source = self.root / "wide.png"
        dest = self.root / "thumb.png"
        from PIL import Image
        Image.new("RGB", (40, 20), (1, 2, 3)).save(source, format="PNG")

        def refuse_shell(command, **kwargs):
            raise AssertionError(command)

        with mock.patch("shutil.which", return_value=None), mock.patch("subprocess.run", refuse_shell):
            self.assertTrue(generate_thumbnail(source, dest, 8, 8, 200000))
        self.assertTrue(dest.is_file())
        self.assertNotEqual(dest.read_bytes(), source.read_bytes())
        with Image.open(dest) as image:
            self.assertLessEqual(image.size[0], 8)
            self.assertLessEqual(image.size[1], 8)
        dest.unlink()
        self.assertFalse(generate_thumbnail(source, dest, 8, 8, 30))
        self.assertFalse(dest.exists())
        self.assertFalse(generate_thumbnail(self.root / "missing.png", dest, 8, 8, 200000))

        images = _Images()
        with mock.patch("desktop_agent.generate_thumbnail", return_value=False):
            text = asyncio.run(exec_desktop_observe_request(
                self.settings, _msg("agent-default"), "截图", port=images,
            ))
        self.assertIn("缩略图生成失败", text)
        self.assertIn("图片未发送", text)
        self.assertEqual(images.sent, [])


if __name__ == "__main__":
    unittest.main()
