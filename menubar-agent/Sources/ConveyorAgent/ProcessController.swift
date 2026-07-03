import Foundation

enum ProcessController {
    enum ControlError: Error, LocalizedError {
        case loadFailed(label: String, status: Int32)
        case unloadFailed(label: String, status: Int32)
        case plistMissing(String)

        var errorDescription: String? {
            switch self {
            case .loadFailed(let label, let s):   return "launchctl load failed for \(label) (exit \(s))"
            case .unloadFailed(let label, let s): return "launchctl unload failed for \(label) (exit \(s))"
            case .plistMissing(let p):            return "plist not found: \(p)"
            }
        }
    }

    @discardableResult
    private static func launchctl(_ args: [String]) throws -> Int32 {
        let task = Process()
        task.executableURL = URL(fileURLWithPath: "/bin/launchctl")
        task.arguments = args
        task.standardOutput = FileHandle.nullDevice
        task.standardError = FileHandle.nullDevice
        try task.run()
        task.waitUntilExit()
        return task.terminationStatus
    }

    static func load(_ service: Service) throws {
        let plist = service.plistPath
        guard FileManager.default.fileExists(atPath: plist) else {
            throw ControlError.plistMissing(plist)
        }
        // Unload first to avoid "service already loaded" errors on refresh.
        _ = try? launchctl(["unload", plist])
        let s = try launchctl(["load", plist])
        guard s == 0 else { throw ControlError.loadFailed(label: service.rawValue, status: s) }
    }

    static func unload(_ service: Service) throws {
        let plist = service.plistPath
        guard FileManager.default.fileExists(atPath: plist) else {
            throw ControlError.plistMissing(plist)
        }
        let s = try launchctl(["unload", plist])
        // Unloading a not-loaded job returns 3; treat as success.
        guard s == 0 || s == 3 else {
            throw ControlError.unloadFailed(label: service.rawValue, status: s)
        }
    }

    static func startAll() -> [String] {
        var errors: [String] = []
        // SSH tunnel: managed by launchd.
        do { try load(.sshTunnel) }
        catch { errors.append("\(Service.sshTunnel.displayName): \(error.localizedDescription)") }
        // Desktop agent: managed by this app's supervisor.
        DispatchQueue.main.sync { AgentSupervisor.shared.start() }
        return errors
    }

    static func stopAll() -> [String] {
        var errors: [String] = []
        do { try unload(.sshTunnel) }
        catch { errors.append("\(Service.sshTunnel.displayName): \(error.localizedDescription)") }
        DispatchQueue.main.sync { AgentSupervisor.shared.stop() }
        return errors
    }

    static func restartAll() -> [String] {
        var errors: [String] = []
        // Restart tunnel via launchd.
        do { try unload(.sshTunnel) } catch { errors.append("\(Service.sshTunnel.displayName): \(error.localizedDescription)") }
        Thread.sleep(forTimeInterval: 1.5)
        do { try load(.sshTunnel) } catch { errors.append("\(Service.sshTunnel.displayName): \(error.localizedDescription)") }
        // Restart agent via supervisor.
        DispatchQueue.main.sync { AgentSupervisor.shared.restart() }
        return errors
    }
}
