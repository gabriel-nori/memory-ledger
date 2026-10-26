// Ledger Agent, terminal box — a floating window holding a REAL terminal that runs the
// `claude` CLI, so everything works the way it does in any terminal: Ctrl-C stops a
// reply, Esc interrupts, /model, /clear, /resume, slash commands, permission prompts.
//
// Built by scripts/agent_panel.py into <home>/LedgerAgentTerminal.app, compiled together
// with SwiftTerm's sources (a pinned tag, cloned once into <home>/build/) — this file is
// the app's main.swift, so SwiftTerm is the same module and needs no import.
//
//   argv: <icon path> <subtitle> <folder> <shell command> <title>
//   The command runs as `/bin/zsh -lc <command>` in <folder> (a login shell, so PATH is
//   the person's own). When it exits, the box closes. Closing the box hangs it up.
//
// Two sizes, each remembered on its own: thin (the default) and expanded. ⌘E (or ⤢)
// switches; dragging any edge or corner resizes whichever size is showing, and that is
// the size it comes back to. The top-left corner stays put on every switch, the window
// is kept on its screen, and sizes, mode and position survive a relaunch. All placement is computed in one place, relayout(), from the
// card's bounds — nothing depends on autoresizing masks surviving a big shrink.
import AppKit

let BLUE   = NSColor(srgbRed:  41/255, green: 151/255, blue: 255/255, alpha: 1)
let INDIGO = NSColor(srgbRed:  44/255, green:  34/255, blue: 140/255, alpha: 1)
let TEAL   = NSColor(srgbRed:  20/255, green: 184/255, blue: 166/255, alpha: 1)
let INK    = NSColor(srgbRed:  12/255, green:  14/255, blue:  24/255, alpha: 1)
let GLOW: CGFloat = 24, RADIUS: CGFloat = 18, HEADER: CGFloat = 40, INSET: CGFloat = 8
// Card sizes (the window adds GLOW on every side).
let THIN = NSSize(width: 720, height: 250)
let BIG = NSSize(width: 960, height: 660)
let MIN_CARD = NSSize(width: 460, height: 170)

let reduceMotion = NSWorkspace.shared.accessibilityDisplayShouldReduceMotion

final class KeyWindow: NSWindow {
    override var canBecomeKey: Bool { true }
    override var canBecomeMain: Bool { true }
}

// The header moves the window; the terminal below it keeps its own mouse (selection).
final class DragView: NSView {
    override func mouseDown(with event: NSEvent) { window?.performDrag(with: event) }
}

// The three-line hint in the bottom-right corner. Drawing only: the corner's EdgeHandle,
// on top of it, does the resizing.
final class GripView: NSView {
    override func hitTest(_ p: NSPoint) -> NSView? { nil }
    override func draw(_ r: NSRect) {
        NSColor(white: 1, alpha: 0.5).setStroke()
        for i in 0..<3 {
            let o = CGFloat(i) * 4 + 3
            let p = NSBezierPath()
            p.move(to: NSPoint(x: bounds.maxX - o, y: 2)); p.line(to: NSPoint(x: bounds.maxX - 2, y: o))
            p.lineWidth = 1.2; p.stroke()
        }
    }
}

struct Edges: OptionSet {
    let rawValue: Int
    static let left = Edges(rawValue: 1), right = Edges(rawValue: 2)
    static let top = Edges(rawValue: 4), bottom = Edges(rawValue: 8)
}

// An invisible strip along one edge (or a square on one corner) that resizes the window
// from there. It paints at 2% alpha, because a fully clear pixel of a non-opaque window
// lets the click through to whatever is behind it.
final class EdgeHandle: NSView {
    let edges: Edges
    var onDrag: ((Edges, NSRect, NSPoint, NSPoint) -> Void)?
    var onEnd: (() -> Void)?
    private var startMouse: NSPoint = .zero
    private var startFrame: NSRect = .zero
    init(_ e: Edges) {
        edges = e
        super.init(frame: .zero)
        wantsLayer = true
        layer?.backgroundColor = NSColor(white: 0, alpha: 0.02).cgColor
    }
    required init?(coder: NSCoder) { fatalError() }
    var cursor: NSCursor {
        let pos: NSCursor.FrameResizePosition
        switch edges {
        case [.top, .left]: pos = .topLeft
        case [.top, .right]: pos = .topRight
        case [.bottom, .left]: pos = .bottomLeft
        case [.bottom, .right]: pos = .bottomRight
        case .left: pos = .left
        case .right: pos = .right
        case .top: pos = .top
        default: pos = .bottom
        }
        return NSCursor.frameResize(position: pos, directions: .all)
    }
    override func resetCursorRects() { addCursorRect(bounds, cursor: cursor) }
    override func mouseDown(with event: NSEvent) {
        startMouse = NSEvent.mouseLocation
        startFrame = window?.frame ?? .zero
    }
    override func mouseDragged(with event: NSEvent) { onDrag?(edges, startFrame, startMouse, NSEvent.mouseLocation) }
    override func mouseUp(with event: NSEvent) { onEnd?() }
}

final class RoundButton: NSView {
    var onClick: (() -> Void)?
    let glyph = NSTextField(labelWithString: "")
    init(symbol: String, tip: String) {
        super.init(frame: NSRect(x: 0, y: 0, width: 24, height: 24))
        wantsLayer = true
        layer?.backgroundColor = NSColor(white: 0.02, alpha: 0.4).cgColor
        layer?.cornerRadius = 12
        toolTip = tip
        glyph.stringValue = symbol
        glyph.alignment = .center
        glyph.textColor = NSColor(white: 1, alpha: 0.92)
        glyph.font = .systemFont(ofSize: 11.5, weight: .bold)
        glyph.frame = NSRect(x: 0, y: 4, width: 24, height: 16)
        addSubview(glyph)
    }
    required init?(coder: NSCoder) { fatalError() }
    override func mouseDown(with event: NSEvent) { alphaValue = 0.6 }
    override func mouseUp(with event: NSEvent) {
        alphaValue = 1
        if bounds.insetBy(dx: -8, dy: -8).contains(convert(event.locationInWindow, from: nil)) { onClick?() }
    }
}

final class GradientView: NSView {
    let gradient = CAGradientLayer()
    override init(frame: NSRect) {
        super.init(frame: frame)
        layer = gradient
        wantsLayer = true
    }
    required init?(coder: NSCoder) { fatalError() }
}

final class App: NSObject, NSApplicationDelegate, NSWindowDelegate, LocalProcessTerminalViewDelegate {
    var win: KeyWindow!
    var card: NSView!
    var aurora: GradientView!
    var header: DragView!
    var icon: NSImageView?
    var title: NSTextField!
    var sub: NSTextField!
    var expandButton: RoundButton!
    var closeButton: RoundButton!
    var well: NSView!
    var term: LocalProcessTerminalView!
    var grip: GripView!
    var handles: [EdgeHandle] = []
    let defaults = UserDefaults.standard
    let selfTesting = ProcessInfo.processInfo.environment["LEDGER_AGENT_SELFTEST"] != nil

    var expanded = false
    var thinSize = THIN
    var bigSize = BIG
    var switching = false          // a switch is animating: its resizes are not the person's
    var finishing = false
    let args = CommandLine.arguments

    func applicationDidFinishLaunching(_ n: Notification) {
        let iconPath = args.count > 1 ? args[1] : ""
        let subtitle = args.count > 2 ? args[2] : ""
        let folder = args.count > 3 && !args[3].isEmpty ? args[3] : FileManager.default.currentDirectoryPath
        let command = args.count > 4 && !args[4].isEmpty ? args[4] : "claude"
        let titleText = args.count > 5 && !args[5].isEmpty ? args[5] : "Ledger Agent"

        let screen = NSScreen.main?.visibleFrame ?? NSRect(x: 0, y: 0, width: 1440, height: 900)
        let size = windowSize(THIN)
        win = KeyWindow(contentRect: NSRect(x: screen.midX - size.width/2, y: screen.minY + screen.height*0.55,
                                            width: size.width, height: size.height),
                        styleMask: .borderless, backing: .buffered, defer: false)
        win.isOpaque = false
        win.backgroundColor = .clear
        win.hasShadow = false
        win.level = .floating
        win.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        win.isReleasedWhenClosed = false
        win.delegate = self
        win.setFrame(onScreen(win.frame), display: false)

        let content = NSView(frame: NSRect(origin: .zero, size: size))
        content.wantsLayer = true
        win.contentView = content

        card = NSView()
        card.wantsLayer = true
        card.layer?.shadowColor = BLUE.cgColor
        card.layer?.shadowOpacity = 0.55
        card.layer?.shadowRadius = 18
        card.layer?.shadowOffset = CGSize(width: 0, height: -2)
        content.addSubview(card)

        aurora = GradientView(frame: .zero)
        aurora.gradient.colors = [INDIGO.cgColor, BLUE.cgColor, TEAL.cgColor]
        aurora.gradient.locations = [0, 0.55, 1]
        aurora.gradient.startPoint = CGPoint(x: 0, y: 0)
        aurora.gradient.endPoint = CGPoint(x: 1, y: 1)
        aurora.gradient.cornerRadius = RADIUS
        aurora.gradient.masksToBounds = true
        aurora.gradient.borderWidth = 1
        aurora.gradient.borderColor = NSColor(white: 1, alpha: 0.35).cgColor
        card.addSubview(aurora)

        header = DragView()
        card.addSubview(header)
        if !iconPath.isEmpty, let img = NSImage(contentsOfFile: iconPath) {
            let iv = NSImageView()
            iv.image = img
            iv.imageScaling = .scaleProportionallyUpOrDown
            header.addSubview(iv)
            icon = iv
        }
        title = label(titleText, size: 13, weight: .semibold)
        header.addSubview(title)
        sub = label(subtitle, size: 11.5, weight: .regular)
        sub.textColor = NSColor(white: 1, alpha: 0.72)
        sub.lineBreakMode = .byTruncatingMiddle
        header.addSubview(sub)
        expandButton = RoundButton(symbol: "\u{2922}", tip: "Expand (⌘E)")
        expandButton.onClick = { [weak self] in self?.toggle() }
        header.addSubview(expandButton)
        closeButton = RoundButton(symbol: "\u{2715}", tip: "Close (⌘W) — ends the session")
        closeButton.onClick = { [weak self] in self?.finish() }
        header.addSubview(closeButton)

        // The terminal sits in a dark, rounded well under the header.
        well = NSView()
        well.wantsLayer = true
        well.layer?.backgroundColor = INK.cgColor
        well.layer?.cornerRadius = 12
        well.layer?.masksToBounds = true
        well.layer?.borderWidth = 1
        well.layer?.borderColor = NSColor(white: 1, alpha: 0.18).cgColor
        card.addSubview(well)

        term = LocalProcessTerminalView(frame: NSRect(x: 0, y: 0, width: 400, height: 200))
        term.processDelegate = self
        term.font = NSFont.monospacedSystemFont(ofSize: 12.5, weight: .regular)
        term.nativeBackgroundColor = INK
        term.nativeForegroundColor = NSColor(white: 0.93, alpha: 1)
        term.caretColor = BLUE
        term.optionAsMetaKey = true
        well.addSubview(term)

        grip = GripView()
        card.addSubview(grip)

        // Last, so they sit on top of everything at the window's rim.
        for e: Edges in [.left, .right, .top, .bottom, [.top, .left], [.top, .right], [.bottom, .left], [.bottom, .right]] {
            let h = EdgeHandle(e)
            h.onDrag = { [weak self] e, f, p0, p in self?.dragResize(e, from: f, mouse0: p0, mouse: p) }
            h.onEnd = { [weak self] in self?.save() }
            content.addSubview(h)
            handles.append(h)
        }

        restore()
        relayout()

        // The person's own environment (PATH included), plus what a terminal advertises.
        var env = ProcessInfo.processInfo.environment
        env["TERM"] = "xterm-256color"
        env["COLORTERM"] = "truecolor"
        env["TERM_PROGRAM"] = "LedgerAgent"
        if env["LANG"] == nil { env["LANG"] = "en_US.UTF-8" }
        term.startProcess(executable: "/bin/zsh", args: ["-lc", command],
                          environment: env.map { "\($0.key)=\($0.value)" },
                          execName: "zsh", currentDirectory: folder)

        win.alphaValue = 0
        win.makeKeyAndOrderFront(nil)
        win.makeFirstResponder(term)
        NSAnimationContext.runAnimationGroup { ctx in
            ctx.duration = reduceMotion ? 0.1 : 0.18
            self.win.animator().alphaValue = 1
        }
        NSApp.activate(ignoringOtherApps: true)
        var tries = 0
        Timer.scheduledTimer(withTimeInterval: 0.15, repeats: true) { [weak self] t in
            guard let self = self else { t.invalidate(); return }
            tries += 1
            if self.win.isKeyWindow || tries > 12 { t.invalidate() }
            else { NSApp.activate(ignoringOtherApps: true); self.win.makeKeyAndOrderFront(nil) }
        }
        if ProcessInfo.processInfo.environment["LEDGER_AGENT_SELFTEST"] != nil { selfTest() }
    }

    // ------------------------------------------------------------ sizes
    func windowSize(_ card: NSSize) -> NSSize { NSSize(width: card.width + 2*GLOW, height: card.height + 2*GLOW) }

    func clampCard(_ s: NSSize) -> NSSize {
        let vis = (win?.screen ?? NSScreen.main)?.visibleFrame.size ?? NSSize(width: 1440, height: 900)
        return NSSize(width: min(max(s.width, MIN_CARD.width), vis.width),
                      height: min(max(s.height, MIN_CARD.height), vis.height))
    }

    // Keep a frame inside its screen's visible area, moving it (never shrinking past what fits).
    func onScreen(_ f: NSRect) -> NSRect {
        guard let vis = (win.screen ?? NSScreen.main)?.visibleFrame else { return f }
        var r = f
        // The glow margin may hang off the edge; the card itself must not.
        let slack = GLOW
        if r.maxX > vis.maxX + slack { r.origin.x = vis.maxX + slack - r.width }
        if r.minX < vis.minX - slack { r.origin.x = vis.minX - slack }
        if r.maxY > vis.maxY + slack { r.origin.y = vis.maxY + slack - r.height }
        if r.minY < vis.minY - slack { r.origin.y = vis.minY - slack }
        return r
    }

    // A frame for `card` that keeps the current top-left corner where it is.
    func frameKeepingTopLeft(_ card: NSSize) -> NSRect {
        let s = windowSize(card)
        return onScreen(NSRect(x: win.frame.minX, y: win.frame.maxY - s.height, width: s.width, height: s.height))
    }

    @objc func toggle() { setExpanded(!expanded) }

    func setExpanded(_ on: Bool) {
        if on == expanded || switching { return }
        expanded = on
        expandButton.glyph.stringValue = on ? "\u{2921}" : "\u{2922}"
        expandButton.toolTip = on ? "Shrink (⌘E)" : "Expand (⌘E)"
        let target = frameKeepingTopLeft(clampCard(on ? bigSize : thinSize))
        switching = true
        win.setFrame(target, display: true, animate: !reduceMotion)
        switching = false
        relayout()
        save()
        win.makeFirstResponder(term)
    }

    func resizeTo(_ card: NSSize) {
        let c = clampCard(card)
        if expanded { bigSize = c } else { thinSize = c }
        win.setFrame(frameKeepingTopLeft(c), display: true)
        save()
    }

    // A drag on `e` from window frame `f`: the dragged edges follow the mouse, the others
    // stay. A moving edge stops at the screen's edge, and at the minimum size.
    func dragResize(_ e: Edges, from f: NSRect, mouse0: NSPoint, mouse: NSPoint) {
        let dx = mouse.x - mouse0.x, dy = mouse.y - mouse0.y
        var minX = f.minX, maxX = f.maxX, minY = f.minY, maxY = f.maxY
        if e.contains(.left) { minX += dx }
        if e.contains(.right) { maxX += dx }
        if e.contains(.bottom) { minY += dy }
        if e.contains(.top) { maxY += dy }
        if let vis = (win.screen ?? NSScreen.main)?.visibleFrame {
            if e.contains(.left) { minX = max(minX, vis.minX - GLOW) }
            if e.contains(.right) { maxX = min(maxX, vis.maxX + GLOW) }
            if e.contains(.bottom) { minY = max(minY, vis.minY - GLOW) }
            if e.contains(.top) { maxY = min(maxY, vis.maxY + GLOW) }
        }
        let minW = MIN_CARD.width + 2*GLOW, minH = MIN_CARD.height + 2*GLOW
        if maxX - minX < minW { if e.contains(.left) { minX = maxX - minW } else { maxX = minX + minW } }
        if maxY - minY < minH { if e.contains(.bottom) { minY = maxY - minH } else { maxY = minY + minH } }
        win.setFrame(NSRect(x: minX, y: minY, width: maxX - minX, height: maxY - minY), display: true)
        let c = NSSize(width: win.frame.width - 2*GLOW, height: win.frame.height - 2*GLOW)
        if expanded { bigSize = c } else { thinSize = c }
    }

    // ------------------------------------------------------------ remembered between launches
    func save() {
        if selfTesting { return }
        defaults.set([thinSize.width, thinSize.height], forKey: "thin")
        defaults.set([bigSize.width, bigSize.height], forKey: "big")
        defaults.set(expanded, forKey: "expanded")
        defaults.set([win.frame.minX, win.frame.maxY], forKey: "topLeft")
    }

    func restore() {
        if selfTesting { return }
        func size(_ k: String) -> NSSize? {
            guard let a = defaults.array(forKey: k) as? [Double], a.count == 2 else { return nil }
            return clampCard(NSSize(width: a[0], height: a[1]))
        }
        if let t = size("thin") { thinSize = t }
        if let b = size("big") { bigSize = b }
        expanded = defaults.bool(forKey: "expanded")
        expandButton.glyph.stringValue = expanded ? "\u{2921}" : "\u{2922}"
        expandButton.toolTip = expanded ? "Shrink (⌘E)" : "Expand (⌘E)"
        let s = windowSize(expanded ? bigSize : thinSize)
        var f = NSRect(x: win.frame.minX, y: win.frame.maxY - s.height, width: s.width, height: s.height)
        if let tl = defaults.array(forKey: "topLeft") as? [Double], tl.count == 2 {
            f.origin = NSPoint(x: tl[0], y: tl[1] - s.height)
        }
        // A screen that is gone (an unplugged monitor) must not strand the box off-screen.
        if !NSScreen.screens.contains(where: { $0.visibleFrame.intersects(f) }), let main = NSScreen.main {
            f.origin = NSPoint(x: main.visibleFrame.midX - s.width/2, y: main.visibleFrame.midY - s.height/2)
        }
        win.setFrame(onScreen(f), display: false)
        save()
    }

    func windowDidMove(_ n: Notification) { if !switching { save() } }

    func windowDidResize(_ n: Notification) { relayout() }
    func windowDidChangeScreen(_ n: Notification) {
        if !switching { win.setFrame(onScreen(win.frame), display: true) }
    }

    // Every view's frame, from the window's current size. Called on every resize.
    func relayout() {
        guard let content = win.contentView else { return }
        content.frame = NSRect(origin: .zero, size: win.frame.size)
        let c = content.bounds.insetBy(dx: GLOW, dy: GLOW)
        card.frame = c
        let W = c.width, H = c.height
        card.layer?.shadowPath = CGPath(roundedRect: CGRect(x: 0, y: 0, width: W, height: H),
                                        cornerWidth: RADIUS, cornerHeight: RADIUS, transform: nil)
        aurora.frame = NSRect(x: 0, y: 0, width: W, height: H)
        header.frame = NSRect(x: 0, y: H - HEADER, width: W, height: HEADER)
        var x: CGFloat = 14
        if let iv = icon { iv.frame = NSRect(x: x, y: (HEADER - 20)/2, width: 20, height: 20); x += 28 }
        title.sizeToFit()
        title.frame.origin = NSPoint(x: x, y: (HEADER - title.frame.height)/2)
        closeButton.frame.origin = NSPoint(x: W - 12 - 24, y: (HEADER - 24)/2)
        expandButton.frame.origin = NSPoint(x: W - 12 - 24 - 30, y: (HEADER - 24)/2)
        let subX = title.frame.maxX + 10
        sub.frame = NSRect(x: subX, y: (HEADER - 16)/2 - 1, width: max(0, expandButton.frame.minX - 10 - subX), height: 16)
        well.frame = NSRect(x: INSET, y: INSET, width: W - 2*INSET, height: max(40, H - HEADER - INSET))
        term.frame = well.bounds.insetBy(dx: 6, dy: 4)
        grip.frame = NSRect(x: W - 16, y: 2, width: 14, height: 14)
        grip.needsDisplay = true
        // Edge strips: the glow margin plus a few points into the card; corners a bit bigger.
        let WW = content.bounds.width, WH = content.bounds.height
        let t = GLOW + 5, k = GLOW + 14
        for h in handles {
            switch h.edges {
            case .left: h.frame = NSRect(x: 0, y: k, width: t, height: WH - 2*k)
            case .right: h.frame = NSRect(x: WW - t, y: k, width: t, height: WH - 2*k)
            case .bottom: h.frame = NSRect(x: k, y: 0, width: WW - 2*k, height: t)
            case .top: h.frame = NSRect(x: k, y: WH - t, width: WW - 2*k, height: t)
            case [.top, .left]: h.frame = NSRect(x: 0, y: WH - k, width: k, height: k)
            case [.top, .right]: h.frame = NSRect(x: WW - k, y: WH - k, width: k, height: k)
            case [.bottom, .left]: h.frame = NSRect(x: 0, y: 0, width: k, height: k)
            default: h.frame = NSRect(x: WW - k, y: 0, width: k, height: k)
            }
            h.window?.invalidateCursorRects(for: h)
        }
    }

    // ------------------------------------------------------------ the session
    func sizeChanged(source: LocalProcessTerminalView, newCols: Int, newRows: Int) {}
    func setTerminalTitle(source: LocalProcessTerminalView, title: String) {}
    func hostCurrentDirectoryUpdate(source: TerminalView, directory: String?) {}
    func processTerminated(source: TerminalView, exitCode: Int32?) {
        DispatchQueue.main.async { [weak self] in self?.finish(hangUp: false) }
    }

    @objc func close(_ s: Any?) { finish() }
    @objc func expand(_ s: Any?) { toggle() }
    @objc func bigger(_ s: Any?) { term.font = NSFont.monospacedSystemFont(ofSize: min(24, term.font.pointSize + 1), weight: .regular) }
    @objc func smaller(_ s: Any?) { term.font = NSFont.monospacedSystemFont(ofSize: max(9, term.font.pointSize - 1), weight: .regular) }

    func finish(hangUp: Bool = true) {
        if finishing { return }
        finishing = true
        if hangUp { term.terminate() }      // SIGHUP to claude, as closing any terminal does
        NSAnimationContext.runAnimationGroup({ ctx in
            ctx.duration = reduceMotion ? 0.08 : 0.14
            self.win.animator().alphaValue = 0
        }, completionHandler: { NSApp.terminate(nil) })
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ s: NSApplication) -> Bool { true }

    // ------------------------------------------------------------ bits
    func label(_ s: String, size: CGFloat, weight: NSFont.Weight) -> NSTextField {
        let l = NSTextField(labelWithString: s)
        l.textColor = NSColor(white: 1, alpha: 0.96)
        l.font = .systemFont(ofSize: size, weight: weight)
        return l
    }

    // LEDGER_AGENT_SELFTEST=1: walk the size edge cases, print one line each, quit.
    func selfTest() {
        func card() -> NSSize { NSSize(width: win.frame.width - 2*GLOW, height: win.frame.height - 2*GLOW) }
        func line(_ what: String) {
            let f = win.frame
            print("\(what)\texpanded=\(expanded)\tcard=\(Int(card().width))x\(Int(card().height))\ttopLeft=\(Int(f.minX)),\(Int(f.maxY))\tterm=\(Int(term.frame.width))x\(Int(term.frame.height))")
            fflush(stdout)
        }
        var steps: [(String, () -> Void)] = [
            ("start", {}),
            ("expand", { self.setExpanded(true) }),
            ("shrink", { self.setExpanded(false) }),
            ("expand-again", { self.setExpanded(true) }),
            ("resize-big-to-800x500", { self.resizeTo(NSSize(width: 800, height: 500)) }),
            ("shrink-after-resize", { self.setExpanded(false) }),
            ("expand-remembers-800x500", { self.setExpanded(true) }),
            ("shrink-twice-1", { self.setExpanded(false) }),
            ("shrink-twice-2", { self.setExpanded(false) }),
            ("resize-thin-below-min", { self.resizeTo(NSSize(width: 100, height: 50)) }),
            ("expand-from-tiny", { self.setExpanded(true) }),
            ("shrink-to-min-thin", { self.setExpanded(false) }),
            ("resize-huge", { self.resizeTo(NSSize(width: 9000, height: 9000)) }),
            ("toggle", { self.toggle() }),
            ("toggle", { self.toggle() }),
            ("shrink-and-centre-for-drags", { self.resizeTo(THIN); self.win.center() }),
            ("drag-left-out-100", { self.drag(.left, -100, 0) }),
            ("drag-top-down-500-stops-at-min", { self.drag(.top, 0, -500) }),
            ("drag-bottom-right-out", { self.drag([.bottom, .right], 120, -150) }),
            ("expand-after-drags", { self.setExpanded(true) }),
            ("shrink-back-to-dragged-thin", { self.setExpanded(false) }),
        ]
        func next() {
            guard !steps.isEmpty else { finish(); return }
            let (name, act) = steps.removeFirst()
            act()
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.45) { line(name); next() }
        }
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) { next() }
    }

    func drag(_ e: Edges, _ dx: CGFloat, _ dy: CGFloat) {
        let p0 = NSPoint(x: 500, y: 500)
        dragResize(e, from: win.frame, mouse0: p0, mouse: NSPoint(x: p0.x + dx, y: p0.y + dy))
    }
}

let app = NSApplication.shared
app.setActivationPolicy(.accessory)
let delegate = App()
// An accessory app has no menu bar, so ⌘ shortcuts had nothing to route through. This
// hidden menu gives the terminal ⌘C/⌘V/⌘A, and the box ⌘E, ⌘W, ⌘+ and ⌘−. Every other
// key — Ctrl-C, Esc, arrows, Tab — goes straight to claude.
let mainMenu = NSMenu()
let editItem = NSMenuItem()
mainMenu.addItem(editItem)
let editMenu = NSMenu(title: "Edit")
editMenu.addItem(withTitle: "Copy", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
editMenu.addItem(withTitle: "Paste", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
editMenu.addItem(withTitle: "Select All", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
for (t, sel, key) in [("Expand", #selector(App.expand(_:)), "e"), ("Close", #selector(App.close(_:)), "w"),
                      ("Bigger", #selector(App.bigger(_:)), "+"), ("Bigger", #selector(App.bigger(_:)), "="),
                      ("Smaller", #selector(App.smaller(_:)), "-")] {
    let item = editMenu.addItem(withTitle: t, action: sel, keyEquivalent: key)
    item.target = delegate
}
editItem.submenu = editMenu
app.mainMenu = mainMenu
app.delegate = delegate
app.run()
