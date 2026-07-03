import Foundation

enum Service: String, CaseIterable {
    case sshTunnel      = "com.conveyor.ssh-tunnel"
    case desktopAgent   = "com.conveyor.desktop-agent"

    var displayName: String {
        switch self {
        case .sshTunnel:    return "SSH Tunnel"
        case .desktopAgent: return "Desktop Agent"
        }
    }

    var plistPath: String {
        "\(NSHomeDirectory())/Library/LaunchAgents/\(rawValue).plist"
    }

    var logPath: String {
        switch self {
        case .sshTunnel:    return "\(NSHomeDirectory())/Library/Logs/conveyor-ssh-tunnel.log"
        case .desktopAgent: return "\(NSHomeDirectory())/Library/Logs/conveyor-desktop-agent.log"
        }
    }
}

enum ServiceState: String {
    case running
    case stopped
    case unknown

    var emoji: String {
        switch self {
        case .running: return "🟢"
        case .stopped: return "🔴"
        case .unknown: return "⚪️"
        }
    }
}

struct ServiceStatus: Equatable {
    let service: Service
    let state: ServiceState
    let pid: pid_t?
    let lastExitCode: Int?
    let uptimeSeconds: TimeInterval?
}

struct AppHealth: Equatable {
    let sshTunnel: ServiceStatus
    let desktopAgent: ServiceStatus

    /// Coarse health bucket. Mirrors the menu-bar icon states.
    var overall: OverallHealth {
        let states = [sshTunnel.state, desktopAgent.state]
        let runningCount = states.filter { $0 == .running }.count
        if runningCount == states.count { return .healthy }
        if runningCount == 0 {
            // 没有 running — 如果全是 unknown,显示 unknown;否则 down
            if states.allSatisfy({ $0 == .unknown }) { return .unknown }
            return .down
        }
        return .partial
    }

    var overallEmoji: String {
        switch overall {
        case .healthy: return "🟢"
        case .partial: return "🟡"
        case .down:    return "🔴"
        case .unknown: return "⚪️"
        }
    }
}

/// Coarse health bucket — drives the menu-bar icon state.
/// 四种状态:healthy / partial / down / unknown
enum OverallHealth: String {
    case healthy  // both services running
    case partial  // exactly one running
    case down     // nothing running (且不是 unknown)
    case unknown  // no signal yet
}
