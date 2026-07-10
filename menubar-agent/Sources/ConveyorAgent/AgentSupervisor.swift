import Foundation

/// Spawns and supervises the Conveyor desktop agent python child process.
///
/// On macOS 26, launchd-spawned CLI binaries (bash/python) can't be granted
/// Full Disk Access to reach `~/Documents`, but a proper `.app` bundle can.
/// So the menu bar app owns the agent: it spawns
///   `<conveyorDir>/.venv/bin/python desktop_agent.py --poll-observe --poll-computer`
/// with the env vars from `<conveyorDir>/.desktop-agent.env`, appends the
/// child's stdout/stderr to `~/Library/Logs/conveyor-desktop-agent.log`, and
/// restarts it (with throttle) if it exits unexpectedly.
@MainActor
final class AgentSupervisor: ObservableObject {
    static let shared = AgentSupervisor()

    @Published private(set) var pid: pid_t?
    @Published private(set) var lastExitCode: Int32?
    @Published private(set) var startedAt: Date?
    @Published private(set) var busy: Bool = false

    private var process: Process?
    private var logHandle: FileHandle?
    private var intentionalStop: Bool = false
    private var restartInFlight: Bool = false
    private var hasStarted: Bool = false
    private let throttle: TimeInterval = 5.0
    private let queue = DispatchQueue(label: "conveyor.agent-supervisor")

    private init() {}

    // MARK: - Public state

    var isRunning: Bool { pid != nil && process?.isRunning == true }

    var status: ServiceStatus {
        // running -> .running
        // stopped by user -> .stopped
        // crashed/restarting (was started, not intentional stop) -> .stopped
        // never started -> .unknown
        let state: ServiceState
        if isRunning {
            state = .running
        } else if intentionalStop || hasStarted {
            state = .stopped
        } else {
            state = .unknown
        }
        let up: TimeInterval? = (isRunning && startedAt != nil)
            ? Date().timeIntervalSince(startedAt!) : nil
        return ServiceStatus(service: .desktopAgent,
                             state: state,
                             pid: pid,
                             lastExitCode: lastExitCode.map(Int.init),
                             uptimeSeconds: up)
    }

    // MARK: - Control

    func start() {
        guard !isRunning, !busy else { return }
        busy = true
        intentionalStop = false
        // Kill any orphaned agent from a previous/crashed app instance so we
        // don't accumulate duplicate pollers across relaunches.
        killOrphanAgents()
        launchNow()
    }

    /// Kill any desktop-agent poller python processes that aren't
    /// our own child. Called on start() to dedupe after an app crash/relaunch.
    private func killOrphanAgents() {
        let task = Process()
        task.executableURL = URL(fileURLWithPath: "/usr/bin/pkill")
        task.arguments = ["-f", "desktop_agent.py --poll-observe"]
        task.standardOutput = FileHandle.nullDevice
        task.standardError = FileHandle.nullDevice
        // pkill exits 1 if nothing matched; ignore.
        try? task.run()
        task.waitUntilExit()
        // Give them a moment to die before we respawn.
        Thread.sleep(forTimeInterval: 0.5)
    }

    func stop() {
        intentionalStop = true
        if let p = process, p.isRunning {
            // SIGTERM for graceful shutdown; escalate to SIGKILL after 5s.
            kill(p.processIdentifier, SIGTERM)
            queue.asyncAfter(deadline: .now() + 5) { [weak p] in
                if let p = p, p.isRunning { kill(p.processIdentifier, SIGKILL) }
            }
        }
    }

    func restart() {
        guard !busy else { return }
        intentionalStop = false
        if let p = process, p.isRunning {
            kill(p.processIdentifier, SIGTERM)
            // watchdog will see the exit and, since intentionalStop is false,
            // schedule a throttled restart.
        } else {
            start()
        }
    }

    // MARK: - Launch

    private func launchNow() {
        let prefs = PreferencesStore.shared
        let dir = prefs.conveyorDir.isEmpty ? Config.repoDir() : prefs.conveyorDir
        let env = mergedEnvironment(fromDir: dir)

        let py = "\(dir)/.venv/bin/python"
        guard FileManager.default.isExecutableFile(atPath: py) else {
            lastExitCode = -1
            busy = false
            appendLog("[supervisor] python not executable: \(py)\n")
            return
        }
        guard FileManager.default.fileExists(atPath: "\(dir)/desktop_agent.py") else {
            lastExitCode = -1
            busy = false
            appendLog("[supervisor] desktop_agent.py not found in \(dir)\n")
            return
        }

        openLog()

        let p = Process()
        p.executableURL = URL(fileURLWithPath: py)
        // -u: unbuffered stdout/stderr so log lines flush immediately
        // (otherwise Python buffers when stdout is a file, not a tty, and
        // the log looks empty even though the agent is working fine).
        // Keep read-only observe and direct computer-use polling in the same
        // supervised child so the production entry point cannot silently lose
        // the direct poller.
        p.arguments = ["-u", "desktop_agent.py", "--poll-observe", "--poll-computer"]
        p.currentDirectoryURL = URL(fileURLWithPath: dir)
        p.environment = env
        // Pipe child output to the shared log file.
        p.standardOutput = logHandle as Any
        p.standardError = logHandle as Any

        let stamp = ISO8601DateFormatter().string(from: Date())
        appendLog("\n[supervisor \(stamp)] launching agent in \(dir)\n")

        do {
            try p.run()
        } catch {
            appendLog("[supervisor] launch failed: \(error.localizedDescription)\n")
            lastExitCode = -1
            busy = false
            scheduleRestart()
            return
        }

        process = p
        pid = p.processIdentifier
        startedAt = Date()
        hasStarted = true
        busy = false

        // Watchdog on a background thread.
        let token = p
        queue.async { [weak self] in
            token.waitUntilExit()
            Task { @MainActor in self?.handleExit(token.terminationStatus, token.processIdentifier) }
        }
    }

    private func handleExit(_ code: Int32, _ exitedPid: pid_t) {
        lastExitCode = code
        if pid == exitedPid { pid = nil; startedAt = nil }
        let stamp = ISO8601DateFormatter().string(from: Date())
        appendLog("[supervisor \(stamp)] agent exited code=\(code)\n")

        if intentionalStop {
            // User asked to stop; leave it down.
            return
        }
        scheduleRestart()
    }

    private func scheduleRestart() {
        guard !restartInFlight, !intentionalStop else { return }
        restartInFlight = true
        appendLog("[supervisor] restarting in \(Int(throttle))s\n")
        queue.asyncAfter(deadline: .now() + throttle) { [weak self] in
            Task { @MainActor in
                self?.restartInFlight = false
                self?.launchNow()
            }
        }
    }

    // MARK: - Env parsing

    /// Build a minimal child env for the python agent.
    ///
    /// Do NOT pass through the menu-bar app's full environment: inherited
    /// `__CFBundleIdentifier` / XPC keys can cause capture-screen-helper to
    /// fail Screen Recording checks when spawned from a long-running poller.
    private func mergedEnvironment(fromDir dir: String) -> [String: String] {
        let parent = ProcessInfo.processInfo.environment
        let passthrough = [
            "HOME", "PATH", "USER", "LOGNAME", "TMPDIR", "LANG", "SHELL",
            "SSH_AUTH_SOCK", "LC_ALL", "LC_CTYPE",
        ]
        var env: [String: String] = [:]
        for key in passthrough {
            if let v = parent[key] { env[key] = v }
        }
        if env["PATH"] == nil || env["PATH"]?.isEmpty == true {
            env["PATH"] = "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
        }
        env["PYTHONUNBUFFERED"] = "1"

        let path = "\(dir)/.desktop-agent.env"
        guard let content = try? String(contentsOfFile: path, encoding: .utf8) else { return env }
        for line in content.split(separator: "\n") {
            let s = line.trimmingCharacters(in: .whitespaces)
            guard !s.isEmpty, !s.hasPrefix("#") else { continue }
            let stripped = s.hasPrefix("export ") ? String(s.dropFirst("export ".count)) : s
            guard let eq = stripped.firstIndex(of: "=") else { continue }
            let k = String(stripped[..<eq]).trimmingCharacters(in: .whitespaces)
            var v = String(stripped[stripped.index(after: eq)...]).trimmingCharacters(in: .whitespaces)
            if v.hasPrefix("\"") && v.hasSuffix("\"") && v.count >= 2 {
                v = String(v.dropFirst().dropLast())
            }
            env[k] = v
        }
        return env
    }

    // MARK: - Logging

    private func openLog() {
        let path = Service.desktopAgent.logPath
        // Ensure log dir exists.
        let dir = (path as NSString).deletingLastPathComponent
        try? FileManager.default.createDirectory(atPath: dir,
                                                  withIntermediateDirectories: true)
        if !FileManager.default.fileExists(atPath: path) {
            FileManager.default.createFile(atPath: path, contents: nil)
        }
        // Open for appending.
        if let h = FileHandle(forWritingAtPath: path) {
            h.seekToEndOfFile()
            logHandle = h
        } else {
            logHandle = nil
        }
    }

    private func appendLog(_ s: String) {
        if let h = logHandle, let d = s.data(using: .utf8) { h.write(d) }
        // Also surface to console for debugging.
        print(s, terminator: "")
    }
}
