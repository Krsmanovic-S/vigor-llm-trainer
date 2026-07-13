from datasets import load_dataset

DATASETS = [
    "its-myrto/fitness-question-answers",
    "hammamwahab/fitness-qa",
    "chibbss/fitness-chat-prompt-completion-dataset",
    "PandurangMopgar/fitness__data",
    "onurSakar/GYM-Exercise",
]

for ds_id in DATASETS:
    print("=" * 70)
    print(f"DATASET: {ds_id}")
    print("=" * 70)
    try:
        ds = load_dataset(ds_id)
        for split_name in ds.keys():
            split = ds[split_name]
            print(f"Split '{split_name}': {len(split)} rows | columns: {split.column_names}")
        # Show 2 sample rows from the first split
        first = ds[list(ds.keys())[0]]
        for i in range(min(2, len(first))):
            print(f"\n--- sample row {i} ---")
            for k, v in first[i].items():
                v = str(v)
                print(f"[{k}]: {v[:400]}" + (" ...[truncated]" if len(v) > 400 else ""))
    except Exception as e:
        print(f"ERROR: {type(e).__name__}: {e}")
    print()