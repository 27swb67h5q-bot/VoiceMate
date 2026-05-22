#!/usr/bin/env python3
"""Generate VoiceMate.xcodeproj from project.yml + add LiveKit dependency."""

import plistlib, uuid, os, json

def new_id():
    return ''.join(random.choices('ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789', k=24))

import random

root_id = new_id()
main_group_id = new_id()
sources_group_id = new_id()
resources_group_id = new_id()
services_group_id = new_id()
views_group_id = new_id()
models_group_id = new_id()
product_ref_id = new_id()
target_id = new_id()
build_config_list_id = new_id()
debug_config_id = new_id()
release_config_id = new_id()
sources_build_phase_id = new_id()
resources_build_phase_id = new_id()
frameworks_build_phase_id = new_id()
shell_script_phase_id = new_id()
livekit_pkg_id = new_id()
livekit_pkg_ref_id = new_id()
livekit_product_id = new_id()

src_root = os.path.dirname(os.path.abspath(__file__))
ios_dir = os.path.join(src_root, 'iOS')

def file_ref(path, parent, name=None, source_tree='SOURCE_ROOT'):
    fid = new_id()
    return fid, {
        'isa': 'PBXFileReference',
        'explicitFileType': 'sourcecode.swift.swift',
        'path': name or os.path.basename(path),
        'sourceTree': source_tree,
    }

def group(name, children, parent):
    gid = new_id()
    return gid, {
        'isa': 'PBXGroup',
        'children': children,
        'name': name,
        'sourceTree': '<group>',
    }, gid

# Collect Swift files
swift_files = []
for root, dirs, files in os.walk(os.path.join(ios_dir, 'VoiceMate')):
    for f in files:
        if f.endswith('.swift'):
            full = os.path.join(root, f)
            rel = os.path.relpath(full, ios_dir)
            fid, fref = file_ref(full, main_group_id, rel)
            swift_files.append((fid, fref, rel))

# Build references
file_refs = {}
build_files = []
for fid, fref, rel in swift_files:
    file_refs[fid] = fref
    bfid = new_id()
    build_files.append((bfid, {
        'isa': 'PBXBuildFile',
        'fileRef': fid,
    }))
    file_refs[bfid] = build_files[-1][1]

# Info.plist ref
info_plist_id = new_id()
file_refs[info_plist_id] = {
    'isa': 'PBXFileReference',
    'path': 'VoiceMate/Resources/Info.plist',
    'sourceTree': '<group>',
}

# Sources
source_children = [fid for fid, _, _ in swift_files] + [info_plist_id]

main_group = {
    'isa': 'PBXGroup',
    'children': source_children + [product_ref_id],
    'sourceTree': '<group>',
}
file_refs[main_group_id] = main_group
file_refs[product_ref_id] = {
    'isa': 'PBXFileReference',
    'explicitFileType': 'wrapper.application',
    'path': 'VoiceMate.app',
    'sourceTree': 'BUILT_PRODUCTS_DIR',
}

# Package references
file_refs[livekit_pkg_ref_id] = {
    'isa': 'XCRemoteSwiftPackageReference',
    'repositoryURL': 'https://github.com/livekit/client-sdk-swift.git',
    'requirement': {
        'kind': 'upToNextMajorVersion',
        'minimumVersion': '2.0.0',
    },
}
file_refs[livekit_pkg_id] = {
    'isa': 'XCSwiftPackageProductDependency',
    'package': livekit_pkg_ref_id,
    'productName': 'LiveKit',
}
file_refs[livekit_product_id] = {
    'isa': 'PBXBuildFile',
    'productRef': livekit_pkg_id,
}

# Build phases
build_phases = [sources_build_phase_id, frameworks_build_phase_id, resources_build_phase_id]
file_refs[sources_build_phase_id] = {
    'isa': 'PBXSourcesBuildPhase',
    'buildActionMask': 2147483647,
    'files': [bfid for bfid, _ in build_files],
    'runOnlyForDeploymentPostprocessing': 0,
}
file_refs[frameworks_build_phase_id] = {
    'isa': 'PBXFrameworksBuildPhase',
    'buildActionMask': 2147483647,
    'files': [livekit_product_id],
    'runOnlyForDeploymentPostprocessing': 0,
}
file_refs[resources_build_phase_id] = {
    'isa': 'PBXResourcesBuildPhase',
    'buildActionMask': 2147483647,
    'files': [],
    'runOnlyForDeploymentPostprocessing': 0,
}

# Native target
file_refs[target_id] = {
    'isa': 'PBXNativeTarget',
    'buildConfigurationList': build_config_list_id,
    'buildPhases': build_phases,
    'buildRules': [],
    'dependencies': [],
    'name': 'VoiceMate',
    'productName': 'VoiceMate',
    'productReference': product_ref_id,
    'productType': 'com.apple.product-type.application',
}

# Build configurations
file_refs[debug_config_id] = {
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
        'SWIFT_VERSION': '5.9',
        'TARGETED_DEVICE_FAMILY': '1',
    },
    'name': 'Debug',
}
file_refs[release_config_id] = {
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
        'SWIFT_VERSION': '5.9',
        'TARGETED_DEVICE_FAMILY': '1',
    },
    'name': 'Release',
}
file_refs[build_config_list_id] = {
    'isa': 'XCConfigurationList',
    'buildConfigurations': [debug_config_id, release_config_id],
    'defaultConfigurationIsVisible': 0,
    'defaultConfigurationName': 'Release',
}

# Root object
root_object_id = new_id()
file_refs[root_object_id] = {
    'isa': 'PBXProject',
    'buildConfigurationList': build_config_list_id,
    'compatibilityVersion': 'Xcode 14.0',
    'developmentRegion': 'zh-Hans',
    'hasScannedForEncodings': 0,
    'knownRegions': ['zh-Hans', 'en', 'Base'],
    'mainGroup': main_group_id,
    'packageReferences': [livekit_pkg_ref_id],
    'productRefGroup': main_group_id,
    'projectDirPath': '',
    'projectRoot': '',
    'targets': [target_id],
}

# Build the plist
pbxproj = {
    'archiveVersion': 1,
    'classes': {},
    'objectVersion': 56,
    'objects': file_refs,
    'rootObject': root_object_id,
}

xcodeproj_dir = os.path.join(ios_dir, 'VoiceMate.xcodeproj')
os.makedirs(xcodeproj_dir, exist_ok=True)

# Write as old-style plist
plistlib.dump(pbxproj, open(os.path.join(xcodeproj_dir, 'project.pbxproj'), 'wb'),
              fmt=plistlib.FMT_XML)

print(f"Generated {xcodeproj_dir}/project.pbxproj")
print(f"Swift files: {len(swift_files)}")
print(f"Build files: {len(build_files)}")
