import Foundation

/// Persistent audio cache: downloads and stores voice files on device.
/// Files are saved to Documents/audio_cache/ so they survive app restarts.
struct AudioCache {
    private static let cacheDirName = "audio_cache"
    
    /// Directory where cached audio files live
    static var cacheDirectory: URL {
        let docs = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]
        let dir = docs.appendingPathComponent(cacheDirName)
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        return dir
    }
    
    /// Derive a local filename from a remote audio URL path (e.g. `/v1/audio/abc123.wav` -> `abc123.wav`)
    static func localFilename(from remotePath: String) -> String {
        // Take last path component
        let filename = (remotePath as NSString).lastPathComponent
        // If it has no extension, default to .mp3
        if (filename as NSString).pathExtension.isEmpty {
            return "\(filename).mp3"
        }
        return filename
    }
    
    /// Full local URL for a cached audio file
    static func localURL(for remotePath: String) -> URL {
        cacheDirectory.appendingPathComponent(localFilename(from: remotePath))
    }
    
    /// Check if a remote audio file is already cached locally
    static func isCached(remotePath: String) -> Bool {
        FileManager.default.fileExists(atPath: localURL(for: remotePath).path)
    }
    
    /// Download audio from remote URL and cache it locally.
    /// Returns the local file URL.
    /// Skips download if already cached.
    @discardableResult
    static func cache(from remoteURL: URL, remotePath: String) async throws -> URL {
        let localURL = localURL(for: remotePath)
        
        // Already cached — return immediately
        if FileManager.default.fileExists(atPath: localURL.path) {
            return localURL
        }
        
        // Download and save
        let (data, _) = try await URLSession.shared.data(from: remoteURL)
        try data.write(to: localURL)
        return localURL
    }
}
