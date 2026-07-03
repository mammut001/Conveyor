import Foundation
import UserNotifications

/// Posts macOS user notifications on service state transitions.
/// Authorization is requested lazily on first use; if the user denies, calls
/// silently no-op.
enum NotificationManager {
    static let shared = UNUserNotificationCenter.current()

    static func requestAuthorization() {
        shared.requestAuthorization(options: [.alert, .sound]) { _, _ in }
    }

    static func post(title: String, body: String) {
        guard PreferencesStore.shared.showNotifications else { return }
        let content = UNMutableNotificationContent()
        content.title = title
        content.body = body
        content.sound = .default
        let req = UNNotificationRequest(
            identifier: UUID().uuidString,
            content: content,
            trigger: nil
        )
        shared.add(req, withCompletionHandler: nil)
    }

    /// Compare two snapshots and emit notifications for meaningful transitions.
    static func notifyTransitions(from prev: AppHealth, to next: AppHealth) {
        notifyService(service: .sshTunnel,
                      name: "SSH Tunnel",
                      prev: prev.sshTunnel, next: next.sshTunnel,
                      downMsg: "VPS 连接已断开，launchd 正在重连…",
                      upMsg: "VPS 连接已恢复")
        notifyService(service: .desktopAgent,
                      name: "Desktop Agent",
                      prev: prev.desktopAgent, next: next.desktopAgent,
                      downMsg: "Desktop Agent 已停止（launchd 将自动重启）",
                      upMsg: "Desktop Agent 已恢复运行")
    }

    private static func notifyService(service: Service, name: String,
                                      prev: ServiceStatus, next: ServiceStatus,
                                      downMsg: String, upMsg: String) {
        // Treat `.unknown` (e.g. plist not in launchctl list) as `.stopped`
        // for transition purposes, so an unload/disappear still fires a "down"
        // notice and a reappearance fires "up".
        let prevEffective: ServiceState = (prev.state == .unknown) ? .stopped : prev.state
        let nextEffective: ServiceState = (next.state == .unknown) ? .stopped : next.state
        if prevEffective == .running && nextEffective == .stopped {
            post(title: "Conveyor: \(name) 下线", body: downMsg)
        } else if prevEffective == .stopped && nextEffective == .running {
            post(title: "Conveyor: \(name) 上线", body: upMsg)
        }
    }
}
