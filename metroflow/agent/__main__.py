"""Ask a warehouse question from the repository root."""
import argparse
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('question')
    parser.add_argument('--show-sql', action='store_true')
    args = parser.parse_args()
    try:
        from metroflow.agent.agent import ask
    except ImportError as exc:
        message = 'Install optional dependencies: .venv/bin/python -m pip install -r requirements-agent.txt\n'
        parser.exit(1, message + str(exc) + '\n')
    result = ask(args.question)
    print(result['answer'])
    if args.show_sql:
        print(f'\nSQL: {result["sql"] or "(none)"}\nAttempts: {result["attempts"]}')
    return 1 if result['error'] else 0


if __name__ == '__main__':
    sys.exit(main())
