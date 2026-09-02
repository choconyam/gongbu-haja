# Output and Layout

Choose the lightest format that satisfies the user's purpose. A note meant for quick editing does not need a typeset PDF; a printable study handout should not stop at raw Markdown.

## Format Routing

### Markdown

Use for chat delivery, iterative drafting, searchable personal notes, or source content that does not require fixed pagination.

- Use a clear heading hierarchy.
- Keep equations in supported math syntax and never expose malformed raw markup.
- Use tables for stable comparisons, not for long prose.
- Keep source/page tags compact and consistent when traceability matters.

### Word-compatible document

Use for collaborative editing, comments, tracked revisions, or a user request for `.docx`.

- Use real heading styles, captions, lists, tables, page breaks, and cross-references.
- Generate a table of contents when the document is long enough to benefit.
- Render and inspect the document before delivery.

### PDF or LaTeX

Use for a polished printable handout, equation-heavy course, stable pagination, or an established TeX series.

- Prefer XeLaTeX for Korean documents, with LuaLaTeX as a reasonable fallback.
- Compile enough times to resolve the table of contents and references.
- Preserve editable source when useful.
- Render pages to images and inspect the result; compilation success is not enough.

## Style Precedence

1. User-specified template or exact requirements.
2. Visual system of a user-designated existing series.
3. One of the profiles below.

Do not blend profiles within one artifact. If the source series is inconsistent, choose one coherent system and state the choice only if it matters to the handoff.

## Adaptive Profile — Default

Use when no template or legacy series is designated.

- A4, 11 pt, approximately 20–22 mm margins.
- Korean body: a readable serif; headings and labels: a compatible sans serif; code: monospace.
- White background, one restrained accent color, pale gray support surfaces.
- Calm paragraph-led layout with selective callouts.
- Title page only for a substantial handout; otherwise begin with title, scope, and learning goals.
- Add contents when navigation benefits.
- Use page or source images only when the learner needs to inspect the visual itself.
- Finish with an appropriate synthesis: formula table, timeline, concept map, comparison table, or key takeaways.

## Classic Red Profile — Legacy Compatibility

Use when matching the consolidated AI-note series or when the user requests the red profile.

- A4 `article`, 11 pt.
- Margins: left/right 22 mm, top 22 mm, bottom 28 mm.
- Body: `Noto Serif CJK KR`; headings: `Noto Sans CJK KR`; code: `Noto Sans Mono CJK KR` or nearest installed equivalents.
- Line spacing about 1.21; first-line indent 1.2 em; paragraph spacing about 0.35 em.
- Accent `#A61E2D`; soft gray `#F7F7F7`; line gray `#D9D9D9`; deep gray `#444444`.
- For slide-by-slide explanation notes: title page, `Contents`, source-page-numbered sections, source image near `0.79\textwidth`, caption `원본 PDF p.[number]`, and a final synthesis.
- Use pale-gray breakable callouts with the red accent for key intuition, exam points, source corrections, and uncertainty.

Do not force page-numbered sections or source images when the request is not slide-based.

## Technical Blue Profile — Legacy Compatibility

Use when matching the consolidated acoustic-engineering series or when the user requests the blue profile.

- A4 `article`, 11 pt, approximately 20 mm margins.
- Line spacing about 1.22, header height about 16 pt, footer separation about 14 mm.
- Same serif/sans/monospace role split as the adaptive profile.
- Accent `#1F4E79`; light accent `#EAF2F8`; soft gray `#F6F7F9`; warning `#A64B00`; positive `#0B6E4F`.
- Use distinct key, warning, and exam-point boxes without filling large portions of the page with strong color.
- For slide-led notes, begin major sections on new pages unless a short connector section and explicit compactness goal justify otherwise.

## Slide-Aware Structure

Use this only when page-level traceability is part of the goal.

- Identify cover, schedule, administration, recap, transition, and substantive pages.
- Exclude routine preliminary pages from major note sections by default, but preserve context needed to understand the lesson.
- Keep the original page number as the section identifier when sections map to pages.
- Place the source page image before its explanation when visual inspection helps.
- Refer to actual elements in the image: top equation, right graph, lower-left note, and so on.
- Do not merge different source pages into one page-numbered section unless the user requests conceptual consolidation.

### Slide-First Explanation — Default for lecture handouts

When a lecture slide PDF and instructor explanation are both available, use this student-facing order unless the user asks for a concept-consolidated note:

1. place one original slide page image at the top of the note page;
2. put the explanation for that slide immediately below it;
3. continue overflow explanation before showing the next slide;
4. then repeat with the next original slide page.

Keep administrative slides brief, but include every original page when the user asks to retain the handout. Do not print internal traceability strings such as `(PDF 15쪽; 강의 00:19:58-00:20:46)` in the student-facing artifact unless explicitly requested. Page and timestamp mappings remain in the internal source map.

## Callouts

Use callouts sparingly and by function:

- key intuition;
- worked miniature example;
- exam point;
- common mistake;
- source correction or notation conflict;
- instructor explanation;
- uncertainty or external verification.

A warning callout should state the judgment first, then the evidence, then the reading adopted in the note.

## Final Synthesis

Match the subject:

- quantitative: `topic / key equation / meaning or use`;
- history: `period or event / cause / development / consequence`;
- concepts: `term / definition / distinction / example`;
- code: `component / input-output / role / failure mode`;
- problem solving: `problem cue / method / checks / common error`.

End with a concise integrative statement or retrieval prompt when helpful. Do not force a formula summary onto a non-formula course.

## Visual QA

Inspect rendered output for:

- missing glyphs and fallback fonts;
- cropped equations or images;
- overfull or underfull pages that harm reading;
- stranded headings, captions, and callouts;
- table widths and repeated headers;
- consistent source-page labels and numbering;
- sufficient contrast and restrained accent use;
- a useful balance of source images, prose, equations, and whitespace.
