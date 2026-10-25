from flask import Flask, render_template, request, jsonify
import csv
import io
import json
import os
import re

app = Flask(__name__)


def normalize(s):
    """Normalize a string to a clean lowercase key (apostrophes dropped: "who's" -> "whos")."""
    s = re.sub(r"['\u2019`]", '', s.lower().strip())
    return re.sub(r'[^a-z0-9]+', '_', s).strip('_')


# Most specific fields first so generic words like "expense" don't steal their columns.
FIELD_PATTERNS = {
    'paid_by':     ['card', 'paid_by', 'who_paid', 'who_paid_for_it', 'payer', 'paid_by_whom',
                    'payment_method', 'payment', 'paid', 'credit_card', 'account'],
    'beneficiary': ['whose_expense', 'whos_expense', 'whos_expense_was_it', 'whose_expense_was_it',
                    'beneficiary', 'for_whom', 'whose', 'owner', 'person', 'expense_of',
                    'belongs_to', 'charged_to', 'for'],
    'date':        ['date', 'expense_date', 'transaction_date', 'date_of_expense', 'when', 'day'],
    'trip':        ['trip', 'trip_name', 'associated_trip', 'event', 'project',
                    'occasion', 'journey', 'vacation', 'holiday'],
    'amount':      ['amount', 'cost', 'price', 'total', 'sum', 'value', 'spend', 'spent'],
    'category':    ['category', 'cat', 'expense_type', 'type', 'group', 'section'],
    'name':        ['name', 'expense_name', 'description', 'item', 'title', 'what', 'expense'],
}


INCOME_PATTERNS = {
    'person': ['person', 'earner', 'received_by', 'whose', 'owner', 'who', 'name'],
    'date':   ['date', 'received_on', 'received', 'when', 'month'],
    'amount': ['amount', 'income', 'salary', 'total', 'value', 'pay'],
    'source': ['source', 'employer', 'company', 'payer', 'from'],
    'type':   ['type_of_income', 'income_type', 'type', 'category', 'kind'],
}


def detect_columns(headers, patterns=None):
    """Auto-detect which CSV columns map to expense (or income) fields.

    Pass 1 takes exact (normalized) header matches; pass 2 falls back to
    substring matches on whatever columns are still unclaimed.
    """
    patterns = patterns or FIELD_PATTERNS
    norm_map = {h: normalize(h) for h in headers}
    col_map = {}
    used = set()

    def claim(field, match):
        for kw in patterns[field]:
            for original, norm in norm_map.items():
                if original not in used and match(kw, norm):
                    col_map[field] = original
                    used.add(original)
                    return

    for field in patterns:
        claim(field, lambda kw, norm: kw == norm)
    for field in patterns:
        if field not in col_map:
            claim(field, lambda kw, norm: norm.startswith(kw + '_') or kw in norm.split('_')
                  or (len(kw) > 4 and kw in norm))

    return col_map


BUDGETS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'budgets.json')


@app.route('/budgets')
def budgets():
    """Default monthly budgets, read fresh from budgets.json on every request."""
    try:
        with open(BUDGETS_FILE, encoding='utf-8') as f:
            data = json.load(f)
    except FileNotFoundError:
        return jsonify({'budgets': {}})
    except ValueError as e:
        return jsonify({'budgets': {}, 'error': f'budgets.json is not valid JSON: {e}'})
    raw = data.get('monthly_budgets', {}) if isinstance(data, dict) else {}
    out = {}
    for cat, val in raw.items():
        try:
            if float(val) > 0:
                out[cat] = float(val)
        except (TypeError, ValueError):
            pass
    return jsonify({'budgets': out})


CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config.json')
DEFAULT_CONFIG = {
    'investment_category_keywords': ['invest', 'stock', 'mutual', 'etf', '401k', 'roth', 'brokerage', 'retirement', 'sip'],
    'subtract_investments_from_savings': True,
}


@app.route('/config')
def config():
    """Overview settings from config.json (falls back to defaults if missing/invalid)."""
    out = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_FILE, encoding='utf-8') as f:
            data = json.load(f)
        if isinstance(data, dict):
            for k in DEFAULT_CONFIG:
                if k in data:
                    out[k] = data[k]
    except (OSError, ValueError):
        pass
    return jsonify(out)


@app.route('/')
def index():
    return render_template('index.html')


def read_csv_upload():
    """Read the uploaded CSV. Returns (headers, rows, None) or (None, None, error_response)."""
    if 'file' not in request.files:
        return None, None, (jsonify({'error': 'No file uploaded'}), 400)

    file = request.files['file']
    if not file.filename:
        return None, None, (jsonify({'error': 'No file selected'}), 400)

    # Try multiple encodings
    content = None
    for enc in ['utf-8-sig', 'utf-8', 'latin-1', 'cp1252']:
        try:
            file.seek(0)
            content = file.read().decode(enc)
            break
        except (UnicodeDecodeError, AttributeError):
            continue

    if content is None:
        return None, None, (jsonify({'error': 'Cannot read file. Please save as UTF-8 CSV.'}), 400)

    try:
        reader = csv.DictReader(io.StringIO(content))
        headers = [h for h in (reader.fieldnames or []) if h and h.strip()]
        rows = []
        for row in reader:
            clean = {k: (v or '').strip() for k, v in row.items() if k and k.strip()}
            rows.append(clean)
    except Exception as e:
        return None, None, (jsonify({'error': f'CSV parse error: {e}'}), 400)

    if not rows:
        return None, None, (jsonify({'error': 'The CSV appears to be empty. Please check the file.'}), 400)

    return headers, rows, None


def upload_response(patterns):
    headers, rows, err = read_csv_upload()
    if err:
        return err
    return jsonify({
        'rows': rows,
        'headers': headers,
        'column_map': detect_columns(headers, patterns),
        'count': len(rows)
    })


@app.route('/upload', methods=['POST'])
def upload():
    return upload_response(FIELD_PATTERNS)


@app.route('/upload_income', methods=['POST'])
def upload_income():
    return upload_response(INCOME_PATTERNS)


if __name__ == '__main__':
    print("\n✅  Expense Dashboard running at: http://localhost:5001\n")
    app.run(debug=True, port=5001)
