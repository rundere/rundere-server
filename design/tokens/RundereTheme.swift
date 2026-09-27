// Rundere design tokens — SwiftUI. Mirror of design/tokens/tokens.json; change both together.
//
// Setup:
//  • Bundle Outfit (Light/Regular/Medium) and list the files under UIAppFonts in Info.plist.
//    Body text stays SF Pro — do not bundle Figtree for iOS.
//  • Apply `.tint(.rdAccent)` at the app root.

import SwiftUI

private extension Color {
    init(hex: UInt32) {
        self.init(.sRGB,
                  red: Double((hex >> 16) & 0xFF) / 255,
                  green: Double((hex >> 8) & 0xFF) / 255,
                  blue: Double(hex & 0xFF) / 255)
    }
    /// Light/Dark pair resolved by the current trait collection.
    init(light: UInt32, dark: UInt32) {
        self.init(UIColor { $0.userInterfaceStyle == .light
            ? UIColor(Color(hex: light)) : UIColor(Color(hex: dark)) })
    }
}

public extension Color {
    // Semantic — use these in views.
    static let rdBg            = Color(light: 0xF4F6F9, dark: 0x0A1624)
    static let rdSurface       = Color(light: 0xFFFFFF, dark: 0x0F2135)
    static let rdSurfaceRaised = Color(light: 0xFFFFFF, dark: 0x16304A)
    static let rdBorder        = Color(light: 0xCBD3DD, dark: 0x22405F)
    static let rdText          = Color(light: 0x0F2135, dark: 0xE6EAF0)
    static let rdTextSecondary = Color(light: 0x44536A, dark: 0xAAB6C4)
    static let rdTextMuted     = Color(light: 0x5E6E82, dark: 0x8594A6)
    static let rdAccent        = Color(light: 0x1F57B0, dark: 0x7FB2FF)   // Beacon
    static let rdOnAccent      = Color(light: 0xFFFFFF, dark: 0x0A1624)
    static let rdHighlight     = Color(light: 0x8A5C08, dark: 0xF2B544)   // Runway: next up / warning
    static let rdSuccess       = Color(light: 0x1E7F52, dark: 0x4CC38A)
    static let rdDanger        = Color(light: 0xC23B30, dark: 0xF2766B)

    // Brand constants (theme-independent) — mark, app icon, Live Activity.
    static let rdSilver   = Color(hex: 0xE6EAF0)
    static let rdNight    = Color(hex: 0x0F2135)
    static let rdBeacon300 = Color(hex: 0xA9CCFF)
}

public extension Font {
    // Outfit only for large titles and brand moments; scales with Dynamic Type.
    static let rdLargeTitle = Font.custom("Outfit-Light", size: 34, relativeTo: .largeTitle)
    static let rdTitle1     = Font.custom("Outfit-Regular", size: 28, relativeTo: .title)
    static let rdTitle2     = Font.custom("Outfit-Regular", size: 22, relativeTo: .title2)
    // Everything else: system text styles (.title3, .headline, .body, .callout, …).
    // Codes (IATA, flight numbers, confirmation refs):
    static let rdCode = Font.system(.footnote, design: .monospaced).weight(.medium)
}

public enum RDSpace {
    public static let x1: CGFloat = 4,  x2: CGFloat = 8,  x3: CGFloat = 12, x4: CGFloat = 16
    public static let x6: CGFloat = 24, x8: CGFloat = 32, x12: CGFloat = 48, x16: CGFloat = 64
}

public enum RDRadius {
    // Always use with style: .continuous
    public static let control: CGFloat = 12
    public static let card: CGFloat = 16
    public static let sheet: CGFloat = 24
}

public enum RDMotion {
    public static let standard = Animation.timingCurve(0.2, 0, 0, 1, duration: 0.20)
    public static let takeoff  = Animation.timingCurve(0.3, 0, 0, 1, duration: 0.32)
}
