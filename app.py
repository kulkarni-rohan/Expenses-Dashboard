from flask import Flask, Response, abort, jsonify, render_template, request, send_file
import csv
import io
import json
import os
import re

import receipts

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


PAYMENT_PATTERNS = {
    'payee':  ['to_whom', 'paid_to', 'payee', 'recipient', 'received_by', 'to'],
    'payer':  ['who_paid', 'paid_by', 'payer', 'from'],
    'date':   ['date', 'paid_on', 'when'],
    'amount': ['amount', 'total', 'value', 'sum'],
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


CARD_REWARDS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'card_rewards.json')


@app.route('/card_rewards')
def card_rewards():
    """Card rewards policy from card_rewards.json (re-read on every request so edits show after a reload)."""
    try:
        with open(CARD_REWARDS_FILE, encoding='utf-8') as f:
            return jsonify(json.load(f))
    except FileNotFoundError:
        return jsonify({'cards': [], 'error': 'card_rewards.json not found'})
    except ValueError as e:
        return jsonify({'cards': [], 'error': f'card_rewards.json is not valid JSON: {e}'})


@app.route('/')
def index():
    return render_template('index.html')


def parse_csv_bytes(raw):
    """Decode and parse CSV bytes. Returns (headers, rows); raises ValueError with a user-facing message."""
    content = None
    for enc in ['utf-8-sig', 'utf-8', 'latin-1', 'cp1252']:
        try:
            content = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if content is None:
        raise ValueError('Cannot read file. Please save as UTF-8 CSV.')

    try:
        reader = csv.DictReader(io.StringIO(content))
        headers = [h for h in (reader.fieldnames or []) if h and h.strip()]
        rows = []
        for row in reader:
            clean = {k: (v or '').strip() for k, v in row.items() if k and k.strip()}
            rows.append(clean)
    except Exception as e:
        raise ValueError(f'CSV parse error: {e}')

    if not rows:
        raise ValueError('The CSV appears to be empty. Please check the file.')
    return headers, rows


def read_csv_upload():
    """Read the uploaded CSV. Returns (headers, rows, None) or (None, None, error_response)."""
    if 'file' not in request.files:
        return None, None, (jsonify({'error': 'No file uploaded'}), 400)

    file = request.files['file']
    if not file.filename:
        return None, None, (jsonify({'error': 'No file selected'}), 400)

    try:
        headers, rows = parse_csv_bytes(file.read())
    except ValueError as e:
        return None, None, (jsonify({'error': str(e)}), 400)
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


ARTIFACTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'artifacts')
os.makedirs(ARTIFACTS_DIR, exist_ok=True)  # git-ignored (see .gitignore): holds your private CSVs

EXPENSE_ONLY = ('name', 'paid_by', 'beneficiary', 'category', 'trip')
INCOME_ONLY = ('source', 'type', 'person')


def kind_scores(headers):
    """How much a header row looks like an expenses file vs an income file."""
    exp = len([f for f in detect_columns(headers, FIELD_PATTERNS) if f in EXPENSE_ONLY])
    inc = len([f for f in detect_columns(headers, INCOME_PATTERNS) if f in INCOME_ONLY])
    joined = ' '.join(normalize(h) for h in headers)
    if 'income' in joined or 'salary' in joined:
        inc += 3
    if 'expense' in joined or 'paid' in joined or 'card' in joined:
        exp += 3
    return exp, inc


@app.route('/artifacts_data')
def artifacts_data():
    """Auto-load the CSVs dropped into artifacts/, working out at runtime which is expenses and which is income."""
    found = []
    for name in sorted(os.listdir(ARTIFACTS_DIR)):
        if not name.lower().endswith('.csv') or name.startswith('.'):
            continue
        try:
            with open(os.path.join(ARTIFACTS_DIR, name), 'rb') as f:
                headers, rows = parse_csv_bytes(f.read())
        except (OSError, ValueError) as e:
            found.append({'fileName': name, 'error': str(e)})
            continue
        exp, inc = kind_scores(headers)
        found.append({'fileName': name, 'headers': headers, 'rows': rows, 'count': len(rows), 'exp': exp, 'inc': inc})

    good = [f for f in found if 'error' not in f]
    out = {'expense': None, 'income': None, 'payments': None,
           'warnings': [f"{f['fileName']}: {f['error']}" for f in found if 'error' in f]}

    def payload(f, patterns):
        return {k: f[k] for k in ('fileName', 'headers', 'rows', 'count')} | {'column_map': detect_columns(f['headers'], patterns)}

    # Person-to-person payment log: has an explicit "to whom"/payee column plus a payer column.
    def is_payments(f):
        cm = detect_columns(f['headers'], PAYMENT_PATTERNS)
        return all(k in cm for k in ('payee', 'payer', 'amount')) and normalize(cm['payee']) in PAYMENT_PATTERNS['payee']

    pay = next((f for f in good if is_payments(f)), None)
    if pay:
        out['payments'] = payload(pay, PAYMENT_PATTERNS)
        good = [f for f in good if f is not pay]

    if len(good) == 1:
        f = good[0]
        kind = 'expense' if f['exp'] >= f['inc'] else 'income'
        out[kind] = payload(f, FIELD_PATTERNS if kind == 'expense' else INCOME_PATTERNS)
    elif len(good) >= 2:
        # best-scoring expenses file, then best-scoring income file among the rest
        e = max(good, key=lambda f: f['exp'] - f['inc'])
        rest = [f for f in good if f is not e]
        i = max(rest, key=lambda f: f['inc'] - f['exp'])
        out['expense'], out['income'] = payload(e, FIELD_PATTERNS), payload(i, INCOME_PATTERNS)
        ignored = [f['fileName'] for f in good if f is not e and f is not i]
        if ignored:
            out['warnings'].append('Ignored extra CSVs: ' + ', '.join(ignored))
    return jsonify(out)


RECEIPTS_DIR = os.environ.get('EXPENSE_RECEIPTS_DIR') or os.path.join(os.path.dirname(os.path.abspath(__file__)), 'receipts')
os.makedirs(RECEIPTS_DIR, exist_ok=True)  # git-ignored (see .gitignore): holds your private receipt photos / PDFs
_receipt_paths = {}


@app.route('/receipts_data')
def receipts_data():
    """Read every receipt in receipts/ (OCR results are cached on disk) and return parsed grocery line items."""
    data = receipts.scan_receipts(RECEIPTS_DIR)
    _receipt_paths.clear()
    _receipt_paths.update({r['id']: os.path.join(RECEIPTS_DIR, r['file']) for r in data['receipts']})
    return jsonify(data)


def _receipt_path(rid):
    if rid not in _receipt_paths:
        receipts_data()
    path = _receipt_paths.get(rid)
    if not path or not os.path.abspath(path).startswith(os.path.abspath(RECEIPTS_DIR) + os.sep):
        abort(404)
    return path


@app.route('/receipt_file/<rid>')
def receipt_file(rid):
    """Serve the original receipt (HEIC photos are converted to JPEG so browsers can show them)."""
    path = _receipt_path(rid)
    if not request.args.get('original'):      # default: the receipt cut out of its background
        crop = receipts.cropped_path(path, os.path.join(RECEIPTS_DIR, '.cache'))
        if crop:
            return send_file(crop)
    if os.path.splitext(path)[1].lower() in ('.heic', '.heif'):
        return Response(receipts.jpeg_bytes(path), mimetype='image/jpeg')
    return send_file(path)


@app.route('/receipt_text/<rid>')
def receipt_text(rid):
    """The text the parser actually saw for this receipt (handy for spotting OCR mistakes)."""
    return Response(receipts.raw_text(_receipt_path(rid), RECEIPTS_DIR), mimetype='text/plain; charset=utf-8')


@app.route('/upload', methods=['POST'])
def upload():
    return upload_response(FIELD_PATTERNS)


@app.route('/upload_income', methods=['POST'])
def upload_income():
    return upload_response(INCOME_PATTERNS)


if __name__ == '__main__':
    print("\n✅  Expense Dashboard running at: http://localhost:5001\n")
    app.run(debug=True, port=5001)
