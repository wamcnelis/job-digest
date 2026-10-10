# Job Digest

A daily, LinkedIn-free view of new job postings. A GitHub Action runs `jobs.py` every morning,
which searches LinkedIn's public job listings, reads each new posting once, tags it by role and
interest area, and publishes `index.html` via GitHub Pages.

- **Change what it searches for:** edit `config.json` (searches, locations, interest rules, title filters).
- **Refresh now:** Actions tab → "Daily job digest" → Run workflow.
