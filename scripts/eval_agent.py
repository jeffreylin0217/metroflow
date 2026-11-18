"""Fixed local execution eval; never calls a model during pytest or CI."""
import argparse
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sys
from threading import Timer

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from metroflow.agent.agent import ask
from metroflow.agent.guardrails import validate_sql
from metroflow.agent.query import WAREHOUSE, json_safe

CASES = [
    ('How many official taxi zones are in the lookup?',
     'SELECT count(*) FROM main.dim_zone'),
    ('How many loaded observation dates are in the weather demand mart?',
     'SELECT count(*) FROM main.mart_weather_demand'),
    ('How many retained trips are in the warehouse in total?',
     'SELECT sum(trip_count) FROM main.mart_weather_demand'),
    ('How many INVALID source records were excluded across all source months?',
     "SELECT sum(trip_count) FROM main.mart_quality WHERE quality_status='INVALID'"),
    ('How many retained trips had a Manhattan pickup in January 2025?',
     "SELECT sum(trip_count) FROM main.mart_borough_daily WHERE borough='Manhattan' "
     "AND date >= DATE '2025-01-01' AND date < DATE '2025-02-01'"),
    ('What was the highest recorded total amount in USD for a single day across all pickup locations?',
     'SELECT max(total_amount) FROM main.mart_weather_demand'),
    ('Show each loaded month as YYYY-MM with its total retained trip count.',
     "SELECT strftime(date, '%Y-%m'), sum(trip_count) FROM main.mart_weather_demand GROUP BY 1"),
    ('What was the average daily retained trip count on rainy days and on dry days? '
     'Use precipitation above zero for rainy and exactly zero for dry, excluding missing measurements.',
     "SELECT CASE WHEN precipitation_mm > 0 THEN 'rainy' ELSE 'dry' END, avg(trip_count) "
     'FROM main.mart_weather_demand WHERE precipitation_mm IS NOT NULL GROUP BY 1'),
    ('Which source month had the most SUSPICIOUS records, and how many?',
     "SELECT source_period, sum(trip_count) FROM main.mart_quality WHERE quality_status='SUSPICIOUS' "
     'GROUP BY 1 ORDER BY 2 DESC, 1 LIMIT 1'),
    ('How many distinct pickup zone keys had retained trips in January 2025?',
     "SELECT count(DISTINCT zone_key) FROM main.mart_zone_daily "
     "WHERE date >= DATE '2025-01-01' AND date < DATE '2025-02-01'"),
    ('Which pickup zone name had the most retained trips overall, and how many trips?',
     'SELECT z.zone, sum(m.trip_count) FROM main.mart_zone_daily m '
     'JOIN main.dim_zone z ON m.zone_key=z.zone_key GROUP BY z.zone '
     'ORDER BY 2 DESC, 1 LIMIT 1'),
    ('What was the average daily Manhattan pickup trip count on weekdays, excluding Saturday and Sunday?',
     "SELECT avg(m.trip_count) FROM main.mart_borough_daily m JOIN main.dim_date d ON m.date=d.date_key "
     "WHERE m.borough='Manhattan' AND NOT d.is_weekend"),
    ('How many retained trips in January 2025 went from JFK Airport to LaGuardia Airport?',
     'SELECT count(*) FROM main.fct_trips f JOIN main.dim_zone p ON f.pickup_zone_key=p.zone_key '
     'JOIN main.dim_zone d ON f.dropoff_zone_key=d.zone_key '
     "WHERE p.zone='JFK Airport' AND d.zone='LaGuardia Airport' "
     "AND f.pickup_date >= DATE '2025-01-01' AND f.pickup_date < DATE '2025-02-01'"),
    ('What was the trip-weighted average distance in miles for Manhattan pickups in January 2025? '
     'Use the zone daily mart and weight each daily zone average by its trip count.',
     'SELECT sum(m.average_trip_distance*m.trip_count)/sum(m.trip_count) '
     'FROM main.mart_zone_daily m JOIN main.dim_zone z ON m.zone_key=z.zone_key '
     "WHERE z.borough='Manhattan' AND m.date >= DATE '2025-01-01' AND m.date < DATE '2025-02-01'"),
    ('How many retained Brooklyn pickup trips happened on rainy weekend days? '
     'Join the borough demand, calendar, and weather marts; rainy means precipitation above zero.',
     'SELECT sum(b.trip_count) FROM main.mart_borough_daily b '
     'JOIN main.dim_date d ON b.date=d.date_key JOIN main.mart_weather_demand w ON b.date=w.date '
     "WHERE b.borough='Brooklyn' AND d.is_weekend AND w.precipitation_mm > 0"),
]


HELD_OUT_CASES = [
    ('What is the total recorded tip amount in USD for retained credit-card trips, where payment_type is 1?',
     'SELECT sum(tip_amount) FROM main.fct_trips WHERE payment_type=1'),
    ('What percentage of retained trips have a usable passenger count?',
     'SELECT 100.0*sum(trips_with_passenger_count)/sum(trip_count) FROM main.mart_zone_daily'),
    ('Give the date and minimum Celsius temperature for the coldest loaded observation; break ties by earliest date.',
     'SELECT date, min_temperature_c FROM main.mart_weather_demand WHERE min_temperature_c IS NOT NULL '
     'ORDER BY min_temperature_c, date LIMIT 1'),
    ('What is the total measured snowfall in millimeters across all loaded observation dates?',
     'SELECT sum(snowfall_mm) FROM main.mart_weather_demand'),
    ('How many source records were flagged for zero distance, across every quality status and source month?',
     'SELECT sum(zero_distance) FROM main.mart_quality'),
    ('Show the average recorded total amount in USD per retained pickup trip for each borough.',
     'SELECT borough, sum(total_amount)/sum(trip_count) FROM main.mart_borough_daily GROUP BY borough'),
    ('What is the longest retained trip duration in minutes?',
     'SELECT max(trip_duration_minutes) FROM main.fct_trips'),
    ('For each weekday name, how many weather measurements were rejected by quality flags across loaded observation dates?',
     'SELECT d.weekday_name, sum(w.quality_flagged_values) FROM main.mart_weather_demand w '
     'JOIN main.dim_date d ON w.date=d.date_key GROUP BY d.weekday_name'),
    ('List the distinct borough labels in the official zone lookup, alphabetically.',
     'SELECT DISTINCT borough FROM main.dim_zone ORDER BY borough'),
    ('What was the largest positive increase in retained trips from the previous observation, and on which date? '
     'Break ties by earliest date.',
     'SELECT date, change_from_previous_day FROM main.mart_weather_demand '
     'WHERE change_from_previous_day > 0 ORDER BY change_from_previous_day DESC, date LIMIT 1'),
]


def case_hash(cases):
    text = json.dumps(cases, ensure_ascii=False, separators=(',', ':'))
    digest = hashlib.sha256(text.encode())
    return digest.hexdigest()


def same_value(left, right):
    if left is None or right is None:
        return left is right
    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is type(right) and left == right
    if isinstance(left, (int, float, Decimal)) and isinstance(right, (int, float, Decimal)):
        left = Decimal(str(left))
        right = Decimal(str(right))
        if not left.is_finite() or not right.is_finite():
            return False
        difference = abs(left-right)
        return difference <= Decimal('0.01')
    return left == right


def same_rows(expected, actual):
    if len(expected) != len(actual):
        return False
    matches = {}

    def assign(index, seen):
        for candidate, row in enumerate(actual):
            if candidate in seen:
                continue
            equal = True
            for left, right in zip(expected[index], row):
                if not same_value(left, right):
                    equal = False
                    break
            if not equal:
                continue
            seen.add(candidate)
            if candidate not in matches or assign(matches[candidate], seen):
                matches[candidate] = index
                return True
        return False

    for index in range(len(expected)):
        if not assign(index, set()):
            return False
    return True


def compare(expected, actual, expected_width, actual_width):
    if len(expected) != len(actual):
        return False, f'row count differs: expected {len(expected)}, got {len(actual)}'
    if actual_width < expected_width:
        return False, f'column count differs: expected at least {expected_width}, got {actual_width}'
    candidates = []
    for column in range(expected_width):
        values = [(row[column],) for row in expected]
        choices = []
        for other in range(actual_width):
            other_values = [(row[other],) for row in actual]
            if same_rows(values, other_values):
                choices.append(other)
        candidates.append(choices)

    def choose(columns):
        if len(columns) == expected_width:
            projected = [tuple(row[index] for index in columns) for row in actual]
            return same_rows(expected, projected)
        for column in candidates[len(columns)]:
            if column not in columns and choose(columns + [column]):
                return True
        return False

    matched = choose([])
    reason = ''
    if not matched:
        reason = 'no consistent column mapping matches all values within 0.01'
    return matched, reason


def execute(connection, sql):
    timer = Timer(20, connection.interrupt)
    timer.daemon = True
    timer.start()
    try:
        cursor = connection.execute(sql)
        width = len(cursor.description)
        return cursor.fetchall(), width
    finally:
        timer.cancel()
        timer.join()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', type=Path, default=WAREHOUSE)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--set', dest='eval_set', choices=['original', 'held-out'], default='original')
    args = parser.parse_args()
    cases = CASES
    if args.eval_set == 'held-out':
        cases = HELD_OUT_CASES
    digest = case_hash(cases)
    print(f'Eval set: {args.eval_set}; question SHA-256: {digest}', flush=True)
    records = []
    with duckdb.connect(str(args.database), read_only=True,
                        config={'enable_external_access': False}) as connection:
        references = [execute(connection, sql) for _, sql in cases]
        for number, case in enumerate(cases, 1):
            question, correct_sql = case
            expected, width = references[number-1]
            result = ask(question, args.database)
            actual = []
            reason = result['error'] or 'no final SQL'
            passed = False
            if result['sql']:
                try:
                    sql = validate_sql(result['sql'])
                    actual, actual_width = execute(connection, sql)
                    passed, reason = compare(expected, actual, width, actual_width)
                except Exception as exc:
                    reason = str(exc)
            record = {
                'number': number,
                'question': question,
                'correct_sql': correct_sql,
                'expected': json_safe(expected),
                'actual': json_safe(actual),
                **result,
                'passed': passed,
                'failure_reason': reason,
            }
            records.append(record)
            status = 'FAIL'
            if passed:
                status = 'PASS'
            print(f'{number:02d} {status} ({result["attempts"]} attempts) {question}', flush=True)
            if not passed:
                print(f'   {reason}\n   Expected: {expected}\n   Actual: {actual}', flush=True)
            for index, attempt in enumerate(result['history'], 1):
                print(f'   Attempt {index}: {attempt["sql"]}\n   Error: {attempt["error"]}', flush=True)
    passed = 0
    for record in records:
        passed += record['passed']
    accuracy = 100*passed/len(records)
    print(f'Execution accuracy: {passed}/{len(records)} ({accuracy:.1f}%)', flush=True)
    if args.output:
        summary = {
            'evaluated_at': datetime.now(timezone.utc).isoformat(),
            'eval_set': args.eval_set,
            'question_sha256': digest,
            'accuracy': accuracy,
            'passed': passed,
            'total': len(records),
            'cases': records,
        }
        text = json.dumps(summary, indent=2) + '\n'
        args.output.write_text(text)


if __name__ == '__main__':
    main()
