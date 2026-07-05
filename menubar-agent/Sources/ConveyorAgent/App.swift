import SwiftUI
import AppKit

@main
struct ConveyorAgentApp: App {
    @StateObject private var monitor = HealthMonitor.shared
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate

    var body: some Scene {
        MenuBarExtra {
            MenuBarView()
                .environmentObject(monitor)
                .frame(width: 280)
        } label: {
            // 主动放大尺寸让机器人在菜单栏里更醒目
            // 菜单栏默认 18pt;我们用 28pt,系统会按比例缩放但优先保留我们的尺寸
            Image(nsImage: IconCatalog.image(for: monitor.health.overall))
                .resizable()
                .aspectRatio(contentMode: .fit)
                .frame(width: 32, height: 24)
        }
        .menuBarExtraStyle(.window)
    }
}

/// Sets the running app's icon at launch. For LSUIElement apps the Dock icon is
/// hidden, but the notification daemon (usernoted) renders the banner's left
/// icon slot from the running process's icon; without pinning it explicitly,
/// locally-signed UI-element apps fall back to the generic placeholder.
final class AppDelegate: NSObject, NSApplicationDelegate {
    func applicationDidFinishLaunching(_ notification: Notification) {
        if let url = Bundle.main.url(forResource: "AppIcon", withExtension: "icns"),
           let img = NSImage(contentsOf: url) {
            NSApp.applicationIconImage = img
        }
    }
}

@MainActor
final class HealthMonitor: ObservableObject {
    static let shared = HealthMonitor()

    @Published var health: AppHealth = {
        AppHealth(sshTunnel: ServiceStatus(service: .sshTunnel, state: .unknown, pid: nil,
                                            lastExitCode: nil, uptimeSeconds: nil),
                  desktopAgent: ServiceStatus(service: .desktopAgent, state: .unknown, pid: nil,
                                               lastExitCode: nil, uptimeSeconds: nil))
    }()
    @Published var lastError: String?
    @Published var busy:Bool = false

    @Published var config = Config.load()
    private var timer: Timer?
    private var previousHealth: AppHealth?

    init() {
        // Request notification authorization up front (user can deny in
        // System Settings; deny = silent no-op).
        NotificationManager.requestAuthorization()
        // Auto-start the supervised agent (ssh-tunnel is handled by launchd).
        AgentSupervisor.shared.start()
        refresh()
        let t = Timer(timeInterval: 2.0, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.refresh() }
        }
        RunLoop.main.add(t, forMode: .common)
        timer = t
    }

    func refresh() {
        // SSH tunnel: owned by launchd -> ask launchctl.
        // Desktop agent: owned by this app's AgentSupervisor.
        let next = AppHealth(
            sshTunnel: StatusChecker.shared.status(for: .sshTunnel),
            desktopAgent: AgentSupervisor.shared.status
        )
        if let prev = previousHealth {
            NotificationManager.notifyTransitions(from: prev, to: next)
        }
        previousHealth = next
        health = next
    }

    /// Re-read config (call after Preferences saves change sshHost/dir/port).
    func reloadConfig() {
        config = Config.load()
    }

    func runControl(_ action: @escaping () -> [String], then refreshAfter: Bool = true) {
        busy = true
        DispatchQueue.global(qos: .userInitiated).async {
            let errs = action()
            DispatchQueue.main.async { [weak self] in
                guard let self = self else { return }
                self.busy = false
                self.lastError = errs.isEmpty ? nil : errs.joined(separator: "\n")
                if refreshAfter { self.refresh() }
            }
        }
    }
}

struct MenuBarView: View {
    @EnvironmentObject var monitor: HealthMonitor

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            header
            Divider().padding(.vertical, 6)
            statusRows
            Divider().padding(.vertical, 6)
            infoRows
            Divider().padding(.vertical, 6)
            controlButtons
            if let err = monitor.lastError {
                Divider().padding(.vertical, 6)
                Text(err)
                    .font(.caption)
                    .foregroundStyle(.red)
                    .lineLimit(3)
            }
            Divider().padding(.vertical, 6)
            logButtons
            Divider().padding(.vertical, 6)
            permissionButton
            Divider().padding(.vertical, 6)
            preferencesButton
            Divider().padding(.vertical, 6)
            testNotificationButton
            Divider().padding(.vertical, 6)
            quitButton
        }
        .padding(10)
    }

    private var header: some View {
        Text("Conveyor Desktop Agent")
            .font(.headline)
            .frame(maxWidth: .infinity, alignment: .leading)
    }

    private var statusRows: some View {
        VStack(alignment: .leading, spacing: 4) {
            statusRow(monitor.health.sshTunnel)
            statusRow(monitor.health.desktopAgent)
        }
    }

    private func statusRow(_ s: ServiceStatus) -> some View {
        HStack {
            Text(s.state.emoji)
            Text(s.service.displayName).font(.body)
            Spacer()
            Text(stateText(s))
                .font(.caption)
                .foregroundStyle(.secondary)
        }
    }

    private func stateText(_ s: ServiceStatus) -> String {
        switch s.state {
        case .running:
            if let up = s.uptimeSeconds {
                return "Running · \(formatUptime(up))"
            }
            return "Running"
        case .stopped:
            if let code = s.lastExitCode, code != 0 {
                return "Stopped (exit \(code))"
            }
            return "Stopped"
        case .unknown:
            return "Unknown"
        }
    }

    private var infoRows: some View {
        VStack(alignment: .leading, spacing: 3) {
            infoLine("VPS", monitor.config.sshHost)
            infoLine("Node", monitor.config.nodeName)
            infoLine("Control", monitor.config.controlPlaneURL)
        }
    }

    private func infoLine(_ k: String, _ v: String) -> some View {
        HStack {
            Text(k).font(.caption).foregroundStyle(.secondary).frame(width: 60, alignment: .leading)
            Text(v).font(.caption).lineLimit(1).truncationMode(.middle)
            Spacer()
        }
    }

    private var controlButtons: some View {
        VStack(spacing: 4) {
            controlButton("▶  Start All", color: .green) {
                monitor.runControl(ProcessController.startAll)
            }
            controlButton("⏹  Stop All", color: .red) {
                monitor.runControl(ProcessController.stopAll)
            }
            controlButton("🔄  Restart All", color: .orange) {
                monitor.runControl(ProcessController.restartAll)
            }
        }
    }

    private func controlButton(_ title: String, color: Color,
                               action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Text(title)
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(.vertical, 4)
                .padding(.horizontal, 8)
                .background(color.opacity(0.12), in: RoundedRectangle(cornerRadius: 5))
        }
        .buttonStyle(.plain)
        .disabled(monitor.busy)
    }

    private var logButtons: some View {
        VStack(spacing: 4) {
            logButton("📋  View SSH Tunnel Log", for: .sshTunnel)
            logButton("📋  View Agent Log", for: .desktopAgent)
        }
    }

    private func logButton(_ title: String, for service: Service) -> some View {
        Button(title) { openLog(service) }
            .buttonStyle(.plain)
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(.vertical, 3)
    }

    private var quitButton: some View {
        HStack {
            Button("🔄 Refresh") { monitor.refresh() }
                .buttonStyle(.plain)
            Spacer()
            Button("Quit") { NSApplication.shared.terminate(nil) }
                .buttonStyle(.plain)
        }
    }

    private var permissionButton: some View {
        Button("🎥  开启屏幕录制权限…") {
            let prefs = PreferencesStore.shared
            let dir = prefs.conveyorDir.isEmpty ? Config.repoDir() : prefs.conveyorDir
            let err = PermissionHelper.guideScreenRecordingPermission(conveyorDir: dir)
            if let err {
                monitor.lastError = err
            }
        }
        .buttonStyle(.plain)
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.vertical, 3)
    }

    private var preferencesButton: some View {
        Button("⚙️  Preferences…") {
            PreferencesController.shared.show()
        }
        .buttonStyle(.plain)
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.vertical, 3)
    }

    private var testNotificationButton: some View {
        Button("🔔  Send test notification") {
            NotificationManager.post(title: "Conveyor: 测试通知",
                                     body: "如果你看到这条，通知管道是通的。")
        }
        .buttonStyle(.plain)
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.vertical, 3)
    }

    private func openLog(_ service: Service) {
        let path = service.logPath
        let fileURL = URL(fileURLWithPath: path)
        guard let appURL = NSWorkspace.shared.urlForApplication(withBundleIdentifier: "com.apple.Console") else {
            NSWorkspace.shared.open(fileURL)
            return
        }
        NSWorkspace.shared.open([fileURL], withApplicationAt: appURL,
                                configuration: NSWorkspace.OpenConfiguration())
    }

    private func formatUptime(_ s: TimeInterval) -> String {
        let total = Int(s)
        let h = total / 3600
        let m = (total % 3600) / 60
        let sec = total % 60
        if h > 0 { return "\(h)h \(m)m" }
        if m > 0 { return "\(m)m \(sec)s" }
        return "\(sec)s"
    }
}
