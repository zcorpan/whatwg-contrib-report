# WHATWG contribution attribution report

This repository skeleton generates a static HTML report showing, for each WHATWG Living Standard, which GitHub users and public participant entities have contributed commits, and what percentage of all commits on `main` those represent.

The report is designed to run weekly from GitHub Actions and publish to GitHub Pages.

## What it measures

For each standard listed in `whatwg/sg`'s `db.json`, the script infers the corresponding `whatwg/<repo>` repository from the standard URL. For example, `https://fs.spec.whatwg.org/` maps to `whatwg/fs`, and `https://html.spec.whatwg.org/multipage/` maps to `whatwg/html`.

For each repository, the script walks commits reachable from `main` and asks GitHub which merged pull request, if any, introduced each commit.

By default:

- the denominator is **all commits reachable from `main`**;
- commits associated with a merged PR are credited to the **PR author**, not the merger or committer;
- direct/no-PR commits are credited to the resolved GitHub commit author;
- commits without a resolvable GitHub login stay in the denominator but are not credited;
- entity affiliation uses public GitHub organization memberships and public participant-data contacts;
- participant-data entries explicitly marked non-`Public` are ignored, with no option to include them;
- if a contributor maps to multiple public entities for a spec, entity credit is split fractionally to avoid totals above 100%.

## Important caveats

Entity attribution uses the latest public GitHub organization data at generation time. Historical employer changes are not reconstructed, so past contributions are credited to the contributor's current public entity affiliation.

Private or concealed GitHub organization memberships are intentionally ignored, even if the token used by the workflow could see them. The script uses public membership surfaces only.

GitHub can associate a commit with a GitHub account only when GitHub can resolve the commit author to a user. Older commits or commits using unlinked email addresses can therefore remain uncredited.

## Local run

```bash
export GITHUB_TOKEN=ghp_...
python scripts/whatwg_contrib_report.py --output public --full-scan
python -m http.server --directory public 8000
```

Then open <http://localhost:8000/>.

## GitHub Actions setup

1. Commit `scripts/whatwg_contrib_report.py` and `.github/workflows/whatwg-contrib-report.yml`.
2. In repository settings, configure GitHub Pages to use **GitHub Actions** as the source.
3. Run the workflow manually once. Later runs happen weekly.

The workflow uses the built-in `GITHUB_TOKEN`. A separate personal token should not be necessary unless a full scan hits rate limits.

The workflow also pushes the generated `index.html`, `report.json`, and cache file to the `gh-pages` branch. The actual Pages deployment uses GitHub's official Pages artifact flow, so it does not depend on a branch-push Pages build trigger.

## Outputs

- `public/index.html`: human-readable report.
- `public/report.json`: machine-readable report data.
- `public/cache/whatwg-contrib-cache.json`: commit-to-PR cache used by incremental weekly runs.

## Useful options

```text
--full-scan                    scan full history instead of stopping at cached commits
--incremental                  stop when a cached history page is reached
--sg-db-url URL                use a different whatwg/sg db.json URL
--include-unverified           include unverified public participant-data entries
--affiliation-source SOURCE    user-orgs, org-members, or both; all use public memberships only
--entity-attribution MODE      fractional or duplicate
```

There is intentionally no option to include non-public participant-data entries or private GitHub organization memberships.

## License

This project is licensed under the MIT License. See the [LICENSE](LICENSE) file for details.

## Attribution

This project was initially created with assistance from ChatGPT (OpenAI) based on requirements provided by the project author. The generated code and documentation have been reviewed and may have been modified after generation.

Users should independently verify the correctness of the implementation and results before relying on them.
