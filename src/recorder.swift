// Records the default microphone to a 16 kHz mono WAV until SIGINT/SIGTERM.
// Usage: recorder <output.wav>
import AVFoundation
import Foundation

guard CommandLine.arguments.count == 2 else {
    FileHandle.standardError.write("usage: recorder <output.wav>\n".data(using: .utf8)!)
    exit(2)
}
let url = URL(fileURLWithPath: CommandLine.arguments[1])

let settings: [String: Any] = [
    AVFormatIDKey: kAudioFormatLinearPCM,
    AVSampleRateKey: 16000.0,
    AVNumberOfChannelsKey: 1,
    AVLinearPCMBitDepthKey: 16,
    AVLinearPCMIsFloatKey: false,
    AVLinearPCMIsBigEndianKey: false,
]

func start() {
    do {
        let recorder = try AVAudioRecorder(url: url, settings: settings)
        guard recorder.record() else {
            FileHandle.standardError.write("failed to start recording\n".data(using: .utf8)!)
            exit(1)
        }
        // Stop cleanly so the WAV header gets finalized.
        for sig in [SIGINT, SIGTERM] {
            signal(sig, SIG_IGN)
            let src = DispatchSource.makeSignalSource(signal: sig, queue: .main)
            src.setEventHandler { recorder.stop(); exit(0) }
            src.resume()
            sources.append(src)
        }
        print("recording → \(url.path)")
    } catch {
        FileHandle.standardError.write("error: \(error)\n".data(using: .utf8)!)
        exit(1)
    }
}

var sources: [DispatchSourceSignal] = []

switch AVCaptureDevice.authorizationStatus(for: .audio) {
case .authorized:
    start()
case .notDetermined:
    AVCaptureDevice.requestAccess(for: .audio) { ok in
        DispatchQueue.main.async {
            if ok { start() } else {
                FileHandle.standardError.write("microphone access denied\n".data(using: .utf8)!)
                exit(1)
            }
        }
    }
default:
    FileHandle.standardError.write("microphone access denied (System Settings → Privacy → Microphone)\n".data(using: .utf8)!)
    exit(1)
}

dispatchMain()
