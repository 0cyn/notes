import AppKit

if CommandLine.arguments[1] == "--frontmost" {
    print(NSWorkspace.shared.frontmostApplication?.processIdentifier ?? 0)
    exit(0)
}
let restoring = CommandLine.arguments[1] == "--restore"
let pid = pid_t(CommandLine.arguments[restoring ? 2 : 1])!
guard let app = NSRunningApplication(processIdentifier: pid),
      restoring || app.bundleIdentifier == "com.vector35.binaryninja",
      app.activate(options: [.activateAllWindows, .activateIgnoringOtherApps]) else {
    fputs("Could not activate the selected Binary Ninja process.\n", stderr)
    exit(1)
}
