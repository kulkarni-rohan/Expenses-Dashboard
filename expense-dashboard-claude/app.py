from flask import Flask, render_template, request, jsonify
import csv
import io
import re

app = Flask(__name__)


def normalize(s):
    """Normalize a string to a clean lowercase key."""
    return re.sub(r'[^a-z0-9]+', '_', s.lower().strip()).strip('_')


def detect_columns(headers):
    """Auto-detect which CSV columns map to expense fields."""
    field_patterns = {
        'name':        ['name', 'expense_name', 'description', 'item', 'title', 'what', 'expense'],
        'amount':      ['amount', 'cost', 'price', 'total', 'sum', 'value', 'spend', 'spent'],
        'category':    ['category', 'cat', 'type', 'expense_type', 'group', 'section'],
        'paid_by':     ['paid_by', 'who_paid', 'payer', 'paid_by_whom', 'paid', 'who_paid_for_it'],
        'beneficiary': ['whose_expense', 'beneficiary', 'for_whom', 'whose', 'owner',
                        'person', 'expense_of', 'belongs_to', 'charged_to', 'for'],
        'date':        ['date', 'expense_date', 'when', 'transaction_date', 'date_of_expense', 'day'],
        'trip':        ['trip', 'trip_name', 'associated_trip', 'event', 'project',
                        'occasion', 'journey', 'vacation', 'holiday'],
    }

    norm_map = {normalize(h): h for h in headers}
    col_map = {}
    used = set()

    for field, keywords in field_patterns.items():
        for kw in keywords:
            for norm, original in norm_map.items():
                if (kw == norm or norm.startswith(kw + '_') or kw in norm) and original not in used:
                    col_map[field] = original
                    used.add(original)
                    break
            if field in col_map:
                break

    return col_map


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/upload', methods=['POST'])
def upload():
    if 'file' not in request.files:
        return jsonify({'error': 'No file uploaded'}), 400

    file = request.files['file']
    if not file.filename:
        return jsonify({'error': 'No file selected'}), 400

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
        return jsonify({'error': 'Cannot read file. Please save as UTF-8 CSV.'}), 400

    try:
        reader = csv.DictReader(io.StringIO(content))
        headers = [h for h in (reader.fieldnames or []) if h and h.strip()]
        rows = []
        for row in reader:
            clean = {k: (v or '').strip() for k, v in row.items() if k and k.strip()}
            rows.append(clean)
    except Exception as e:
        return jsonify({'error': f'CSV parse error: {e}'}), 400

    if not rows:
        return jsonify({'error': 'The CSV appears to be empty. Please check the file.'}), 400

    return jsonify({
        'rows': rows,
        'headers': headers,
        'column_map': detect_columns(headers),
        'count': len(rows)
    })


if __name__ == '__main__':
    print("\n✅  Expense Dashboard running at: http://localhost:5001\n")
    app.run(debug=True, port=5001)
