#!/bin/bash
# VoiceMate build script for Codemagic
set -e

cd iOS

# Generate Xcode project from project.yml
echo "==> Generating Xcode project..."
rm -rf VoiceMate.xcodeproj
xcodegen generate
xcodebuild -resolvePackageDependencies \
    -project VoiceMate.xcodeproj \
    -scheme VoiceMate

# Build
echo "==> Building..."
xcodebuild -project VoiceMate.xcodeproj \
    -scheme VoiceMate \
    -sdk iphoneos \
    -destination 'generic/platform=iOS' \
    -configuration Release \
    CODE_SIGN_IDENTITY="" \
    CODE_SIGNING_REQUIRED=NO \
    CODE_SIGNING_ALLOWED=NO \
    build

echo "==> Build complete"
