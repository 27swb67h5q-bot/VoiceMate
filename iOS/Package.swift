// Swift-tools-version:5.9
import PackageDescription

let package = Package(
    name: "VoiceMate",
    platforms: [
        .iOS(.v16)
    ],
    dependencies: [
        .package(url: "https://github.com/livekit/client-sdk-swift.git", exact: "2.14.1"),
    ],
    targets: [
        .executableTarget(
            name: "VoiceMate",
            dependencies: [
                .product(name: "LiveKit", package: "client-sdk-swift"),
            ],
            path: ".",
            exclude: ["Info.plist", "project.yml"],
            swiftSettings: [
                .unsafeFlags(["-parse-as-library"])
            ]
        )
    ]
)
