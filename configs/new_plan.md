# Vigor AI Coach - Tool Specification

Contract between the on-device model and the app. Every training example is
generated against this file, so changes after data generation begins invalidate
the dataset. Freeze before generating.

Target model: Qwen3-1.7B (non-thinking mode), 4 tools.

---

## Global conventions

### Units

`settings.measurementSystem` is `metric` or `imperial`. The tool layer converts
all values to the user's active display unit before returning them - default units
are `metric`, and every numeric group carries an explicit units field. 
The model never converts and never guesses - it repeats the unit it was given.

```jsonc
"weight": 82.5, "weight_units": "kg"      // metric user
"weight": 181.9, "weight_units": "lbs"    // imperial user
```

Distances use `km`/`mi`, body measurements use `cm`/`in`.

### Names, not IDs

The model never sees or emits database IDs. It refers to exercises and
templates by name; the Dart layer resolves names to IDs and returns a
structured error on ambiguity or no match. Matching is case-insensitive and
trims whitespace.

### Error shape

Every tool can return an error instead of a payload. One consistent shape:

```jsonc
{
  "error": "not_found",
  "message": "No exercise named 'Bench Pres' in the catalog.",
  "suggestions": ["Bench Press", "Incline Bench Press"]
}
```

Codes: `not_found`, `ambiguous`, `no_data`, `no_active_workout`,
`invalid_argument`, `duplicate_name`.

`suggestions` is present when a close match exists. Every error code needs its
own training examples - the model must learn to relay the error and ask, not
invent a result.

### Truncation

Reads that could return unbounded data are capped and flagged:

```jsonc
{ "workouts": [...], "returned": 20, "truncated": true }
```

When `truncated` is true the model should say the window was large rather than
implying it saw everything.

### Write confirmation

Destructive writes (`manage_template` with `operation: "delete"`) require the
user to have explicitly confirmed in the conversation first. The model asks,
waits for a yes, then calls. Non-destructive writes (create, add set) may be
called directly when the request is unambiguous.

---

## Enums

```
ReadScope:
  profile | measurements | muscle_balance | workout_history |
  exercise_history | templates | active_workout | menstrual

TemplateOperation:
  create | modify | delete

ActiveWorkoutAction:
  add_exercise | remove_exercise | swap_exercise | add_set | remove_set

BodyPart:      chest | back | shoulders | arms | legs | abs | cardio | other
EquipmentType: dumbbell | barbell | cable | machine | cardio | treadmill |
               bodyweight | other
Muscles:       neck | chest | abs | obliques | front_delt | lateral_delt |
               rear_delt | biceps | triceps | forearms | traps | lats |
               lower_back | quadriceps | hamstrings | calves | tibia | glutes
```

`manage_template` and `manage_active_workout` deliberately use separate enums
so the schema itself makes `add_set` un-emittable on a template call.

---

## Tool 1: read_user_data

**Description given to the model:**

> Read the user's own data: their profile, body measurements, logged workout
> history, performance history for a specific exercise, saved templates, or the
> workout currently in progress. Use this for any question about the user
> themselves or what they have done. Do NOT use this to look up exercises in
> the app's catalog - use `search_exercises` for that.

### Parameters

| Name | Type | Required | Default | Notes |
|---|---|---|---|---|
| `scope` | array of ReadScope | yes | - | One or more scopes in a single call |
| `days` | integer | no | `0` | Lookback window. `0` = current values only |
| `exercise_name` | string | conditional | - | Required when scope includes `exercise_history` |

`scope` is an array so "am I making progress?" is one call:
`["profile", "measurements", "workout_history"]` with `days: 90`.

`days` applies only to `measurements`, `profile`, `workout_history`, and
`exercise_history`. It is ignored for the others.

### Scope: profile

Current stats. With `days > 0`, adds change deltas from `measurement_history`.

```jsonc
{
  "age": 29,
  "gender": "male",
  "height": 183, "height_units": "cm",
  "weight": 82.5, "weight_units": "kg",
  "body_fat_pct": 14.2,
  "activity_level": "moderatelyActive",
  "tdee_kcal": 2840,
  "changes": {                        // only when days > 0
    "window_days": 90,
    "weight_delta": 1.8,
    "body_fat_pct_delta": 0.3
  }
}
```

If `menstrualTrackingEnabled` is false, no menstrual fields appear here at all.

### Scope: measurements

Body circumferences. With `days > 0`, adds per-site deltas.

```jsonc
{
  "units": "cm",
  "current": {
    "chest": 104.0, "shoulders": 126.0, "waist": 82.0, "neck": 39.0,
    "hip": 98.0, "glutes": 101.0,
    "left_arm": 38.4, "right_arm": 38.6,
    "left_forearm": 29.0, "right_forearm": 29.2,
    "left_leg": 60.0, "right_leg": 60.2,
    "left_calf": 39.0, "right_calf": 39.1
  },
  "changes": {                        // only when days > 0
    "window_days": 90,
    "chest": 2.0, "left_arm": 0.8, "right_arm": 0.8, "waist": -0.5
  }
}
```

Sites with a `0.0` value are omitted entirely rather than returned as zero, so
the model does not report "your neck is 0 cm". Sites with no history entry in
the window are omitted from `changes`.

### Scope: muscle_balance

From `user_data.muscleScores` - training frequency score 0-5 per muscle. This
is the strongest signal for spotting undertrained areas.

```jsonc
{
  "scale": "0-5, higher means more training volume recently",
  "scores": {
    "chest": 4, "lats": 3, "quadriceps": 4, "hamstrings": 2,
    "rear_delt": 1, "calves": 1, "biceps": 3, "triceps": 3
  }
}
```

### Scope: workout_history

Completed workouts within `days`. Summary level only - top working set per
exercise, not every set. Capped at 20 workouts, most recent first.

```jsonc
{
  "window_days": 30,
  "returned": 12,
  "truncated": false,
  "weight_units": "kg",
  "workouts": [
    {
      "date": "2026-07-24",
      "name": "Upper Body",
      "duration_min": 68,
      "total_weight": 14820.0,
      "progress_count": 3,
      "exercises": [
        { "name": "Bench Press", "sets": 4, "top_set": "8 x 85.0" },
        { "name": "Barbell Row", "sets": 4, "top_set": "10 x 70.0" }
      ]
    }
  ]
}
```

`progress_count` is the app's count of sets that beat previous performance.
Warmup sets are excluded from `sets` and never eligible as `top_set`.
Cardio exercises use `"top_set": "5.2 km in 28:00"` instead.

### Scope: exercise_history

Per-session performance for one exercise. Requires `exercise_name`. Working
sets only, warmups filtered out. Capped at 15 sessions.

```jsonc
{
  "exercise": "Bench Press",
  "equipment": "barbell",
  "window_days": 90,
  "returned": 8,
  "truncated": false,
  "weight_units": "kg",
  "lifetime": {
    "estimated_1rm": 104,
    "total_reps": 3820,
    "total_weight": 241500.0
  },
  "sessions": [
    { "date": "2026-07-24", "sets": ["8 x 85.0", "8 x 85.0", "7 x 85.0", "6 x 85.0"], "total_reps": 29, "top_weight": 85.0, "volume": 2465.0 },
    { "date": "2026-07-17", "sets": ["8 x 82.5", "8 x 82.5", "8 x 82.5", "7 x 82.5"], "total_reps": 29, "top_weight": 85.0, "volume": 2465.0 }
  ]
}
```

Returns `no_data` if the exercise exists in the catalog but has never been
performed. That is a distinct case from `not_found` and needs its own training
examples - "you have not logged this yet" rather than inventing numbers.

### Scope: templates

```jsonc
{
  "weight_units": "kg",
  "templates": [
    {
      "name": "Upper Body",
      "last_performed": "2026-07-24",
      "exercises": [
        { "name": "Bench Press", "target_sets": 4, "target_reps": 8 },
        { "name": "Barbell Row", "target_sets": 4, "target_reps": 10 }
      ]
    }
  ]
}
```

Per-set detail from `template_sets` is omitted at this level. If the user asks
about a specific template's set structure, that is a follow-up concern - noted
as an open question below.

### Scope: active_workout

```jsonc
{
  "active": true,
  "name": "Lower Body",
  "started_at": "2026-07-27T09:14:00",
  "elapsed_min": 34,
  "weight_units": "kg",
  "exercises": [
    {
      "name": "Barbell Squat",
      "sets": [
        { "set": 1, "type": "warmup", "weight": 60.0, "reps": 10, "completed": true },
        { "set": 2, "type": "normal", "weight": 100.0, "reps": 8, "completed": true },
        { "set": 3, "type": "normal", "weight": 100.0, "reps": 8, "completed": false }
      ]
    }
  ]
}
```

Returns `{"active": false}` when nothing is in progress. Not an error - the
model should handle it conversationally.

### Scope: menstrual

Only returned when `menstrualTrackingEnabled` is true, otherwise `no_data`.
Kept as its own scope so it is never fetched incidentally with `profile`.

```jsonc
{
  "enabled": true,
  "current_phase": "follicular",
  "cycle_day": 9,
  "phase_lengths": { "menstruation": 5, "follicular": 8, "ovulation": 3, "luteal": 12 }
}
```

---

## Tool 2: search_exercises

**Description given to the model:**

> Search the app's exercise catalog by name, body part, equipment, or targeted
> muscle. Use this to find exercises the user could do, or to check whether an
> exercise exists before adding it to a template or workout. This searches the
> catalog only - it does not return the user's performance history.

Split from `read_user_data` because the parameter sets are disjoint: catalog
search filters on body part / equipment / muscle, history filters on name +
days. Nothing is shared.

### Parameters

| Name | Type | Required | Default | Notes |
|---|---|---|---|---|
| `query` | string | no | - | Free text name match |
| `body_part` | BodyPart | no | - | |
| `equipment` | EquipmentType | no | - | |
| `muscle` | Muscles | no | - | Matches primary or secondary |
| `limit` | integer | no | `10` | Max 25 |

At least one of `query`, `body_part`, `equipment`, `muscle` must be present.

### Returns

```jsonc
{
  "returned": 3,
  "truncated": false,
  "exercises": [
    {
      "name": "Dumbbell Row",
      "equipment": "dumbbell",
      "body_part": "back",
      "primary_muscles": ["lats"],
      "secondary_muscles": ["biceps", "rear_delt"],
      "is_user_created": false
    }
  ]
}
```

The model cannot create exercises. If nothing matches, it says so and offers
catalog alternatives - it must not invent an exercise name and pass it to a
write tool.

---

## Tool 3: manage_template

**Description given to the model:**

> Create, modify, or delete a saved workout template. For modify, pass the
> complete desired exercise list - it replaces the existing one. Read the
> template with `read_user_data` first so you know its current contents.
> Deleting requires the user to confirm first.

### Parameters

| Name | Type | Required | Notes |
|---|---|---|---|
| `operation` | TemplateOperation | yes | `create`, `modify`, `delete`. `rename` |
| `template_name` | string | yes | Target template |
| `new_name` | string | no | Rename, `modify` only |
| `exercises` | array | conditional | Required for `create` and `modify` |

Each exercise entry:

```jsonc
{ "name": "Bench Press", "target_sets": 4, "target_reps": 8, "target_weight": 80.0 }
```

`target_weight` is optional and defaults to `0.0` (means "not prescribed").

**Modify is declarative, not incremental.** There is no `add_exercises` /
`remove_exercises` / `replace_exercise` - the model sends the full desired
list. This trades more output tokens for one shape to learn instead of four,
which matters at 1.7B. It also enforces the read-before-write pattern.

### Returns

```jsonc
{ "ok": true, "operation": "create", "template_name": "Upper Body", "exercise_count": 6 }
```

Errors: `duplicate_name` on create, `not_found` on modify/delete, `not_found`
with `suggestions` if any exercise name does not resolve. On a partial name
failure nothing is written - the whole call fails atomically so the model never
reports a success that half happened.

---

## Tool 4: manage_active_workout

**Description given to the model:**

> Modify the workout currently in progress: add, remove, or swap an exercise,
> or add and remove sets. Only works when a workout is active. You cannot mark
> sets as completed - the user does that in the app.

### Parameters

| Name | Type | Required | Notes |
|---|---|---|---|
| `action` | ActiveWorkoutAction | yes | |
| `exercise_name` | string | yes | Target exercise |
| `replacement_name` | string | conditional | Required for `swap_exercise` |
| `count` | integer | no | For `add_set` / `remove_set`, default `1` |
| `position` | integer | no | For `add_exercise`, 1-based, default = end |

Marking sets complete is deliberately excluded. Logging is the user's action;
the model observes and advises.

### Returns

```jsonc
{ "ok": true, "action": "swap_exercise", "from": "Barbell Squat", "to": "Leg Press" }
```

Errors: `no_active_workout`, `not_found`, `invalid_argument` (e.g. removing
more sets than exist).

---

## Open questions

Decide these before generating training data:

1. **Template set detail.** `template_sets` holds per-set weight/reps that can
   differ from `target_sets`/`target_reps`. Currently unexposed. Add a
   `detail: true` parameter to the templates scope, or leave it out of v1?
2. **Cardio and static-hold in templates.** `manage_template` exercise entries
   assume weight x reps. Cardio needs distance/time, static holds need hold
   seconds. Add optional `target_distance`, `target_time`, `target_hold`?
3. **Parallel tool calls.** Curated conversation 1 emits two `create` calls in
   one turn. Confirm the app handles multiple calls per assistant turn, or
   require one per turn and train accordingly.
4. **Language.** `settings.languageCode` exists. Does the coach reply in the
   app language, and if so does the training set need non-English examples?
5. **Scope count.** Eight scopes on one enum. If evaluation shows the model
   confusing `profile` and `measurements`, merge them - both are "body stats"
   and the split is arguable.

---

## Coverage check against curated examples

| Conversation | Tools needed | Status |
|---|---|---|
| 1 - Workout split creation | `manage_template` x2 | OK, pending Q3 |
| 2 - Exercise replacement | `read_user_data(templates)` + `manage_template` | OK |
| 3 - Progress analysis | `read_user_data(profile, measurements, days=90)` | OK, needs `measurement_history` |
| 4 - Bench plateau | `read_user_data(exercise_history, days)` | OK |
| 5 - Home equipment | `search_exercises(equipment=dumbbell, body_part=back)` | OK |
| 6 - Post-workout meal | none | OK |
| 7 - Should I bulk | none (clarifying) | OK |
| 8 - Motivation | none | OK |

All eight are expressible. Conversation 3 must be regenerated with the
`tool` result turn included - as written it states measurement numbers with no
tool result in between, which trains the model to fabricate them.