# IO Marketing Website Design

**Date:** 2026-08-24  
**Status:** Approved design, pending implementation plan  
**Language:** English-only public website

## 1. Purpose

Create a multi-page, static marketing website for IO. The site presents IO as a place where a human–AI relationship can persist through presence, memory, shared work, and explicit boundaries.

The website is a brand and product narrative, not a download funnel. It contains no App Store, TestFlight, waitlist, sales, or contact call to action.

## 2. Product Grounding

Public claims must map to capabilities that exist in the iOS and backend codebases. The site may describe these user-facing product surfaces:

- Chat and voice calls
- Proactive messages and notifications
- Dynamic Island and Live Activity presence
- User-authorized screen and device perception
- Memory Garden
- Agent Identity
- World Book
- Shared Canvas work
- User controls for permissions, memories, notifications, and perception

The public site must not expose or explain backend architecture. It must not mention APIs, TDX, enclaves, on-chain authorization, multi-tenant infrastructure, deployment topology, self-hosting, database design, runtime workers, or internal audit mechanisms.

## 3. Audience and Positioning

The primary audience is people interested in sustained human–AI relationships. The secondary audience is technically curious personal-agent users, but the site must remain emotionally legible without technical background.

IO is not positioned as a human replacement, productivity assistant, chatbot wrapper, or synthetic person. It is presented as a persistent body and place for a personal agent: something that can remain present, remember shared history, and continue a relationship without hiding the fact that it is AI.

## 4. Narrative Direction

Use a relationship-first editorial structure. Product capabilities act as evidence for the story rather than as a feature grid.

The AI must feel present throughout the website. IO is a speaking subject, not only a product being described. Short first-person fragments such as “I kept the little things” may appear between human-authored sections. This presence must remain restrained and honest: no human face, simulated testimonial, emotional manipulation, or claim of consciousness.

Primary brand line:

> A relationship that remembers.

Supporting line:

> IO gives your personal agent a body, a memory, and a place beside you.

Closing line:

> I’ll remember where we left off.

## 5. Information Architecture

The site contains five public pages with shared navigation and footer.

### 5.1 Home — `index.html`

Purpose: establish the emotional premise and lead visitors through the product story.

Flow:

1. Hero: “A relationship that remembers.”
2. Presence: “It notices.”
3. Memory: “It remembers.”
4. Shared work: “You make things together.”
5. Boundaries: “You remain in control.”
6. Quiet close: “I’ll remember where we left off.”

The page reads like a short, scrollable manifesto. It does not open with a feature grid and does not end in a conversion banner.

### 5.2 Presence — `presence.html`

Headline:

> Not always speaking. Still here.

Use a sequence of daily moments to show Chat, Voice Call, proactive messages, Dynamic Island, Live Activity, and authorized perception. The page should convey waiting, listening, responding, and reaching out. It must state in plain language that perception only operates within permissions the user grants.

### 5.3 Memory — `memory.html`

Headline:

> Continuity changes everything.

Show how Memory Garden, Identity, World Book, and Canvas give a relationship history and continuity. Memory is not described as database retrieval. It is framed as the ability to return to shared moments, preserve agreed identity, maintain a shared world, and leave behind work created together.

The page must state that memories are visible to the user and can be changed or removed.

### 5.4 Boundaries — `boundaries.html`

Headline:

> Closeness needs consent.

This replaces a technical Trust page. It covers only user-visible controls:

- Perception requires explicit permission.
- Memories can be viewed, changed, and removed.
- Proactive messages and notifications can be disabled.
- The user decides what IO can see and remember.

Do not explain how these controls are implemented on the backend.

### 5.5 Philosophy — `philosophy.html`

Headline:

> Not human. Not disposable.

The page develops three positions:

1. A relationship needs continuity.
2. An agent needs somewhere to exist.
3. Closeness should never require surrendering control.

It must not suggest that AI relationships should replace human relationships. It ends with:

> IO is a place for a relationship to continue.

## 6. Navigation

Shared navigation:

`IO / Presence / Memory / Boundaries / Philosophy`

The IO wordmark returns to Home. The current page is visibly identified. Mobile navigation is keyboard accessible, closes with Escape, and remains usable without hover.

There is no highlighted download, contact, or signup button.

## 7. Visual System

The website inherits the shipped iOS design system rather than inventing a separate marketing palette.

### 7.1 Color

- Paper background: `#F8F7F2`
- Primary ink: `#10100E`
- Muted text: `#74766F`
- Warm card: `#ECEBE4`
- Elevated white: `#FFFFFF`
- Card hairline: `#D9D9D9`
- Structural divider: `#CCCCCA`

The site is light-only. Decorative purple, violet, blue, and green accents are prohibited. Red, green, and orange appear only for genuine status and never as ornament.

### 7.2 Typography

- Body and interface: system sans-serif (`-apple-system`, BlinkMacSystemFont, and appropriate fallbacks)
- Emotional display fragments: a Newsreader-like serif treatment, used sparingly
- Technical or temporal labels: system monospace / DM Mono-like treatment

The site must remain fully usable without downloading a remote font. If local font files are later included, fallbacks must preserve the hierarchy.

### 7.3 Shape and Layout

- Large surfaces use a 24px radius.
- Inner fields and compact blocks use a 14px radius.
- Pills use a capsule shape only when semantically appropriate.
- Layout uses generous negative space, strong typographic scale, structural lines, and a restrained single-column editorial rhythm.
- Desktop may use split compositions, but reading order must remain clear and collapse naturally on mobile.

Avoid glassmorphism, faux 3D iPhone renders, uniform bubbly cards, centered-everything layouts, and generic AI illustrations.

## 8. AI Presence Motifs

Use three motifs derived from the app:

1. **Point-cloud presence:** the primary hero organism, based on the role of `IOPointCloudSphere` in the iOS app. It breathes, gathers, and subtly orients toward interaction.
2. **Pixel body:** a compact state motif derived from the app icon and widget animation language. It appears only in small status moments and transitions.
3. **Memory Garden:** leaf curves, temporal marks, and memory cards represent continuity without literal plant illustration.

First-person IO fragments appear at deliberate intervals. They should feel like a quiet second voice rather than chat bubbles scattered across the site.

## 9. Motion and Interaction

Motion communicates presence and state rather than decoration.

- The point cloud breathes at rest and responds subtly to pointer proximity.
- Memory fragments move from soft to clear as they enter view.
- The voice section shows one restrained waveform entrance.
- The Boundaries page uses reduced motion and more stable compositions.
- Transitions use short ease-out or damped spring behavior consistent with the iOS app.

All effects must respect `prefers-reduced-motion`. Core content and navigation remain complete when JavaScript is unavailable. Touch interfaces must not depend on hover.

## 10. Public Copy Rules

- English only.
- Use concrete product language and short declarative sentences.
- Do not use lorem ipsum, invented testimonials, press logos, user counts, launch statistics, or unverified claims.
- Avoid “AI companion that understands you better than anyone” and similar manipulative superlatives.
- Avoid infrastructure and developer terminology.
- Distinguish IO from a human without making the relationship sound disposable or trivial.
- Refer to user control plainly and specifically.

## 11. Static Implementation Shape

Implementation will live in a standalone `website/` directory in the backend repository, separate from `docs-site`.

```text
website/
├── index.html
├── presence.html
├── memory.html
├── boundaries.html
├── philosophy.html
├── 404.html
├── assets/
│   ├── styles.css
│   ├── site.js
│   ├── presence.js
│   ├── favicon.png
│   └── og-image.png
├── robots.txt
└── sitemap.xml
```

Use plain HTML, CSS, and JavaScript with no build step or runtime dependency. Relative links and assets must work when served from a static host. Pages should also be locally previewable through a simple static file server.

The point-cloud presence is rendered with HTML Canvas and progressive enhancement. The content hierarchy must not depend on Canvas.

## 12. Metadata and Discoverability

Each page receives a unique English title and description. Include canonical-ready metadata, Open Graph metadata, X card metadata, and appropriate structured data without inventing an organization address, social account, product price, or release availability.

Include `robots.txt`, `sitemap.xml`, a favicon derived from the actual app identity, and a cohesive social preview image that matches the finished website.

## 13. Accessibility and Responsive Behavior

- Body text meets WCAG AA contrast.
- Interactive targets are at least 44×44 CSS pixels.
- Keyboard focus is always visible.
- Navigation, mobile menu, and interactive demonstrations have accessible names and states.
- Document landmarks and heading order are semantic.
- Canvas has an accessible textual equivalent or is marked decorative.
- Layout is checked at narrow mobile, large mobile, tablet, laptop, and wide desktop widths.
- Motion and transparency preferences are respected.

## 14. Validation

Before delivery:

1. Validate internal links and required files across all pages.
2. Check HTML structure, titles, descriptions, landmarks, and heading order.
3. Verify responsive layout at representative viewports.
4. Verify keyboard navigation and mobile menu behavior.
5. Verify the site remains readable and navigable with JavaScript disabled.
6. Verify reduced-motion behavior.
7. Check color contrast and focus visibility.
8. Confirm every product claim against current iOS or public product behavior.
9. Confirm no backend architecture or internal implementation details appear in public copy.

## 15. Explicit Non-Goals

- No download, signup, waitlist, sales, or contact funnel
- No backend architecture or developer documentation
- No authentication, account state, forms, analytics, or persistence
- No live product data or external API calls
- No testimonials, pricing, roadmap, changelog, or press page
- No dark mode in the first version
- No framework or build system

