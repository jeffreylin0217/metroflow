"""Ask about daily totals and review the answer."""
import argparse
from pathlib import Path
import sys
from uuid import uuid4

from langgraph.types import Command
from openai import APIError

from metroflow.agent.query import WAREHOUSE
from metroflow.review.graph import build_graph


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('question')
    parser.add_argument('--start', required=True)
    parser.add_argument('--end', required=True)
    parser.add_argument('--database', type=Path, default=WAREHOUSE)
    args = parser.parse_args()
    try:
        graph = build_graph(args.start, args.end, args.database)
        state = {'question': args.question,
                 'draft': {'answer': '', 'sql': None, 'rows': [], 'attempts': 0, 'feedback': ''},
                 'score': 0.0, 'decision': 'needs_review'}
        config = {'configurable': {'thread_id': str(uuid4())}}
        result = graph.invoke(state, config)
        print(result['draft']['answer'], flush=True)
        print(f'Score: {result["score"]}', flush=True)
        print(f'Attempts: {result["draft"]["attempts"]}', flush=True)
        print(f'Decision: {result["decision"]}', flush=True)
        if '__interrupt__' in result:
            print(f'Feedback: {result["draft"]["feedback"]}', flush=True)
            choice = input('approve or reject: ').strip().lower()
            while choice not in {'approve', 'reject'}:
                choice = input('approve or reject: ').strip().lower()
            result = graph.invoke(Command(resume=choice), config)
            print(f'Decision: {result["decision"]}', flush=True)
    except (ValueError, APIError, EOFError) as exc:
        parser.exit(1, str(exc) + '\n')
    return 0


if __name__ == '__main__':
    sys.exit(main())
