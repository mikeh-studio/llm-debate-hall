# Design System — Multi-Agent Council

## Product Context
- **What this is:** A local-first web app where multiple AI agents deliberate as philosopher personas in a chamber-style setup
- **Who it's for:** Developers experimenting with multi-agent systems
- **Space/industry:** AI tooling, multi-agent systems, developer playgrounds
- **Project type:** Web app (FastAPI + vanilla HTML/CSS/JS)

## Aesthetic Direction
- **Direction:** Enlightenment Chamber — a civic amphitheater where arguments have weight
- **Decoration level:** Intentional — paper grain texture, thin brass rules between sections, no glassmorphism. Surfaces feel printed, not floating.
- **Mood:** Warm paper, smoked brass, and the energy of a public argument about to begin. "Debate matters here" not "cozy reading nook."
- **Reference sites:** LMSYS Chatbot Arena (lmarena.ai), Vercel AI Playground (play.vercel.ai), Val Town (val.town) — studied as counter-examples to differentiate against

## Typography
- **Display/Hero:** Cormorant Garamond — high-drama serif with philosophical weight
- **Body:** Source Serif 4 — readable long-form serif for debate transcripts, maintains warmth
- **UI/Labels:** DM Sans — clean geometric sans for buttons, badges, metadata (the machine layer)
- **Data/Tables:** DM Sans with tabular-nums for numeric alignment
- **Code:** JetBrains Mono — for model names, command overrides, technical config
- **Loading:** Google Fonts CDN
  ```html
  <link href="https://fonts.googleapis.com/css2?family=Cormorant+Garamond:ital,wght@0,400;0,600;0,700;1,400&family=Source+Serif+4:ital,opsz,wght@0,8..60,400;0,8..60,600;0,8..60,700;1,8..60,400&family=DM+Sans:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet" />
  ```
- **Scale:**
  - Hero: clamp(3rem, 6vw, 4.5rem)
  - Section title: 2.2rem
  - Card heading: 1.4rem
  - Body: 17px (1.05rem)
  - UI text: 0.95rem
  - Labels/eyebrows: 11-12px uppercase, 0.15-0.2em tracking
  - Code: 0.88rem

## Color
- **Approach:** Restrained — one accent family plus one dramatic verdict color
- **Light mode:**
  ```css
  :root {
    --bg: #ede5d8;           /* warm vellum */
    --surface: #f7f1e6;      /* lighter parchment */
    --surface-deep: #d9c9ad; /* aged paper, judge backgrounds */
    --ink: #1f1a14;          /* rich brown-black, primary text */
    --muted: #6e5c50;        /* warm gray, secondary text */
    --accent: #9f5a1a;       /* oxidized brass, links, eyebrows */
    --accent-dark: #6b3c12;  /* deep brass, hover states */
    --verdict: #8b2e20;      /* tribunal red, judge verdicts ONLY */
    --rule: #b89a73;         /* brass divider lines */
    --shadow: 0 12px 40px rgba(31, 26, 20, 0.10);
    --shadow-lg: 0 24px 60px rgba(31, 26, 20, 0.14);
  }
  ```
- **Dark mode:**
  ```css
  [data-theme="dark"] {
    --bg: #1a1510;
    --surface: #252019;
    --surface-deep: #312a20;
    --ink: #e8dfd2;
    --muted: #9a8b7a;
    --accent: #c8792a;
    --accent-dark: #e8943a;
    --verdict: #c44a3a;
    --rule: #4a3d2e;
    --shadow: 0 12px 40px rgba(0, 0, 0, 0.3);
    --shadow-lg: 0 24px 60px rgba(0, 0, 0, 0.4);
  }
  ```
- **Semantic:** success #3e7832, warning var(--accent), error var(--verdict), info #2d5064
- **Dark mode strategy:** Darken surfaces to warm near-blacks, boost accent saturation 10-15%, maintain brass/amber identity

## Spacing
- **Base unit:** 8px
- **Density:** Comfortable
- **Scale:** 2xs(2) xs(4) sm(8) md(16) lg(24) xl(32) 2xl(48) 3xl(64)

## Layout
- **Approach:** Creative-editorial — the first viewport reads like a debate broadside, not a dashboard
- **Grid:** Main content + 360px sidebar at desktop (>1080px), single column mobile
- **Max content width:** 1120px
- **Border radius:** Hierarchical, tightened for authority
  - Cards/panels: 16px (`--radius-card`)
  - Inputs: 12px (`--radius-input`)
  - Buttons/pills: 8px (`--radius-btn`)
  - Judge verdict blocks: 4px (`--radius-verdict`) — sharp, formal

## Motion
- **Approach:** Minimal-functional — only transitions that aid comprehension. The content is dramatic enough.
- **Easing:** enter(ease-out) exit(ease-in) move(ease-in-out)
- **Duration:** micro(50-100ms) short(150-250ms) medium(250-400ms)
- **Principles:** No bounce, no choreography. State transitions only (hover, focus, show/hide).

## Key Design Decisions
- **Debate entries are "orator placards," not chat bubbles.** Each debater gets a named panel with family, provider, and speaking order. The app looks like a chamber of positions, not a messaging app.
- **The judge is architecturally distinct.** Darker surface (--surface-deep), sharper border radius (4px), verdict scores in tribunal red. When the judge speaks, you feel it.
- **No glassmorphism.** Surfaces feel tactile and printed — paper grain, brass rules, ink contrast. Not frosted glass.
- **Serif body text for transcripts.** These are arguments worth reading carefully, not chat messages to skim.
- **Tribunal red is reserved.** --verdict only appears on judge verdicts and final scores. Discipline required — overuse kills the drama.

## Decisions Log
| Date | Decision | Rationale |
|------|----------|-----------|
| 2026-03-24 | Initial design system created | Created by /design-consultation with competitive research (LMSYS Arena, Vercel AI, Val Town) and outside design voices (Codex + Claude subagent). All three voices agreed on pushing the theatrical metaphor harder — tighter radius, serif display, orator placards over chat bubbles, judge as architectural presence. |
| 2026-03-24 | Tribunal red (--verdict) added | Deliberate risk: a single dramatic color reserved for judge moments. Both outside voices recommended stronger judge differentiation. |
| 2026-03-24 | Border radius tightened 28px→16px/8px/4px | Trades friendliness for authority. Hierarchical scale gives cards warmth while verdict blocks feel formal. |
