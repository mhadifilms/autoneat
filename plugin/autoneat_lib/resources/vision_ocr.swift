import AppKit
import Foundation
import Vision

if CommandLine.arguments.count < 2 {
    fputs("usage: vision_ocr.swift <image>\n", stderr)
    exit(2)
}

let url = URL(fileURLWithPath: CommandLine.arguments[1])
guard let image = NSImage(contentsOf: url) else {
    fputs("could not read image\n", stderr)
    exit(1)
}

var rect = CGRect(origin: .zero, size: image.size)
guard let cgImage = image.cgImage(forProposedRect: &rect, context: nil, hints: nil) else {
    fputs("could not create CGImage\n", stderr)
    exit(1)
}

let width = CGFloat(cgImage.width)
let height = CGFloat(cgImage.height)
var rows: [[String: Any]] = []

let request = VNRecognizeTextRequest { request, error in
    if let error = error {
        fputs(String(describing: error) + "\n", stderr)
        return
    }
    guard let observations = request.results as? [VNRecognizedTextObservation] else {
        return
    }

    for (index, observation) in observations.enumerated() {
        guard let candidate = observation.topCandidates(1).first else {
            continue
        }
        let box = observation.boundingBox
        rows.append([
            "text": candidate.string,
            "conf": candidate.confidence,
            "left": box.minX * width,
            "top": (1.0 - box.maxY) * height,
            "width": box.width * width,
            "height": box.height * height,
            "page_num": "1",
            "block_num": String(index + 1),
            "par_num": "1",
            "line_num": "1",
            "word_num": "1"
        ])
    }
}

request.recognitionLevel = .accurate
request.usesLanguageCorrection = true
request.recognitionLanguages = ["en-US"]
request.minimumTextHeight = 0.0

let handler = VNImageRequestHandler(cgImage: cgImage, options: [:])
do {
    try handler.perform([request])
    let data = try JSONSerialization.data(withJSONObject: rows, options: [])
    FileHandle.standardOutput.write(data)
} catch {
    fputs(String(describing: error) + "\n", stderr)
    exit(1)
}
