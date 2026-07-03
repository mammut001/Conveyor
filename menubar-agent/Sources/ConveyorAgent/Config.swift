import Foundation

struct Config {
    let nodeID: String
    let nodeName: String
    let controlPlaneURL: String
    let sshHost: String

    static let `default` = Config(
        nodeID: "macbook-payton",
        nodeName: "Payton MacBook",
        controlPlaneURL: "http://127.0.0.1:8766",
        sshHost: "vps-oracle"
    )

    /// Reads `<conveyorDir>/.desktop-agent.env` if present.
    /// `conveyorDir` and `sshHost` come from PreferencesStore so the menu bar
    /// UI and the rendered plists stay in sync. Falls back to defaults.
    static func load() -> Config {
        let prefs = PreferencesStore.shared
        let repo = prefs.conveyorDir.isEmpty ? repoDir() : prefs.conveyorDir

        var d: [String: String] = [:]
        for key in ["CONVEYOR_DESKTOP_NODE_ID",
                    "CONVEYOR_DESKTOP_NODE_NAME",
                    "CONVEYOR_CONTROL_PLANE_URL"] {
            if let v = ProcessInfo.processInfo.environment[key] { d[key] = v }
        }
        let envPath = "\(repo)/.desktop-agent.env"
        if let content = try? String(contentsOfFile: envPath, encoding: .utf8) {
            for line in content.split(separator: "\n") {
                let s = line.trimmingCharacters(in: .whitespaces)
                guard s.hasPrefix("export ") || s.contains("=") else { continue }
                let stripped = s.hasPrefix("export ") ? String(s.dropFirst("export ".count)) : s
                guard let eq = stripped.firstIndex(of: "=") else { continue }
                let k = String(stripped[..<eq]).trimmingCharacters(in: .whitespaces)
                var v = String(stripped[stripped.index(after: eq)...]).trimmingCharacters(in: .whitespaces)
                if v.hasPrefix("\"") && v.hasSuffix("\"") && v.count >= 2 {
                    v = String(v.dropFirst().dropLast())
                }
                d[k] = v
            }
        }
        return Config(
            nodeID:     d["CONVEYOR_DESKTOP_NODE_ID"]   ?? `default`.nodeID,
            nodeName:   d["CONVEYOR_DESKTOP_NODE_NAME"] ?? `default`.nodeName,
            controlPlaneURL: d["CONVEYOR_CONTROL_PLANE_URL"] ?? `default`.controlPlaneURL,
            sshHost:    prefs.sshHost
        )
    }

    /// Find the repo dir by walking up from a few candidate locations.
    static func repoDir() -> String {
        let candidates = [
            ProcessInfo.processInfo.environment["CONVEYOR_DIR"],
            "\(NSHomeDirectory())/Documents/GitHub/Conveyor"
        ]
        for c in candidates {
            if let c = c, FileManager.default.fileExists(atPath: "\(c)/desktop_agent.py") {
                return c
            }
        }
        return candidates.compactMap { $0 }.last ?? ""
    }
}
