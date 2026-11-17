// On-device text recognition for app_insights: prints "### <frame>" then "x,y|text" per line found
// (normalised, y from the top) for every .jpg in the folder given. macOS only (Vision framework).
// Built and run by scripts/import_app_insights.py extract.

import Foundation
import Vision
import AppKit
let dir = CommandLine.arguments[1]
let files = try! FileManager.default.contentsOfDirectory(atPath: dir).filter { $0.hasSuffix(".jpg") }.sorted()
for f in files {
    let url = URL(fileURLWithPath: dir).appendingPathComponent(f)
    guard let img = NSImage(contentsOf: url), let cg = img.cgImage(forProposedRect: nil, context: nil, hints: nil) else { continue }
    let req = VNRecognizeTextRequest()
    req.recognitionLevel = .accurate
    req.usesLanguageCorrection = false
    try? VNImageRequestHandler(cgImage: cg, options: [:]).perform([req])
    let obs = (req.results ?? []).sorted { $0.boundingBox.minY > $1.boundingBox.minY }
    let lines = obs.compactMap { o -> String? in
        guard let t = o.topCandidates(1).first?.string else { return nil }
        return String(format: "%.2f,%.2f|%@", o.boundingBox.minX, 1 - o.boundingBox.maxY, t)
    }
    print("### \(f)")
    print(lines.joined(separator: "\n"))
}
