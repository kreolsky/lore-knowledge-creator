/// Lore Audio Capture — Swift CLI for microphone recording.
///
/// Usage:
///   lore-audio-capture start <output.m4a>   — start recording, writes PID to stdout
///   lore-audio-capture stop <pid>            — signal the recording process to stop
///
/// Records microphone input to M4A (AAC, 128kbps, 44.1kHz, mono).
/// Stops on SIGUSR1.

import Foundation
import AVFoundation

let args = CommandLine.arguments
guard args.count >= 3 else {
    FileHandle.standardError.write("Usage: lore-audio-capture start <output.m4a>\n       lore-audio-capture stop <pid>\n".data(using: .utf8)!)
    exit(1)
}

let command = args[1]

if command == "stop" {
    guard let pid = Int32(args[2]) else {
        FileHandle.standardError.write("Invalid PID\n".data(using: .utf8)!)
        exit(1)
    }
    kill(pid, SIGUSR1)
    exit(0)
}

guard command == "start" else {
    FileHandle.standardError.write("Unknown command: \(command)\n".data(using: .utf8)!)
    exit(1)
}

let outputPath = args[2]
let outputURL = URL(fileURLWithPath: outputPath)

nonisolated(unsafe) var shouldStop = false
signal(SIGUSR1) { _ in shouldStop = true }

let settings: [String: Any] = [
    AVFormatIDKey: Int(kAudioFormatMPEG4AAC),
    AVSampleRateKey: 44100.0,
    AVNumberOfChannelsKey: 1,
    AVEncoderBitRateKey: 128_000,
    AVEncoderAudioQualityKey: AVAudioQuality.high.rawValue,
]

do {
    let recorder = try AVAudioRecorder(url: outputURL, settings: settings)
    recorder.prepareToRecord()

    guard recorder.record() else {
        FileHandle.standardError.write("Failed to start recording\n".data(using: .utf8)!)
        exit(1)
    }

    FileHandle.standardError.write("Recording started to \(outputPath)\n".data(using: .utf8)!)

    // Print PID so Rust parent can send SIGUSR1 to stop
    print(ProcessInfo.processInfo.processIdentifier)
    fflush(stdout)

    // Wait for stop signal (SIGUSR1) — no duration limit
    while !shouldStop {
        RunLoop.current.run(until: Date().addingTimeInterval(0.1))
    }

    recorder.stop()
    FileHandle.standardError.write("Recording stopped, file size: \(try FileManager.default.attributesOfItem(atPath: outputPath)[.size] ?? 0)\n".data(using: .utf8)!)

} catch {
    FileHandle.standardError.write("Error: \(error.localizedDescription)\n".data(using: .utf8)!)
    exit(1)
}
