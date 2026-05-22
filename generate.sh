#!/bin/bash
# Generate a proper xcodeproj using xcodegen in a Docker Swift container
# OR fallback: create a minimal xcodeproj

cd /root/VoiceMate/iOS

# Try to use mint/xcodegen through various methods
if which xcodegen 2>/dev/null; then
    xcodegen generate
    exit 0
fi

# Generate a clean xcodeproj using Python
python3 << 'PYEOF'
import os, uuid, json, shutil

ios_dir = '/root/VoiceMate/iOS'
proj_dir = os.path.join(ios_dir, 'VoiceMate.xcodeproj')
os.makedirs(proj_dir, exist_ok=True)

def newid():
    return ''.join(__import__('random').choices('ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789', k=24))

root_id = newid()
main_group_id = newid()
target_id = newid()
product_ref_id = newid()
build_list_id = newid()
debug_id = newid()
release_id = newid()
src_phase_id = newid()
fw_phase_id = newid()
res_phase_id = newid()

# Swift files
swift_files = []
for root, dirs, files in os.walk(os.path.join(ios_dir, 'VoiceMate')):
    for f in sorted(files):
        if f.endswith('.swift'):
            swift_files.append(os.path.relpath(os.path.join(root, f), ios_dir))

# Build objects
objects = {}

# File refs + build files
all_children = []
for sf in swift_files:
    fid = newid()
    bfid = newid()
    all_children.append(fid)
    objects[fid] = {
        'isa': 'PBXFileReference',
        'lastKnownFileType': 'sourcecode.swift.swift',
        'path': sf,
        'sourceTree': '<group>',
    }
    objects[bfid] = {
        'isa': 'PBXBuildFile',
        'fileRef': fid,
    }

# Info.plist, Assets
info_id = newid()
assets_id = newid()
all_children.extend([info_id, assets_id])
objects[info_id] = {
    'isa': 'PBXFileReference',
    'lastKnownFileType': 'text.plist.xml',
    'path': 'VoiceMate/Resources/Info.plist',
    'sourceTree': '<group>',
}
objects[assets_id] = {
    'isa': 'PBXFileReference',
    'lastKnownFileType': 'folder.assetcatalog',
    'path': 'VoiceMate/Resources/Assets.xcassets',
    'sourceTree': '<group>',
}

# Product ref
all_children.append(product_ref_id)
objects[product_ref_id] = {
    'isa': 'PBXFileReference',
    'explicitFileType': 'wrapper.application',
    'path': 'VoiceMate.app',
    'sourceTree': 'BUILT_PRODUCTS_DIR',
}

# Main group
objects[main_group_id] = {
    'isa': 'PBXGroup',
    'children': all_children,
    'name': 'VoiceMate',
    'sourceTree': '<group>',
}

# Package references
pkg_ref_id = newid()
pkg_product_id = newid()
pkg_bf_id = newid()

objects[pkg_ref_id] = {
    'isa': 'XCRemoteSwiftPackageReference',
    'repositoryURL': 'https://github.com/livekit/client-sdk-swift.git',
    'requirement': {
        'kind': 'upToNextMajorVersion',
        'minimumVersion': '1.0.0',
    },
}

objects[pkg_product_id] = {
    'isa': 'XCSwiftPackageProductDependency',
    'package': pkg_ref_id,
    'productName': 'LiveKit',
}

objects[pkg_bf_id] = {
    'isa': 'PBXBuildFile',
    'productRef': pkg_product_id,
}

# Build phases
bf_ids = [bfid for fid, bfid in [(None, bfid) for _ in swift_files]]
# Actually let's collect the actual build file IDs
actual_bf_ids = []
for sf in swift_files:
    for k, v in objects.items():
        if v.get('isa') == 'PBXBuildFile' and 'fileRef' in v:
            actual_bf_ids.append(k)

actual_bf_ids = [k for k, v in objects.items() if v.get('isa') == 'PBXBuildFile' and 'fileRef' in v]

objects[src_phase_id] = {
    'isa': 'PBXSourcesBuildPhase',
    'buildActionMask': 2147483647,
    'files': actual_bf_ids,
    'runOnlyForDeploymentPostprocessing': 0,
}

objects[fw_phase_id] = {
    'isa': 'PBXFrameworksBuildPhase',
    'buildActionMask': 2147483647,
    'files': [pkg_bf_id],
    'runOnlyForDeploymentPostprocessing': 0,
}

objects[res_phase_id] = {
    'isa': 'PBXResourcesBuildPhase',
    'buildActionMask': 2147483647,
    'files': [],
    'runOnlyForDeploymentPostprocessing': 0,
}

# Target
objects[target_id] = {
    'isa': 'PBXNativeTarget',
    'buildConfigurationList': build_list_id,
    'buildPhases': [src_phase_id, fw_phase_id, res_phase_id],
    'buildRules': [],
    'dependencies': [],
    'name': 'VoiceMate',
    'productName': 'VoiceMate',
    'productReference': product_ref_id,
    'productType': 'com.apple.product-type.application',
}

# Build configs
for cfg_id, cfg_name in [(debug_id, 'Debug'), (release_id, 'Release')]:
    objects[cfg_id] = {
        'isa': 'XCBuildConfiguration',
        'buildSettings': {
            'ASSETCATALOG_COMPILER_APPICON_NAME': 'AppIcon',
            'CODE_SIGN_STYLE': 'Manual',
            'CODE_SIGN_IDENTITY': '',
            'CODE_SIGNING_REQUIRED': 'NO',
            'CODE_SIGNING_ALLOWED': 'NO',
            'INFOPLIST_FILE': 'VoiceMate/Resources/Info.plist',
            'IPHONEOS_DEPLOYMENT_TARGET': '16.0',
            'PRODUCT_BUNDLE_IDENTIFIER': 'com.voicemate.app',
            'PRODUCT_NAME': 'VoiceMate',
            'SDKROOT': 'iphoneos',
            'SUPPORTED_PLATFORMS': 'iphoneos',
            'SWIFT_VERSION': '5.9',
            'TARGETED_DEVICE_FAMILY': '1',
            'FRAMEWORK_SEARCH_PATHS': ['$(inherited)', '$(PLATFORM_DIR)/Developer/Library/Frameworks'],
        },
        'name': cfg_name,
    }

objects[build_list_id] = {
    'isa': 'XCConfigurationList',
    'buildConfigurations': [debug_id, release_id],
    'defaultConfigurationIsVisible': 0,
    'defaultConfigurationName': 'Release',
}

# Root project
objects[root_id] = {
    'isa': 'PBXProject',
    'buildConfigurationList': build_list_id,
    'compatibilityVersion': 'Xcode 14.0',
    'developmentRegion': 'zh-Hans',
    'hasScannedForEncodings': 0,
    'knownRegions': ['zh-Hans', 'en', 'Base'],
    'mainGroup': main_group_id,
    'packageReferences': [pkg_ref_id],
    'productRefGroup': main_group_id,
    'projectDirPath': '',
    'projectRoot': '',
    'targets': [target_id],
}

pbxproj = {
    'archiveVersion': 1,
    'classes': {},
    'objectVersion': 56,
    'objects': objects,
    'rootObject': root_id,
}

# Write as plist
import plistlib
with open(os.path.join(proj_dir, 'project.pbxproj'), 'wb') as f:
    plistlib.dump(pbxproj, f, fmt=plistlib.FMT_XML)

print(f'Generated xcodeproj with {len(swift_files)} Swift files')
print(f'Package ref: {pkg_ref_id}')
print(f'Package product: {pkg_product_id}')
print(f'Objects: {len(objects)}')
PYEOF