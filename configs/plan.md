## Available Tools

1. read_user_data(days: int = 0)

This just gives us current value of all user related data and a history for each entry if applicable.

If the `days` parameter is 0, we just return current values.

Returns:
    - age
    - gender
    - height
    - weight
    - body fat
    - activity level
    - measurements
    - muscle scores
    - menstrual settings (if enabled)

2. read_workout_history(days: int = 0)

Gives past finished workouts within a current timeframe of days, e.g. workouts happened in the past 10 days.

Returns:
    - workout names
    - exercises
    - sets
    - reps
    - workout duration

3. read_exercise_history(exercise_name: str)

Retrieves the history for a given exercise, when it was performed, how much weight was lifted etc..
We can include stuff like:
    - estimated 1RM
    - heaviest weight
    - best volume

4. modify_workout_template(operation: str, action: str)

CRUD operations for templates, operations can be `create_template`, `delete_template`, `modify_template`. Make sure
to provide a good argument list here so it is easy to distinguish and to actually code this out.

5. read_workout_templates()

Returns the list of current templates the user has.

7. read_exercises(muscle_group: str = None, equipment: str = None)

Seaches the exercises in the DB to match a certain muscle group or equipment type.

8. get_active_workout()

Returns the currently active session if present.

9. modify_active_workout(action: str, exercise: str, ...)

Actions can be `add_exercise`, `swap_exercise`, `remove_exercise`. 

10. modify_user_exercise(name: str, equipment: str, muscle_groups: list)

CRUD operations for exercises, operations can be `create_exercise`, `modify_exercise`. Make sure
to provide a good argument list here so it is easy to distinguish and to actually code this out.



