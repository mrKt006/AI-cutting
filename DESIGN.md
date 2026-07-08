# Design

## Identity

AI-cutting should feel like a focused creator workbench: closer to a lightweight video editing tool than a web admin app. The visual system is restrained and practical, with a dark editing workspace for preview-heavy pages and a quieter light workspace for task setup.

Reference mix:
- Jianying/CapCut for preview-first editing structure and familiar property panels.
- Figma inspector for compact, precise controls.
- Local desktop utilities for clarity and speed.

Avoid copying Jianying/CapCut visual details directly.

## Color

Use a neutral tool palette with one calm blue production accent.

- App chrome: near-black navy for top bars and editing workspaces.
- Work surface: cool blue-gray for task setup and job pages.
- Panels: subtle blue-gray surfaces with low-contrast borders.
- Accent: refined cobalt/azure blue for primary actions, selected presets, focus, and active controls.
- Status colors: green for complete, blue for processing, red for failed, amber for waiting.

Do not use generic SaaS default blue buttons on white cards. The blue should feel like editing software: calm, cool, and precise. Do not use large gradients, glass, decorative orbs, or beige/cream paper backgrounds.

## Typography

Use the system Chinese UI stack:

`"Microsoft YaHei", "Segoe UI", system-ui, -apple-system, BlinkMacSystemFont, Arial, sans-serif`

Use compact product typography:
- Page title: 22-26px, strong but not hero-sized.
- Section titles: 14-16px.
- Labels: 12-13px, semibold.
- Body/help text: 13px with clear contrast.
- Data/status text: 12-13px.

Avoid display typography, oversized headings, and marketing-style copy.

## Layout

The product has two main layout modes:

1. Task workbench:
   - Left/main: upload, task configuration, and primary action.
   - Right/secondary: recent task status, limited to a few items.
   - The primary action should be visible in a 1280x720 desktop viewport whenever possible.

2. Style editor:
   - Left: style presets.
   - Center: preview stage as the dominant area.
   - Right: parameter inspector.
   - The preview must preserve the selected aspect ratio and must not crop text in misleading ways.

Use panels only when they serve workspace boundaries. Avoid nested cards and repeated card grids.

## Components

Buttons:
- Primary: blue accent, used for start/save/create only.
- Secondary: neutral button, used for navigation and low-risk actions.
- Destructive: red tint, used only for delete.

Inputs:
- Compact height.
- Clear labels.
- Visible focus states.
- Native controls are acceptable when polished, but raw file inputs should be visually hidden inside upload/drop areas.

Status:
- Always display Chinese status text.
- Do not rely on color alone.
- Old failed tasks should not dominate the home page.

Preview:
- The preview stage should feel like a canvas/work area.
- Uploaded preview frames are preferred; fallback backgrounds should be plain and unobtrusive.

## Motion

Use minimal state motion only: hover, focus, selected, loading. Keep transitions short, around 120-180ms. Avoid page-load choreography. Respect `prefers-reduced-motion`.

## Responsive Behavior

Desktop is the primary experience. At narrow widths:
- Stack sections vertically.
- Keep upload and task creation usable.
- Preserve preview aspect ratio.
- Avoid horizontal scrolling.

Mobile is acceptable for quick checks and small edits, not for complex preset creation.
