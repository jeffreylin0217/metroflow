from datetime import date
from types import SimpleNamespace

import duckdb
from langgraph.types import Command
import pytest

from metroflow.review import __main__ as cli
from metroflow.review import graph as review
from metroflow.review import model
from tests.test_agent_guardrails import FakeClient, FakeMessage

ANSWER = 'March 15, 2025: 1,200 trips. March 16, 2025: 2,300 trips. Total: 3,500 trips.'
GOOD = '{"score": 0.9, "feedback": "The dates and totals match."}'
LOW = '{"score": 0.3, "feedback": "Name both dates clearly."}'


@pytest.fixture
def database(tmp_path):
    path = tmp_path/'warehouse.duckdb'
    with duckdb.connect(str(path)) as connection:
        connection.execute('CREATE TABLE mart_weather_demand(date DATE, trip_count BIGINT)')
        connection.execute("INSERT INTO mart_weather_demand VALUES "
                           "('2025-03-14', 9999), ('2025-03-15', 1200), ('2025-03-16', 2300)")
    return path


@pytest.fixture
def state():
    return {'question': 'Give daily totals for March 15 and 16, 2025, and their sum.',
            'draft': {'answer': '', 'sql': None, 'rows': [], 'attempts': 0, 'feedback': ''},
            'score': 0.0, 'decision': 'needs_review'}


@pytest.fixture
def config():
    return {'configurable': {'thread_id': 'review-test'}}


def client_with(*texts):
    return FakeClient([FakeMessage(content=text) for text in texts])


def test_approved(database, state, config):
    client = client_with(ANSWER, GOOD)
    graph = review.build_graph('2025-03-15', '2025-03-16', database, client)
    result = graph.invoke(state, config)
    assert result['decision'] == 'approved' and result['score'] == 0.9
    assert result['draft']['attempts'] == 1 and len(client.requests) == 2
    assert result['draft']['rows'] == [['2025-03-15', 1200], ['2025-03-16', 2300]]
    assert result['draft']['sql'].endswith('LIMIT 200')
    assert set(graph.get_state(config).values) == set(review.ReviewState.__annotations__)


def test_retry(database, state, config):
    threshold = '{"score": 0.8, "feedback": "Enough detail."}'
    client = client_with(ANSWER, LOW, ANSWER, threshold)
    graph = review.build_graph('2025-03-15', '2025-03-16', database, client)
    result = graph.invoke(state, config)
    assert result['decision'] == 'approved' and result['draft']['attempts'] == 2
    assert len(client.requests) == 4
    assert 'Name both dates clearly.' in client.requests[2]['messages'][1]['content']


@pytest.mark.parametrize('choice, decision', [('approve', 'approved_by_human'), ('reject', 'rejected')])
def test_human(database, state, config, choice, decision):
    client = client_with(ANSWER, LOW, ANSWER, LOW)
    graph = review.build_graph('2025-03-15', '2025-03-16', database, client)
    paused = graph.invoke(state, config)
    assert paused['decision'] == 'needs_review' and paused['draft']['attempts'] == 2
    payload = paused['__interrupt__'][0].value
    assert payload == {'question': state['question'], 'answer': ANSWER, 'score': 0.3,
                       'feedback': 'Name both dates clearly.'}
    result = graph.invoke(Command(resume=choice), config)
    assert result['decision'] == decision and len(client.requests) == 4
    assert set(result) == {'question', 'draft', 'score', 'decision'}


def test_unknown_number(database, state, config):
    client = client_with('March 15 had 999 trips.', 'March 15 had 999 trips.')
    graph = review.build_graph('2025-03-15', '2025-03-16', database, client)
    result = graph.invoke(state, config)
    assert result['score'] == 0 and '999' in result['draft']['feedback']
    assert len(client.requests) == 2 and '__interrupt__' in result
    assert '999' in client.requests[1]['messages'][1]['content']


@pytest.mark.parametrize('answer', [ANSWER, 'March 15: 1200. March 16: 2300. Sum: 3500.'])
def test_numbers(database, state, config, answer):
    client = client_with(answer, GOOD)
    graph = review.build_graph('2025-03-15', '2025-03-16', database, client)
    result = graph.invoke(state, config)
    assert result['decision'] == 'approved' and len(client.requests) == 2


def test_wrong_date(database, state, config):
    wrong = 'March 15, 2025: 2300 trips. March 16, 2025: 1200 trips.'
    assert review.check_numbers(wrong, [['2025-03-15', 1200], ['2025-03-16', 2300]],
                                date(2025, 3, 15), date(2025, 3, 16)) == ''
    client = client_with(wrong, LOW, ANSWER, GOOD)
    graph = review.build_graph('2025-03-15', '2025-03-16', database, client)
    result = graph.invoke(state, config)
    assert result['draft']['attempts'] == 2 and result['decision'] == 'approved'
    request = client.requests[1]['messages']
    assert wrong in request[1]['content']
    assert 'match every number to the date it is tied to' in request[0]['content']


@pytest.mark.parametrize('reply', ['not JSON', '[]', 'prefix {broken} suffix', '{"score": 2, "feedback": "bad"}',
                                  '{"score": true, "feedback": "bad"}',
                                  '{"score": 0.9, "feedback": null}',
                                  '{"score": NaN, "feedback": "bad"}'])
def test_bad_review(database, state, config, reply):
    client = client_with(ANSWER, reply, ANSWER, reply)
    graph = review.build_graph('2025-03-15', '2025-03-16', database, client)
    result = graph.invoke(state, config)
    assert result['score'] == 0 and result['decision'] == 'needs_review'
    assert '__interrupt__' in result and result['draft']['attempts'] == 2
    assert 'valid JSON' in result['draft']['feedback']


@pytest.mark.parametrize('start, end', [('bad', '2025-03-31'), ('2025-02-30', '2025-03-01'),
                                       ('2025-03-01', 'bad'), ('2025-03-02', '2025-03-01'),
                                       ('2025-03-01', '2025-04-01')])
def test_bad_dates(database, monkeypatch, start, end):
    def no_client():
        pytest.fail('dates must be checked before building a model client')
    monkeypatch.setattr(review, 'make_client', no_client)
    with pytest.raises(ValueError):
        review.build_graph(start, end, database)


def test_date_limit():
    assert review.validate_dates('2025-03-01', '2025-03-31') == (date(2025, 3, 1), date(2025, 3, 31))
    assert review.validate_dates('2025-03-15', '2025-03-15') == (date(2025, 3, 15), date(2025, 3, 15))


def test_thinking(database, state, config):
    client = client_with('<think>9999 is not needed.</think>' + ANSWER,
                         '<think>Check dates.</think>\n```json\n' + GOOD + '\n```')
    graph = review.build_graph('2025-03-15', '2025-03-16', database, client)
    result = graph.invoke(state, config)
    assert result['decision'] == 'approved' and result['draft']['answer'] == ANSWER
    assert '<think>' not in client.requests[1]['messages'][1]['content']


@pytest.mark.parametrize('key, fallback, url, expected', [
    ('review-test', 'fallback-test', 'https://api.anthropic.com/v1/', 'review-test'),
    ('', 'fallback-test', 'https://api.anthropic.com/v1/', 'fallback-test'),
    ('', '', 'http://localhost:11434/v1', 'ollama'),
])
def test_client(monkeypatch, key, fallback, url, expected):
    monkeypatch.setenv('REVIEW_API_KEY', key)
    monkeypatch.setenv('ANTHROPIC_API_KEY', fallback)
    monkeypatch.setenv('REVIEW_BASE_URL', url)
    monkeypatch.setattr(model, 'OpenAI', lambda **kwargs: SimpleNamespace(**kwargs))
    client = model.make_client()
    assert client.api_key == expected and client.base_url == url
    assert client.timeout == 90 and client.max_retries == 0


def test_model_env(database, state, config, monkeypatch):
    monkeypatch.setenv('REVIEW_MODEL', 'local-review')
    client = client_with(ANSWER, GOOD)
    graph = review.build_graph('2025-03-15', '2025-03-16', database, client)
    graph.invoke(state, config)
    assert all(request['model'] == 'local-review' for request in client.requests)


def test_query_error(tmp_path, state, config):
    client = client_with()
    graph = review.build_graph('2025-03-15', '2025-03-16', tmp_path/'missing.duckdb', client)
    with pytest.raises(ValueError, match='database'):
        graph.invoke(state, config)
    assert not client.requests


def test_cli(database, monkeypatch, capsys):
    client = client_with(ANSWER, LOW, ANSWER, LOW)
    monkeypatch.setattr(cli, 'build_graph', lambda start, end, path:
                        review.build_graph(start, end, path, client))
    monkeypatch.setattr('sys.argv', ['review', 'Daily totals?', '--start', '2025-03-15',
                                    '--end', '2025-03-16', '--database', str(database)])
    monkeypatch.setattr('builtins.input', lambda prompt: 'approve')
    assert cli.main() == 0
    output = capsys.readouterr().out
    assert ANSWER in output and 'Score: 0.3' in output
    assert 'Attempts: 2' in output and 'Decision: approved_by_human' in output


def test_wrapped_json(database, state, config):
    client = client_with(ANSWER, 'Here is my review:\n' + GOOD + '\nThat is the result.')
    graph = review.build_graph('2025-03-15', '2025-03-16', database, client)
    result = graph.invoke(state, config)
    assert result['decision'] == 'approved' and result['score'] == 0.9
    assert result['draft']['attempts'] == 1 and len(client.requests) == 2


@pytest.mark.parametrize('url, name, disabled', [
    ('http://localhost:11434/v1', 'qwen3.5:4b', True),
    ('http://localhost:11434/v1', 'llama3.2:3b', False),
    ('https://api.anthropic.com/v1/', model.DEFAULT_MODEL, False),
])
def test_thinking_setting(monkeypatch, url, name, disabled):
    monkeypatch.setenv('REVIEW_BASE_URL', url)
    client = client_with('answer')
    model.call_model(client, name, 'instructions', 'question')
    assert (client.requests[0].get('reasoning_effort') == 'none') == disabled
