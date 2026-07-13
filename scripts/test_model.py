import os, torch
from threading import Thread
from dotenv import load_dotenv
from transformers import AutoModelForCausalLM, AutoTokenizer, TextIteratorStreamer, BitsAndBytesConfig
import gradio as gr

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
MODEL_ID = "Qwen/Qwen2.5-1.5B-Instruct"

SYSTEM_PROMPT = """
    You are a knowledgeable personal fitness and nutrition coach. Give accurate, practical, and concise advice to any 
    questions regarding fitness, bodybuilding, exercises, nutrition and supplements. Never suggest or explain any kind
    of steroid use or give advice on illegal substances.

    If a question pertains to a topic not related to fitness tell the user that you are a fitness coach, not a general
    knowledge LLM.

    If you do not know an answer to a question, say so immidiatelly. If you need more information from the user, respond
    with only the questions you have for them. 
"""

MAX_NEW_TOKENS = 128

# ---------------------------------------------------------------------------
# Load once at module level so chat() is cheap to call repeatedly
# ---------------------------------------------------------------------------
load_dotenv()
# The base model is public, so a token is optional here. It is used if present
# (needed for gated models and helps avoid download rate limits).
HF_TOKEN = os.getenv("HF_TOKEN") or None

_device = "cuda" if torch.cuda.is_available() else "cpu"
print(f"Loading {MODEL_ID} on {_device} (first run downloads ~3GB) ...")

quant_config = BitsAndBytesConfig(
    load_in_4bit=True,
    bnb_4bit_use_double_quant=True,
    bnb_4bit_compute_dtype=torch.bfloat16,
    bnb_4bit_quant_type="nf4",
)

tokenizer = AutoTokenizer.from_pretrained(MODEL_ID, token=HF_TOKEN)
model = AutoModelForCausalLM.from_pretrained(
    MODEL_ID,
    quantization_config=quant_config,
    torch_dtype="auto",
    device_map="auto",
    token=HF_TOKEN,
)

print("Model loaded.\n")


def chat(message, history):
    """Stream one reply given the new message and prior turns."""
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]

    # Gradio 6 may hand back content as a list of blocks - flatten to text.
    for turn in history:
        content = turn["content"]
        if isinstance(content, list):
            content = " ".join(
                p.get("text", "") for p in content if isinstance(p, dict)
            )
        messages.append({"role": turn["role"], "content": content})

    messages.append({"role": "user", "content": message})

    text = tokenizer.apply_chat_template(
        messages, tokenize=False, add_generation_prompt=True
    )
    inputs = tokenizer([text], return_tensors="pt").to(model.device)

    streamer = TextIteratorStreamer(
        tokenizer, skip_prompt=True, skip_special_tokens=True
    )
    generation_kwargs = dict(
        **inputs,
        streamer=streamer,
        max_new_tokens=MAX_NEW_TOKENS,
    )

    thread = Thread(target=model.generate, kwargs=generation_kwargs)
    thread.start()

    reply = ""
    for token in streamer:
        reply += token
        yield reply


# ---------------------------------------------------------------------------
# Minimal Gradio chatbox
# ---------------------------------------------------------------------------
demo = gr.ChatInterface(
    fn=chat,
    title="Vigor Coach - base model test",
)

if __name__ == "__main__":
    demo.launch()