// swift-tools-version:5.9
import PackageDescription

let package = Package(
    name: "ConveyorAgent",
    platforms: [.macOS(.v13)],
    targets: [
        .executableTarget(
            name: "ConveyorAgent",
            path: "Sources/ConveyorAgent"
        )
    ]
)
