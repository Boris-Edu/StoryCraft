// apple_transcribe — on-device transcription with Apple's Speech framework (macOS 26+).
//
// Writes the same JSON contract as BadWords' faster-whisper runner:
//   {"segments": [{"start", "end", "text", "words": [{"word", "start", "end", "probability"}]}], ...}
//
// Usage: apple-transcribe <audio> <out.json> [--module speech|dictation] [--locale en-US]
//   speech    = SpeechTranscriber (new long-form model)
//   dictation = DictationTranscriber (older dictation model, punctuation on)
//
// Build: xcrun swiftc -O -parse-as-library engines/apple_transcribe.swift -o build/apple-transcribe

import AVFoundation
import CoreMedia
import Foundation
import Speech

struct Word: Codable { let word: String; let start: Double; let end: Double; let probability: Double? }
struct Segment: Codable { let start: Double; let end: Double; let text: String; let words: [Word] }
struct Output: Codable {
    let engine: String
    let model: String
    let language: String
    let timings: [String: Double]
    let segments: [Segment]
}

func seconds(_ t: CMTime) -> Double { t.isNumeric ? CMTimeGetSeconds(t) : 0 }

/// Splits an attributed result into words. Runs usually carry one word each; when a run holds
/// several tokens, its time range is shared out evenly between them.
func words(from text: AttributedString) -> [Word] {
    var out: [Word] = []
    for run in text.runs {
        let raw = String(text[run.range].characters)
        let tokens = raw.split(whereSeparator: { $0.isWhitespace }).map(String.init)
        guard !tokens.isEmpty, let tr = run.audioTimeRange else { continue }
        let s = seconds(tr.start), e = seconds(tr.end)
        let step = tokens.count > 1 ? (e - s) / Double(tokens.count) : (e - s)
        for (i, tok) in tokens.enumerated() {
            let ws = s + step * Double(i)
            out.append(Word(word: " " + tok, start: ws, end: ws + step, probability: run.transcriptionConfidence))
        }
    }
    return out
}

func ensureAssets(_ module: any SpeechModule) async throws {
    if let request = try await AssetInventory.assetInstallationRequest(supporting: [module]) {
        FileHandle.standardError.write("Downloading Apple speech assets...\n".data(using: .utf8)!)
        try await request.downloadAndInstall()
    }
}

/// Runs the analyzer over the file and collects finalized results from `results`.
func analyze<R: AsyncSequence & Sendable>(
    file: AVAudioFile, module: any SpeechModule, results: R,
    extract: @escaping @Sendable (R.Element) -> (CMTimeRange, AttributedString)
) async throws -> [Segment] where R.Element: Sendable {
    let collector = Task { () throws -> [Segment] in
        var segs: [Segment] = []
        for try await r in results {
            let (range, text) = extract(r)
            let ws = words(from: text)
            segs.append(Segment(start: seconds(range.start), end: seconds(range.end),
                                text: String(text.characters), words: ws))
        }
        return segs
    }
    let analyzer = SpeechAnalyzer(modules: [module])
    if let last = try await analyzer.analyzeSequence(from: file) {
        try await analyzer.finalizeAndFinish(through: last)
    } else {
        await analyzer.cancelAndFinishNow()
    }
    return try await collector.value
}

@main
struct AppleTranscribe {
    static func main() async {
        let args = CommandLine.arguments
        guard args.count >= 3 else {
            FileHandle.standardError.write("usage: apple-transcribe <audio> <out.json> [--module speech|dictation] [--locale en-US]\n".data(using: .utf8)!)
            exit(2)
        }
        let input = URL(fileURLWithPath: args[1]), output = URL(fileURLWithPath: args[2])
        var moduleName = "speech", localeId = "en-US"
        var i = 3
        while i < args.count - 1 {
            if args[i] == "--module" { moduleName = args[i + 1] }
            if args[i] == "--locale" { localeId = args[i + 1] }
            i += 2
        }

        do {
            let t0 = Date()
            let file = try AVAudioFile(forReading: input)
            let segments: [Segment]
            let attrs: Set<SpeechTranscriber.ResultAttributeOption> = [.audioTimeRange, .transcriptionConfidence]

            if moduleName == "dictation" {
                guard let locale = await DictationTranscriber.supportedLocale(equivalentTo: Locale(identifier: localeId)) else {
                    throw NSError(domain: "apple-transcribe", code: 1, userInfo: [NSLocalizedDescriptionKey: "Locale \(localeId) not supported by DictationTranscriber"])
                }
                let module = DictationTranscriber(locale: locale, contentHints: [], transcriptionOptions: [.punctuation],
                                                  reportingOptions: [], attributeOptions: [.audioTimeRange, .transcriptionConfidence])
                try await ensureAssets(module)
                let tLoad = Date()
                segments = try await analyze(file: file, module: module, results: module.results) { ($0.range, $0.text) }
                try write(output, engine: "apple-dictation", locale: locale, t0: t0, tLoad: tLoad, segments: segments)
            } else {
                guard let locale = await SpeechTranscriber.supportedLocale(equivalentTo: Locale(identifier: localeId)) else {
                    throw NSError(domain: "apple-transcribe", code: 1, userInfo: [NSLocalizedDescriptionKey: "Locale \(localeId) not supported by SpeechTranscriber"])
                }
                let module = SpeechTranscriber(locale: locale, transcriptionOptions: [], reportingOptions: [], attributeOptions: attrs)
                try await ensureAssets(module)
                let tLoad = Date()
                segments = try await analyze(file: file, module: module, results: module.results) { ($0.range, $0.text) }
                try write(output, engine: "apple-speech", locale: locale, t0: t0, tLoad: tLoad, segments: segments)
            }
        } catch {
            FileHandle.standardError.write("apple-transcribe error: \(error)\n".data(using: .utf8)!)
            exit(1)
        }
    }

    static func write(_ url: URL, engine: String, locale: Locale, t0: Date, tLoad: Date, segments: [Segment]) throws {
        let now = Date()
        let out = Output(engine: engine, model: "Apple on-device (\(engine))", language: locale.identifier,
                         timings: ["load_s": tLoad.timeIntervalSince(t0), "transcribe_s": now.timeIntervalSince(tLoad)],
                         segments: segments)
        let enc = JSONEncoder()
        enc.outputFormatting = [.prettyPrinted]
        try enc.encode(out).write(to: url)
    }
}
