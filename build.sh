#!/bin/bash
# VoiceMate build script for Codemagic
set -e

cd iOS

# Generate Xcode project from project.yml
echo "==> Generating Xcode project..."
xcodegen generate

# Build
echo "==> Building..."
xcodebuild -project VoiceMate.xcodeproj \
    -scheme VoiceMate \
    -sdk iphoneos \
    -configuration Release \
    CODE_SIGN_IDENTITY="" \
    CODE_SIGNING_REQUIRED=NO \
    CODE_SIGNING_ALLOWED=NO \
    build

echo "==> Build complete"
