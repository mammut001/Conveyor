import Foundation
import SwiftUI
import ServiceManagement

/// App-level preferences, persisted in UserDefaults.
/// Values that affect the LaunchAgent plists (sshHost, localPort, conveyorDir)
/// are pushed back into `install-launchagents.sh` on save so the plists stay
/// in sync with what the menu bar app shows.
final class PreferencesStore: ObservableObject {
    static let shared = PreferencesStore()

    private let defaults = UserDefaults.standard

    // Keys
    private let kSSHHost     = "prefs.sshHost"
    private let kLocalPort   = "prefs.localPort"
    private let kConveyorDir = "prefs.conveyorDir"
    private let kNotify      = "prefs.showNotifications"
    // launchAtLogin is derived from SMAppService.status, not stored separately.
    // autoReconnect is inherent to launchd KeepAlive (always on); stored only
    // so the checkbox reflects user awareness.
    private let kAutoReconnect = "prefs.autoReconnect"

    private init() {}

    var sshHost: String {
        get { defaults.string(forKey: kSSHHost) ?? Config.default.sshHost }
        set { defaults.set(newValue, forKey: kSSHHost); objectWillChange.send() }
    }

    var localPort: Int {
        get {
            let v = defaults.object(forKey: kLocalPort) as? Int
            return v ?? Int(Config.default.controlPlaneURL.split(separator: ":").last ?? "8766") ?? 8766
        }
        set { defaults.set(newValue, forKey: kLocalPort); objectWillChange.send() }
    }

    var conveyorDir: String {
        get { defaults.string(forKey: kConveyorDir) ?? Config.repoDir() }
        set { defaults.set(newValue, forKey: kConveyorDir); objectWillChange.send() }
    }

    var showNotifications: Bool {
        get { defaults.object(forKey: kNotify) as? Bool ?? true }
        set { defaults.set(newValue, forKey: kNotify); objectWillChange.send() }
    }

    var autoReconnect: Bool {
        get { defaults.object(forKey: kAutoReconnect) as? Bool ?? true }
        set { defaults.set(newValue, forKey: kAutoReconnect); objectWillChange.send() }
    }

    var launchAtLogin: Bool {
        SMAppService.mainApp.status == .enabled
    }

    func setLaunchAtLogin(_ on: Bool) {
        do {
            if on { try SMAppService.mainApp.register() }
            else  { try SMAppService.mainApp.unregister() }
            objectWillChange.send()
        } catch {
            // If register needs approval, status becomes .requiresApproval;
            // surface that to the caller via the thrown error path elsewhere.
            print("[prefs] SMAppService error: \(error.localizedDescription)")
            objectWillChange.send()
        }
    }

    var launchAtLoginStatusText: String {
        switch SMAppService.mainApp.status {
        case .enabled:           return "Registered — will launch at login"
        case .notRegistered:     return "Not registered"
        case .requiresApproval:  return "Requires approval (open System Settings → Login Items)"
        case .notFound:          return "App not found — install to /Applications first"
        @unknown default:        return "Unknown"
        }
    }

    /// Re-install the two LaunchAgent plists using current prefs. Returns the
    /// installer's combined stderr/stdout for display in the UI.
    @discardableResult
    func reinstallLaunchAgents() -> String {
        let script = "\(conveyorDir)/scripts/install-launchagents.sh"
        guard FileManager.default.isExecutableFile(atPath: script) else {
            return "installer not found or not executable: \(script)"
        }
        let task = Process()
        task.executableURL = URL(fileURLWithPath: "/bin/bash")
        task.arguments = [script]
        var env = ProcessInfo.processInfo.environment
        env["CONVEYOR_DIR"] = conveyorDir
        env["CONVEYOR_SSH_HOST"] = sshHost
        env["CONVEYOR_LOCAL_PORT"] = "\(localPort)"
        task.environment = env
        let pipe = Pipe()
        task.standardOutput = pipe
        task.standardError = pipe
        do { try task.run() } catch { return "failed to launch installer: \(error)" }
        task.waitUntilExit()
        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        let out = String(data: data, encoding: .utf8) ?? ""
        return task.terminationStatus == 0
            ? "Reinstalled (exit 0).\n\(out)"
            : "Installer exited \(task.terminationStatus).\n\(out)"
    }
}

/// SwiftUI preferences form, hosted in its own NSWindow via PreferencesController.
struct PreferencesView: View {
    @ObservedObject var prefs = PreferencesStore.shared
    @State private var sshHostInput: String = ""
    @State private var portInput: String = ""
    @State private var dirInput: String = ""
    @State private var installResult: String?
    @State private var installBusy: Bool = false

    var body: some View {
        Form {
            Section("Connection") {
                HStack {
                    Text("SSH Host").frame(width: 110, alignment: .leading)
                    TextField("vps-oracle", text: $sshHostInput)
                        .textFieldStyle(.roundedBorder)
                }
                HStack {
                    Text("Local Port").frame(width: 110, alignment: .leading)
                    TextField("8766", text: $portInput)
                        .textFieldStyle(.roundedBorder)
                }
                HStack {
                    Text("Conveyor Dir").frame(width: 110, alignment: .leading)
                    TextField("…/Conveyor", text: $dirInput)
                        .textFieldStyle(.roundedBorder)
                    Button("Browse…") { pickFolder() }
                }
            }

            Section("Startup") {
                Toggle("Launch at Login", isOn: Binding(
                    get: { prefs.launchAtLogin },
                    set: { prefs.setLaunchAtLogin($0) }
                ))
                Text(prefs.launchAtLoginStatusText)
                    .font(.caption)
                    .foregroundStyle(.secondary)

                Toggle("Auto-reconnect on failure", isOn: $prefs.autoReconnect)
                Text("Built into launchd KeepAlive — always active while plists are loaded.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }

            Section("Notifications") {
                Toggle("Show state-change notifications", isOn: $prefs.showNotifications)
            }

            Section {
                HStack {
                    Button("Apply & Reinstall Plists") {
                        applyAndReinstall()
                    }
                    .disabled(installBusy || sshHostInput.isEmpty || portInput.isEmpty || dirInput.isEmpty)
                    if installBusy { ProgressView().scaleEffect(0.7) }
                    Spacer()
                    Button("Done") { PreferencesController.shared.close() }
                        .buttonStyle(.borderedProminent)
                }
                if let r = installResult {
                    Text(r)
                        .font(.system(.caption, design: .monospaced))
                        .foregroundStyle(r.hasPrefix("Reinstalled") ? Color.secondary : Color.red)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .textSelection(.enabled)
                }
            }
        }
        .formStyle(.grouped)
        .padding(16)
        .frame(width: 520, height: 480)
        .onAppear { syncInputs() }
    }

    private func syncInputs() {
        sshHostInput = prefs.sshHost
        portInput = "\(prefs.localPort)"
        dirInput = prefs.conveyorDir
    }

    private func pickFolder() {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.allowsMultipleSelection = false
        if panel.runModal() == .OK, let url = panel.url {
            dirInput = url.path
        }
    }

    private func applyAndReinstall() {
        prefs.sshHost = sshHostInput.trimmingCharacters(in: .whitespaces)
        if let p = Int(portInput.trimmingCharacters(in: .whitespaces)), p > 0 && p < 65536 {
            prefs.localPort = p
        }
        prefs.conveyorDir = dirInput.trimmingCharacters(in: .whitespaces)
        installBusy = true
        DispatchQueue.global(qos: .userInitiated).async {
            let result = PreferencesStore.shared.reinstallLaunchAgents()
            DispatchQueue.main.async {
                self.installResult = result
                self.installBusy = false
                HealthMonitor.shared.reloadConfig()
                HealthMonitor.shared.refresh()
            }
        }
    }
}

/// Owns the preferences window. Single shared instance so repeated menu clicks
/// just focus the existing window instead of spawning duplicates.
final class PreferencesController {
    static let shared = PreferencesController()
    private var window: NSWindow?

    func show() {
        if let w = window, w.isVisible || w.isMiniaturized {
            NSApp.activate(ignoringOtherApps: true)
            w.makeKeyAndOrderFront(nil)
            return
        }
        let hosting = NSHostingController(rootView: PreferencesView())
        let w = NSWindow(contentViewController: hosting)
        w.title = "Conveyor Agent Preferences"
        w.styleMask = [.titled, .closable, .miniaturizable]
        w.isReleasedWhenClosed = false
        w.center()
        w.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        window = w
    }

    func close() {
        window?.orderOut(nil)
    }
}
