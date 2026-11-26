// swift-tools-version:5.9
import PackageDescription

let package = Package(
  name: "SwiftApp",
  dependencies: [
    .package(url: "http://{HOST}/{ORG}/swift-kit.git", from: "1.0.0"),
    .package(url: "https://github.com/apple/swift-log.git", from: "1.5.0"),
  ]
)
