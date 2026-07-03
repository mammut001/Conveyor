import Foundation

enum Shell {
    /// Run a command and return trimmed stdout. Returns nil on failure.
    @discardableResult
    static func run(_ command: String, arguments: [String] = []) -> String? {
        let task = Process()
        task.executableURL = URL(fileURLWithPath: command)
        task.arguments = arguments
        let pipe = Pipe()
        task.standardOutput = pipe
        task.standardError = FileHandle.nullDevice  // silence
        do {
            try task.run()
        } catch {
            return nil
        }
        task.waitUntilExit()
        guard task.terminationStatus == 0 else { return nil }
        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        return String(data: data, encoding: .utf8)?
            .trimmingCharacters(in: .whitespacesAndNewlines)
    }
}

final class StatusChecker {
    static let shared = StatusChecker()

    /// Snapshot all launchd jobs for this user session.
    /// Output rows: `<PID>\t<Status>\t<Label>` (PID is `-` when not running).
    private func launchctlDump() -> [String: (pid: pid_t?, lastExit: Int)] {
        guard let raw = Shell.run("/bin/launchctl", arguments: ["list"]) else { return [:] }
        var out: [String: (pid: pid_t?, lastExit: Int)] = [:]
        for line in raw.split(separator: "\n") {
            let cols = line.split(separator: "\t", omittingEmptySubsequences: false)
            guard cols.count >= 3 else { continue }
            let pidStr = String(cols[0])
            let exitStr = String(cols[1])
            let label = String(cols[2])
            let pid: pid_t? = (pidStr == "-" || pidStr.isEmpty) ? nil : pid_t(pidStr)
            let exitCode = Int(exitStr) ?? 0
            out[label] = (pid, exitCode)
        }
        return out
    }

    private func uptime(forPID pid: pid_t) -> TimeInterval? {
        guard let lstart = Shell.run("/bin/ps", arguments: ["-p", "\(pid)", "-o", "lstart="]) else {
            return nil
        }
        let df = DateFormatter()
        df.locale = Locale(identifier: "en_US_POSIX")
        df.dateFormat = "EEE MMM d HH:mm:ss yyyy"
        guard let start = df.date(from: lstart) else { return nil }
        return Date().timeIntervalSince(start)
    }

    func status(for service: Service) -> ServiceStatus {
        let dump = launchctlDump()
        guard let entry = dump[service.rawValue] else {
            return ServiceStatus(service: service, state: .unknown, pid: nil,
                                 lastExitCode: nil, uptimeSeconds: nil)
        }
        let state: ServiceState = (entry.pid != nil) ? .running : .stopped
        let up: TimeInterval? = (entry.pid != nil) ? uptime(forPID: entry.pid!) : nil
        return ServiceStatus(service: service, state: state, pid: entry.pid,
                             lastExitCode: entry.lastExit, uptimeSeconds: up)
    }

    func snapshot() -> AppHealth {
        AppHealth(
            sshTunnel: status(for: .sshTunnel),
            desktopAgent: status(for: .desktopAgent)
        )
    }
}
