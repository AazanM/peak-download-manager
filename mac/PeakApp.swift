// Peak Download Manager — native Mac shell.
// Owns the Python backend, shows the bento UI in a window and a menu bar popover,
// and posts real notifications / Dock badge / login item as "Peak Download Manager".
import AppKit
import WebKit
import UserNotifications
import ServiceManagement
import UniformTypeIdentifiers

let APP_URL = "http://127.0.0.1:7878"
let SIZES: [String: NSSize] = ["compact": NSSize(width: 380, height: 350), "full": NSSize(width: 1080, height: 680)]
let BG = NSColor(srgbRed: 0, green: 0, blue: 0, alpha: 1)

final class AppDelegate: NSObject, NSApplicationDelegate, WKScriptMessageHandler, WKNavigationDelegate,
                         UNUserNotificationCenterDelegate, NSPopoverDelegate, NSWindowDelegate {
    var backend: Process?
    var window: NSWindow!
    var webView: WKWebView!
    var statusItem: NSStatusItem!
    var popover: NSPopover!
    var popoverWeb: WKWebView!
    var lastStatus: [String: String] = [:]
    var firstPoll = true
    var backendFails = 0
    var compactHeight: CGFloat = 350
    var fullFrame: NSRect?          // remembered size of the full window

    // MARK: launch

    func applicationDidFinishLaunching(_ note: Notification) {
        retireLaunchAgent()
        startBackend()
        buildWindow()
        buildStatusItem()
        UNUserNotificationCenter.current().delegate = self
        UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound, .badge]) { _, _ in }
        if !UserDefaults.standard.bool(forKey: "didSetupLogin") {       // start at login by default
            UserDefaults.standard.set(true, forKey: "didSetupLogin")
            try? SMAppService.mainApp.register()
        }
        if !UserDefaults.standard.bool(forKey: "askedTorrentDefault") {    // once: take over magnet links
            UserDefaults.standard.set(true, forKey: "askedTorrentDefault")
            useForTorrents()
        }
        Timer.scheduledTimer(withTimeInterval: 1, repeats: true) { [weak self] _ in self?.poll() }
        waitForBackend { [weak self] in
            self?.load(self?.webView, surface: "window")
            self?.load(self?.popoverWeb, surface: "popover")
        }
        showWindow()
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ s: NSApplication) -> Bool { false }

    func applicationShouldHandleReopen(_ s: NSApplication, hasVisibleWindows: Bool) -> Bool {
        showWindow(); return true
    }

    func applicationWillTerminate(_ note: Notification) {
        backend?.terminate()
        backend?.waitUntilExit()
    }

    // peakdm://show  (the extension calls this through the backend)
    // Also magnet: links and .torrent files, once Peak is their default app.
    func application(_ app: NSApplication, open urls: [URL]) {
        for url in urls {
            // Both open the New download box (file list, sizes, folder) instead of starting straight away.
            if url.scheme == "magnet" {
                post("/api/prompt", ["url": url.absoluteString])
            } else if url.isFileURL, url.pathExtension.lowercased() == "torrent" {
                post("/api/prompt", ["url": url.path])
            }
        }
        showWindow()
    }

    /// Waits for the backend (Peak may have just been launched by the click) and sends the request.
    func post(_ path: String, _ body: [String: Any]) {
        waitForBackend {
            var req = URLRequest(url: URL(string: "\(APP_URL)\(path)")!)
            req.httpMethod = "POST"
            req.setValue("application/json", forHTTPHeaderField: "Content-Type")
            req.httpBody = try? JSONSerialization.data(withJSONObject: body)
            URLSession.shared.dataTask(with: req).resume()
        }
    }

    /// Make Peak open magnet links and .torrent files (macOS asks the user to confirm).
    @objc func useForTorrents() {
        let me = Bundle.main.bundleURL
        NSWorkspace.shared.setDefaultApplication(at: me, toOpenURLsWithScheme: "magnet") { _ in }
        if let t = UTType("org.bittorrent.torrent") ?? UTType(filenameExtension: "torrent") {
            NSWorkspace.shared.setDefaultApplication(at: me, toOpen: t) { _ in }
        }
    }

    // MARK: backend

    /// The old install ran the backend as a LaunchAgent; the app owns it now.
    func retireLaunchAgent() {
        let plist = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/LaunchAgents/com.peakdm.backend.plist")
        guard FileManager.default.fileExists(atPath: plist.path) else { return }
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/bin/launchctl")
        p.arguments = ["bootout", "gui/\(getuid())/com.peakdm.backend"]
        try? p.run(); p.waitUntilExit()
        try? FileManager.default.removeItem(at: plist)
        Thread.sleep(forTimeInterval: 0.5)
    }

    func python() -> String {
        for p in ["/opt/homebrew/bin/python3", "/usr/local/bin/python3", "/opt/anaconda3/bin/python3", "/usr/bin/python3"]
        where FileManager.default.isExecutableFile(atPath: p) { return p }
        return "/usr/bin/python3"
    }

    func startBackend() {
        guard let server = Bundle.main.url(forResource: "server", withExtension: "py", subdirectory: "backend") else { return }
        let logs = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Logs/Peak Download Manager")
        try? FileManager.default.createDirectory(at: logs, withIntermediateDirectories: true)
        let logURL = logs.appendingPathComponent("backend.log")
        FileManager.default.createFile(atPath: logURL.path, contents: nil)
        let log = try? FileHandle(forWritingTo: logURL)

        let p = Process()
        p.executableURL = URL(fileURLWithPath: python())
        p.arguments = ["-u", server.path]
        var env = ProcessInfo.processInfo.environment
        env["PEAK_APP"] = "1"
        env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
        p.environment = env
        p.standardOutput = log
        p.standardError = log
        p.terminationHandler = { [weak self] proc in
            // Restart if it dies unexpectedly (not when we quit).
            DispatchQueue.main.async {
                guard let self, proc.terminationReason != .uncaughtSignal || proc.terminationStatus != 15 else { return }
                self.backendFails += 1
                if self.backendFails < 5 {
                    DispatchQueue.main.asyncAfter(deadline: .now() + 1) { self.startBackend() }
                }
            }
        }
        try? p.run()
        backend = p
    }

    func waitForBackend(tries: Int = 40, then: @escaping () -> Void) {
        var req = URLRequest(url: URL(string: "\(APP_URL)/api/health")!)
        req.timeoutInterval = 0.5
        URLSession.shared.dataTask(with: req) { _, resp, _ in
            DispatchQueue.main.async {
                if (resp as? HTTPURLResponse)?.statusCode == 200 || tries <= 0 { then() }
                else { DispatchQueue.main.asyncAfter(deadline: .now() + 0.25) { self.waitForBackend(tries: tries - 1, then: then) } }
            }
        }.resume()
    }

    // MARK: window + popover

    func makeWebView() -> WKWebView {
        let cfg = WKWebViewConfiguration()
        cfg.userContentController.add(self, name: "peak")
        let login = SMAppService.mainApp.status == .enabled
        cfg.userContentController.addUserScript(WKUserScript(
            source: "window.PEAK_LOGIN = \(login);", injectionTime: .atDocumentStart, forMainFrameOnly: true))
        let wv = DragWebView(frame: .zero, configuration: cfg)
        wv.navigationDelegate = self
        wv.setValue(false, forKey: "drawsBackground")
        return wv
    }

    func load(_ wv: WKWebView?, surface: String) {
        // The saved view rides along so the page never flips an expanded window back to compact.
        let view = surface == "popover" ? "compact" : (UserDefaults.standard.string(forKey: "view") ?? "compact")
        wv?.load(URLRequest(url: URL(string: "\(APP_URL)/?shell=mac&surface=\(surface)&view=\(view)")!))
    }

    func buildWindow() {
        let size = SIZES[UserDefaults.standard.string(forKey: "view") ?? "compact"] ?? SIZES["compact"]!
        window = NSWindow(contentRect: NSRect(origin: .zero, size: size),
                          styleMask: [.titled, .closable, .miniaturizable, .resizable, .fullSizeContentView],
                          backing: .buffered, defer: false)
        window.title = "Peak Download Manager"
        window.titleVisibility = .hidden
        window.titlebarAppearsTransparent = true
        window.isMovableByWindowBackground = true
        window.backgroundColor = .clear          // compact view is frosted glass; the page tints it
        window.isOpaque = false
        window.appearance = NSAppearance(named: .darkAqua)
        window.isReleasedWhenClosed = false
        window.delegate = self
        webView = makeWebView()
        let glass = NSVisualEffectView()
        glass.material = .hudWindow
        glass.blendingMode = .behindWindow
        glass.state = .active
        webView.autoresizingMask = [.width, .height]
        glass.addSubview(webView)
        window.contentView = glass
        webView.frame = glass.bounds
        window.setContentSize(size)
        window.center()
        if size.width > 400, let f = savedFullFrame() { window.setFrame(f, display: false) }
        showTrafficLights(size.width > 400)
    }

    func buildStatusItem() {
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        if let b = statusItem.button {
            b.image = menuBarIcon()
            b.imagePosition = .imageLeading
            b.target = self
            b.action = #selector(statusClicked)
            b.sendAction(on: [.leftMouseUp, .rightMouseUp])
        }
        popoverWeb = makeWebView()
        let vc = NSViewController()
        vc.view = popoverWeb
        vc.preferredContentSize = SIZES["compact"]!
        popoverWeb.setValue(false, forKey: "drawsBackground")
        popover = NSPopover()
        popover.contentViewController = vc
        popover.contentSize = SIZES["compact"]!
        popover.behavior = .transient
        popover.appearance = NSAppearance(named: .darkAqua)
        popover.delegate = self
    }

    @objc func statusClicked() {
        if NSApp.currentEvent?.type == .rightMouseUp {
            let m = NSMenu()
            m.addItem(withTitle: "Open Peak", action: #selector(openFromMenu), keyEquivalent: "o").target = self
            m.addItem(withTitle: "Show Downloads Folder", action: #selector(openFolder), keyEquivalent: "").target = self
            m.addItem(withTitle: "Use Peak for Magnet Links & Torrents", action: #selector(useForTorrents), keyEquivalent: "").target = self
            m.addItem(.separator())
            m.addItem(withTitle: "Quit Peak", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
            statusItem.menu = m
            statusItem.button?.performClick(nil)
            statusItem.menu = nil
            return
        }
        if popover.isShown { popover.performClose(nil); return }
        popoverWeb.evaluateJavaScript("window.peakShell && window.peakShell.setView('compact'); typeof refresh==='function' && refresh();")
        popover.show(relativeTo: statusItem.button!.bounds, of: statusItem.button!, preferredEdge: .minY)
        popover.contentViewController?.view.window?.makeKey()
    }

    @objc func openFromMenu() { showWindow() }
    @objc func openFolder() {
        NSWorkspace.shared.open(FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Downloads/Peak Downloads"))
    }

    func showWindow(view: String? = nil) {
        if let view { resize(view) }
        NSApp.activate(ignoringOtherApps: true)
        window.makeKeyAndOrderFront(nil)
    }

    /// Grow/shrink keeping the top-left corner where it is.
    /// Compact = the widget (fixed width, height fits its rows); full = resizable app window.
    func resize(_ view: String, height: CGFloat? = nil) {
        guard var size = SIZES[view] else { return }
        let wasFull = UserDefaults.standard.string(forKey: "view") == "full"
        if wasFull && view == "compact" { fullFrame = window.frame }
        UserDefaults.standard.set(view, forKey: "view")
        webView.evaluateJavaScript("window.peakShell && window.peakShell.setView('\(view)')")
        showTrafficLights(view == "full")
        if view == "compact" {
            size.height = compactHeight           // fixed square
            window.styleMask.remove(.resizable)
            window.minSize = .zero
        } else {
            window.styleMask.insert(.resizable)
            window.contentMinSize = NSSize(width: 640, height: 420)
            if let f = fullFrame ?? savedFullFrame() { window.setFrame(f, display: true, animate: true); return }
        }
        let content = window.contentRect(forFrameRect: window.frame)
        var frame = window.frameRect(forContentRect: NSRect(origin: content.origin, size: size))
        frame.origin.y = window.frame.maxY - frame.height
        if let vis = window.screen?.visibleFrame {
            if frame.minY < vis.minY { frame.origin.y = vis.minY }
            if frame.maxX > vis.maxX { frame.origin.x = vis.maxX - frame.width }
        }
        let animate = abs(frame.height - window.frame.height) > 40 || abs(frame.width - window.frame.width) > 40
        window.setFrame(frame, display: true, animate: animate)
    }

    /// The small card has its own expand button; the traffic lights would sit on top of it.
    func showTrafficLights(_ on: Bool) {
        for b in [NSWindow.ButtonType.closeButton, .miniaturizeButton, .zoomButton] {
            window.standardWindowButton(b)?.isHidden = !on
        }
    }

    func savedFullFrame() -> NSRect? {
        guard let s = UserDefaults.standard.string(forKey: "fullFrame") else { return nil }
        let r = NSRectFromString(s)
        return r.width >= 640 ? r : nil
    }

    func windowDidResize(_ n: Notification) {
        if UserDefaults.standard.string(forKey: "view") == "full" {
            fullFrame = window.frame
            UserDefaults.standard.set(NSStringFromRect(window.frame), forKey: "fullFrame")
        }
    }

    func menuBarIcon() -> NSImage {
        if let img = Bundle.main.image(forResource: "menubar") {
            img.size = NSSize(width: img.size.width * 18 / max(img.size.height, 1), height: 18)
            img.isTemplate = true
            return img
        }
        let img = NSImage(size: NSSize(width: 18, height: 18), flipped: true) { r in
            let box = NSBezierPath(roundedRect: r.insetBy(dx: 1.5, dy: 1.5), xRadius: 5, yRadius: 5)
            box.lineWidth = 1.5
            NSColor.black.setStroke(); box.stroke()
            let a = NSBezierPath()
            a.move(to: NSPoint(x: 9, y: 4.8)); a.line(to: NSPoint(x: 9, y: 11.5))
            a.move(to: NSPoint(x: 6, y: 9)); a.line(to: NSPoint(x: 9, y: 12)); a.line(to: NSPoint(x: 12, y: 9))
            a.lineWidth = 1.8; a.lineCapStyle = .round; a.lineJoinStyle = .round
            a.stroke()
            return true
        }
        img.isTemplate = true
        return img
    }

    // MARK: page <-> app bridge

    func userContentController(_ ucc: WKUserContentController, didReceive msg: WKScriptMessage) {
        guard let body = msg.body as? [String: Any], let type = body["type"] as? String else { return }
        let fromPopover = msg.webView === popoverWeb
        switch type {
        case "resize":
            let size = body["size"] as? String ?? "compact"
            let h = (body["height"] as? NSNumber).map { CGFloat($0.doubleValue) }
            if fromPopover {
                if size == "full" {
                    popoverWeb.evaluateJavaScript("window.peakShell.setView('compact')")
                    popover.performClose(nil); showWindow(view: "full")
                }
            } else if !(size == "compact" && UserDefaults.standard.string(forKey: "view") == "compact") {
                resize(size, height: h)
            }
        case "drag":
            let rects = { (k: String) -> [NSRect] in
                ((body[k] as? [[NSNumber]]) ?? []).compactMap { r in
                    r.count == 4 ? NSRect(x: r[0].doubleValue, y: r[1].doubleValue, width: r[2].doubleValue, height: r[3].doubleValue) : nil }
            }
            if let wv = msg.webView as? DragWebView { wv.drag = rects("drag"); wv.nodrag = rects("nodrag") }
        case "pickFolder":
            let key = body["key"] as? String ?? "download_dir"
            let panel = NSOpenPanel()
            panel.canChooseDirectories = true
            panel.canChooseFiles = false
            panel.canCreateDirectories = true
            panel.prompt = "Choose"
            panel.message = "Choose where Peak saves downloads"
            if fromPopover { popover.performClose(nil); showWindow() }
            panel.beginSheetModal(for: window) { [weak self] r in
                guard r == .OK, let url = panel.url else { return }
                let js = "window.peakShell.folderPicked(\(Self.json(key)), \(Self.json(url.path)))"
                self?.webView.evaluateJavaScript(js)
            }
        case "login":
            let on = body["on"] as? Bool ?? true
            do { on ? try SMAppService.mainApp.register() : try SMAppService.mainApp.unregister() } catch {}
            let now = SMAppService.mainApp.status == .enabled
            for wv in [webView, popoverWeb] { wv?.evaluateJavaScript("window.peakShell.login(\(now))") }
        default: break
        }
    }

    static func json(_ s: String) -> String {
        let d = try! JSONSerialization.data(withJSONObject: [s])
        return String(String(data: d, encoding: .utf8)!.dropFirst().dropLast())
    }

    // Open external links (e.g. source pages) in the browser, not inside the app.
    func webView(_ wv: WKWebView, decidePolicyFor action: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        if let url = action.request.url, !url.absoluteString.hasPrefix(APP_URL), action.navigationType == .linkActivated {
            NSWorkspace.shared.open(url); decisionHandler(.cancel); return
        }
        decisionHandler(.allow)
    }

    // Backend restarted / page failed to load -> retry.
    func webView(_ wv: WKWebView, didFailProvisionalNavigation n: WKNavigation!, withError e: Error) {
        DispatchQueue.main.asyncAfter(deadline: .now() + 1) { [weak self] in
            self?.load(wv, surface: wv === self?.popoverWeb ? "popover" : "window")
        }
    }

    // MARK: status polling -> notifications, Dock badge, menu bar count

    func poll() {
        URLSession.shared.dataTask(with: URL(string: "\(APP_URL)/api/jobs")!) { [weak self] data, _, _ in
            guard let data, let jobs = try? JSONSerialization.jsonObject(with: data) as? [[String: Any]] else { return }
            DispatchQueue.main.async { self?.handle(jobs) }
        }.resume()
    }

    func handle(_ jobs: [[String: Any]]) {
        var finished: [[String: Any]] = []
        var active = 0
        var progress: [Double] = []
        for j in jobs {
            guard let id = j["id"] as? String, let st = j["status"] as? String else { continue }
            if ["queued", "downloading", "converting"].contains(st) {
                active += 1
                if let p = j["progress"] as? Double { progress.append(p) }
            }
            if let prev = lastStatus[id], prev != st, ["done", "error"].contains(st), !firstPoll { finished.append(j) }
            lastStatus[id] = st
        }
        firstPoll = false
        NSApp.dockTile.badgeLabel = active > 0 ? "\(active)" : nil
        if let b = statusItem.button {
            b.title = active > 0 && !progress.isEmpty ? " \(Int(progress.reduce(0, +) / Double(progress.count)))%" : ""
            b.font = NSFont.monospacedDigitSystemFont(ofSize: 12, weight: .medium)
        }
        if !finished.isEmpty { notify(finished) }
    }

    func notify(_ jobs: [[String: Any]]) {
        URLSession.shared.dataTask(with: URL(string: "\(APP_URL)/api/settings")!) { data, _, _ in
            let s = (data.flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: Any] }) ?? [:]
            guard s["notify"] as? Bool ?? true else { return }
            for j in jobs {
                let ok = j["status"] as? String == "done"
                let c = UNMutableNotificationContent()
                c.title = ok ? "Download complete" : "Download failed"
                c.body = String(((ok ? j["title"] : (j["error"] ?? j["title"])) as? String ?? "").prefix(180))
                if let f = j["file"] as? String { c.userInfo = ["file": f] }
                UNUserNotificationCenter.current().add(
                    UNNotificationRequest(identifier: (j["id"] as? String ?? UUID().uuidString) + "-" + (j["status"] as? String ?? ""),
                                          content: c, trigger: nil))
            }
        }.resume()
    }

    func userNotificationCenter(_ c: UNUserNotificationCenter, willPresent n: UNNotification,
                                withCompletionHandler done: @escaping (UNNotificationPresentationOptions) -> Void) {
        done([.banner, .list])
    }

    // Clicking a "Download complete" notification reveals the file.
    func userNotificationCenter(_ c: UNUserNotificationCenter, didReceive r: UNNotificationResponse,
                                withCompletionHandler done: @escaping () -> Void) {
        DispatchQueue.main.async {
            if let f = r.notification.request.content.userInfo["file"] as? String, FileManager.default.fileExists(atPath: f) {
                NSWorkspace.shared.activateFileViewerSelecting([URL(fileURLWithPath: f)])
            } else { self.showWindow() }
        }
        done()
    }
}

// Standard app menu so ⌘Q / ⌘W / ⌘C / ⌘V work in the web view.
func buildMainMenu() -> NSMenu {
    let main = NSMenu()
    let appItem = NSMenuItem(); main.addItem(appItem)
    let app = NSMenu()
    app.addItem(withTitle: "About Peak Download Manager", action: #selector(NSApplication.orderFrontStandardAboutPanel(_:)), keyEquivalent: "")
    app.addItem(.separator())
    app.addItem(withTitle: "Hide Peak", action: #selector(NSApplication.hide(_:)), keyEquivalent: "h")
    app.addItem(withTitle: "Quit Peak", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
    appItem.submenu = app
    let editItem = NSMenuItem(); main.addItem(editItem)
    let edit = NSMenu(title: "Edit")
    edit.addItem(withTitle: "Undo", action: Selector(("undo:")), keyEquivalent: "z")
    edit.addItem(.separator())
    edit.addItem(withTitle: "Cut", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
    edit.addItem(withTitle: "Copy", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
    edit.addItem(withTitle: "Paste", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
    edit.addItem(withTitle: "Select All", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
    editItem.submenu = edit
    let winItem = NSMenuItem(); main.addItem(winItem)
    let win = NSMenu(title: "Window")
    win.addItem(withTitle: "Close", action: #selector(NSWindow.performClose(_:)), keyEquivalent: "w")
    win.addItem(withTitle: "Minimize", action: #selector(NSWindow.performMiniaturize(_:)), keyEquivalent: "m")
    winItem.submenu = win
    return main
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.mainMenu = buildMainMenu()
app.setActivationPolicy(.regular)
app.run()


/// WKWebView fills the window, so there's no title bar to grab. The page reports its
/// header strips (data-drag); a mouse-down there moves the window like a title bar does.
final class DragWebView: WKWebView {
    var drag: [NSRect] = []
    var nodrag: [NSRect] = []

    override func mouseDown(with e: NSEvent) {
        var p = convert(e.locationInWindow, from: nil)
        if !isFlipped { p.y = bounds.height - p.y }             // page coordinates: top-left origin
        let inStrip = p.y < 28 || drag.contains { $0.contains(p) }
        if inStrip && !nodrag.contains(where: { $0.contains(p) }) {
            if e.clickCount == 2 { window?.zoom(nil); return }
            window?.performDrag(with: e)
            return
        }
        super.mouseDown(with: e)
    }
}
