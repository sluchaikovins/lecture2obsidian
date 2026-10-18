// Renders an HTML fragment to a phone-friendly PNG (for sending notes as pictures to Telegram).
// Usage: render <input.html> <output.png> [width in points, default 720]
import AppKit

let args = CommandLine.arguments
guard args.count >= 3 else {
    FileHandle.standardError.write("usage: render <input.html> <output.png> [width]\n".data(using: .utf8)!)
    exit(2)
}
let width = CGFloat(args.count > 3 ? Double(args[3]) ?? 720 : 720)
let padding: CGFloat = 36
let scale: CGFloat = 2  // retina: text stays sharp when zoomed on a phone

guard let html = try? Data(contentsOf: URL(fileURLWithPath: args[1])),
      let text = NSAttributedString(html: html, options: [.characterEncoding: String.Encoding.utf8.rawValue],
                                    documentAttributes: nil) else {
    FileHandle.standardError.write("cannot read html\n".data(using: .utf8)!)
    exit(1)
}

let textWidth = width - 2 * padding
let bounds = text.boundingRect(with: NSSize(width: textWidth, height: .greatestFiniteMagnitude),
                               options: [.usesLineFragmentOrigin, .usesFontLeading])
let height = ceil(bounds.height) + 2 * padding

guard let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: Int(width * scale), pixelsHigh: Int(height * scale),
                                 bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
                                 colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0) else { exit(1) }
rep.size = NSSize(width: width, height: height)
NSGraphicsContext.saveGraphicsState()
NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)
NSColor(calibratedRed: 0.99, green: 0.98, blue: 0.96, alpha: 1).setFill()  // warm paper, easy on the eyes
NSRect(x: 0, y: 0, width: width, height: height).fill()
text.draw(with: NSRect(x: padding, y: padding, width: textWidth, height: height - 2 * padding),
          options: [.usesLineFragmentOrigin, .usesFontLeading])
NSGraphicsContext.restoreGraphicsState()

guard let png = rep.representation(using: .png, properties: [:]) else { exit(1) }
do {
    try png.write(to: URL(fileURLWithPath: args[2]))
} catch {
    FileHandle.standardError.write("cannot write png: \(error)\n".data(using: .utf8)!)
    exit(1)
}
