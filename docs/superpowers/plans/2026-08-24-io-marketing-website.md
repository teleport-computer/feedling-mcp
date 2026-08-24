# IO Marketing Website Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build an English-only, five-page static IO marketing website that presents a sustained human–AI relationship through presence, memory, shared work, and user-controlled boundaries.

**Architecture:** Create a standalone `website/` directory with plain HTML, shared CSS, and progressive-enhancement JavaScript. Each page owns its editorial content while `assets/styles.css` and `assets/site.js` provide the shared system; `assets/presence.js` renders the optional point-cloud organism without becoming a content dependency. A Node built-in verifier checks page structure, metadata, links, required copy, and the ban on backend architecture language.

**Tech Stack:** HTML5, CSS3, Canvas 2D, browser-native JavaScript, Node.js built-ins for verification

**Spec:** `docs/superpowers/specs/2026-08-24-io-marketing-website-design.md`

## Global Constraints

- Public website copy is English only.
- Use only plain HTML, CSS, and JavaScript; add no framework, package manager, build step, analytics, persistence, or external API call.
- Pages are `index.html`, `presence.html`, `memory.html`, `boundaries.html`, and `philosophy.html`, plus `404.html`.
- Shared navigation is `IO / Presence / Memory / Boundaries / Philosophy` with no download, signup, waitlist, sales, or contact call to action.
- Public copy must not mention APIs, TDX, enclaves, on-chain authorization, multi-tenant infrastructure, deployment topology, self-hosting, database design, runtime workers, or internal audit mechanisms.
- Use the shipped palette: paper `#F8F7F2`, ink `#10100E`, muted `#74766F`, card `#ECEBE4`, white `#FFFFFF`, hairline `#D9D9D9`, divider `#CCCCCA`.
- Do not use generic AI illustrations, human faces, testimonials, press logos, user counts, launch statistics, or unverified product claims.
- Core content and navigation must remain complete without JavaScript; all motion respects `prefers-reduced-motion`.

---

### Task 1: Contract Verifier and Shared Site Foundation

**Files:**
- Create: `website/tests/verify-site.mjs`
- Create: `website/assets/styles.css`
- Create: `website/assets/site.js`
- Create: `website/index.html`

**Interfaces:**
- Consumes: the page names, banned terminology, palette, and content rules in the design spec
- Produces: `node website/tests/verify-site.mjs`, CSS utility/component classes, and the shared `window.IOSite` browser interface used by every later page

- [ ] **Step 1: Write the failing site contract verifier**

Create `website/tests/verify-site.mjs` using only Node built-ins. It must:

```js
import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import { dirname, extname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const root = resolve(here, "..");
const pages = [
  "index.html",
  "presence.html",
  "memory.html",
  "boundaries.html",
  "philosophy.html",
  "404.html",
];
const publicPages = pages.slice(0, 5);
const banned = [
  /\bAPI(?:s)?\b/i,
  /\bTDX\b/i,
  /\benclave(?:s)?\b/i,
  /\bon-chain\b/i,
  /\bmulti-tenant\b/i,
  /\bself-host(?:ed|ing)?\b/i,
  /\bdatabase design\b/i,
  /\bruntime workers?\b/i,
  /\bbackend architecture\b/i,
];
const requiredHeadlines = new Map([
  ["index.html", "A relationship that remembers."],
  ["presence.html", "Not always speaking. Still here."],
  ["memory.html", "Continuity changes everything."],
  ["boundaries.html", "Closeness needs consent."],
  ["philosophy.html", "Not human. Not disposable."],
]);

for (const page of pages) {
  assert.ok(existsSync(join(root, page)), `missing ${page}`);
}

const titles = new Set();
const descriptions = new Set();
for (const page of publicPages) {
  const html = readFileSync(join(root, page), "utf8");
  assert.match(html, /<html\s+lang="en"/i, `${page}: lang must be en`);
  assert.match(html, /<header\b/i, `${page}: missing header`);
  assert.match(html, /<nav\b[^>]*aria-label="Primary"/i, `${page}: missing primary nav`);
  assert.match(html, /<main\b/i, `${page}: missing main`);
  assert.match(html, /<footer\b/i, `${page}: missing footer`);
  assert.equal((html.match(/<h1\b/gi) ?? []).length, 1, `${page}: needs one h1`);
  assert.ok(html.includes(requiredHeadlines.get(page)), `${page}: wrong headline`);
  assert.ok(html.includes('href="index.html"'), `${page}: missing Home link`);
  assert.ok(html.includes('href="presence.html"'), `${page}: missing Presence link`);
  assert.ok(html.includes('href="memory.html"'), `${page}: missing Memory link`);
  assert.ok(html.includes('href="boundaries.html"'), `${page}: missing Boundaries link`);
  assert.ok(html.includes('href="philosophy.html"'), `${page}: missing Philosophy link`);

  const title = html.match(/<title>([^<]+)<\/title>/i)?.[1]?.trim();
  const description = html.match(/<meta\s+name="description"\s+content="([^"]+)"/i)?.[1]?.trim();
  assert.ok(title, `${page}: missing title`);
  assert.ok(description, `${page}: missing description`);
  assert.ok(!titles.has(title), `${page}: duplicate title`);
  assert.ok(!descriptions.has(description), `${page}: duplicate description`);
  titles.add(title);
  descriptions.add(description);

  for (const pattern of banned) assert.doesNotMatch(html, pattern, `${page}: banned public term ${pattern}`);
  assert.doesNotMatch(html, /\b(download|sign up|join the waitlist|contact us)\b/i, `${page}: CTA is out of scope`);

  for (const href of html.matchAll(/href="([^"]+)"/g)) {
    const target = href[1];
    if (/^(?:https?:|mailto:|tel:|#)/.test(target)) continue;
    const localPath = target.split("#", 1)[0];
    if (!localPath || extname(localPath) === "") continue;
    assert.ok(existsSync(join(root, localPath)), `${page}: broken link ${target}`);
  }
}

console.log(`Verified ${pages.length} static pages.`);
```

- [ ] **Step 2: Run the verifier to prove the site is absent**

Run: `node website/tests/verify-site.mjs`

Expected: FAIL with `missing index.html` or an equivalent missing-page assertion.

- [ ] **Step 3: Create the shared CSS system**

Create `website/assets/styles.css` with:

- CSS custom properties for every exact palette value in Global Constraints
- fluid type scales using `clamp()` for display, title, body, caption, and mono labels
- system sans, editorial serif fallback, and system monospace stacks
- skip link, visible focus ring, shared header/nav/mobile-menu/footer styles
- editorial containers, split sections, 24px cards, 14px inner blocks, hairlines, and responsive grids
- reusable AI-fragment, memory-card, status-line, voice-wave, and canvas-stage classes
- breakpoints for 760px and 1040px
- `@media (prefers-reduced-motion: reduce)` rules that remove smooth scrolling, transitions, and keyframe animation
- print-safe defaults and no remote asset references

The first declarations must be:

```css
:root {
  --paper: #f8f7f2;
  --ink: #10100e;
  --muted: #74766f;
  --card: #ecebe4;
  --surface: #ffffff;
  --hairline: #d9d9d9;
  --divider: #ccccca;
  --radius-card: 24px;
  --radius-field: 14px;
  --page-gutter: clamp(20px, 4vw, 64px);
  --content-max: 1280px;
}
```

- [ ] **Step 4: Create the shared progressive-enhancement script**

Create `website/assets/site.js` as a non-module deferred script. It must expose:

```js
window.IOSite = Object.freeze({
  reduceMotion: window.matchMedia("(prefers-reduced-motion: reduce)").matches,
  setupMenu,
  setupReveal,
  setupLocalTime,
});
```

`setupMenu()` toggles `aria-expanded`, closes on Escape, closes after a nav link activation, and returns focus to the menu button. `setupReveal()` adds `.is-visible` through `IntersectionObserver` and immediately reveals content when reduced motion is enabled or the observer is unavailable. `setupLocalTime()` writes a visitor-local daypart into elements marked `[data-local-time]` without hiding their fallback text.

After defining the frozen interface, the script invokes all three setup functions on `DOMContentLoaded` (or immediately when the document is already ready), so pages need no inline initialization code.

- [ ] **Step 5: Create the Home page shell and full editorial content**

Create `website/index.html` with:

- English document language, viewport, unique title and description
- shared stylesheet and deferred shared scripts
- skip link, semantic header/nav/main/footer, and accessible mobile-menu button
- one H1: `A relationship that remembers.`
- supporting line: `IO gives your personal agent a body, a memory, and a place beside you.`
- sections headed `It notices.`, `It remembers.`, `You make things together.`, and `You remain in control.`
- a decorative Canvas element with an adjacent text equivalent
- quiet first-person fragments including `I kept the little things.`
- closing line `I’ll remember where we left off.`
- links from each narrative section to its matching interior page
- no CTA banner

- [ ] **Step 6: Run the verifier and confirm only later pages are missing**

Run: `node website/tests/verify-site.mjs`

Expected: FAIL with `missing presence.html`; it must not fail on the Home page rules already implemented.

- [ ] **Step 7: Commit the site foundation**

```bash
git add website/index.html website/assets/styles.css website/assets/site.js website/tests/verify-site.mjs
git commit -m "feat(website): establish IO editorial foundation"
```

---

### Task 2: Presence Page and Point-Cloud Organism

**Files:**
- Create: `website/presence.html`
- Create: `website/assets/presence.js`
- Modify: `website/index.html`
- Modify: `website/tests/verify-site.mjs`

**Interfaces:**
- Consumes: `.presence-canvas`, `.voice-wave`, `.reveal`, and `window.IOSite.reduceMotion` from Task 1
- Produces: `window.IOPresence.mount(canvas)` and the reusable `[data-io-presence]` Canvas behavior used by Home and Presence

- [ ] **Step 1: Add Presence-specific failing assertions**

Extend `website/tests/verify-site.mjs` after the page loop:

```js
const presence = readFileSync(join(root, "presence.html"), "utf8");
for (const phrase of ["Chat", "Voice Call", "Dynamic Island", "Live Activity", "permission"]) {
  assert.ok(presence.includes(phrase), `presence.html: missing ${phrase}`);
}
assert.ok(presence.includes("data-io-presence"), "presence.html: missing point-cloud canvas");

const presenceScript = readFileSync(join(root, "assets/presence.js"), "utf8");
assert.ok(presenceScript.includes("prefers-reduced-motion"), "presence.js: reduced motion not handled");
assert.ok(presenceScript.includes("devicePixelRatio"), "presence.js: high-DPI canvas not handled");
```

- [ ] **Step 2: Run the verifier to prove Presence is missing**

Run: `node website/tests/verify-site.mjs`

Expected: FAIL with `missing presence.html`.

- [ ] **Step 3: Build the Presence page narrative**

Create `website/presence.html` with one H1, `Not always speaking. Still here.`, and a time-of-day editorial sequence:

- Morning: proactive greeting and Dynamic Island / Live Activity presence
- Day: Chat and only user-authorized screen context
- Evening: Voice Call, listening, and a concise call-memory handoff
- Rest: a quiet state showing that presence does not mean constant interruption

Include exact plain-language boundary copy: `IO only perceives what you choose to share.`

Use actual capability names without implementation details. Add a decorative `<canvas data-io-presence aria-hidden="true">` and a visible text description beside it.

- [ ] **Step 4: Implement the point-cloud organism**

Create `website/assets/presence.js` as an IIFE that:

- finds all `[data-io-presence]` canvases
- renders 90–130 monochrome points distributed on a sphere using deterministic seeded coordinates
- sizes the backing buffer with `devicePixelRatio`
- uses slow breathing and rotation, plus bounded pointer influence
- stops its animation loop while the document is hidden
- renders one stable frame when `prefers-reduced-motion: reduce` matches
- removes pointer listeners when a canvas leaves the document
- exposes `window.IOPresence = Object.freeze({ mount })`

Use Canvas 2D only. Point colors stay within ink/muted values and opacity; do not add chromatic gradients.

- [ ] **Step 5: Activate the shared organism on Home**

Modify `website/index.html` to load `assets/presence.js` with `defer`, mark the hero Canvas with `data-io-presence`, and retain the adjacent textual equivalent.

- [ ] **Step 6: Run the verifier**

Run: `node website/tests/verify-site.mjs`

Expected: FAIL with `missing memory.html`, proving Presence and its script contract pass.

- [ ] **Step 7: Commit Presence**

```bash
git add website/presence.html website/index.html website/assets/presence.js website/tests/verify-site.mjs
git commit -m "feat(website): give IO a persistent presence"
```

---

### Task 3: Memory, Boundaries, and Philosophy Pages

**Files:**
- Create: `website/memory.html`
- Create: `website/boundaries.html`
- Create: `website/philosophy.html`
- Modify: `website/tests/verify-site.mjs`

**Interfaces:**
- Consumes: shared navigation, editorial sections, memory-card components, AI fragments, and reveal behavior from Task 1
- Produces: all remaining public narrative pages and complete cross-page navigation

- [ ] **Step 1: Add failing page-content contracts**

Extend `website/tests/verify-site.mjs`:

```js
const contentContracts = new Map([
  ["memory.html", ["Memory Garden", "Identity", "World Book", "Canvas", "change", "remove"]],
  ["boundaries.html", ["permission", "Memories", "notifications", "what IO can see", "what IO can remember"]],
  ["philosophy.html", ["A relationship needs continuity.", "An agent needs somewhere to exist.", "Closeness should never require surrendering control.", "IO is a place for a relationship to continue."]],
]);
for (const [page, phrases] of contentContracts) {
  const html = readFileSync(join(root, page), "utf8");
  for (const phrase of phrases) assert.ok(html.includes(phrase), `${page}: missing ${phrase}`);
}
```

- [ ] **Step 2: Run the verifier to prove the three pages are missing**

Run: `node website/tests/verify-site.mjs`

Expected: FAIL with `missing memory.html`.

- [ ] **Step 3: Build the Memory page**

Create `website/memory.html` with H1 `Continuity changes everything.` and four editorial movements:

- Memory Garden as shared moments that can be revisited
- Identity as the agreed shape and voice of the agent
- World Book as durable people, places, and shared-world context
- Canvas as artifacts made together, not only messages exchanged

Use a vertical memory timeline and restrained leaf-curve CSS motif. Include visible copy that memories can be viewed, changed, and removed. Do not describe storage or retrieval architecture.

- [ ] **Step 4: Build the Boundaries page**

Create `website/boundaries.html` with H1 `Closeness needs consent.` and four user-control sections:

- explicit permission before perception
- visible and editable memories
- proactive message and notification controls
- user choice over what IO can see and remember

Use stable layouts, little motion, and direct wording. Do not include infrastructure, encryption implementation, audit, server, or developer terminology.

- [ ] **Step 5: Build the Philosophy page**

Create `website/philosophy.html` with H1 `Not human. Not disposable.` and three numbered statements matching the spec exactly. Include a paragraph that clearly rejects replacing human relationships. End with `IO is a place for a relationship to continue.` and no conversion action.

- [ ] **Step 6: Run the verifier**

Run: `node website/tests/verify-site.mjs`

Expected: FAIL with `missing 404.html`; all five public page contracts must pass first.

- [ ] **Step 7: Commit the complete narrative**

```bash
git add website/memory.html website/boundaries.html website/philosophy.html website/tests/verify-site.mjs
git commit -m "feat(website): complete IO relationship narrative"
```

---

### Task 4: Static-Hosting Assets, Metadata, and Error Page

**Files:**
- Create: `website/404.html`
- Create: `website/robots.txt`
- Create: `website/sitemap.xml`
- Create: `website/assets/favicon.png`
- Create: `website/assets/og-image.png`
- Modify: `website/index.html`
- Modify: `website/presence.html`
- Modify: `website/memory.html`
- Modify: `website/boundaries.html`
- Modify: `website/philosophy.html`
- Modify: `website/tests/verify-site.mjs`

**Interfaces:**
- Consumes: finished public page titles, descriptions, palette, point-cloud motif, and app icon identity
- Produces: deployable crawler metadata, per-page social metadata, favicon, social card, and final 404 handling

- [ ] **Step 1: Add failing deployment-asset assertions**

Extend `website/tests/verify-site.mjs`:

```js
for (const asset of ["robots.txt", "sitemap.xml", "assets/favicon.png", "assets/og-image.png"]) {
  assert.ok(existsSync(join(root, asset)), `missing ${asset}`);
}
for (const page of publicPages) {
  const html = readFileSync(join(root, page), "utf8");
  assert.match(html, /property="og:title"/i, `${page}: missing og:title`);
  assert.match(html, /property="og:description"/i, `${page}: missing og:description`);
  assert.match(html, /property="og:image"/i, `${page}: missing og:image`);
  assert.match(html, /name="twitter:card"\s+content="summary_large_image"/i, `${page}: missing X card`);
  assert.match(html, /rel="icon"\s+href="assets\/favicon\.png"/i, `${page}: missing favicon`);
}
```

- [ ] **Step 2: Run the verifier to prove deployment assets are missing**

Run: `node website/tests/verify-site.mjs`

Expected: FAIL with `missing 404.html` or `missing robots.txt`.

- [ ] **Step 3: Create the 404 page**

Create `website/404.html` in the same visual system with one H1, `We lost the thread.`, a short line from IO, and one ordinary text link back to `index.html`. Do not add a conversion action.

- [ ] **Step 4: Create crawler files**

Create `website/robots.txt` that permits crawling and points to `/sitemap.xml`. Create `website/sitemap.xml` with the five public routes using the intended canonical origin `https://io.feedling.app/`; do not include `404.html`.

- [ ] **Step 5: Create and inspect the brand assets**

Derive `website/assets/favicon.png` from the actual IO app icon in the iOS repository, preserving the warm-paper background and near-black pixel body. Generate one cohesive 1200×630 `website/assets/og-image.png` after the final Home composition is stable. The card must use the exact headline `A relationship that remembers.`, the shipped paper/ink palette, and the point-cloud or pixel-body motif; inspect it for incorrect or invented text before use.

- [ ] **Step 6: Add complete metadata to every public page**

Add unique Open Graph title/description values, `og:type=website`, `og:image=/assets/og-image.png`, `twitter:card=summary_large_image`, favicon metadata, and JSON-LD `WebSite` data to Home. Do not invent price, availability, address, social account, or organization claims.

- [ ] **Step 7: Run the verifier to green**

Run: `node website/tests/verify-site.mjs`

Expected: PASS with `Verified 6 static pages.`

- [ ] **Step 8: Commit hosting assets and metadata**

```bash
git add website/404.html website/robots.txt website/sitemap.xml website/assets/favicon.png website/assets/og-image.png website/*.html website/tests/verify-site.mjs
git commit -m "feat(website): add static hosting metadata"
```

---

### Task 5: Accessibility, Responsive QA, and Delivery Verification

**Files:**
- Modify: `website/assets/styles.css`
- Modify: `website/assets/site.js`
- Modify: `website/assets/presence.js`
- Modify: `website/*.html`
- Modify: `website/tests/verify-site.mjs`
- Create: `website/README.md`

**Interfaces:**
- Consumes: the complete static website from Tasks 1–4
- Produces: verified responsive behavior, accessible interactions, reduced-motion behavior, and deployment instructions

- [ ] **Step 1: Add final accessibility source checks**

Extend `website/tests/verify-site.mjs` so every public page asserts a skip link, labelled menu button, `aria-current="page"` on the active navigation item, and no positive `tabindex`. Assert that every Canvas is either `aria-hidden="true"` or has a non-empty accessible label.

```js
for (const page of publicPages) {
  const html = readFileSync(join(root, page), "utf8");
  assert.match(html, /class="skip-link"\s+href="#main"/i, `${page}: missing skip link`);
  assert.match(html, /button[^>]+aria-label="Open navigation"/i, `${page}: menu button unlabeled`);
  assert.match(html, /aria-current="page"/i, `${page}: current nav item missing`);
  assert.doesNotMatch(html, /tabindex="[1-9]/i, `${page}: positive tabindex forbidden`);
  for (const canvas of html.matchAll(/<canvas\b[^>]*>/gi)) {
    assert.match(canvas[0], /aria-hidden="true"|aria-label="[^"]+"/i, `${page}: canvas needs accessibility treatment`);
  }
}
```

- [ ] **Step 2: Run the verifier and observe any accessibility failures**

Run: `node website/tests/verify-site.mjs`

Expected: FAIL on any missing skip link, menu label, current-page state, or Canvas treatment introduced earlier.

- [ ] **Step 3: Fix source-level accessibility failures**

Update the exact failing pages and shared scripts. Preserve a visible focus ring, 44×44 minimum menu targets, Escape handling, focus return, semantic landmarks, one H1 per page, logical heading order, and text equivalents for decorative visualizations.

- [ ] **Step 4: Create local preview and deployment instructions**

Create `website/README.md` with:

```markdown
# IO website

This directory is a dependency-free static website.

## Preview

From the repository root:

    python3 -m http.server 8080 --directory website

Open `http://localhost:8080/`.

## Verify

    node website/tests/verify-site.mjs

## Deploy

Publish the contents of `website/` as the static site root. No build command or output directory is required.
```

- [ ] **Step 5: Run automated verification**

Run: `node website/tests/verify-site.mjs`

Expected: PASS with `Verified 6 static pages.`

Run: `git diff --check -- website docs/superpowers/specs/2026-08-24-io-marketing-website-design.md docs/superpowers/plans/2026-08-24-io-marketing-website.md`

Expected: no output and exit code 0.

- [ ] **Step 6: Perform visual and interaction QA**

Serve `website/` locally and check:

- 375×812: no horizontal overflow; mobile menu opens, closes, and keeps visible focus
- 768×1024: editorial grids collapse without orphaned headings
- 1440×900: hero, point cloud, and section rhythm match the approved paper/ink direction
- keyboard-only: skip link, menu, navigation, and all links work in order
- reduced motion: point cloud becomes a stable frame and reveal content is immediately visible
- JavaScript disabled: every page remains readable and cross-page navigation works
- Boundaries: no backend architecture or implementation details appear

- [ ] **Step 7: Confirm the working tree contains only intended website work**

Run: `git status --short`

Expected: only intended `website/` changes and any pre-existing unrelated user changes; never stage unrelated files.

- [ ] **Step 8: Commit delivery documentation and QA fixes**

```bash
git add website
git commit -m "test(website): verify static IO experience"
```
