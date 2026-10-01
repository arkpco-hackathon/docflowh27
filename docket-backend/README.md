# DocFlow (Flask + SQLite/PostgreSQL)

Real working app: accounts, uploads, AI pre-review, reviewer approval, history. No demo data.

## Run it (2 minutes)
```
python -m venv venv
venv\Scripts\activate          # Mac/Linux: source venv/bin/activate
pip install -r requirements.txt
copy .env.example .env          # Mac/Linux: cp .env.example .env   (then set SECRET_KEY and REVIEWER_CODE)
flask --app app run --debug
```
Open http://127.0.0.1:5000. The database is created automatically (`docflow.db`, SQLite).
To use PostgreSQL instead, set `DATABASE_URL` in `.env` and `pip install psycopg2-binary`.

## Accounts
- **Submitter:** pick Submitter on the sign-up page and register.
- **Admin / Reviewer:** pick Admin / Reviewer, register, and enter the `REVIEWER_CODE` from your `.env`.
  (Or from the terminal: `flask --app app create-reviewer "Name" email password`.)

## Rules enforced on the server
- Submitters see only their own documents. Reviewers see every non-draft document.
- Only the owner can submit, and only drafts or documents sent back for changes.
- Only reviewers can approve, reject or request changes. Reject / request changes need remarks.
- Owners can download a file only after approval. Reviewers can open any submitted file.
- Every status change is stored in `status_history`.
- AI never decides anything: it only shows a summary, completeness and flags to the reviewer.

## AI pre-review
`GET /api/documents/<id>/ai-review` reads the uploaded PDF / DOCX text, checks for the sections expected for that
document type, and extracts emails, dates and amounts. It is rule-based (no external AI service). Images are flagged
because their text cannot be read. To use an LLM later, replace the body of `ai_review()` and keep the JSON shape.

## Not built yet
Revised-version upload and Version 1 vs Version 2 comparison.
