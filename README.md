# Work Schedule Bot

Automates your weekly or biweekly workflow: pulls your work schedule from Gmail, parses the shifts you care about, and writes clean entries into a Notion database. Runs locally or with GitHub Actions on a schedule.

## Why this is useful

* Stops you from copy pasting shifts into Notion
* Keeps a single source of truth for your week
* Handles real world mess: early outs, workshops, multi site rows, and name filtering
* Runs headless in the cloud so you do not have to remember

## What it does

1. Reads Gmail using the Gmail API and a search query you control
2. Finds the latest schedule email that actually contains day headers and time ranges
3. Parses each date row and site row, including:

   * Time ranges like `12 PM - 6 PM` and compact variants
   * "until" notes like `until 2:30` which shorten the end time shown in Notion
   * Workshop rows. Text like `WORKSHOP (Cybersecurity - Spot the Scammer)` moves into the Notion title
4. Filters rows to your name only if you want
5. Writes to your Notion database using your property types
6. Skips formula fields. Your Day of Week formula stays in Notion and updates itself

## Notion schema this repo expects

You can use any names. The bot autodetects types and also supports explicit mappings via env. The example schema used in this repo:

* Title property: **Task** (type title)
* Date: **Date** (type date)
* Time: **Time** (type rich text)
* Location: **Location** (type select with options like Tempe, CTEC, Aeroterra, Mesa, Chandler, Guadalupe)
* Notes: **Notes** (type rich text)
* Day of Week: **Day of Week** (type formula) which formats Date as a weekday. The bot does not write to formulas

## How parsing works

* Anchors to the date window found in the email body or subject. Example: `Here is the schedule for September 1st - 14th`
* Daily header rows like `9 Tuesday` set the current date, then following site rows are tied to that date
* Time ranges are normalized to `H[:MM] AM/PM - H[:MM] AM/PM`
* Notes handling:

  * Early outs: phrases like `until 2:30` or `til 4pm` shorten the end time shown in Time. The phrase is removed from Notes
  * Workshops: lines that contain `WORKSHOP (...)` move the workshop title into the Task title for that row and the workshop text is removed from Notes

## Repo layout

```
work-schedule-bot/
  run.py                 # main program
  requirements.txt
  .github/workflows/weekly.yml  # GitHub Actions workflow
  .gitignore
```

## Setup

### 1) Notion

1. Create a database called Work Schedule or use your existing one
2. Add properties to match your needs. Suggested:

   * Task (title)
   * Date (date)
   * Time (rich text)
   * Location (select)
   * Notes (rich text)
   * Day of Week (formula) with `formatDate(prop("Date"), "dddd")`
3. Create a Notion internal integration and copy its token
4. Share the database with the integration
5. Copy your database ID from the URL

### 2) Gmail API

1. In Google Cloud Console create a project and enable Gmail API
2. Create OAuth 2.0 credentials for Desktop and download `credentials.json`
3. Run the bot locally once to complete OAuth. It will create `token.json`
4. Do not commit these files

### 3) Local run

```
python -m venv .venv && source .venv/bin/activate   # Windows PowerShell: .venv\\Scripts\\Activate.ps1
pip install -r requirements.txt
# Configure environment variables in your shell or create a .env if you prefer
python run.py
```

Environment variables you can set:

```
NOTION_TOKEN=secret_xxx
NOTION_DATABASE_ID=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
# Optional explicit property mappings if autodetect is not right
NOTION_TITLE_PROP=Task
NOTION_DATE_PROP=Date
NOTION_TIME_PROP=Time
NOTION_LOCATION_PROP=Location
NOTION_NOTES_PROP=Notes
NOTION_DAY_PROP=Day of Week
# Parser and filtering
YOUR_NAME=
FILTER_BY_NAME=true       # set to false to ingest all rows
TIMEZONE=America/Phoenix
# Gmail search. Tune this to your sender and subject
GMAIL_QUERY=from:john subject:(schedule OR shifts) newer_than:30d -subject:debrief
```

## GitHub Actions deployment

Run on a schedule in the cloud using your repo secrets.

### Create secrets

Repo settings → Secrets and variables → Actions → New repository secret. Create:

* `NOTION_TOKEN` your Notion integration token
* `NOTION_DATABASE_ID` the database ID
* `CREDENTIALS_JSON` paste full JSON from your Google OAuth `credentials.json`
* `GMAIL_TOKEN_JSON` paste full JSON from your local `token.json` that includes a `refresh_token`
* Optional `GMAIL_QUERY` to override the Gmail search

### Biweekly on Sunday

Cron cannot express every two weeks. The workflow runs every Sunday and a small bash step gates execution to "only Sundays that are 14 days after an anchor Sunday". Set the anchor to the last Sunday you actually received a schedule.

`.github/workflows/weekly.yml`:

```yaml
name: work-schedule-bot-biweekly

on:
  schedule:
    - cron: "5 14 * * 0"   # Sundays 14:05 UTC = 07:05 Phoenix
  workflow_dispatch: {}

permissions:
  contents: read

jobs:
  run:
    runs-on: ubuntu-latest
    steps:
      - name: Biweekly gate
        id: gate
        run: |
          ANCHOR_UTC="2025-08-31"   # last schedule Sunday you received
          TODAY_UTC="$(date -u +%Y-%m-%d)"
          secs_today=$(date -ud "$TODAY_UTC" +%s)
          secs_anchor=$(date -ud "$ANCHOR_UTC" +%s)
          days=$(( (secs_today - secs_anchor) / 86400 ))
          if [ $(( days % 14 )) -eq 0 ]; then
            echo "ok=true" >> $GITHUB_OUTPUT
            echo "Running. day_offset=$days"
          else
            echo "ok=false" >> $GITHUB_OUTPUT
            echo "Skipping. day_offset=$days"
          fi

      - uses: actions/checkout@v4
        if: steps.gate.outputs.ok == 'true'

      - uses: actions/setup-python@v5
        if: steps.gate.outputs.ok == 'true'
        with:
          python-version: "3.11"

      - name: Write credentials.json
        if: steps.gate.outputs.ok == 'true'
        run: |
          cat > credentials.json <<'JSON'
          ${{ secrets.CREDENTIALS_JSON }}
          JSON

      - name: Write token.json
        if: steps.gate.outputs.ok == 'true'
        run: |
          cat > token.json <<'JSON'
          ${{ secrets.GMAIL_TOKEN_JSON }}
          JSON

      - name: Install dependencies
        if: steps.gate.outputs.ok == 'true'
        run: |
          python -m pip install --upgrade pip
          pip install -r requirements.txt

      - name: Run bot
        if: steps.gate.outputs.ok == 'true'
        env:
          NOTION_TOKEN: ${{ secrets.NOTION_TOKEN }}
          NOTION_DATABASE_ID: ${{ secrets.NOTION_DATABASE_ID }}
          NOTION_TITLE_PROP: Task
          NOTION_DATE_PROP: Date
          NOTION_TIME_PROP: Time
          NOTION_LOCATION_PROP: Location
          NOTION_NOTES_PROP: Notes
          NOTION_DAY_PROP: "Day of Week"
          GMAIL_QUERY: ${{ secrets.GMAIL_QUERY }}
          TIMEZONE: America/Phoenix
          YOUR_NAME: #Your name
          FILTER_BY_NAME: "true"
        run: python run.py

      - name: Upload debug artifacts
        if: always()
        uses: actions/upload-artifact@v4
        with:
          name: last-email
          path: |
            last_email.txt
            last_email.html
```

## Tuning the Gmail search

Use Gmail search operators to get only the right mail. Examples:

* Narrow to a sender address `from:john.doe@company.org`
* Exclude debriefs `-subject:debrief`
* Date bound `newer_than:30d`

Example good default:

```
from:john subject:("schedule" OR "shifts") newer_than:30d -subject:debrief
```

## Config options that matter

* `YOUR_NAME` and `FILTER_BY_NAME`: set your exact name as it appears in the email. Set filter to false if you want every row
* `TIMEZONE`: used to render times consistently with your region
* Property overrides: if your DB uses different names, set the six `NOTION_*_PROP` env vars

## Troubleshooting

* Action says Parsed zero rows

  * Your Gmail query matched a debrief or a message without day headers. Tighten `GMAIL_QUERY` or use the manual Run workflow to test
  * Temporarily set `FILTER_BY_NAME` to false. If rows appear then your name string does not match. Update `YOUR_NAME`
* Notion error 400 about property not existing or wrong type

  * Your property names or types differ. Set explicit env overrides for the six Notion props
  * If Location is a select in Notion, the bot will create options on the fly if needed
* Gmail OAuth fails in Actions

  * Your `GMAIL_TOKEN_JSON` secret is missing `refresh_token`. Run locally once, then paste the new `token.json`

## Security and privacy

* Do not commit `credentials.json` or `token.json`
* Use GitHub Secrets for tokens and IDs
* This bot reads only Gmail message content and writes only to the Notion database you shared with the integration

## Roadmap ideas

* Support multiple senders and templates
* Write time ranges as Notion date ranges instead of text if you prefer
* Add slack or email notifications when new shifts are saved

## Credit

Built to turn messy email schedules into clean Notion entries with minimal effort.
