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

    var overallEmoji: String {
        let states = [sshTunnel.state, desktopAgent.state]
        if states.allSatisfy({ $0 == .running }) { return "🟢" }
        if states.allSatisfy({ $0 == .stopped }) { return "🔴" }
        return "🟡"
    }
}
