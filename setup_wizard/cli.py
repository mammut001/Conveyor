"""setup_wizard/cli.py — `conveyor setup` entry point.

    python -m setup_wizard              # dashboard: pick modules interactively
    python -m setup_wizard email        # configure one module
    python -m setup_wizard --status     # status table only
    python -m setup_wizard --check      # live-test every configured module

Each module's changes are previewed (secrets masked) and written to `.env`
right after the operator confirms, so an interrupted session keeps what was
already saved. Services that need a restart are reported at the end (and
written to $CONVEYOR_SETUP_RESTART_FILE for the `conveyor` wrapper).
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from setup_wizard.envfile import EnvFile
from setup_wizard.modules import (
    BY_KEY, MISSING, MODULES, OK, STATUS_ICON, Change, Module, Skip, describe_change,
)
from setup_wizard.ui import UI, Abort, Option, pad

SERVICE_LABELS = {"telegram": "Telegram 机器人", "feishu": "飞书机器人", "web": "Web 控制台"}


def default_env_path() -> Path:
    root = os.environ.get("CONVEYOR_DIR")
    if root:
        return Path(root).expanduser() / ".env"
    return Path(__file__).resolve().parents[1] / ".env"


def render_status(ui: UI, env: dict[str, str]) -> None:
    for m in MODULES:
        state, text = m.status(env)
        tag = "必填" if m.required else "可选"
        ui.print(f"  {STATUS_ICON[state]} {ui.bold(pad(m.title, 18))}{ui.dim(tag)}  {text}")


def run_module(ui: UI, envfile: EnvFile, module: Module) -> Change | None:
    env = envfile.read()
    ui.section(f"{module.title} — {module.blurb}")
    try:
        change = module.run(ui, env)
    except Skip:
        ui.warn("已跳过，没有修改。")
        return None
    if not change:
        ui.info("没有需要保存的修改。")
        return None
    ui.print()
    ui.info(ui.bold("将写入 .env："))
    for line in describe_change(change):
        ui.info("  " + line)
    if not ui.confirm("保存？", default=True):
        ui.warn("没有保存。")
        return None
    backup = envfile.write(change.updates, change.removals)
    ui.ok("已保存" + (f"（旧文件备份为 {backup.name}）" if backup else ""))
    for note in change.notes:
        ui.hint(note)
    return change


def check_all(ui: UI, env: dict[str, str]) -> bool:
    ui.section("测试已配置的连接")
    all_ok = True
    tested = 0
    for m in MODULES:
        state, _ = m.status(env)
        if state == MISSING:
            continue
        with ui.spinner(m.title) as step:
            result = m.verify(env)
            if result is None:
                step.done(True, "未启用")
            else:
                step.done(result.ok, result.detail)
                all_ok &= result.ok
        tested += 1
    if not tested:
        ui.warn("还没有配置任何模块。")
    return all_ok


def _dashboard_options(env: dict[str, str]) -> list[Option]:
    options = []
    for m in MODULES:
        state, text = m.status(env)
        options.append(Option(m.key, pad(f"{STATUS_ICON[state]} {m.title}", 20), text))
    todo = [m for m in MODULES if m.status(env)[0] != OK]
    if todo:
        options.insert(0, Option("__todo", f"▶ 依次配置未完成的 {len(todo)} 项"))
    options.append(Option("__check", "🔍 测试所有已配置的连接"))
    options.append(Option("__done", "✔ 完成"))
    return options


def interactive(ui: UI, envfile: EnvFile) -> set[str]:
    services: set[str] = set()
    ui.title("Conveyor 设置向导")
    ui.hint(f"配置文件：{envfile.path}\n"
            "密钥只保存在这台服务器的 .env（权限 600），不会经过聊天。\n"
            "任何时候按 Ctrl-C 退出，已保存的模块不受影响。")

    env = envfile.read()
    missing_required = [m for m in MODULES if m.required and m.status(env)[0] != OK]
    if missing_required:
        ui.section("先完成必填的 " + " 和 ".join(m.title for m in missing_required))
        for m in missing_required:
            change = run_module(ui, envfile, m)
            if change:
                services.update(change.services)

    while True:
        env = envfile.read()
        ui.print()
        choice = ui.select("接下来做什么？", _dashboard_options(env))
        if choice == "__done":
            break
        if choice == "__check":
            check_all(ui, env)
            continue
        targets = [m for m in MODULES if m.status(env)[0] != OK] if choice == "__todo" else [BY_KEY[choice]]
        for m in targets:
            if choice == "__todo" and not m.required and not ui.confirm(f"配置「{m.title}」？（{m.blurb}）"):
                continue
            change = run_module(ui, envfile, m)
            if change:
                services.update(change.services)
    return services


def report_services(ui: UI, services: set[str]) -> None:
    if not services:
        return
    ordered = [s for s in ("telegram", "feishu", "web") if s in services]
    ui.section("让修改生效")
    ui.info("需要重启：" + "、".join(SERVICE_LABELS[s] for s in ordered))
    ui.info("命令：sudo conveyor restart " + (" && sudo conveyor restart ".join(ordered)))
    target = os.environ.get("CONVEYOR_SETUP_RESTART_FILE")
    if target:
        try:
            Path(target).write_text("\n".join(ordered) + "\n", encoding="utf-8")
        except OSError:
            pass


def main(argv: list[str] | None = None, ui: UI | None = None) -> int:
    parser = argparse.ArgumentParser(prog="conveyor setup", description="Conveyor 交互式设置向导")
    parser.add_argument("module", nargs="?", choices=sorted(BY_KEY), help="只配置某一项")
    parser.add_argument("--env", type=Path, default=None, help=".env 路径")
    parser.add_argument("--status", action="store_true", help="只显示状态")
    parser.add_argument("--check", action="store_true", help="测试所有已配置的连接")
    args = parser.parse_args(argv)

    ui = ui or UI()
    envfile = EnvFile(args.env or default_env_path())
    try:
        if args.status:
            render_status(ui, envfile.read())
            return 0
        if args.check:
            return 0 if check_all(ui, envfile.read()) else 1
        if args.module:
            change = run_module(ui, envfile, BY_KEY[args.module])
            report_services(ui, set(change.services) if change else set())
            return 0
        report_services(ui, interactive(ui, envfile))
        ui.print()
        ui.ok("设置完成。之后随时可以再运行 conveyor setup。")
        return 0
    except (Abort, KeyboardInterrupt, EOFError):
        ui.print()
        ui.warn("已退出。之前保存的模块不受影响。")
        return 130


if __name__ == "__main__":
    sys.exit(main())
