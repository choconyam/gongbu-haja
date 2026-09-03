# Content Modes

Select only the sections relevant to the current material. Mixed courses may combine modes within one note.

These subject/content modes are separate from the user-selected production mode in `note-production-modes.md`. Apply the relevant subject guidance within the limits of `faithful` or `deep`; for example, `faithful` verifies a printed derivation without adding a new one, while `deep` may restore verified intermediate steps.

## Mode Selection Guide

Route by what the sources actually contain, not by course title or file extension. Apply every mode that matches; most real courses need two or more.

| Signal in the sources | Mode to apply |
|---|---|
| Equations, derivations, units, graphs, experimental data | STEM and Quantitative |
| Source code, notebooks, lab procedures, environment setup | Code, Notebook, and Lab |
| Chronology, actors, institutions, arguments, primary-source passages | Humanities and Social Sciences |
| Dense new terminology, translation-sensitive terms, language learning | Terminology-Heavy or Language |
| Numbered problems, worked solutions, exam questions | Problem Sets and Worked Solutions |
| Lecture recording or transcript present | Lecture Transcripts layer, combined with the modes above |
| An existing note to revise or audit | Existing Notes |

Misrouting decisions propagate downstream: treating an economics lecture as humanities-only skips formula verification, and treating a history lecture as terminology-only flattens causal narrative into word lists. When a source shows signals from several rows, record the combination in the source map so later roles inherit the routing.

### Demonstration, studio, and seminar material

- For screen demonstrations, performances, or clinical/skill practice, the recording shows actions the transcript cannot carry. Mark such spans as `[시연 HH:MM:SS 대상]` with the demonstrated object, keep the timestamp so a reviewer can re-watch, and describe in the note what was demonstrated and what the learner should be able to reproduce.
- For seminar or discussion sessions with multiple speakers, extend functional speaker labels (`발표자 1`, `발표자 2`, `사회자`) without guessing identity, and preserve who claimed what when positions conflict.

## STEM and Quantitative Material

For each important equation or model:

- explain why it is introduced;
- define every new symbol at first use, including units, direction, sign, domain, and index range when relevant;
- show the starting relation, transformations, and result when the derivation matters;
- name the rule used at non-obvious transitions: substitution, chain rule, logarithm, Euler relation, coordinate change, approximation, conservation law, or boundary condition;
- state assumptions and what effects they retain or discard;
- interpret how changing each important variable changes the result;
- check dimensions, signs, scale, limiting cases, and a small numerical example when useful;
- end with how to recognize when the formula applies and how to express its meaning in words.

Do not manufacture a derivation if the source is ambiguous. Give a verified standard derivation only when it supports the taught result and mark it as supplemental.

### Graphs, diagrams, and experiments

- Identify axes, units, scale type, curves, regions, arrows, blocks, and measurement conditions.
- Tell the learner where to look and what comparison reveals the conclusion.
- For a block diagram, follow information or energy from input to output and state what each block preserves, discards, or transforms.
- For an experiment, separate observation, interpretation, and limitation.
- Connect the visual to the next equation, decision, or application.

## Code, Notebook, and Lab Material

- Explain purpose before syntax.
- Map each important code block to inputs, outputs, shapes or types, state changes, and failure modes.
- Classify each source-code `TODO` before carrying it into the note: a graded implementation task or learning objective, an intentionally incomplete example, a template placeholder, or irrelevant source residue.
- Preserve a `TODO` only when completing or recognizing it is part of the learning goal. Present it as a deliberate practice task or quote it inside a code block; do not leave a raw `TODO` in ordinary prose as if the note itself were unfinished.
- Verify relevant TODO solutions, functions, tensor shapes, dimensions, parameter counts, loss, optimizer, learning rate, and reported outputs against the actual code.
- Run a minimal check when safe and useful; do not claim unexecuted output as observed.
- Separate what the code does from why the model, algorithm, or experimental step is designed that way.
- Preserve environment or dependency constraints that affect reproducibility.

For lab notes, include apparatus or setup, variables, procedure logic, observation, calculation, uncertainty, and conclusion when present in the sources.

## Humanities and Social Sciences

Organize material around chronology, causality, comparison, argument, and evidence rather than forcing equation-oriented headings.

For each major topic:

- establish time, place, actors, and relevant terms;
- distinguish trigger, structural cause, immediate event, development, and consequence;
- separate a source's claim from the note writer's synthesis;
- explain why an example or primary-source passage matters;
- compare similar concepts using a stable axis such as period, institution, ideology, mechanism, or outcome;
- identify contested interpretations or uncertain evidence without presenting one as settled fact;
- add recall hooks such as timelines, cause-effect chains, contrast tables, or thesis-evidence pairs.

Avoid turning the note into disconnected names and dates. Dates should support a story of change or causation.

## Terminology-Heavy or Language Material

- Give a plain-language meaning, precise definition, representative example, and non-example.
- Show confusing neighbors in a contrast table.
- Preserve original-language terms when they are tested or when translation loses precision.
- Add pronunciation, morphology, or usage notes only when relevant to the learning goal.

## Problem Sets and Worked Solutions

Keep problem identity and subproblem order traceable.

Use this reasoning pattern when appropriate:

1. what is given and what is asked;
2. the cue that selects a method;
3. assumptions and unit normalization;
4. symbolic setup before substitution;
5. calculation with intermediate steps;
6. result with units and sign;
7. plausibility check;
8. reusable lesson and common wrong turn.

Distinguish an official solution, instructor supplement, and newly generated explanation. Never present an inferred answer as official.

## Lecture Transcripts and Instructor Voice

Treat transcripts as a core interpretation layer when they accompany slides or handouts.

Before integration, route the material by source state:

- recording only: transcribe, validate the transcript package, then audit against audio and align to the handout;
- transcript only: preserve it as transcript-only and do not claim audio verification;
- recording plus transcript: preserve the provided transcript as an input and audit it against the recording;
- reviewed transcript plus alignment map: proceed to instructor-content integration.

Do not write learning notes directly from raw ASR output. Keep timestamps near uncertain wording, quantitative statements, equations read aloud, corrections, and assessment cues so a reviewer can return to the recording.

Extract and map:

- definitions and explanations not present on slides;
- why a page, figure, or example was shown;
- memorable analogies and decision rules;
- corrections and warnings;
- exam cues and solution defaults;
- numerical plausibility checks;
- simplifications and their limitations;
- learning strategy that materially affects study.

Classify transcript blocks internally as `directly integrate`, `compress`, or `exclude`. Exclusion should have a reason.

Do not weaken a useful causal statement into vague association. Do not preserve garbled wording when the meaning is uncertain. Use an uncertainty marker or leave the claim unresolved.

The handout is authoritative for printed notation, equations, diagrams, and page order. The recording is authoritative for what was actually said. When the instructor corrects or qualifies the handout, preserve both the printed statement and the spoken correction with their source locations.

## Existing Notes

When revising, keep correct material, intentional tone, stable numbering, and useful visual conventions. Repair:

- thin paraphrase of source headings;
- missing intermediate reasoning;
- ungrounded additions;
- source misattribution;
- inconsistent terminology or symbols;
- missing instructor-specific content;
- visual density that obstructs reading.

When auditing only, report findings with evidence and location; do not rewrite the artifact unless requested.
