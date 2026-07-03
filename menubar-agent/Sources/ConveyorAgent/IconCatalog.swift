import AppKit

/// Loads the cute conveyor-bot menu-bar icons from the app bundle.
///
/// Each icon is a pre-tinted PNG (green/yellow/red/gray for healthy/partial/
/// down/unknown). The full bot body itself carries the status color, so no
/// template-tinting is needed — it just works in light and dark mode.
enum IconCatalog {
    /// Map the overall app health to the icon resource name (without @Nx suffix).
    static func resourceName(for overall: OverallHealth) -> String {
        switch overall {
        case .healthy:  return "icon-state-healthy"
        case .partial:  return "icon-state-partial"
        case .down:     return "icon-state-down"
        case .unknown:  return "icon-state-unknown"
        }
    }

    /// Load a colored NSImage for the given state. Falls back to the
    /// healthy icon if the resource is missing.
    ///
    /// `isTemplate = false` so the pre-tinted color shows through.
    /// `pointSize` defaults to 28 — larger than the 18pt macOS uses for system
    /// icons, so the bot reads bigger in the menu bar.
    static func image(for overall: OverallHealth, pointSize: CGFloat = 28) -> NSImage {
        let name = resourceName(for: overall)
        let backingScale = NSScreen.main?.backingScaleFactor ?? 2.0
        // 总是优先用 @3x master 拿最锐利的源 — pointSize 由 frame 控制
        let scale = 3
        let scaleSuffix: String
        switch scale {
        case ...1:  scaleSuffix = ""
        case 2:     scaleSuffix = "@2x"
        default:    scaleSuffix = "@3x"
        }
        let resource = "\(name)\(scaleSuffix)"
        let img = (Bundle.main.image(forResource: NSImage.Name(resource))
                   ?? Bundle.main.image(forResource: NSImage.Name(name))
                   ?? Bundle.main.image(forResource: NSImage.Name("icon-state-healthy@2x"))
                   ?? NSImage())
        img.isTemplate = false
        // 让 NSImage 自己按 pointSize 渲染 — SwiftUI 会按 frame 进一步控制
        img.size = NSSize(width: pointSize, height: pointSize)
        return img
    }
}
