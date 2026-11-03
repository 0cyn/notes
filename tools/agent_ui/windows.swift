import CoreGraphics
import Foundation
import AppKit

let windows = CGWindowListCopyWindowInfo([.optionOnScreenOnly, .excludeDesktopElements],
    kCGNullWindowID) as? [[String: Any]] ?? []
let rows = windows.filter { window in
    guard let pid = window[kCGWindowOwnerPID as String] as? Int32 else { return false }
    return NSRunningApplication(processIdentifier: pid)?.bundleIdentifier == "com.vector35.binaryninja" &&
        (window[kCGWindowLayer as String] as? Int) == 0
}.map { window -> [String: Any] in
    return ["id": window[kCGWindowNumber as String] ?? 0,
            "pid": window[kCGWindowOwnerPID as String] ?? 0,
            "title": window[kCGWindowName as String] ?? "",
            "bounds": window[kCGWindowBounds as String] ?? [:]]
}
let data = try JSONSerialization.data(withJSONObject: rows, options: [.prettyPrinted, .sortedKeys])
print(String(data: data, encoding: .utf8)!)
