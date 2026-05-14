// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "VoiceMate",
    platforms: [
        .iOS(.v16)
    ],
    dependencies: [],
    targets: [
        .target(
            name: "VoiceMate",
            dependencies: [],
            path: "Sources/VoiceMate"
        )
    ]
)
