# WHATWG contribution attribution report

This repository skeleton generates a static HTML report showing, for each WHATWG Living Standard, which GitHub users and participant entities have contributed commits on `main`, and what percentage of all commits on `main` those represent.

## What it measures

For each standard listed in `whatwg/sg`'s `db.json`, the script infers the `whatwg/<repo>` repository from the standard's `href` URL, walks commits reachable from `main`, and asks GitHub which merged pull request introduced each commit when applicable. For example, `https://fs.spec.whatwg.org/` maps to `https://github.com/whatwg/fs`.

By default and without an opt-out flag:

- the denominator is **all commits reachable from `main`**;
- commits associated with a merged PR are credited to the **PR author**, not the merger or committer;
- direct/no-PR commits are credited to the **GitHub commit author** where GitHub resolves one;
- commits without a resolvable GitHub login stay in the denominator but are not credited;
- entity affiliation uses only **public** GitHub organization memberships for the `gitHubOrganization` values in `entities.json`;
- private/concealed GitHub organization memberships are ignored even if the token has permission to see them;
- participant-data records explicitly marked non-`Public` are skipped/suppressed, and there is no option to include them;
- participant-data entity contacts are not used as an affiliation shortcut;
- if a contributor maps to multiple entities for a spec, entity credit is split fractionally to avoid totals above 100%.

## Important caveats

Entity attribution uses the latest public GitHub organization data at generation time. Historical employer changes are not reconstructed, so past contributions are credited to the contributor's current public entity affiliation.

The default affiliation source is `--affiliation-source user-orgs`, which fetches the public organizations listed on each contributor's GitHub profile. To crawl every entity `gitHubOrganization` public member list instead, run with `--affiliation-source org-public-members` or `--affiliation-source both`; all modes use only public membership visibility. There is no mode that uses private organization membership visibility or explicitly non-public participant records.

## Why not scrape `/graphs/contributors?all=1`?

The script deliberately does not use GitHub's web contributor graph. The graph is useful as a visual cross-check, but it is not a stable machine API, only shows the top contributors in the UI, excludes merge and empty commits, depends on GitHub's default-branch contributor-graph rules, and does not expose the PR-author vs direct-commit distinction needed for this report. The GraphQL commit history query gives commit-level data, preserves the all-commits denominator, and exposes `associatedPullRequests` for PR attribution.

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

The workflow also pushes the generated `index.html`, `report.json`, and cache file to the `gh-pages` branch. The actual Pages deployment uses GitHub's official Pages artifact flow so it does not depend on a branch-push Pages build trigger.

## Outputs

- `public/index.html`: human-readable report.
- `public/report.json`: machine-readable report data.
- `public/cache/whatwg-contrib-cache.json`: commit-to-PR cache used by incremental weekly runs.

## Useful options

```text
--full-scan                    scan full history instead of stopping at cached commits
--incremental                  stop when a cached history page is reached
--include-unverified           include unverified participant-data entries that are still publicly visible
--affiliation-source SOURCE    user-orgs, org-public-members, or both
--entity-attribution MODE      fractional or duplicate
--sg-db-url URL                WHATWG SG db.json URL
```

## Attribution

This project was initially created with assistance from ChatGPT (OpenAI) based on requirements provided by the project author. The generated code and documentation have been reviewed and may have been modified after generation.

## License

This project is licensed under the MIT License. See the [LICENSE](LICENSE) file for details.
