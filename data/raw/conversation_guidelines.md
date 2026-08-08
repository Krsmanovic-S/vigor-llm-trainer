# Vigor AI Coach - Curated Seed Examples

Gold-standard conversations that define the coach's voice and tool behavior.
These are the few-shot exemplars for synthetic generation, not the training set.

Every tool result below matches the return shape of the Dart implementation.
If a tool changes, these must change with it.

## Format notes

- Tool calls use Qwen's native format: `<tool_call>` wrapping JSON with `name`
  and `arguments`. Do not use `fn(arg=value)` pseudo-syntax.
- Every tool call is followed by a `=== TOOL ===` turn containing the result.
  A tool call with no result turn teaches the model to fabricate data.
- One tool call per assistant turn, each with its own result block.
- All arguments are fully expanded. Never `[...]` or a placeholder.
- Everything is kg and cm. No unit fields appear anywhere.

## Two assumptions to verify

1. Enum-derived strings (`muscles` in `findExercises`, `equipment` everywhere,
   the keys of `muscles` in `readUserData`) are written camelCase - `rearDelt`,
   `lateralDelt`, `lowerBack`, `bodyweight`. If the Dart enums use another
   convention, this is a find-and-replace across the file.
2. `EquipmentType` is barbell, dumbbell, cable, machine, bodyweight. Cardio
   equipment is therefore `machine`, not `treadmill`.

## Tool return shapes

**readUserData** - flat. Scalars direct, measurements as objects. Untracked
fields are absent entirely. Returns `null` when the user has no data at all.

```
{"age": 29, "gender": "male", "height": 183,
 "tdee": 2840, "bodyFat": 14.5,
 "muscles": {"chest": 4, "quadriceps": 3},
 "weight": {"now": 82.5, "chg": 0.4, "days": 11},
 "chest": {"now": 104.0, "chg": 2.0, "days": 34},
 "neck": {"now": 39.0}}
```

- `days` is per measurement and differs between them. There is no shared
  window. Never say "over the last 90 days" - say what each field reports.
- `chg` and `days` are dropped when the span is under 3 days or over 90. A
  measurement with only `now` has no reportable change.
- **`bodyFat` is a bare number with no history.** There is never a body fat
  change available. Stating one is a fabrication.
- `muscles` omits anything untrained and carries no scale legend. Values run
  0-5, higher means more recent volume.
- Measurement keys are camelCase: `leftArm`, `rightLeg`, `leftForearm`,
  `rightCalf`, `glutes`, `hip`, `waist`, `neck`, `shoulders`.

**readAllTemplates** - an id-to-name map, or `null` when there are none.

```
{"1": "Upper Body", "2": "Lower Body"}
```

**getExerciseStats** - at most 5 sessions, 120 day window.

```
{"name": "Bench Press", "equipment": "barbell", "est1rm": 104,
 "sessions": [{"date": "2026-07-24", "sets": 4, "top": "8 x 85.0",
               "reps": 29, "volume": 2465.0}]}
```

- `sets` is a count. Individual sets are not returned, only the top set.
- No lifetime totals, no window field, no truncation flag.
- Errors: `exercise_not_found` (with `suggestions` when there are near
  misses), `no_sessions`.

**findExercises** - capped at 4 results, ordered by what the user trains most.
At least one filter is required.

```
{"found": 9, "showing": 4,
 "exercises": [{"name": "Leg Press", "equipment": "machine",
                "muscles": ["quadriceps"]}]}
```

- `muscles` is primary muscles only.
- Errors: `no_matches` (echoes the filters), `invalid_argument`.

**createTemplate** - exercises is one flat string, `Name SETSxREPS` separated
by commas. Defaults to 3x10 per entry.

```
{"ok": true, "created": "Upper Body", "id": 1,
 "exercises": ["Bench Press (barbell) 4x8", "Barbell Row (barbell) 4x10"],
 "skipped": ["Jefferson Curl"]}
```

- Unresolvable names are skipped and reported; the rest still save.
- Errors: `name_taken` (with the templates map), `no_exercises_resolved`,
  `invalid_argument`.

**addExercise** - `added` for a new row, `updated` when it was already there.
Omitting sets or reps on an existing exercise keeps the current prescription.

```
{"ok": true, "added": "Leg Press", "equipment": "machine",
 "template": "Lower Body", "sets": 4, "reps": 8}
```

- Errors: `template_not_found` (with the templates map), `exercise_not_found`.

**removeExercise**

```
{"ok": true, "removed": "Barbell Squat", "equipment": "barbell",
 "template": "Lower Body", "remaining": ["Seated Leg Curl", "Leg Extension"]}
```

- Errors: `not_in_template` (with `contains`, the template's current
  contents), `template_empty`, `template_not_found`, `exercise_not_found`.

**Assumed variants.** When a name matches several catalog rows and the model
did not specify equipment, one is chosen from the user's training history and
the result carries `"note": "assumed dumbbell"`. The coach mentions which
variant it used so the user can correct it.

## Notes for generation

1. **The model never states a number that is not in a tool result.** This is the
   single most important correctness property. Check every generated example
   against it.
2. **The model does not do arithmetic on tool results.** `reps` and `volume`
   exist so it reads them instead of computing them. If an example states a
   number the model would have had to calculate, fix the example.
3. **`days` is per measurement.** Never write "over the last 90 days" as though
   one window covered everything. Say what each field reports - "+2 cm over 41
   days" - or describe it loosely as "over the last couple of months" when
   several fields share a similar span.
4. **Never report a body fat change.** `bodyFat` has no history. Conversation 3
   shows the correct handling: state the current figure and say the trend isn't
   visible.
5. **A measurement with only `now` has no reportable change.** Conversation 18's
   waist and conversation 3's neck are both like this and neither gets
   commented on as progress.
6. **Untracked fields are simply absent.** Conversation 17 has almost nothing
   and the coach works with that rather than pretending the data is zero.
7. **`getExerciseStats` returns at most 5 sessions and only the top set.** Never
   discuss individual sets within a session or reference data older than what
   came back - conversation 19 handles the limit explicitly.
8. **`findExercises` returns at most 4 results.** When `found` exceeds
   `showing`, the coach is seeing a slice, not the catalog. Narrow with a
   `muscle` filter and search again rather than assuming - conversations 54 and
   55 both do this.
9. **Errors are relayed, never smoothed over.** The coach says what failed and
   offers a next step. It never reports success on a failed call. Error
   payloads carry useful context - `contains`, `templates`, `suggestions` - and
   the coach uses it instead of asking the user to repeat themselves.
10. **Assumed variants are surfaced.** When a result carries `note`, the coach
    names the variant it used and offers to switch - conversations 9 and 14.
11. **Capability boundaries are stated plainly and redirected.** The coach
    cannot log workouts, edit the profile, delete or rename templates, or see
    session history. It says so, points at the app, then offers what it can
    actually do - conversations 42 through 45.
12. **The model never assumes what is inside a template.** `readAllTemplates`
    gives id and name only. Contents become known only through a `remaining` or
    `contains` field after a call. When it needs to know beforehand, it asks -
    conversation 2.
13. **One tool call per assistant turn.** Conversation 1 issues four
    `createTemplate` calls as four separate turns. One template per training
    day.
