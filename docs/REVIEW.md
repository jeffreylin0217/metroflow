# Checking the agent's answers

A small model can sound sure of itself and still get a number wrong, so I added a review step before I trust an answer. It's a LangGraph workflow for questions about daily retained trip totals. It reads date and trip_count from main.mart_weather_demand through the same SQL checks the agent uses, so the database stays read only. Each request covers 31 days at most.

The answer node sends the question and the daily rows to a model. Before paying for a second model call, I check its numbers in code. Any number that isn't in the rows gets a score of 0 and feedback right away. Otherwise a reviewer model checks the question, dates and values and returns a score and feedback as JSON.

A score of 0.8 or higher approves the answer. A lower score gets one retry with the reviewer's feedback. If the retry also scores low, the run pauses so I can type approve or reject. The terminal prints the answer, score, number of attempts and the decision.

## Run it

From the repo root, after setting up the venv from the main README:

    pip install -r requirements-review.txt

With ANTHROPIC_API_KEY set, this runs the review on Claude Haiku:

    python -m metroflow.review "Give the daily totals for March 15 and 16, 2025." \
      --start 2025-03-15 --end 2025-03-16

The default model is claude-haiku-5-5 through Anthropic's OpenAI compatible endpoint, using the same openai client as the agent. REVIEW_MODEL, REVIEW_BASE_URL and REVIEW_API_KEY change the defaults, and REVIEW_API_KEY falls back to ANTHROPIC_API_KEY. Haiku sees the question, the daily rows and the answer.

To run everything locally with Ollama and qwen3.5:4b:

    REVIEW_BASE_URL=http://localhost:11434/v1 REVIEW_MODEL=qwen3.5:4b \
      python -m metroflow.review "Give the daily totals for March 15 and 16, 2025." \
      --start 2025-03-15 --end 2025-03-16

A local endpoint doesn't need a key. For local Qwen I turn thinking off, because it was using the whole output budget before writing any JSON. The reviewer's JSON can be wrapped in extra text, and anything missing or invalid scores 0.

## Known limitations

* The code check only looks at numbers with three or more digits, after removing commas, and compares them to the daily counts and their total. Years in the range are skipped. It can't tell if a real number is matched to the wrong date, so date pairing depends on the reviewer model.
* There's only one retry, and the reviewer's score can still be wrong.
* The checkpointer lives in memory, so closing the CLI loses a paused run.
* It only handles daily retained trip totals.
