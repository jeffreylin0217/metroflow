"""Text calls to a compatible review model."""
import os
import re
from urllib.parse import urlparse

from openai import OpenAI

DEFAULT_MODEL = 'claude-haiku-5-5'


def make_client():
    base_url = os.getenv('REVIEW_BASE_URL', 'https://api.anthropic.com/v1/')
    api_key = os.getenv('REVIEW_API_KEY') or os.getenv('ANTHROPIC_API_KEY')
    if not api_key and urlparse(base_url).hostname in {'localhost', '127.0.0.1', '::1'}:
        api_key = 'ollama'
    if not api_key:
        raise ValueError('Set REVIEW_API_KEY or ANTHROPIC_API_KEY.')
    return OpenAI(base_url=base_url, api_key=api_key, timeout=90, max_retries=0)


def call_model(client, model, instructions, prompt):
    messages = [
        {'role': 'system', 'content': instructions},
        {'role': 'user', 'content': prompt},
    ]
    options = {}
    host = urlparse(os.getenv('REVIEW_BASE_URL', '')).hostname
    if host in {'localhost', '127.0.0.1', '::1'} and model.startswith('qwen'):
        options['reasoning_effort'] = 'none'
    response = client.chat.completions.create(model=model, messages=messages,
                                              max_tokens=1800, **options)
    text = response.choices[0].message.content or ''
    text = re.sub(r'<think>.*?(?:</think>|$)', '', text, flags=re.DOTALL | re.IGNORECASE)
    text = text.strip()
    if text.startswith('```') and text.endswith('```'):
        text = '\n'.join(text.splitlines()[1:-1]).strip()
    return text
