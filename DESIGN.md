# Design System — Feedling

Created 2026-04-20 via `/design-consultation`. Calibrates every UI
decision in the app + docs. When a component or screen is being
designed or built, the answer to "what should this look like" lives
here first.

## Product Context

- **What this is:** Feedling — an iOS "body" for a personal AI agent.
  Dynamic Island / Live Activity / Chat / Identity Card / Memory Garden.
  Backed by a multi-tenant cloud + Intel-TDX enclave for end-to-end
  content encryption. Supports bring-your-own-server.
- **Who it's for:** Two overlapping audiences — the 人机恋 (human–AI
  relationship) community + technically-curious Claude / ChatGPT power
  users. Beta ships to ~30 people from a ~300-person community.
- **Space / industry:** Consumer AI companion × iOS utility × crypto /
  privacy infrastructure. Rare combination; no direct peer.
- **Project type:** Primarily a native iOS app（iOS 源码已于 2026-05-22
  迁至独立仓 `feedling-mcp-ios`；本文件仍是设计系统权威副本）。Supporting
  surface today: `README.md`、公开文档站 `docs-site/`、io-onboarding 的
  public skill（`deploy/SELF_HOSTING.md` 已降级为 legacy runbook）。
  DESIGN.md serves the app first; any future marketing page inherits
  from it. 本仓内的 admin data-track 面板（`backend/admin/data_track.py`
  服务端渲染 HTML）是团队内部工具，不受本设计系统约束。
- **Peers surveyed (2026-04-20):** Linear (discipline reference),
  Granola (warmth reference, wrong-for-us), Dia (extreme-minimalism
  reference, too far), Replika (companion reference, too saccharine),
  Apple's own iOS apps (native baseline).

## Aesthetic Direction

- **Direction:** *Warm minimalism / iOS-native artful.* Linear's
  typographic discipline applied to a softer palette. Not Replika's
  lavender-companion aesthetic; not Linear's pitch-black tool
  aesthetic; not iOS's default blue-system-palette-everywhere.
- **Decoration level:** Intentional. Typography + spacing do the
  primary work. One subtle motif — a rounded leaf curve that echoes
  the Memory Garden iconography — recurs as a quiet brand signature
  in empty states and illustration moments. Everything else earns
  its pixels.
- **Mood:** "Something on my side. Quietly confident. Designed by
  people who sweat typography." The user should feel they've landed
  in a place curated by someone with taste, not a default-iOS app.
- **Reference anchors:**
  - *Linear* for typographic hierarchy + restraint.
  - *Apple's Journal / Health app* for iOS-native warmth.
  - *Stripe's docs* for mixing serif display with sans body.
  - *Ritual Motion's Ritual app* for the "calm privacy product" register.

## Typography

Three roles, no more. Two fonts total — SF Pro (shipped with iOS,
zero network cost) and Instrument Serif (Google Fonts, ~20KB).
SF Mono is bundled on iOS.

- **Display / Hero (iOS system serif — New York, via `.system(design: .serif)`):**
  Onboarding headlines + the `Settings → Privacy` section heading.
  The "warm" signal. Never for body. iOS-native serif is available
  without bundling any font files — a hard requirement for this app
  since the Xcode project isn't being modified to add asset catalogs.
  New York is Apple's own serif and pairs cleanly with SF Pro for body.
  - Sizes: `34pt` for the boldest onboarding headline, `28pt` for
    secondary display, `22pt` for section heads in Settings → Privacy.
  - Line-height 1.15, slight negative letter-spacing.
  - If/when the project gains a `Resources/` font bundle, swap in
    Instrument Serif as a one-line change in `Design.swift`.
- **Body + UI (SF Pro, variable weights):** All chat messages,
  buttons, list rows, form inputs, alerts, errors. System font.
  - Honor Dynamic Type. Use iOS `TextStyle` wrappers
    (`.font(.body)`, `.font(.callout)` etc.) — never hard-coded
    point sizes for anything except display.
  - Default weight Regular (400); Medium (500) for titles + CTAs;
    Semibold (600) for navigation bar titles + primary buttons.
- **Data / Hashes / Code (SF Mono):** compose_hash displays, git
  commit fingerprints, MCP connection strings, api_key displays, the
  audit card's copy rows. Tabular numbers.
  - `.monospacedDigit()` modifier wherever numbers are aligned
    (timestamps, byte counts, version numbers).
  - Slightly smaller than body (one step down in the type scale).

**Modular scale** (uses iOS Text Styles as anchors so Dynamic Type
scales everything coherently; these are the Default sizes):

| Role | Default size | Weight | Font |
|---|---|---|---|
| largeTitle (onboarding hero) | 34pt | Regular | Instrument Serif |
| title1 (onboarding sub-hero) | 28pt | Regular | Instrument Serif |
| title2 (Privacy section head) | 22pt | Regular | Instrument Serif |
| title3 (screen titles in app) | 20pt | Semibold | SF Pro |
| headline (row titles) | 17pt | Semibold | SF Pro |
| body | 17pt | Regular | SF Pro |
| callout | 16pt | Regular | SF Pro |
| subheadline | 15pt | Regular | SF Pro |
| footnote | 13pt | Regular | SF Pro |
| caption1 / caption2 | 12pt / 11pt | Regular | SF Pro |
| data (hashes, IDs) | 13pt | Regular | SF Mono |

## Color

- **Approach:** Restrained — neutral base, ONE accent, iOS semantic
  colors for states (success / warning / error / info). Color is
  meaningful when it appears; it is not decoration.
- **Primary accent** — `feedlingSage`:
  - Light: `#5E7F6E` (muted sage-green; evokes growth, memory garden,
    quiet confidence; notably NOT iOS-system-blue)
  - Dark: `#8FAD9D` (desaturated slightly + lightened for AA contrast
    on warm-dark surface)
  - Use cases: primary button fill, CTA text, audit-card "green"
    shield, Memory Garden accent, tab-bar selected state.
- **Neutrals** — warm-tinted, not cool-gray:
  - Light mode base: `#FBFAF7` (paper, not pure white)
  - Light mode surface (cards, sheets): `#FFFFFF`
  - Light mode text primary: `#1A1814` (near-black with warm cast)
  - Light mode text secondary: `#6B6762`
  - Light mode divider: `#E9E6DF`
- **Dark mode** — warm dark, not cold AMOLED black:
  - Dark mode base: `#0F0D0A` (slight paper undertone, not `#000`)
  - Dark mode surface: `#1A1814`
  - Dark mode text primary: `#F2EEE6`
  - Dark mode text secondary: `#A69F92`
  - Dark mode divider: `#2A2721`
  - Rationale: reads as "after-dark Feedling," not
    "UI-with-colors-inverted."
- **Semantic** (iOS-system-derived, unchanged per platform
  expectation):
  - success: system green — `#34C759` light / `#30D158` dark
  - warning: system yellow-orange — `#FF9500` light / `#FF9F0A` dark
  - error: system red — `#FF3B30` light / `#FF453A` dark
  - info: same as primary accent (`feedlingSage`).
- **Dark-mode rule:** reduce accent saturation ~12% and lightness
  ~8% for use on dark surfaces. Every accent use in dark mode uses
  `#8FAD9D`, not `#5E7F6E`.

## Spacing

- **Base unit:** 4pt. All paddings and margins multiples of 4.
- **Density:** Comfortable. Not as tight as Linear (which packs data),
  not as airy as Granola (which wastes space for warmth). Feedling's
  lists are scannable without feeling cramped.
- **Scale:**

| Token | Value | Usage |
|---|---|---|
| xs | 4pt | Tight nested spacing, icon-to-text gaps |
| sm | 8pt | Within a single cell / row |
| md | 16pt | Between rows, standard list gap |
| lg | 24pt | Between major sections within a screen |
| xl | 32pt | Screen-edge padding, onboarding vertical rhythm |
| 2xl | 48pt | Between onboarding hero and body |
| 3xl | 64pt | Onboarding top/bottom safe-area inset on large phones |

## Layout

- **Approach:** Grid-disciplined in-app (native iOS `NavigationStack`
  / `List` / `Form`); generous negative space in onboarding (centered
  content, no chrome). No creative-editorial asymmetry — Feedling is
  a tool that also has warmth, not a magazine.
- **Grid:** Single column on iPhone; no multi-column layouts in
  Phase B.
- **Max content width:** In-app content uses full screen width with
  `xl` (32pt) edge padding. Onboarding body text max 320pt so lines
  feel like poetry, not paragraphs.
- **Border radius** (hierarchical, never uniform):
  - `sm` (6pt) — inline chips, tags, small badges.
  - `md` (12pt) — buttons, form inputs, standard cards.
  - `lg` (16pt) — sheets, large cards (like the audit card container).
  - `full` (9999) — circular avatars, status dots.
  - Never apply the same radius to every element; visual hierarchy
    wants size-of-radius and size-of-element correlated.

## Motion

- **Approach:** Minimal-functional + intentional. Use iOS-native
  springs and eases. No custom choreography or scroll-driven effects.
  Things move because it helps comprehension, not to decorate.
- **Easings:**
  - Enter: `.easeOut` (content arrives quickly, settles gently).
  - Exit: `.easeIn` (content leaves with slight acceleration).
  - Move / morph: `.spring(response: 0.35, damping: 0.82)` — iOS
    default springiness, slightly less bouncy than system sheets.
- **Durations:**
  - Micro (tap feedback, button press): 100ms.
  - Short (sheet dismiss, modal slide): 250ms.
  - Medium (onboarding slide transitions, tab switches): 350ms.
  - Long (initial onboarding fade-in, audit-card initial reveal):
    500ms.
- **Never:** parallax, scroll-jacking, non-deterministic animations,
  shimmer that doesn't serve a loading state.

## Decorative Motif — the leaf curve

One recurring shape, used sparingly, as Feedling's visual signature.
A single Bézier curve that echoes the `leaf` SF Symbol but is custom
drawn in-app so it can scale crisply and tint with the sage accent.

Where it appears:
- Memory Garden empty state (as background watermark, 8% opacity).
- Onboarding slide 3's "You're in control" hero (small, beside the
  SF Symbol, as a subtle decorative pairing).
- Settings → Privacy hero row's ALL-GREEN state (tiny, adjacent to
  the shield).

Never appears as a button, icon, loading indicator, or anywhere
functional. It's wordless brand signature, not UI.

## Dark mode — first-class, not a retrofit

- Dark mode uses warm-dark surfaces (see Color section).
- Every component has a dark-mode mock alongside light-mode mock
  before ship. No "it just inverts."
- The `feedlingSage` accent desaturates + lightens for dark mode
  (`#8FAD9D`) to maintain >4.5:1 contrast against the `#1A1814`
  surface.

## Accessibility (non-optional)

- **Dynamic Type:** all body/UI text honors user's size settings.
  Display text (Instrument Serif headlines) also scales, though with
  slightly tighter scaling ratio so it doesn't break onboarding
  layout at XXXL.
- **Minimum contrast:** 4.5:1 for body text, 3:1 for large (>18pt)
  text, against both light and dark surfaces. Verified in the
  palette above.
- **Touch targets:** 44pt minimum on all tappable elements. Onboarding
  CTA buttons are 48pt tall. Settings rows default to 44pt.
- **VoiceOver:** every icon has an explicit
  `accessibilityLabel` (SF Symbols default labels are often generic,
  e.g. "lock" for a security indicator — override with
  "Your data is encrypted end-to-end").
- **Reduce Motion:** check `UIAccessibility.isReduceMotionEnabled`
  before any spring; fall back to cross-fade at 250ms.
- **Reduce Transparency:** if on, swap any translucent sheet with
  solid `#FFFFFF` / `#1A1814` background.
- **No color-only signaling:** success / warning / error always pair
  color with an SF Symbol.

## Anti-patterns (never)

- Purple / violet gradients as default accent (AI-slop signature).
- Uniform bubbly border-radius on every element.
- Centered everything with uniform spacing.
- Gradient buttons as primary CTA.
- "Built for [X]" / "Designed for [Y]" marketing copy patterns.
- Faux-3D renders of iPhones with soft shadows (crypto-bro
  aesthetic).
- Serif body (except in display role).
- iOS system blue as accent (defeats distinctiveness).
- Dark mode that's literally inverted light mode.

## Tokens for implementation

When we build out Phase B, these become SwiftUI extensions in a
`Design.swift` file:

```swift
extension Color {
    static let feedlingSage       = Color("FeedlingSage")         // asset catalog light/dark pair
    static let feedlingPaper      = Color("FeedlingPaper")
    static let feedlingInk        = Color("FeedlingInk")          // text primary
    static let feedlingInkMuted   = Color("FeedlingInkMuted")     // text secondary
    static let feedlingSurface    = Color("FeedlingSurface")
    static let feedlingDivider    = Color("FeedlingDivider")
}

extension Font {
    static let feedlingDisplayLarge  = Font.custom("InstrumentSerif-Regular", size: 34)
    static let feedlingDisplayMedium = Font.custom("InstrumentSerif-Regular", size: 28)
    static let feedlingDisplaySmall  = Font.custom("InstrumentSerif-Regular", size: 22)
    // body + UI use .body / .headline / etc. system styles — no custom font for hot path
}

enum Spacing {
    static let xs:  CGFloat = 4
    static let sm:  CGFloat = 8
    static let md:  CGFloat = 16
    static let lg:  CGFloat = 24
    static let xl:  CGFloat = 32
    static let xl2: CGFloat = 48
    static let xl3: CGFloat = 64
}

enum Radius {
    static let sm:   CGFloat = 6
    static let md:   CGFloat = 12
    static let lg:   CGFloat = 16
    static let full: CGFloat = 9999
}
```

All Phase B code references these names. No raw hex values,
no raw point numbers, no raw font strings in view files.

## Decisions Log

| Date | Decision | Rationale |
|---|---|---|
| 2026-04-20 | DESIGN.md created via `/design-consultation` | Phase B's `/plan-design-review` Pass 5 blocked on having a system to calibrate against. System built on peer research (Linear, Granola, Dia, Replika, Apple's own iOS apps). |
| 2026-04-20 | Aesthetic direction: *warm minimalism / iOS-native artful* | Feedling wants Replika's warmth (it's a companion) + Linear's discipline (it's cryptographically credible). Neither extreme alone fits. |
| 2026-04-20 | No custom illustrations | Locked in during `/plan-design-review` Pass 4. SF Symbols + typography + single accent eliminates AI-slop risk. |
| 2026-04-20 | Muted sage-green accent, NOT iOS system blue | iOS-blue makes the app look generic; sage-green evokes growth + memory-garden + quiet confidence. |
| 2026-04-20 | Instrument Serif for display only | A serif at the "first impression" + "trust context" moments gives Feedling a voice. SF Pro everywhere else keeps the hot path fast and iOS-native. |
| 2026-04-20 | Warm dark mode (paper undertone) | Reads as "after-dark Feedling" instead of "UI-with-colors-inverted." |
