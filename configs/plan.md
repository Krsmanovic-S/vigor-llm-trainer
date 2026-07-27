## Enum Definition

This enum will be used as `scope` when retrieving data with a tool call:
```
enum ReadDataScope {
    profile,
    measurements,
    workout_history,
    exercise_history,
    exercises,
    templates,
    active_workout
}
```
This enum will be used as `operation` when using the tools `manage_templates` and `manage_active_workout`:
```
enum ManageOperations {

}
```

## Available Tools

### read_user_data(scope: Map<ReadDataScope, int days = 0>, exercise_name: string = '')

Parameter description:
- scope - required, of type ReadDataScope, tells the function what data to retrieve, is a list so multiple retrievals can be done in one call if needed 
- days - optional parameter, how far in the past to look for for the given scope, default of 0 means current/latest values
- exercise_name - optional parameter, used only in the case of `exercise_history` or `exercises` scope to look up specific exercises

Scope descriptions:

`profile` returns: `age`, `gender`, `height`, `weight`, `body fat`, `activity level`, `menstrual data` (if enabled).

`measurement` returns the following measurements: `chest`, `shoulders`, `left arm`, `right arm`, `waist`, `hip`, `glutes`, `left leg`, `right leg`, `left calf`, `right calf`. 

`workout_history` returns a list of past workouts within a given timeframe defined by days in the scope map.

`exercise_history` returns the past performances (sets, weights, reps, distances etc..) for a given exercise within the timeframe defined by days.

`exercises` searches the exercises database for a specific exercise, specified by the `exercise_name` parameter.

`templates` returns a list of all templates currently present in the app.

`active_workout` returns all information about a currently active workout if present (name, elapsed time, exercises, set data etc..).

2. manage_template(operation: ManageOperation ..)

CRUD operations for templates, operations can be `create_template`, `delete_template`, `modify_template`. Make sure
to provide a good argument list here so it is easy to distinguish and to actually code this out.

3. manage_active_workout(action: ManageOperation ..)

Actions can be `add_exercise`, `swap_exercise`, `remove_exercise`, `add_set`, `remove_set`. 



