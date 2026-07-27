"""Generate synthetic training conversations for the Vigor fitness coach.

Two phases:
  A - generate diverse user first-messages ("scenarios") per category, then
      deduplicate them before spending tokens on full conversations.
  B - expand each surviving scenario into a full conversation, using seeds from
      the same category as few-shot exemplars.

Output is raw "=== " conversation text plus metadata, one JSON object per line.
Converting that into training JSONL is a separate step, so the converter can be
re-run without regenerating anything.

    python scripts/generate_dataset.py --phase a
    python scripts/generate_dataset.py --phase a --category Refusal
    python scripts/generate_dataset.py --phase b

Both phases are resumable - rerun the same command and finished work is skipped.
"""

import argparse
import json
import os
import random
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from anthropic import Anthropic
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Config - edit these freely
# ---------------------------------------------------------------------------
MODEL = "claude-sonnet-5"

SCENARIO_TEMPERATURE = 1.0      # high - we want spread
CONVERSATION_TEMPERATURE = 0.8  # lower - we want consistent execution
MAX_TOKENS = 4096               # tool results can be long
SCENARIOS_PER_CALL = 60
MAX_WORKERS = 8
MAX_RETRIES = 5
DEDUPE_THRESHOLD = 0.85         # Sorensen-Dice, same approach as Tessera
SEEDS_PER_PROMPT = 3

_PROJECT = Path(__file__).resolve().parent.parent
SEEDS_PATH = _PROJECT / "data" / "raw" / "curated_data.md"
TOOLS_PATH = _PROJECT / "configs" / "tools.json"
SCENARIOS_PATH = _PROJECT / "data" / "processed" / "scenarios.jsonl"
CONVERSATIONS_PATH = _PROJECT / "data" / "processed" / "conversations.jsonl"

# Category -> how many conversations to generate.
CATEGORIES = {
    "Plan Creation": 300,
    "Template Modification": 300,
    "Progress Analysis": 300,
    "Exercise History": 250,
    "Exercise Recommendation": 250,
    "Active Workout": 250,
    "Nutrition": 250,
    "Training Principles": 200,
    "Recovery": 150,
    "Constraints": 150,
    "Safety": 150,
    "Supplements": 100,
    "Refusal": 100,
    "Motivation": 80,
    "Empty state": 60,
    "Capability boundary": 60,
}

# What the generator actually reads. The CATEGORIES key is only a lookup key -
# never send it to the model on its own. A bare label like "Refusal" gets read
# as "write refusals" rather than "write messages that should be refused", which
# silently inverts an entire category.
CATEGORY_PROMPT_LABELS = {
    "Plan Creation":
        "asking for a training plan or workout templates to be built for them",
    "Template Modification":
        "asking to change a saved template - swapping exercises, adding or "
        "removing them, changing sets and reps, renaming or deleting it",
    "Progress Analysis":
        "asking whether they are making progress, answerable from their logged "
        "workouts, measurements or body stats",
    "Exercise History":
        "asking about their own past performance on one specific exercise",
    "Exercise Recommendation":
        "asking which exercises to do for a muscle, a goal, or the equipment "
        "they have available",
    "Active Workout":
        "sent DURING a workout that is currently in progress - changing the "
        "session on the spot, cutting it short, swapping a machine that is "
        "taken, adding or dropping sets",
    "Nutrition":
        "asking about food, protein, calories, or eating for their goal",
    "Training Principles":
        "asking how training works - rep ranges, rest times, frequency, "
        "progression, warmups, technique concepts",
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
        "asking the app to do something it is not able to do",
}

# Which seed categories to draw few-shot examples from, where substring matching
# on the CATEGORIES key would not find the right ones.
CATEGORY_SEED_MATCH = {
    "Constraints": ["user disagreement", "Exercise Recommendation"],
}

CAPABILITIES = """\
The app tracks resistance training only. It has:
- Workout templates: a named, ordered list of exercises with target sets and reps
- Logged workouts: sets, reps, weight, RPE, plus distance/time for cardio machines
- An exercise catalog tagged by body part, equipment and muscles
- Profile: age, gender, height, weight, body fat, activity level, estimated TDEE
- Body measurements with history
- Per-muscle training volume scores
- Optional menstrual cycle tracking
- Per-exercise rest timers

The app does NOT have:
- A calendar or schedule. Templates are not assigned to days, and there is no
  concept of a rest day, a training week, or a programme with dates
- Food or calorie logging, macro tracking, meal plans, or a food database
- Step counting, sleep tracking, heart rate, or any wearable integration
- GPS, outdoor running or any sport that is not logged as sets and reps
- Photos, social features, or sharing
- Paid tiers, group classes, or human coaching services

Messages must only reference things the app actually has. Do not write messages
about rescheduling days, planning around a time of day, logging meals, swimming
sessions, step counts, sleep data, or premium plans."""

CAPABILITY_BOUNDARY_HINT = """\
For THIS category specifically, the messages must ask for things the app cannot
do - scheduling rest days, logging what they ate, syncing a watch, tracking
steps or sleep, editing their profile for them, or logging sets on their behalf.
These should sound like reasonable requests a real user would make, not absurd
ones."""

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
1. Every <tool_call> is followed by a "=== TOOL ===" block with a realistic result.
   One TOOL block per tool call.
2. The assistant NEVER states a number that is not present in a tool result.
3. The assistant NEVER does arithmetic. Aggregates such as total_reps and volume
   are read from the tool result, not calculated.
4. Aggregates MUST match their sets array exactly. total_reps is the sum of reps,
   volume is the sum of reps x weight, top_weight is the heaviest weight used.
5. The assistant reports WHAT the data shows, never WHY it happened. Never write
   "your bulk went well" or "you have clearly been slacking" - the tool result
   contains numbers, not intent.
6. Not every message needs a tool call. If it can be answered from knowledge,
   answer it directly with no tool call at all.
7. Tool results must look like real logs - inconsistent, non-monotonic, messy.
   Never suspiciously clean progressions.
8. Exercise names written into a tool call must be plausible catalog entries.
9. In any conversation touching restriction, compensation or body image, the
   assistant gives NO numbers at all - no calorie targets, no deficit sizes, no
   rates of loss, not even a "safer" figure.
10. The assistant never claims the app can do something listed above as missing."""

# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------
load_dotenv()
_client = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
_write_lock = threading.Lock()


def _call(system, user, temperature):
    """One API call with retry on transient failures."""
    for attempt in range(MAX_RETRIES):
        try:
            msg = _client.messages.create(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                temperature=temperature,
                system=system,
                messages=[{"role": "user", "content": user}],
            )
            return "".join(b.text for b in msg.content if b.type == "text")
        except Exception as e:
            if attempt == MAX_RETRIES - 1:
                raise
            wait = (2 ** attempt) + random.random()
            print(f"    retry {attempt + 1}/{MAX_RETRIES} in {wait:.1f}s - {type(e).__name__}")
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
        sys.exit(f"No conversations parsed from {path} - check the '## Conversation' headers")
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
    # Falling back to every seed is nearly always wrong - say so loudly.
    print(f"    WARNING: no seeds matched '{category}' - falling back to all seeds")
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
- At least a third must be statements or commands rather than questions
- Many should have no question mark at all - people type fast on phones
- Include several that are only 3-5 words, and several that are 20 or more
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
            print(f"    WARNING: no prompt label for '{category}' - using the raw key")
            label = category

        cat_seeds = seeds_for(seeds, category)
        examples = "\n".join(
            f"- {m.group(1).strip()}"
            for s in random.sample(cat_seeds, min(4, len(cat_seeds)))
            if (m := re.search(r"=== USER ===\s*\n(.+)", s))
        )

        # Capability boundary is the one category that WANTS impossible requests.
        extra = (
            f"\n{CAPABILITY_BOUNDARY_HINT}"
            if category == "Capability boundary"
            else ""
        )

        pool = list(existing)
        rounds = 0
        while len(pool) < target and rounds < 20:
            rounds += 1
            batches = min(-(-(target - len(pool)) // SCENARIOS_PER_CALL), MAX_WORKERS)
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
                        SCENARIO_TEMPERATURE,
                    )
                    for _ in range(batches)
                ]
                for fut in as_completed(futures):
                    try:
                        lines = [l.strip() for l in fut.result().splitlines() if l.strip()]
                    except Exception as e:
                        print(f"    batch failed: {type(e).__name__}: {e}")
                        continue
                    # Strip any numbering or bullet the model added anyway.
                    lines = [re.sub(r"^\s*(?:[-*\u2022]|\d+[.)])\s*", "", l) for l in lines]
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
exactly one conversation in the required format, with no preamble, no commentary
and no markdown code fences around it."""

CONVERSATION_PROMPT = """\
Produce ONE training conversation for the user message at the bottom.

## What the app can do
{capabilities}

## Tool schema
The assistant may only call these tools, with exactly these parameters:

{tools}

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
  </tool_call>
The read_user_data result is an object keyed by scope name.
Start the output with "=== USER ===". Do not include a category header or title.

## Reference conversations in this category
{examples}

## Now generate
The user message belongs to this category: {category}
USER MESSAGE: {scenario}

The conversation may be a single exchange or run several turns, whichever is
natural for this message. Output only the conversation."""


def run_phase_b(seeds):
    if not TOOLS_PATH.exists():
        sys.exit(
            f"Tool definitions not found: {TOOLS_PATH}\n"
            "Create the JSON tool definitions file before running phase B - the "
            "generator needs the exact schema the model will see at inference."
        )
    tools = TOOLS_PATH.read_text(encoding="utf-8")

    scenarios = load_jsonl(SCENARIOS_PATH)
    if not scenarios:
        sys.exit("No scenarios found - run phase A first.")

    done = {r["scenario"] for r in load_jsonl(CONVERSATIONS_PATH)}
    todo = [s for s in scenarios if s["scenario"] not in done]
    print(f"{len(scenarios)} scenarios, {len(done)} already expanded, {len(todo)} to go\n")

    def work(item):
        category = item["category"]
        cat_seeds = seeds_for(seeds, category)
        picked = random.sample(cat_seeds, min(SEEDS_PER_PROMPT, len(cat_seeds)))
        prompt = CONVERSATION_PROMPT.format(
            tools=tools,
            voice=VOICE_RULES,
            capabilities=CAPABILITIES,
            correctness=CORRECTNESS_RULES,
            examples="\n\n---\n\n".join(picked),
            category=CATEGORY_PROMPT_LABELS.get(category, category),
            scenario=item["scenario"],
        )
        text = _call(CONVERSATION_SYSTEM, prompt, CONVERSATION_TEMPERATURE)
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


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", choices=["a", "b"], required=True)
    parser.add_argument("--category", default=None, help="Phase A only - one category")
    args = parser.parse_args()

    if not os.getenv("ANTHROPIC_API_KEY"):
        sys.exit("ANTHROPIC_API_KEY not set - add it to your .env file.")

    if args.category and args.category not in CATEGORIES:
        sys.exit(
            f"Unknown category '{args.category}'. Options:\n  "
            + "\n  ".join(CATEGORIES)
        )

    seeds = parse_seeds(SEEDS_PATH)
    print(f"Parsed {len(seeds)} seed conversations")
    print(f"Target total: {sum(CATEGORIES.values())} conversations\n")

    if args.phase == "a":
        run_phase_a(seeds, only_category=args.category)
    else:
        run_phase_b(seeds)


if __name__ == "__main__":
    main()