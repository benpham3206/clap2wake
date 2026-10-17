import AppKit
import SwiftUI
import Foundation

private struct CommandResult: Sendable {
    let status: Int32
    let output: String
}

private func command(_ executable: String, _ arguments: [String]) -> CommandResult {
    let process = Process()
    let pipe = Pipe()
    process.executableURL = URL(fileURLWithPath: executable)
    process.arguments = arguments
    process.standardOutput = pipe
    process.standardError = pipe
    do {
        try process.run()
        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        process.waitUntilExit()
        return CommandResult(status: process.terminationStatus,
                             output: String(decoding: data, as: UTF8.self))
    } catch {
        return CommandResult(status: -1, output: error.localizedDescription)
    }
}

private struct LogEvent: Identifiable, Sendable {
    let id = UUID()
    let name: String
    let date: Date
    let clock: String
    let device: String?
    let threshold: Double?
    let gap: Double?

    var label: String {
        switch name {
        case "wake_triggered": return "Wake"
        case "sleep_triggered": return "Dim"
        case "sleep_suppressed": return "Dim skipped (locked)"
        case "audio_stream_dead": return "Mic stalled"
        case "listening": return "Listening"
        default: return name
        }
    }
}

// EventLogs owns each tail. Keep partial JSON lines until complete.
private final class LogTail {
    private let path: String
    private var offset: UInt64 = 0
    private var inode: UInt64?
    private var pending = Data()
    private var started = false
    private let dates: DateFormatter = {
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.dateFormat = "yyyy-MM-dd'T'HH:mm:ss"
        return formatter
    }()

    init(_ path: String) { self.path = path }

    func read() -> [LogEvent] {
        do {
            let attributes = try FileManager.default.attributesOfItem(atPath: path)
            let size = (attributes[.size] as? NSNumber)?.uint64Value ?? 0
            let currentInode = (attributes[.systemFileNumber] as? NSNumber)?.uint64Value
            if size < offset || (inode != nil && inode != currentInode) {
                offset = 0
                pending.removeAll()
            }
            inode = currentInode
            if !started {
                started = true
                offset = size > 65_536 ? size - 65_536 : 0
            }
            let handle = try FileHandle(forReadingFrom: URL(fileURLWithPath: path))
            defer { try? handle.close() }
            try handle.seek(toOffset: offset)
            let data = try handle.readToEnd() ?? Data()
            offset += UInt64(data.count)
            pending.append(data)
            var events: [LogEvent] = []
            while let newline = pending.firstIndex(of: 10) {
                let line = pending[..<newline]
                pending.removeSubrange(...newline)
                guard let object = try? JSONSerialization.jsonObject(with: line) as? [String: Any],
                      let name = (object["event"] ?? object["failure_type"]) as? String,
                      !name.contains("heartbeat"),
                      let stamp = object["ts"] as? String,
                      let date = dates.date(from: stamp) else { continue }
                let band = object["wake_band"] as? [Double]
                events.append(LogEvent(name: name, date: date, clock: String(stamp.suffix(8)),
                                       device: object["device_name"] as? String,
                                       threshold: object["threshold"] as? Double,
                                       gap: band?.last))
            }
            // A damaged unterminated line must not retain an unbounded buffer.
            if pending.count > 262_144 { pending.removeAll() }
            return events
        } catch { return [] }
    }
}

private actor EventLogs {
    private let out = LogTail("/tmp/clapwake.out")
    private let err = LogTail("/tmp/clapwake.err")

    func read() -> [LogEvent] {
        (out.read() + err.read()).sorted { $0.date < $1.date }
    }
}

@MainActor
private final class BarModel: ObservableObject {
    @Published var loaded = false
    @Published var running = false
    @Published var sleepDisabled = false
    @Published var powerKnown = false
    @Published var device = "Microphone"
    @Published var displayedPeak = 0.0
    @Published var peak = 0.0
    @Published var threshold = 0.55
    @Published var release = 0.08
    @Published var hold = 0.0
    @Published var lastPacket = Date.distantPast
    @Published var stalledAt = Date.distantPast
    @Published var sensitivity: Double = 0.55
    @Published var gap: Double = 1.2
    @Published var applying = false
    @Published var writing = false
    @Published var settingsStatus = ""
    @Published var controlError = ""
    @Published var changingPower = false
    @Published var changingListener = false
    @Published var healthRunning = false
    @Published var healthOutput = ""
    @Published var events: [LogEvent] = []
    @Published var now = Date()
    private let repo: URL
    private let settingsURL: URL
    private let domain = "gui/\(getuid())"
    private let logs = EventLogs()
    private var timer: Timer?
    private var receiver: DispatchSourceRead?
    private var generation = 0
    // Live updates only while the panel is open; closed, they redraw for nobody.
    var visible = false { didSet { if visible { now = Date(); refresh() } } }
    private var ticks = 0
    private var holdUntil = Date.distantPast
    private var expected: (threshold: Double, gap: Double, since: Date)?

    init() {
        let env = ProcessInfo.processInfo.environment
        repo = URL(fileURLWithPath: (env["CLAPWAKE_DIR"] ?? "~/clapwake")
            .expandingTilde)
        settingsURL = URL(fileURLWithPath: (env["CLAPWAKE_SETTINGS"] ??
            "~/Library/Application Support/clapwake/settings.json").expandingTilde)
        let path = settingsURL
        Task.detached {
            let object = (try? Data(contentsOf: path)).flatMap {
                try? JSONSerialization.jsonObject(with: $0) as? [String: Any]
            }
            let threshold = object?["threshold"] as? Double ?? 0.55
            let gap = object?["max_gap_s"] as? Double ?? 1.2
            await MainActor.run { self.sensitivity = threshold; self.gap = gap }
        }
        startReceiver()
        refresh()
        tailLogs()
        timer = Timer.scheduledTimer(withTimeInterval: 0.05, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.tick() }
        }
    }

    var hasSignal: Bool { now.timeIntervalSince(lastPacket) <= 1.5 }
    var stalled: Bool { now.timeIntervalSince(stalledAt) < 30 }

    private func tick() {
        ticks += 1
        if visible {
            now = Date()
            if now > holdUntil && hold > 0 { hold = max(0, hold - 0.025) }
        }
        if ticks % 20 == 0 { tailLogs() }
        if ticks % 100 == 0 { refresh() }
        if let expected, applying && now.timeIntervalSince(expected.since) >= 10 {
            applying = false
            self.expected = nil
            settingsStatus = "Error: No matching listening event after 10 s."
        }
    }

    private func startReceiver() {
        let fd = socket(AF_INET, SOCK_DGRAM, 0)
        guard fd >= 0 else { controlError = "Cannot create the level socket."; return }
        var address = sockaddr_in()
        address.sin_len = UInt8(MemoryLayout<sockaddr_in>.size)
        address.sin_family = sa_family_t(AF_INET)
        address.sin_port = UInt16(47811).bigEndian
        address.sin_addr.s_addr = inet_addr("127.0.0.1")
        let bound = withUnsafePointer(to: &address) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                bind(fd, $0, socklen_t(MemoryLayout<sockaddr_in>.size))
            }
        }
        guard bound == 0, fcntl(fd, F_SETFL, O_NONBLOCK) == 0 else {
            close(fd)
            controlError = "Cannot bind 127.0.0.1:47811. Another panel may be open."
            return
        }
        let source = DispatchSource.makeReadSource(fileDescriptor: fd,
            queue: DispatchQueue(label: "com.you.clapwake.bar.level"))
        source.setEventHandler { [weak self] in
            var buffer = [UInt8](repeating: 0, count: 1024)
            while true {
                let count = recv(fd, &buffer, buffer.count, 0)
                guard count > 0 else { break }
                guard let object = try? JSONSerialization.jsonObject(with: Data(buffer.prefix(count))) as? [String: Double],
                      let peak = object["peak"], let threshold = object["thresh"],
                      let release = object["release"],
                      [peak, threshold, release].allSatisfy({ $0.isFinite && (0...1).contains($0) }) else { continue }
                Task { @MainActor [weak self] in
                    guard let self, self.visible else { return }
                    self.peak = peak
                    self.threshold = threshold
                    self.release = release
                    self.lastPacket = Date()
                    if peak >= self.hold {
                        self.hold = peak
                        self.holdUntil = Date().addingTimeInterval(1)
                    }
                }
            }
        }
        source.setCancelHandler { close(fd) }
        receiver = source
        source.resume()
    }

    func refresh() {
        let started = generation
        let service = "\(domain)/com.you.clapwake"
        Task.detached {
            let power = command("/usr/bin/pmset", ["-g"])
            let listener = command("/bin/launchctl", ["print", service])
            let value = power.output.split(separator: "\n").compactMap { line -> Bool? in
                let fields = line.split(whereSeparator: { $0.isWhitespace })
                guard fields.count >= 2, fields[0] == "SleepDisabled" else { return nil }
                return fields[1] == "1"
            }.first
            await MainActor.run {
                guard self.generation == started else { return }
                let known = power.status == 0 && value != nil
                if known != self.powerKnown { self.powerKnown = known }
                if let value, value != self.sleepDisabled { self.sleepDisabled = value }
                let loaded = listener.status == 0
                let running = loaded && listener.output.contains("\tpid = ")
                if loaded != self.loaded { self.loaded = loaded }
                if running != self.running { self.running = running }
            }
        }
    }

    private func tailLogs() {
        Task {
            let batch = await logs.read()
            for event in batch {
                if event.name == "audio_stream_dead" { stalledAt = event.date }
                if event.name == "listening" {
                    device = event.device ?? "Microphone"
                    if let expected, applying,
                       event.date.timeIntervalSince1970 >= floor(expected.since.timeIntervalSince1970),
                       let threshold = event.threshold, let gap = event.gap,
                       abs(threshold - expected.threshold) < 0.0001,
                       abs(gap - expected.gap) < 0.0001 {
                        applying = false
                        settingsStatus = "Applied"
                        self.expected = nil
                    }
                }
            }
            if !batch.isEmpty { events = Array((events + batch).sorted { $0.date > $1.date }.prefix(6)) }
        }
    }

    func setPower(_ enabled: Bool) {
        guard !changingPower else { return }
        changingPower = true
        generation += 1
        controlError = ""
        Task.detached {
            let value = enabled ? "1" : "0"
            let attempt = command("/usr/bin/sudo", ["-n", "/usr/bin/pmset", "-a", "disablesleep", value])
            var failure = ""
            if attempt.status != 0 {
                var error: NSDictionary?
                let script = NSAppleScript(source:
                    "do shell script \"/usr/bin/pmset -a disablesleep \(value)\" with administrator privileges")
                if script?.executeAndReturnError(&error) == nil {
                    failure = (error?[NSAppleScript.errorMessage] as? String) ?? "Keep-awake change was cancelled."
                }
            }
            // Always read the real value, including after a cancelled prompt.
            let result = command("/usr/bin/pmset", ["-g"])
            let fields = result.output.split(separator: "\n").map { $0.split(whereSeparator: { $0.isWhitespace }) }
            let actual = fields.first { $0.count >= 2 && $0[0] == "SleepDisabled" }.map { $0[1] == "1" }
            let message = failure
            await MainActor.run {
                self.powerKnown = result.status == 0 && actual != nil
                if let actual { self.sleepDisabled = actual }
                self.controlError = message
                if message.isEmpty && actual != enabled { self.controlError = "Keep-awake read-back did not match." }
                self.changingPower = false
                self.generation += 1
            }
        }
    }

    func setListener(_ enabled: Bool) {
        guard !changingListener else { return }
        changingListener = true
        generation += 1
        controlError = ""
        let domain = domain
        Task.detached {
            let args = enabled ? ["bootstrap", domain,
                ("~/Library/LaunchAgents/com.you.clapwake.plist" as NSString).expandingTildeInPath]
                : ["bootout", "\(domain)/com.you.clapwake"]
            let result = command("/bin/launchctl", args)
            await MainActor.run {
                self.controlError = result.status == 0 ? "" : result.output
                self.changingListener = false
                self.generation += 1
                self.refresh()
            }
        }
    }

    func applySettings(threshold: Double? = nil, gap: Double? = nil) {
        guard !applying && !writing else { return }
        if let threshold { sensitivity = threshold }
        if let gap { self.gap = gap }
        let threshold = sensitivity
        let maxGap = self.gap
        guard (0.20...0.90).contains(threshold), (0.50...2.50).contains(maxGap) else {
            settingsStatus = "Error: Settings are outside the supported bounds."
            return
        }
        applying = true
        writing = true
        settingsStatus = "Applying…"
        expected = (threshold, maxGap, Date())
        let path = settingsURL
        let service = "\(domain)/com.you.clapwake"
        Task.detached {
            do {
                var object: [String: Any] = [:]
                if FileManager.default.fileExists(atPath: path.path) {
                    let data = try Data(contentsOf: path)
                    object = (try? JSONSerialization.jsonObject(with: data) as? [String: Any]) ?? [:]
                }
                object["threshold"] = threshold
                object["max_gap_s"] = maxGap
                try FileManager.default.createDirectory(at: path.deletingLastPathComponent(), withIntermediateDirectories: true)
                let data = try JSONSerialization.data(withJSONObject: object, options: [.sortedKeys])
                try data.write(to: path, options: [.atomic])
                let result = command("/bin/launchctl", ["kickstart", "-k", service])
                if result.status != 0 { throw NSError(domain: "clapwake", code: Int(result.status),
                    userInfo: [NSLocalizedDescriptionKey: result.output]) }
                await MainActor.run { self.writing = false }
            } catch {
                let message = error.localizedDescription
                await MainActor.run {
                    self.writing = false
                    self.applying = false
                    self.expected = nil
                    self.settingsStatus = "Error: \(message)"
                }
            }
        }
    }

    func healthCheck() {
        guard !healthRunning else { return }
        healthRunning = true
        let repo = repo
        Task.detached {
            let result = command(repo.appendingPathComponent(".venv/bin/python3").path,
                                 [repo.appendingPathComponent("check_clapwake.py").path])
            await MainActor.run {
                self.healthOutput = result.output.isEmpty ? "Exit \(result.status)" : result.output
                self.healthRunning = false
            }
        }
    }

    func openLog() {
        Task.detached { _ = command("/usr/bin/open", ["-a", "Console", "/tmp/clapwake.err"]) }
    }
}

private extension String {
    var expandingTilde: String { (self as NSString).expandingTildeInPath }
}

private struct LevelMeter: View {
    @ObservedObject var model: BarModel
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            GeometryReader { geometry in
                ZStack(alignment: .leading) {
                    Capsule().fill(.quaternary)
                    Capsule().fill(model.peak >= model.threshold ? Color.accentColor : Color.secondary.opacity(0.5))
                        .frame(width: geometry.size.width * (model.hasSignal ? model.displayedPeak : 0))
                    Rectangle().fill(.secondary.opacity(0.4)).frame(width: 1)
                        .offset(x: geometry.size.width * model.release)
                    Rectangle().fill(.primary).frame(width: 1)
                        .offset(x: geometry.size.width * model.threshold)
                    Rectangle().fill(.primary.opacity(0.7)).frame(width: 2, height: 12)
                        .offset(x: min(geometry.size.width - 2, geometry.size.width * model.hold))
                }
            }.frame(height: 10)
            Text(model.hasSignal ? "Mic level" : "No signal")
                .font(.caption).foregroundStyle(.secondary)
        }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("Microphone level")
        .accessibilityValue(model.hasSignal ? "\(Int(model.peak * 100)) percent. Threshold \(Int(model.threshold * 100)) percent." : "No signal")
        .onChange(of: model.lastPacket) { _, _ in
            if reduceMotion || model.peak >= model.displayedPeak { model.displayedPeak = model.peak }
            else { withAnimation(.easeOut(duration: 0.1)) { model.displayedPeak = model.peak } }
        }
        .onChange(of: reduceMotion) { _, _ in model.displayedPeak = model.peak }
        .onAppear { model.displayedPeak = model.peak }
    }
}

private struct Panel: View {
    @ObservedObject var model: BarModel
    private var status: String {
        if !model.loaded { return "Stopped" }
        if !model.running { return "Not running" }
        if model.stalled { return "Mic stalled" }
        return "Listening · \(model.device)"
    }
    private var statusColor: Color {
        !model.loaded ? .secondary : !model.running || model.stalled ? .orange : .green
    }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                VStack(alignment: .leading, spacing: 5) {
                    Text("clap2wake").font(.headline)
                    HStack(spacing: 6) {
                        Circle().fill(statusColor).frame(width: 6, height: 6).accessibilityHidden(true)
                        Text(status).font(.caption).foregroundStyle(.secondary).lineLimit(2)
                    }.accessibilityElement(children: .combine)
                }
                LevelMeter(model: model)
                Divider()
                VStack(alignment: .leading, spacing: 12) {
                    Toggle("Keep awake with lid closed", isOn: Binding(get: { model.sleepDisabled }, set: { model.setPower($0) }))
                        .disabled(model.changingPower || !model.powerKnown)
                        .accessibilityLabel("Keep awake with lid closed")
                    Text("Off before you pack the Mac.").font(.caption).foregroundStyle(.secondary)
                    if !model.powerKnown { Text("Power state unavailable").font(.caption).foregroundStyle(.secondary) }
                    Toggle("Listener", isOn: Binding(get: { model.loaded }, set: { model.setListener($0) }))
                        .disabled(model.changingListener || model.applying || model.writing).accessibilityLabel("Listener")
                    if !model.controlError.isEmpty {
                        Text(model.controlError).font(.caption).foregroundStyle(.red).textSelection(.enabled)
                    }
                }.toggleStyle(.switch)
                Divider()
                VStack(alignment: .leading, spacing: 10) {
                    Text("Sensitivity").font(.caption).foregroundStyle(.secondary)
                    Picker("Sensitivity", selection: Binding(get: { model.sensitivity }, set: { model.applySettings(threshold: $0) })) {
                        Text("Low").tag(0.65)
                        Text("Medium").tag(0.55)
                        Text("High").tag(0.45)
                    }.pickerStyle(.segmented).labelsHidden().accessibilityLabel("Sensitivity")
                    Text("Clap gap").font(.caption).foregroundStyle(.secondary)
                    Picker("Clap gap", selection: Binding(get: { model.gap }, set: { model.applySettings(gap: $0) })) {
                        Text("0.8 s").tag(0.8)
                        Text("1.2 s").tag(1.2)
                        Text("1.6 s").tag(1.6)
                    }.pickerStyle(.segmented).labelsHidden().accessibilityLabel("Clap gap")
                    if !model.settingsStatus.isEmpty {
                        Text(model.settingsStatus).font(.caption).foregroundStyle(.secondary)
                            .accessibilityLabel("Settings status: \(model.settingsStatus)")
                    }
                }.disabled(model.applying || model.writing || model.changingListener)
                Divider()
                VStack(alignment: .leading, spacing: 8) {
                    Text("Recent events").font(.caption).foregroundStyle(.secondary)
                    if model.events.isEmpty { Text("No events").font(.caption).foregroundStyle(.secondary) }
                    ForEach(model.events) { event in
                        HStack(alignment: .firstTextBaseline) {
                            Text(event.label).lineLimit(2)
                            Spacer(minLength: 6)
                            Text(event.clock)
                                .monospacedDigit().foregroundStyle(.secondary)
                        }.font(.caption).accessibilityElement(children: .combine)
                    }
                }
                Divider()
                HStack {
                    Button(model.healthRunning ? "Checking…" : "Health check") { model.healthCheck() }
                        .disabled(model.healthRunning).accessibilityLabel("Health check")
                    Spacer()
                    Button("Open log") { model.openLog() }.accessibilityLabel("Open log")
                    Button("Quit") { NSApplication.shared.terminate(nil) }.accessibilityLabel("Quit")
                }.buttonStyle(.borderless).font(.caption)
                if !model.healthOutput.isEmpty {
                    Text(model.healthOutput).font(.system(.caption, design: .monospaced))
                        .textSelection(.enabled).frame(maxWidth: .infinity, alignment: .leading)
                        .accessibilityLabel("Health check output")
                }
            }.padding(18)
        }.frame(width: 320).frame(maxHeight: 740)
            .onAppear { model.visible = true }
            .onDisappear { model.visible = false }
    }
}

@main
struct ClapwakeBar: App {
    @StateObject private var model = BarModel()
    private var symbol: String {
        let name = model.running ? "hands.clap.fill" : "hands.clap"
        return NSImage(systemSymbolName: name, accessibilityDescription: nil) == nil ? "waveform" : name
    }
    var body: some Scene {
        MenuBarExtra {
            Panel(model: model)
        } label: {
            Image(systemName: symbol).symbolRenderingMode(.hierarchical)
                .foregroundStyle(model.running ? .primary : .secondary)
                .accessibilityLabel(model.running ? "clap2wake listening" : "clap2wake stopped")
        }.menuBarExtraStyle(.window)
    }
}
