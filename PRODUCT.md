# Product

## Register

product

## Users

AI-cutting is for short-form creators, solo operators, and small self-media teams who record talking-head videos and need to turn raw footage into usable clips quickly. The primary user may not be technical; they want a workflow that feels familiar from tools like Jianying/CapCut, but simpler and more focused.

The first release is still local and single-user, but the product should not paint itself into a corner. Future public deployment needs clearer trust, safer credential handling, multi-user separation, and task visibility.

## Product Purpose

AI-cutting helps users upload raw talking-head videos, remove pauses and obvious delivery issues, generate accurate subtitles from ASR, apply reusable subtitle/cover presets, and download finished videos with covers. Success means a creator can go from raw footage to a presentable video without opening a full editing timeline.

The core job on any screen is practical: prepare a video task, tune text appearance, check the preview, start processing, and retrieve output files.

## Brand Personality

Focused, capable, calm.

The interface should feel like a compact creator tool rather than a business admin panel. It can borrow the spatial logic of Jianying/CapCut: media/preview first, property controls nearby, and direct manipulation when useful. It should also borrow Figma-like restraint for controls: clear labels, precise spacing, and predictable inspector panels.

## Anti-references

- Do not look like a generic admin dashboard: no big white form slabs, default blue buttons, or card piles pretending to be product design.
- Do not look AI-generated: no fake premium gradients, decorative blobs, glass panels, ornamental icons, or marketing hero sections.
- Do not become a Jianying clone: use familiar editing patterns, but avoid copying its exact chrome, colors, control treatment, or interaction density.
- Do not make settings feel like a developer console. Non-technical creators should understand what matters without reading logs.

## Design Principles

1. Preview is the source of truth. If the user is adjusting subtitles or cover text, the visual result should be the dominant element.
2. Hide complexity until it earns attention. Common settings stay visible; advanced effects, export artifacts, and diagnostics stay secondary.
3. Use creator language, not backend language. Prefer "处理中" over "running", "成片" over "final output", and inline explanations over technical jargon.
4. Make the local MVP feel trustworthy enough for others. Status, errors, saved credentials, and output locations should be clear without feeling alarming.
5. Familiar, not copied. Editing-tool conventions are welcome, but the product should have its own restrained, practical identity.

## Accessibility & Inclusion

Target WCAG AA contrast for text and controls. Avoid color-only status communication; pair color with text. Keep keyboard focus visible. Avoid decorative motion and provide reduced-motion fallbacks for any transitions. Mobile layouts should remain usable for checking status and small adjustments, but desktop remains the primary editing experience.
