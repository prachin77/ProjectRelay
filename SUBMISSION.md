# Submission — what to upload and how

The brief's rule: **"Do not include your key in your submission."** The key lives in
`.env`, so `.env` must never leave this machine. Everything below is built around that.

---

## 1. The four things that go in the Drive folder

| # | Item | Where it comes from | Why |
|---|---|---|---|
| 1 | **Write-up (PDF)** | `reports/REPORT.md`, exported to PDF | The graded two pages, 12 headings, Tables 1, 2a, 2b, 3 |
| 2 | **Code (zip)** | `dist/the-relay-submission.zip` | Source, tests, config, the three PDFs, evidence logs |
| 3 | **Raw results** | `results.csv` (copy it next to the zip, unrenamed) | The brief names this file; every table must be recomputable from it |
| 4 | **Conversation history** | exported from the assistant session | The brief asks for the real history, "not a cleaned-up one" |

Do not rename `results.csv`. Do not add a second CSV with a different name.

---

## 2. Build the zip (already done, rebuild any time)

```
$env:PYTHONPATH='P:\ProjectRelay'
.\\.venv\\Scripts\\python.exe scripts/package_submission.py
```

Last run:

```
wrote P:\ProjectRelay\dist\the-relay-submission.zip
files: 84  size: 11.3 MB
withheld secrets/local files: ['.env']
checked: no Groq key pattern in any shipped file
checked: required files present
```

The script is not a formality. It:

- excludes `.env`, `.env.local`, and every `.env*` except `.env.example`;
- excludes `.venv/`, `__pycache__/`, `indexes/`, `dist/`, `scratch/`, `*.db`;
- reads **every** shipped file and refuses to finish if it finds a `gsk_…` key;
- refuses to finish if `results.csv`, `logs/results.csv`, `README.md`,
  `reports/REPORT.md`, `the_relay/main.py` or `requirements.txt` is missing.

If it ever prints `REFUSING TO SHIP`, do not upload the folder by hand — fix the file it
named and run it again.

### Verify it yourself in 10 seconds

```
.\\.venv\\Scripts\\python.exe -c "import zipfile;n=zipfile.ZipFile('dist/the-relay-submission.zip').namelist();print([x for x in n if x.startswith('.env')])"
```

Expected: `['.env.example']` — and nothing else.

---

## 3. Make the PDF

1. Open `reports/REPORT.md` in VS Code.
2. `Ctrl+Shift+P` → **Markdown PDF: Export (pdf)** (extension: *Markdown PDF* by yzane).
3. Save as `The-Relay-Writeup.pdf` in the same folder you will upload.

Fallbacks if the extension is unavailable: paste the rendered Markdown into Google Docs and
*File → Download → PDF*, or print the VS Code Markdown preview to PDF.

**Check before exporting:** the PDF must show all twelve headings, in order, and Tables 1,
2a, 2b, 3 with numbers matching `results.csv`.

**Known risk:** the write-up is currently ~2,900 words, which is about five pages against
the brief's *"maximum 2 pages of writing"*. Trim it or accept the overrun deliberately —
do not let it happen by accident.

---

## 4. Conversation history

This tool has no export command, so pick one:

- **Preferred:** in the assistant app, copy the full session transcript into
  `CONVERSATION.md` (raw, including the mistakes).
- **Fallback the brief allows:** "the prompts if the tool cannot export" — a plain list of
  the prompts you actually sent, in order, including the ones that were rejected.

Do not write a cleaned-up version. The brief says it is looking for how you directed the
assistant, including where you corrected it.

---

## 5. Put it on Drive

1. Go to https://drive.google.com → **New → Folder** → name it `The Relay — <your name>`.
2. Upload the four items. Keep them at the top level of that folder; no need for subfolders.
3. Right-click the folder → **Share** → **General access** → **Anyone with the link** →
   role **Viewer** → **Copy link**.
4. Open the link in a private/incognito window and confirm you can see all four items.
   If you can see them logged out, the reviewer can too.
5. Paste that link into the submission form, using **the same email address you applied
   with**.

---

## 6. Final checklist before you hit submit

- [ ] `dist/the-relay-submission.zip` built by the script on this machine, `REFUSING TO
      SHIP` absent
- [ ] zip contains no `.env` (only `.env.example`)
- [ ] `results.csv` uploaded separately, unrenamed, 129 lines (header + 128 rows)
- [ ] PDF has 12 headings in the brief's order and Tables 1, 2a, 2b, 3
- [ ] §11 Vaani feedback is present, in your own words
- [ ] conversation history included, unedited
- [ ] Drive folder shared as *Anyone with the link → Viewer*, link tested logged out
- [ ] form submitted with the same email used to apply
- [ ] confirmation email received (check spam after a few minutes)
