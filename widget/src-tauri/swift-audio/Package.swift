// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "lore-audio-capture",
    platforms: [.macOS(.v14)],
    targets: [
        .executableTarget(
            name: "lore-audio-capture",
            path: "Sources",
            linkerSettings: [
                .linkedFramework("AVFoundation"),
            ]
        ),
    ]
)
