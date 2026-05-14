// Swift-tools-version:5.9
import PackageDescription

let package = Package(
    name: "VoiceMate",
    platforms: [
        .iOS(.v16)
    ],
    dependencies: [
    ],
    targets: [
        .executableTarget(
            name: "VoiceMate",
            dependencies: [],
            path: ".",
            exclude: ["Info.plist", "project.yml"],
            swiftSettings: [
                .unsafeFlags(["-parse-as-library"])
            ]
        )
    ]
)
