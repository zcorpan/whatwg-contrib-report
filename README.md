# WHATWG contribution attribution report

This repository generates a static HTML report showing, for each WHATWG standard listed in `whatwg/sg`'s `db.json`, which GitHub users and participant entities have contributed commits, and what percentage of all commits on `main` those represent.

## What it measures

For each standard listed in `https://github.com/whatwg/sg/blob/main/db.json`, the script uses the standard URL to infer the repository. For example, `https://fs.spec.whatwg.org/` maps to `https://github.com/whatwg/fs`.

For each inferred repository, the script walks commits reachable from the configured branch, `main` by default, and asks GitHub which merged pull request introduced each commit.

By default:

- the denominator is **all commits reachable from `main`**;
- commits associated with a merged PR are credited to the **PR author**, not the merger or committer;
- direct/no-PR commits are credited to the resolved GitHub commit author;
- entity affiliation uses participant-data contacts and public GitHub organization memberships only;
- private GitHub organization memberships are intentionally ignored, even if visible to the token used for the run;
- if a contributor maps to multiple entities for a spec, entity credit is split fractionally to avoid totals above 100%.

## Important caveats

Entity attribution uses the latest public GitHub organization memberships and participant-data contacts at generation time. Historical employer changes are not reconstructed, so past contributions are credited to the contributor's current visible entity affiliation.

Some contributors may appear without an entity attribution even when they are affiliated with a participating organization, because GitHub organization memberships can be private.

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

The workflow also pushes the generated `index.html`, `report.json`, and cache file to the `gh-pages` branch. The actual Pages deployment uses GitHub's official Pages artifact flow, so it does not depend on a branch-push Pages build trigger.

## Outputs

- `public/index.html`: human-readable report.
- `public/report.json`: machine-readable report data.
- `public/cache/whatwg-contrib-cache.json`: commit-to-PR cache used by incremental weekly runs.

## Useful options

```text
--full-scan                    scan full history instead of stopping at cached commits
--incremental                  stop when a cached history page is reached
--include-unverified           include unverified participant-data entries
--affiliation-source SOURCE    user-orgs, org-public-members, org-members, or both
--entity-attribution MODE      fractional or duplicate
```

`org-members` is accepted as a legacy alias for `org-public-members`; it does **not** use private organization membership APIs.

## Attribution

This project was initially created with assistance from ChatGPT (OpenAI) based on requirements provided by the project author. The generated code and documentation have been reviewed and may have been modified after generation.

Users should independently verify the correctness of the implementation and results before relying on them.

## License

This project is licensed under the MIT License. See the [LICENSE](LICENSE) file for details.
