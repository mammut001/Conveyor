import AppKit
import Foundation

/// Guides the operator through granting Screen Recording to capture-screen-helper.
enum PermissionHelper {
    private static let screenRecordingSettingsURL =
        URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture")!

    static func screenshotHelperPath(conveyorDir: String) -> String? {
        let envPath = "\(conveyorDir)/.desktop-agent.env"
        guard let content = try? String(contentsOfFile: envPath, encoding: .utf8) else {
            return nil
        }
        for line in content.split(separator: "\n") {
            let s = line.trimmingCharacters(in: .whitespaces)
            guard !s.isEmpty, !s.hasPrefix("#") else { continue }
            let stripped = s.hasPrefix("export ") ? String(s.dropFirst("export ".count)) : s
            guard let eq = stripped.firstIndex(of: "=") else { continue }
            let key = String(stripped[..<eq]).trimmingCharacters(in: .whitespaces)
            guard key == "CONVEYOR_DESKTOP_SCREENSHOT_HELPER" else { continue }
            var value = String(stripped[stripped.index(after: eq)...]).trimmingCharacters(in: .whitespaces)
            if value.hasPrefix("\"") && value.hasSuffix("\"") && value.count >= 2 {
                value = String(value.dropFirst().dropLast())
            }
            return value.isEmpty ? nil : value
        }
        return nil
    }

    @MainActor
    static func guideScreenRecordingPermission(conveyorDir: String) -> String? {
        let dir = conveyorDir.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !dir.isEmpty else { return "未配置 Conveyor 目录" }

        guard let helperPath = screenshotHelperPath(conveyorDir: dir) else {
            return "未在 .desktop-agent.env 找到 CONVEYOR_DESKTOP_SCREENSHOT_HELPER"
        }
        guard FileManager.default.isExecutableFile(atPath: helperPath) else {
            return "截图工具不存在或不可执行：\(helperPath)"
        }

        let helperURL = URL(fileURLWithPath: helperPath)
        let appURL = Bundle.main.bundleURL
        NSWorkspace.shared.activateFileViewerSelecting([helperURL, appURL])
        NSWorkspace.shared.open(screenRecordingSettingsURL)

        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(helperPath, forType: .string)

        let alert = NSAlert()
        alert.messageText = "开启屏幕录制权限"
        alert.informativeText = """
        已为你打开：
        1. 系统设置 → 屏幕录制
        2. Finder 里的 capture-screen-helper 和 Conveyor Agent.app

        请在屏幕录制列表里把这两项都打开（没有就点 + 添加）。
        若提示退出应用，请选择「退出并重新打开」。
        点「好的」后会自动 Restart All，再在飞书重试截图。
        """
        alert.alertStyle = .informational
        alert.addButton(withTitle: "好的")
        alert.runModal()
        AgentSupervisor.shared.restart()
        return nil
    }
}