"""Verify DeepSeek V4 vision support by sending an existing screenshot."""

import base64
import glob
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from openai import OpenAI

from backend.app.config import settings

# 1. Find an existing screenshot
screenshots = glob.glob("data/screenshots/*/step_*.png")
if not screenshots:
    print("No screenshots found. Run a browser task first.")
    sys.exit(1)

screenshot_path = sorted(screenshots)[-1]  # latest
with open(screenshot_path, "rb") as f:
    screenshot_bytes = f.read()

print("=== DeepSeek V4 Vision Test ===")
print(f"Model (big): {settings.big_model}")
print(f"Model (small): {settings.small_model}")
print(f"Screenshot: {screenshot_path} ({len(screenshot_bytes)} bytes)")
print()

# 2. Test with two models
models_to_test = ["deepseek-chat", settings.big_model]
if settings.small_model not in models_to_test:
    models_to_test.append(settings.small_model)

client = OpenAI(
    api_key=settings.openai_api_key,
    base_url=settings.openai_base_url,
)

b64 = base64.b64encode(screenshot_bytes).decode("utf-8")

for model in models_to_test:
    print(f"--- Testing model: {model} ---")
    try:
        response = client.chat.completions.create(
            model=model,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
                        {
                            "type": "text",
                            "text": "Describe what you see on this webpage in 2-3 sentences.",
                        },
                    ],
                }
            ],
            max_tokens=300,
        )
        print(f"  Response: {response.choices[0].message.content[:200]}")
        usage = response.usage
        if usage:
            print(f"  Prompt tokens: {usage.prompt_tokens} (image est: {usage.prompt_tokens - 50})")
            print(f"  Completion tokens: {usage.completion_tokens}")
            if usage.prompt_tokens > 200:
                print(f"  Verdict: VISION WORKS for {model}")
            else:
                print("  Verdict: INCONCLUSIVE (low token count)")
        print()
    except Exception as e:
        print(f"  ERROR: {str(e)[:200]}")
        print()

print("=== Cost comparison ===")
print("For a browser task with 2 screenshots in plan + answer:")
for model in ["deepseek-chat", settings.big_model]:
    print(f"  {model}: ~1000 image tokens × 2 = ~2000 tokens extra per session")
