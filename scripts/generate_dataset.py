"""Generate synthetic training conversations for the Vigor fitness coach.

Three phases:
  A - generate diverse user first-messages ("scenarios") per category, then
      deduplicate them before spending tokens on full conversations.
  B - expand each surviving scenario into a full conversation, using seeds from
      the same category as few-shot exemplars.
  C - continue conversations that stopped early. Phase B output often ends on a
      tool call or on "would you like me to create these?" - neither is fixable
      by prompting harder, so those get fed back and continued.

Output is raw "=== " conversation text plus metadata, one JSON object per line.
Converting that into training JSONL is a separate step, so the converter can be
re-run without regenerating anything.

    python scripts/generate_dataset.py --phase a
    python scripts/generate_dataset.py --phase a --category "Plan Creation"
    python scripts/generate_dataset.py --phase b
    python scripts/generate_dataset.py --phase c --dry-run
    python scripts/generate_dataset.py --phase c --limit 20
    python scripts/generate_dataset.py --phase c

All phases are resumable - rerun the same command and finished work is skipped.
"""

import argparse, json, os, random, re, sys, threading, time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI, BadRequestError

# ---------------------------------------------------------------------------
# Config - edit these freely
# ---------------------------------------------------------------------------
MODEL = "qwen/qwen3-30b-a3b-instruct-2507:floor"

MAX_TOKENS = 4096               # tool results can be long
SCENARIOS_PER_CALL = 60
MAX_WORKERS = 8
MAX_RETRIES = 5
DEDUPE_THRESHOLD = 0.75         # Sorensen-Dice
SEEDS_PER_PROMPT = 5
MAX_CONTINUE_ROUNDS = 3         # phase C - a conversation may stop twice

_PROJECT = Path(__file__).resolve().parent.parent
SEEDS_PATH = _PROJECT / "data" / "raw" / "curated_data.md"
TOOLS_PATH = _PROJECT / "configs" / "tools.json"
CATALOG_PATH = _PROJECT / "configs" / "trimmed_catalog.txt"
SCENARIOS_PATH = _PROJECT / "data" / "processed" / "scenarios.jsonl"
CONVERSATIONS_PATH = _PROJECT / "data" / "processed" / "conversations.jsonl"

# Category -> 1200 entries total.
CATEGORIES = {
    "Plan Creation": 170,
    "Template Modification": 140,
    "Refusal": 120,
    "Safety": 110,
    "Progress Analysis": 105,
    "Constraints": 90,
    "Capability boundary": 80,
    "Exercise History": 80,
    "Exercise Recommendation": 75,
    "Nutrition": 60,
    "Training Principles": 50,
    "Empty state": 45,
    "Recovery": 40,
    "Supplements": 20,
    "Motivation": 15,
}

# What the generator actually reads. The CATEGORIES key is only a lookup key
CATEGORY_PROMPT_LABELS = {
    "Plan Creation":
        "asking for workout templates to be built for them - a training split, "
        "a routine for a goal like strength or muscle, or a session built "
        "around the equipment they have. The request must be satisfiable by "
        "creating one or more templates. Do NOT write messages asking for a "
        "weekly schedule, a multi-week programme, or a plan that changes over "
        "time - the app has no calendar and templates do not have dates",
    "Template Modification":
        "asking to change a saved template - adding an exercise, removing one, "
        "changing sets or reps, or swapping one exercise for another",
    "Progress Analysis":
        "asking whether they are making progress, answerable from their body "
        "measurements, weight, body fat and per-muscle training volume, or "
        "from their history on one specific exercise",
    "Exercise History":
        "asking about their own past performance on one specific exercise",
    "Exercise Recommendation":
        "asking which exercises to do for a muscle, a goal, or the equipment "
        "they have available",
    "Nutrition":
        "asking about food, protein, calories, or eating for their goal",
    "Training Principles":
        "asking how training works - rep ranges, rest times, frequency, "
        "warmups, technique concepts",
    "Recovery":
        "asking about rest, soreness, fatigue, deloads, sleep, or returning "
        "after time off",
    "Constraints":
        "stating a limitation the coach should work around and adapt to - an "
        "old injury, equipment they do not have access to, exercises they "
        "dislike or refuse to do, or a preference about how they train. The "
        "user is setting a boundary for the coach to accommodate, NOT asking "
        "the coach to refuse anything",
    "Safety":
        "showing a warning sign the coach must handle carefully - wanting to "
        "eat far too little, wanting to lose weight dangerously fast, "
        "exercising to burn off food they ate, distress about how their body "
        "looks, wanting to train while ill, or mentioning a medical condition "
        "or pregnancy",
    "Supplements":
        "asking about supplements and whether they are worth taking",
    "Refusal":
        "asking the coach for something it must decline - advice on steroids "
        "or other performance enhancing drugs, help with a topic that has "
        "nothing to do with fitness, or a diagnosis for pain or an injury that "
        "the coach is not qualified to give. The USER is making the request "
        "and the coach will decline it",
    "Motivation":
        "struggling with consistency or motivation, or feeling discouraged "
        "about their training",
    "Empty state":
        "sent by a brand new user who has not logged any workouts and has not "
        "created any templates yet",
    "Capability boundary":
        "asking the app to do something it is not able to do, including "
        "renaming or deleting a template, logging a workout, syncing a "
        "wearable, or editing their profile",
}

# Which seed categories to draw few-shot examples from, where substring
# matching on the CATEGORIES key would not find the right ones.
CATEGORY_SEED_MATCH = {
    "Constraints": ["user disagreement", "Exercise Recommendation"],
}

CAPABILITIES = """\
The app tracks resistance training only. It has:
- Workout templates: a named, ordered list of exercises with target sets and reps
- Logged workouts: sets, reps, weight, RPE, plus distance/time for cardio machines
- An exercise catalog tagged by body part, equipment and muscles
- User data: age, gender, height, weight, body fat, activity level, estimated TDEE
- Body measurements with history
- Per-muscle training volume scores
- Optional menstrual cycle tracking

The app does NOT have:
- A calendar or schedule. Templates are not assigned to days, and there is no
  concept of a rest day, a training week, or a programme with dates
- Food or calorie logging, macro tracking, meal plans, or a food database
- Step counting, sleep tracking, heart rate, or any wearable integration
- GPS, outdoor running or any sport that is not logged as sets and reps
- Photos, social features, or sharing
- Paid tiers, group classes, or human coaching services

The coach can only do these things: read the user's stats, list their template
names, look up their history for one exercise, search the exercise catalog,
create a template, and add or remove one exercise from an existing template.
It cannot rename or delete templates, log workouts, edit the profile, or change
a workout in progress. For anything else it says so plainly and does not offer
a workaround involving tools it does not have.

Messages must only reference things the app actually has. Do not write messages
about rescheduling days, planning around a time of day, logging meals, swimming
sessions, step counts, sleep data, or premium plans."""

CATEGORY_EXTRA_HINTS = {
    "Plan Creation": """\
The assistant may ask one preference question first, but the conversation must
then CONTINUE - a following USER turn answering it, then the createTemplate
calls, then a short confirmation of what was built.

Never end on "would you like me to create these?" without the user answering
and the templates being created. Every conversation in this category must
contain at least one createTemplate call.

Vary the reply - some pick an option, some say "whatever you think", some add a
constraint. A multi-day plan means one createTemplate call per day.""",

    "Template Modification": """\
Spread across the changes the tools actually support: adding an exercise,
removing one, changing sets or reps, and swapping one exercise for another
(which is a remove followed by an add). Every one of these needs the template
id from readAllTemplates first.""",

    "Progress Analysis": """\
Spread these across everything that can be analysed: body measurements, weight
trend, body fat, whether a specific muscle is being neglected, and progress on
one exercise. Not all of them should be about weight.""",

    "Exercise History": """\
Use a wide range of exercises - not just bench, squat and deadlift. Include
machine work, isolation movements and cardio machines. Vary what is being
asked: am I progressing, what is my best set, when did I last do this, how does
this month compare to last, has this stalled.""",

    "Exercise Recommendation": """\
Cover every body part, not just chest and arms. Vary the constraint that drives
the question - a muscle they want to target, equipment they have, an exercise
they want to replace, a weak point they have noticed, limited time.""",

    "Nutrition": """\
Go beyond protein and calories. Include meal timing, eating around training,
hydration, eating enough while busy, appetite, food choices, alcohol, eating
out, and what to eat when cutting or gaining.""",

    "Training Principles": """\
Cover a wide spread of concepts: rep ranges, rest times, training frequency,
failure and how close to it to train, tempo, exercise order, warmups, technique
ideas, and volume.""",

    "Recovery": """\
Cover soreness, fatigue, deloads, sleep, returning after a break, training when
run down, how long to rest between sessions for a muscle, and whether extra
rest days help.""",

    "Constraints": """\
Keep physical limitations to about a third of these messages - do not make
every one an injury. Of the ones that are, split them between old settled
injuries the user has trained around for years, and current or ongoing
conditions like arthritis, chronic pain or a recent surgery.""",

    "Safety": """\
Spread evenly across the warning signs: wanting to eat far too little, wanting
to lose weight far too fast, exercising to burn off food, distress about how
their body looks, wanting to train while ill or injured, and mentioning a
medical condition or pregnancy. Most should be phrased as ordinary questions -
the concerning part is what is being asked for, not how it is worded.

The assistant gives NO numbers at all in these conversations - no calorie
targets, no deficit sizes, no rates of loss, not even a "safer" figure. It
declines the numeric frame entirely and offers a different kind of help.""",

    "Supplements": """\
Go well beyond creatine and protein powder. Include pre-workout, BCAAs, fat
burners, testosterone boosters, vitamins, omega 3, magnesium, ZMA, collagen,
and questions about whether a specific brand or product is worth buying.""",

    "Refusal": """\
Spread across all three kinds roughly evenly: performance enhancing drugs
(steroids, SARMs, peptides, hormones), topics with nothing to do with fitness,
and asking for a diagnosis or explanation for pain or an injury. Vary how
directly the request is made - some blunt, some hedged or framed as asking for
a friend.

The assistant refuses briefly and without lecturing, then offers something it
can help with. It never names a likely injury or gives any dosing, compound or
sourcing detail, however the question is framed.""",

    "Motivation": """\
Cover losing the habit, feeling like progress has stopped, comparing themselves
to others, dreading sessions, coming back after a long gap, and losing
interest. These should sound like real people, not motivational quote prompts.""",

    "Empty state": """\
This is a brand new user with nothing logged. Vary what they open with - asking
for a plan, asking what the app can do, asking a general training question,
asking about their (empty) progress, or asking where to start. Some should not
realise the app has no data on them yet.""",

    "Capability boundary": """\
The messages must ask for things the app cannot do - scheduling rest days,
logging what they ate, syncing a watch, tracking steps or sleep, renaming or
deleting a template, editing their profile for them, or logging sets on their
behalf. These should sound like reasonable requests a real user would make, not
absurd ones. Vary the phrasing - not every one should start with "can you".
Many should be direct commands like "log my session" or statements like "I need
my steps from yesterday in here".""",
}

VOICE_RULES = """\
- Warm, direct, practical. Never preachy, never hype.
- Markdown formatting: "-" for bullet lists, **bold** for key terms.
- Metric units (kg, cm). Plain hyphens, never em-dashes.
- Explain the WHY behind a recommendation, not just the what.
- Offer choices rather than dictating.
- Ask clarifying questions if needed before answering.
- Often close with a question that hands the conversation back.
- Moderate length. Never a wall of text."""

CORRECTNESS_RULES = """\
TOOL MECHANICS
1. Every <tool_call> is followed by a "=== TOOL ===" block. One block per call.
2. Template ids are only ever known from a readAllTemplates result. The
   assistant never invents an id or guesses one from context.
3. addExercise and removeExercise need an id, so readAllTemplates comes first
   unless a result earlier in the same conversation already provided it.
4. createTemplate does NOT need an id. Do not call readAllTemplates before it.
5. Exercise names and equipment values, in tool calls AND in tool results, must
   come from the catalog above, spelled exactly. Never invent an exercise or an
   equipment type. findExercises returns at most 4 results. Muscle names in
   readUserData results are snake_case: front_delt, rear_delt, lower_back,
   lateral_delt.
6. Many messages need no tool at all. Anything answerable from general fitness
   knowledge is answered directly.

USING RESULTS
7. The assistant never states a number that is not in a tool result. No
   invented weights, dates, session counts or measurements.
8. The example values in the result shapes above show FORMAT only. Never quote
   them as if they were this user's data. Any number about the user must come
   from a "=== TOOL ===" block in this same conversation.
9. The assistant never calculates. Totals and volumes are read from the result,
   never derived. If a number is not there, it is not mentioned.
10. The assistant reports WHAT the data shows, never WHY. "Your chest is up
    2 cm over 34 days" is fine. "Your bulk went well" and "you have clearly
    been slacking" are not - the result contains numbers, not intent.
11. When a tool returns "note":"assumed X", the assistant says which variation
    it used so the user can correct it.
12. Errors are relayed and recovered from, never ignored. The assistant does
    not report success on a failed call.
13. Tool results in these conversations must look like real logs - uneven
    progress, missed weeks, the odd bad session. Never clean upward lines.

PROGRAMMING QUALITY
14. Templates must be coherent. A push day contains pushing movements, a full
    body day covers the whole body, a leg day does not contain rows. Beginner
    plans favour simpler movements and moderate volume.
15. Keep templates to roughly 4-7 exercises. Longer sessions are rarely what
    the user wants and are not what the app is used for.
16. Sets and reps are always at least 1. "To failure" or "as many as possible"
    is not expressible - use a realistic target rep count and say so.
17. A conversation never ends on a tool call or a tool result. The last turn is
    always the assistant speaking to the user."""

RESULT_SHAPES = """\
These show the SHAPE of each result - which keys exist and how they nest. Every
value below is a placeholder. NN, XX.X and YYYY-MM-DD are not data, they are
gaps for you to fill with realistic values invented for the conversation you
are writing. Never copy a placeholder into a result and never repeat one in
prose.

Do not add, rename or omit keys. Do not invent alternative structures. All
weights are kg and all measurements cm - there are no unit fields.

readUserData: {"age":NN,"gender":"male","height":NNN,"activity":"moderatelyActive","tdee":NNNN,"bodyFat":XX.X,"muscles":{"chest":N,"lats":N,"rear_delt":N,"hamstrings":N},"weight":{"now":XX.X,"chg":-X.X,"days":NN},"chest":{"now":XXX.X,"chg":X.X,"days":NN},"waist":{"now":XX.X},"leftArm":{"now":XX.X,"chg":X.X,"days":NN}}

Measurement keys are camelCase and only ever these: weight, waist, neck, chest,
shoulders, leftArm, rightArm, leftLeg, rightLeg, glutes, hip, leftForearm,
rightForearm, leftCalf, rightCalf. Each is {"now":XX.X} plus "chg" and "days"
when there is enough history. Unset fields are simply absent.

"muscles" scores are INTEGERS 0 to 5 and the names are snake_case from the
Muscles enum: chest, lats, traps, lower_back, front_delt, lateral_delt,
rear_delt, biceps, triceps, forearms, abs, obliques, quadriceps, hamstrings,
glutes, calves, neck, tibia. "back", "shoulders", "frontDelt" and "rearDelt"
are NOT valid muscle names.

readAllTemplates: {"templates":{"N":"TEMPLATE NAME","N":"TEMPLATE NAME"}}
Ids are strings. This returns names only - it does NOT include exercises, sets,
reps or when the template was last performed.

getExerciseStats: {"name":"EXERCISE","equipment":"EQUIPMENT","sessions":[{"date":"YYYY-MM-DD","sets":N,"top":"N x XX.X","reps":NN,"volume":XXXX.X}],"est1rm":NNN}
At most 5 sessions, newest first. "note":"assumed EQUIPMENT" is added when the
name matched several variations and one was chosen.

findExercises: {"found":N,"showing":N,"exercises":[{"name":"EXERCISE","equipment":"EQUIPMENT","muscles":["MUSCLE"]}]}
At most 4 results, one per exercise name. Names and equipment come from the
catalog above.

createTemplate: {"ok":true,"created":"TEMPLATE NAME","id":N,"exercises":["EXERCISE (EQUIPMENT) NxNN"]}
"skipped":["NAME"] is added when a name was not in the catalog. The rest are
still saved.

addExercise: {"ok":true,"added":"EXERCISE","equipment":"EQUIPMENT","template":"TEMPLATE NAME","sets":N,"reps":NN}
"updated" replaces "added" when the exercise was already in the template.
"note":"assumed EQUIPMENT" is added when a variation was chosen.

removeExercise: {"ok":true,"removed":"EXERCISE","equipment":"EQUIPMENT","template":"TEMPLATE NAME","remaining":["EXERCISE","EXERCISE"]}

Errors: {"error":"CODE","name":"...","suggestions":["..."]}
The error value must be one of: exercise_not_found, template_not_found,
not_in_template, template_empty, no_sessions, no_matches, name_taken,
no_exercises_resolved, invalid_argument. Never invent other error codes.

template_not_found also carries "templates" with the full id to name map.
not_in_template and template_empty carry "contains" listing what is in there.
no_matches echoes the filters that were used."""

# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------
load_dotenv()
_client = OpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=os.getenv("OPENROUTER_API_KEY"),
)
_write_lock = threading.Lock()
_usage_logged = 0

BLOCK_RE = re.compile(r"^=== (USER|ASSISTANT|TOOL) ===[ \t]*$", re.MULTILINE)
TOOLCALL_RE = re.compile(r"<tool_call>\s*(.*?)\s*</tool_call>", re.DOTALL)


def _call(system, user, cache=False):
    """One API call with retry on transient failures.

    cache=True marks the system block for prompt caching. OpenRouter passes
    cache_control through to providers that support it and ignores it
    elsewhere, so it is safe to leave on.
    """
    global _usage_logged

    system_content = (
        [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
        if cache
        else system
    )

    for attempt in range(MAX_RETRIES):
        try:
            resp = _client.chat.completions.create(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                messages=[
                    {"role": "system", "content": system_content},
                    {"role": "user", "content": user},
                ],
            )
            if cache and _usage_logged < 3:
                with _write_lock:
                    _usage_logged += 1
                u = resp.usage
                cached = getattr(
                    getattr(u, "prompt_tokens_details", None), "cached_tokens", 0
                )
                print(f"    usage: prompt={u.prompt_tokens} cached={cached} "
                      f"completion={u.completion_tokens}")
            content = resp.choices[0].message.content
            if not content:
                raise RuntimeError("empty response")
            return content
        except BadRequestError:
            raise
        except Exception as e:
            if attempt == MAX_RETRIES - 1:
                raise
            wait = (2 ** attempt) + random.random()
            print(f"    retry {attempt + 1}/{MAX_RETRIES} in {wait:.1f}s - "
                  f"{type(e).__name__}")
            time.sleep(wait)


# ---------------------------------------------------------------------------
# Seed parsing
# ---------------------------------------------------------------------------

def parse_seeds(path):
    """Split curated_data.md into (category, full_text) pairs."""
    if not path.exists():
        sys.exit(f"Seed file not found: {path}")
    text = path.read_text(encoding="utf-8")
    blocks = re.split(r"^## Conversation ", text, flags=re.MULTILINE)[1:]
    seeds = []
    for block in blocks:
        match = re.search(r"=== CATEGORY ===\s*\n(.+)", block)
        if not match:
            continue
        category = match.group(1).strip()
        body = block[block.index("=== CATEGORY ==="):].strip()
        seeds.append((category, body))
    if not seeds:
        sys.exit(f"No conversations parsed from {path} - check the headers")
    return seeds


def seeds_for(seeds, category):
    """Few-shot seeds for a category, by explicit map or substring match."""
    keys = CATEGORY_SEED_MATCH.get(category, [category])
    matched = [
        body for cat, body in seeds
        if any(k.lower() in cat.lower() for k in keys)
    ]
    if matched:
        return matched
    print(f"    WARNING: no seeds matched '{category}' - falling back to all")
    return [body for _, body in seeds]


# ---------------------------------------------------------------------------
# Dedupe
# ---------------------------------------------------------------------------

def _bigrams(s):
    s = re.sub(r"\s+", " ", s.lower().strip())
    return {s[i:i + 2] for i in range(len(s) - 1)}


def dedupe(items):
    """Drop near-duplicates. O(n^2) but n is per-category so it stays fine."""
    kept, kept_grams = [], []
    for item in items:
        grams = _bigrams(item)
        if not grams:
            continue
        if any(
            2 * len(grams & g) / (len(grams) + len(g)) >= DEDUPE_THRESHOLD
            for g in kept_grams
        ):
            continue
        kept.append(item)
        kept_grams.append(grams)
    return kept


# ---------------------------------------------------------------------------
# IO helpers
# ---------------------------------------------------------------------------

def load_jsonl(path):
    if not path.exists():
        return []
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def append_jsonl(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    with _write_lock:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(obj, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# Phase A - scenarios
# ---------------------------------------------------------------------------

SCENARIO_SYSTEM = """\
You write realistic first-messages that users send to a fitness coaching chatbot
inside a workout tracking app. You output nothing but the messages themselves."""

SCENARIO_PROMPT = """\
Generate {n} realistic first-messages a user might send to a fitness coaching
chatbot inside a workout tracking app.

## The messages must be
{category}

## What the app can do
{capabilities}

## Vary deliberately - do not cluster around one profile
- Experience: complete beginner, six months in, several years, returning after a break
- Goal: muscle gain, fat loss, strength, general health, sport-specific
- Equipment: full gym, home dumbbells only, bodyweight, limited machines
- Tone: polite full sentences, terse fragments, frustrated, anxious, casual with typos
- Situation: student, parent, shift worker, older lifter, desk job

## Examples of this category that already exist
Do NOT copy these, write different ones:
{examples}

## Rules
- These are FIRST messages with no prior context
- Do not number them, do not add commentary or headers
- One message per line, plain text only
- No two messages should be answerable by the same response
- No more than 3 messages may begin with the same two words
- Roughly half should be questions and half statements or commands
- Of the questions, about half should end with a question mark and half should
  omit it - people type fast on phones
- At least 5 of the messages must be 20 words or longer
- At least 5 must be 6 words or shorter
{extra}

Output exactly {n} lines."""


def run_phase_a(seeds, only_category=None):
    done = load_jsonl(SCENARIOS_PATH)
    have = {}
    for row in done:
        have.setdefault(row["category"], []).append(row["scenario"])
    print(f"Resuming with {len(done)} existing scenarios\n")

    for category, target in CATEGORIES.items():
        if only_category and category != only_category:
            continue

        existing = have.get(category, [])
        if len(existing) >= target:
            print(f"{category}: {len(existing)}/{target} - done")
            continue

        label = CATEGORY_PROMPT_LABELS.get(category)
        if label is None:
            print(f"    WARNING: no prompt label for '{category}'")
            label = category

        cat_seeds = seeds_for(seeds, category)
        examples = "\n".join(
            f"- {m.group(1).strip()}"
            for s in random.sample(cat_seeds, min(4, len(cat_seeds)))
            if (m := re.search(r"=== USER ===\s*\n(.+)", s))
        )

        hint = CATEGORY_EXTRA_HINTS.get(category)
        extra = f"\n{hint}" if hint else ""

        pool = list(existing)
        rounds = 0
        while len(pool) < target and rounds < 20:
            rounds += 1
            batches = min(-(-(target - len(pool)) // SCENARIOS_PER_CALL),
                          MAX_WORKERS)
            print(f"{category}: {len(pool)}/{target} - requesting {batches} batch(es)")

            with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
                futures = [
                    ex.submit(
                        _call,
                        SCENARIO_SYSTEM,
                        SCENARIO_PROMPT.format(
                            n=SCENARIOS_PER_CALL,
                            category=label,
                            capabilities=CAPABILITIES,
                            examples=examples,
                            extra=extra,
                        ),
                    )
                    for _ in range(batches)
                ]
                for fut in as_completed(futures):
                    try:
                        lines = [l.strip() for l in fut.result().splitlines()
                                 if l.strip()]
                    except Exception as e:
                        print(f"    batch failed: {type(e).__name__}: {e}")
                        continue
                    lines = [re.sub(r"^\s*(?:[-*\u2022]|\d+[.)])\s*", "", l)
                             for l in lines]
                    pool.extend(l for l in lines if l)

            before = len(pool)
            pool = dedupe(pool)
            print(f"    deduped {before} -> {len(pool)}")

        # Compare against a set rather than slicing - dedupe may have dropped
        # some existing entries, which would misalign a positional slice.
        seen = set(existing)
        written = 0
        for s in pool[:target]:
            if s in seen:
                continue
            append_jsonl(SCENARIOS_PATH, {"category": category, "scenario": s})
            seen.add(s)
            written += 1
        print(f"{category}: wrote {written} new\n")


# ---------------------------------------------------------------------------
# Phase B - conversations
# ---------------------------------------------------------------------------

CONVERSATION_SYSTEM = """\
You generate training data for an on-device fitness coaching model. You output
conversations in the required format, with no preamble, no commentary and no
markdown code fences around them.

## What the app can do
{capabilities}

## Tool schema
The assistant may only call these tools, with exactly these parameters:

{tools}

## Exercise catalog
Every exercise in the app, one per line:
name|equipment|body_part|primary_muscles|secondary_muscles

{catalog}

CRITICAL: findExercises results may ONLY contain exercises from this list, with
names copied EXACTLY as written. Never invent an exercise or an equipment type.
Any name written into createTemplate, addExercise or removeExercise must also
come from this list.

The | character is a column separator in the list above, NOT part of any
exercise name. Exercise names must never contain a pipe character.

## Tool result shapes
{shapes}

## Voice rules
{voice}

## Hard correctness rules
{correctness}

## Format
Turns are marked with these exact headers, each on its own line:
  === USER ===
  === ASSISTANT ===
  === TOOL ===
Tool calls are written inside the assistant turn as:
  <tool_call>
  {{"name": "...", "arguments": {{...}}}}
  </tool_call>"""

CONVERSATION_USER = """\
## Reference conversations in this category
{examples}

## Now generate
The user message belongs to this category: {category}
USER MESSAGE: {scenario}

Write ONE complete conversation starting with "=== USER ===". It may be a
single exchange or run several turns, whichever is natural. It must end on an
assistant turn speaking to the user, never on a tool call or a tool result. Do
not include a category header or title. Output only the conversation."""


def build_static_system():
    """The system block shared by phases B and C. Byte-identical text across
    both is what keeps prompt caching hitting."""
    if not TOOLS_PATH.exists():
        sys.exit(f"Tool definitions not found: {TOOLS_PATH}")
    if not CATALOG_PATH.exists():
        sys.exit(f"Exercise catalog not found: {CATALOG_PATH}")
    return CONVERSATION_SYSTEM.format(
        capabilities=CAPABILITIES,
        tools=TOOLS_PATH.read_text(encoding="utf-8"),
        catalog=CATALOG_PATH.read_text(encoding="utf-8"),
        shapes=RESULT_SHAPES,
        voice=VOICE_RULES,
        correctness=CORRECTNESS_RULES,
    )


def run_phase_b(seeds):
    static_system = build_static_system()

    scenarios = load_jsonl(SCENARIOS_PATH)
    if not scenarios:
        sys.exit("No scenarios found - run phase A first.")

    # scenarios.jsonl may hold more than we want to expand. Cap per category
    # rather than trimming the file, so raising a count later is just a rerun.
    capped, seen = [], Counter()
    for s in scenarios:
        cat = s["category"]
        if seen[cat] < CATEGORIES.get(cat, 0):
            capped.append(s)
            seen[cat] += 1
    scenarios = capped
    print(f"capped to {len(scenarios)} scenarios across {len(seen)} categories")

    done = {r["scenario"] for r in load_jsonl(CONVERSATIONS_PATH)}
    todo = [s for s in scenarios if s["scenario"] not in done]
    random.shuffle(todo)      # spread the first batch across all categories
    print(f"{len(done)} already expanded, {len(todo)} to go\n")

    def work(item):
        category = item["category"]
        cat_seeds = seeds_for(seeds, category)
        picked = random.sample(cat_seeds, min(SEEDS_PER_PROMPT, len(cat_seeds)))
        user = CONVERSATION_USER.format(
            examples="\n\n---\n\n".join(picked),
            category=CATEGORY_PROMPT_LABELS.get(category, category),
            scenario=item["scenario"],
        )
        text = _call(static_system, user, cache=True)
        text = re.sub(r"^```[a-z]*\n|\n```$", "", text.strip())
        return {
            "category": category,
            "scenario": item["scenario"],
            "conversation": text,
        }

    completed = 0
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = {ex.submit(work, item): item for item in todo}
        for fut in as_completed(futures):
            try:
                append_jsonl(CONVERSATIONS_PATH, fut.result())
            except Exception as e:
                print(f"  failed: {type(e).__name__}: {e}")
                continue
            completed += 1
            if completed % 25 == 0:
                print(f"  {completed}/{len(todo)}")

    print(f"\nWrote {completed} conversations to {CONVERSATIONS_PATH}")
    print("Run --phase c next to finish any that stopped early.")


# ---------------------------------------------------------------------------
# Phase C - continue conversations that stopped early
#
# Two failure modes come out of phase B, neither fixable by prompting harder:
#
#   A. The assistant proposes a plan, asks "would you like me to create these?",
#      and stops. It is waiting for the user.
#   B. The output ends on a tool call or a tool result. A model trained for tool
#      use stops generating after emitting a call - it expects the environment
#      to answer. Correct agent behaviour, wrong for writing a transcript.
#
# Both are continuations rather than rewrites, so the partial goes back in and
# only the missing turns come out.
# ---------------------------------------------------------------------------

CONTINUE_TOOL = """\
Below is a partial training conversation that stops at a tool call. Continue it.

Write the "=== TOOL ===" result for the unanswered call, then the assistant turn
that uses it, and carry the conversation to a natural end. If the assistant
needs more tool calls, include them with their results.

The conversation must finish on an assistant turn speaking to the user - never
on a tool call or a tool result.

Output ONLY the new turns. Start with "=== TOOL ===". Do not repeat any of the
text below.

--- PARTIAL CONVERSATION ---
{partial}"""

CONTINUE_QUESTION = """\
Below is a partial training conversation. The assistant has proposed a plan and
asked whether to create it, then stopped. Continue it.

Write a "=== USER ===" turn where the user agrees - vary how, some just say yes,
some pick one of the offered options, some add a small constraint. Then the
assistant turn with the createTemplate call or calls, each followed by its
"=== TOOL ===" result, then a short assistant turn confirming what was built and
why those exercises.

One createTemplate call per template. The conversation must finish on an
assistant turn speaking to the user.

Output ONLY the new turns. Start with "=== USER ===". Do not repeat any of the
text below.

--- PARTIAL CONVERSATION ---
{partial}"""

# Phrases meaning the assistant stopped to ask permission rather than acting.
_ASKS_PERMISSION = (
    "would you like me to create",
    "would you like me to build",
    "would you like me to make",
    "would you like me to go ahead",
    "would you like me to save",
    "would you like me to turn this",
    "would you like me to set",
    "would you like to go ahead",
    "should i create",
    "shall i create",
    "want me to create",
    "want me to build",
    "want me to make",
    "want me to save",
    "want me to set",
    "should i save",
    "save this as a template",
    "save these as templates",
    "create these now",
    "create them now",
    "create this as a template",
    "i'll create them",
    "i'll make them now",
)


def parse_blocks(text):
    parts = BLOCK_RE.split(text)
    return [(parts[i], parts[i + 1].strip()) for i in range(1, len(parts) - 1, 2)]


def diagnose(text):
    """None when complete, otherwise 'tool', 'question' or 'broken'."""
    blocks = parse_blocks(text)
    if not blocks:
        return "broken"

    role, content = blocks[-1]

    if role == "TOOL":
        return "tool"

    if role == "ASSISTANT":
        calls = len(TOOLCALL_RE.findall(content))
        prose = TOOLCALL_RE.sub("", content).strip()

        if calls and not prose:
            return "tool"
        if calls:
            answered = sum(1 for r, _ in blocks if r == "TOOL")
            total = sum(len(TOOLCALL_RE.findall(c))
                        for r, c in blocks if r == "ASSISTANT")
            if answered < total:
                return "tool"

        tail = prose[-250:].lower()
        if any(p in tail for p in _ASKS_PERMISSION):
            return "question"
        return None

    return "broken"     # ends on a USER turn


def _strip_preamble(text):
    """Keep only from the first === marker, drop code fences."""
    text = re.sub(r"^```[a-z]*\n|\n```$", "", text.strip())
    m = BLOCK_RE.search(text)
    return text[m.start():].strip() if m else text.strip()


def run_phase_c(dry_run=False, limit=0):
    rows = load_jsonl(CONVERSATIONS_PATH)
    if not rows:
        sys.exit("No conversations found - run phase B first.")

    before = Counter(diagnose(r["conversation"]) for r in rows)
    print(f"{len(rows)} conversations")
    print(f"  complete          {before[None]}")
    print(f"  ends on tool      {before['tool']}")
    print(f"  ends on question  {before['question']}")
    print(f"  broken            {before['broken']}\n")

    todo = [(i, r) for i, r in enumerate(rows)
            if diagnose(r["conversation"]) in ("tool", "question")]
    if limit:
        todo = todo[:limit]

    if dry_run:
        print("Sample of what would be continued:\n")
        for _, r in todo[:6]:
            print(f"  [{diagnose(r['conversation'])}] {r['scenario'][:58]}")
            print(f"        ...{r['conversation'].strip()[-110:]}\n")
        print(f"{len(todo)} conversations would be continued.")
        return

    if not todo:
        print("Nothing to continue.")
        return

    static_system = build_static_system()
    print(f"continuing {len(todo)}...\n")

    def work(item):
        idx, row = item
        text = row["conversation"]
        for _ in range(MAX_CONTINUE_ROUNDS):
            kind = diagnose(text)
            if kind in (None, "broken"):
                break
            template = CONTINUE_TOOL if kind == "tool" else CONTINUE_QUESTION
            added = _strip_preamble(
                _call(static_system, template.format(partial=text), cache=True)
            )
            if not added:
                break
            text = text.rstrip() + "\n\n" + added
        return idx, text

    done = 0
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = {ex.submit(work, item): item for item in todo}
        for fut in as_completed(futures):
            try:
                idx, text = fut.result()
            except Exception as e:
                print(f"  failed: {type(e).__name__}: {e}")
                continue
            with _write_lock:
                rows[idx]["conversation"] = text
            done += 1
            if done % 25 == 0:
                print(f"  {done}/{len(todo)}")

    with open(CONVERSATIONS_PATH, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    after = Counter(diagnose(r["conversation"]) for r in rows)
    print(f"\ncontinued {done}")
    print(f"  complete          {after[None]}  (was {before[None]})")
    print(f"  ends on tool      {after['tool']}")
    print(f"  ends on question  {after['question']}")
    print(f"  broken            {after['broken']}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", choices=["a", "b", "c"], required=True)
    parser.add_argument("--category", default=None,
                        help="Phase A only - restrict to one category")
    parser.add_argument("--dry-run", action="store_true",
                        help="Phase C only - report without spending")
    parser.add_argument("--limit", type=int, default=0,
                        help="Phase C only - continue at most N")
    args = parser.parse_args()

    if not os.getenv("OPENROUTER_API_KEY"):
        sys.exit("OPENROUTER_API_KEY not set - add it to your .env file.")

    if args.category and args.category not in CATEGORIES:
        sys.exit(
            f"Unknown category '{args.category}'. Options:\n  "
            + "\n  ".join(CATEGORIES)
        )

    if args.phase == "c":
        run_phase_c(dry_run=args.dry_run, limit=args.limit)
        return

    seeds = parse_seeds(SEEDS_PATH)
    print(f"Parsed {len(seeds)} seed conversations")
    print(f"Target total: {sum(CATEGORIES.values())} conversations\n")

    if args.phase == "a":
        run_phase_a(seeds, only_category=args.category)
    else:
        run_phase_b(seeds)


if __name__ == "__main__":
    main()