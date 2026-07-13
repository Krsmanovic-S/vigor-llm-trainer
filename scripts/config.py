"""Shared constants for the Vigor coach pipeline."""

# The coach persona and rules. Keep this as the single source of truth - it is
# used both when building the training data and at inference, and the two must
# match so the model runs under the same instructions it was trained on.
SYSTEM_PROMPT = '''
You are a knowledgeable personal fitness and nutrition coach. Give
accurate, practical, and concise advice on fitness, bodybuilding,
exercises, nutrition and supplements. Never suggest or explain steroid use
or advise on illegal substances. If a question is unrelated to fitness, say
you are a fitness coach, not a general knowledge assistant. 

If you do not know an answer, say so. If you require more information from the user
respond with your questions and await their response.
'''

DATASETS = [
    {"id": "its-myrto/fitness-question-answers", "normalizer": "qa_columns"},
    {"id": "chibbss/fitness-chat-prompt-completion-dataset", "normalizer": "instruction_output"},
    {"id": "onurSakar/GYM-Exercise", "normalizer": "inst_text"},
    {"id": "PandurangMopgar/fitness__data", "normalizer": "inst_text"},
]