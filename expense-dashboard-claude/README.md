# 💳 Expense Dashboard

A beautiful, interactive expense dashboard that reads your Google Sheets CSV and
visualises your spending with charts, filters, and balance summaries.

---

## Quick Start

### 1. Install Python dependencies
```bash
pip install flask
```
*(Or: `pip install -r requirements.txt`)*

### 2. Run the server
```bash
python app.py
```

### 3. Open in browser
Navigate to: **http://localhost:5001**

---

## Exporting from Google Sheets

1. Open your expense spreadsheet in Google Sheets
2. Click **File → Download → Comma Separated Values (.csv)**
3. Save the file anywhere on your computer
4. Drag & drop it onto the dashboard (or click to browse)

---

## Expected Columns

The dashboard auto-detects column names — they can be in any order and any
capitalisation. It looks for columns matching these fields:

| Field         | Examples of accepted column names                          |
|---------------|------------------------------------------------------------|
| Expense Name  | `Name`, `Description`, `Item`, `Expense Name`             |
| Amount        | `Amount`, `Cost`, `Price`, `Total`                        |
| Category      | `Category`, `Cat`, `Type`, `Expense Type`                 |
| Paid By       | `Paid By`, `Who Paid`, `Payer`                            |
| Whose Expense | `Whose Expense`, `Beneficiary`, `For`, `Owner`            |
| Date          | `Date`, `Expense Date`, `When`                            |
| Trip          | `Trip`, `Trip Name`, `Associated Trip`, `Event`           |

After uploading, a **mapping confirmation** screen lets you verify or adjust
which column maps to which field.

---

## Features

- **Drag & drop CSV upload** — works with any Google Sheets export
- **Smart column auto-detection** — handles varied naming conventions
- **Summary stats** — total spent, transaction count, average, largest expense
- **4 interactive charts:**
  - Category breakdown (donut)
  - Monthly spending trend (line)
  - Trip breakdown (horizontal bar)
  - Paid-by breakdown (bar)
- **Balance summary** — who paid vs. whose expense, net balance per person
- **Filterable table** — search, filter by category/trip/person, sort any column
- **Pagination** — handles large datasets smoothly

---

## File Structure

```
expense-dashboard/
├── app.py              ← Flask backend (runs the server)
├── requirements.txt    ← Python dependencies
├── README.md           ← This file
└── templates/
    └── index.html      ← Full frontend (HTML + CSS + JS)
```
