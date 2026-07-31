# Profile contribution graphic

[`scripts/generate_profile_stats.py`](scripts/generate_profile_stats.py) requests GitHub's contribution calendar for the previous 365 complete UTC dates, groups the dates into Monday-anchored calendar weeks, and writes [`assets/generated/contributions.svg`](assets/generated/contributions.svg). Boundary weeks are intentionally partial, and five quarterly month labels provide the timeline.

## Regenerate locally

Python 3.11 or newer is sufficient; the generator uses only the standard library.

```bash
python -m unittest discover -s tests -p "test_*.py"
GH_LOGIN=simonesiega 
GITHUB_TOKEN="$(gh auth token)" 
python scripts/generate_profile_stats.py
```

On PowerShell, set `$env:GH_LOGIN` and `$env:GITHUB_TOKEN` before running the same Python command. The token is sent only in the GitHub GraphQL authorization header and must never be committed.

The figures reflect contribution data visible to the supplied token. The repository's Actions-provided `GITHUB_TOKEN` is sufficient for the scheduled public-data refresh, but it may not include activity from private repositories that the workflow token cannot access.

## Automation and ownership

`.github/workflows/update-profile-stats.yml` runs the tests and generator daily at 04:23 UTC, as well as on manual dispatch. GitHub supplies `GITHUB_TOKEN`, and the workflow derives `GH_LOGIN` from the repository owner, so no custom secret or personal access token is required. It commits only the SVG and only when that file changes. Concurrency prevents overlapping refreshes.

`README.md`—including the introduction, project descriptions, links, and contact copy—is manually maintained. Only `assets/generated/contributions.svg` is automatic.
