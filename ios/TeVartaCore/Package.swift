// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "TeVartaCore",
    platforms: [.macOS(.v14), .iOS(.v17)],
    products: [.library(name: "TeVartaCore", targets: ["TeVartaCore"])],
    targets: [
        .target(name: "TeVartaCore"),
        .testTarget(name: "TeVartaCoreTests", dependencies: ["TeVartaCore"]),
    ]
)
