#!/usr/bin/env python3
"""Generate a WHATWG contribution attribution report.

The report is intentionally conservative:

* It discovers WHATWG standards from whatwg/sg db.json.
* It infers each standard's GitHub repository from the standard URL's
  .spec.whatwg.org hostname, e.g. https://fs.spec.whatwg.org/ -> whatwg/fs.
* It walks commits reachable from the requested branch (main by default).
* It credits commits associated with merged PRs to the PR author.
* It credits direct/no-PR commits to the GitHub commit author where GitHub
  resolves one. Commits without a resolvable GitHub login remain in the
  denominator but are not credited.
* It never reports participant-data entries explicitly marked non-Public; there
  is no flag or endpoint path to include them.
* Entity affiliation is based only on public GitHub organization memberships for
  participant-data entity GitHub organizations, plus public participant-data
  contacts unless disabled. Private/concealed memberships are intentionally
  ignored, even if the token could see them.
* Historical affiliation changes are not reconstructed.

Requires Python 3.11+ and a GitHub token in GITHUB_TOKEN for GraphQL.
No third-party Python packages are required.
"""

from __future__ import annotations

import argparse
import collections
import dataclasses
import datetime as _dt
import hashlib
import html as html_lib
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, DefaultDict, Iterable, Iterator, Mapping, MutableMapping, Optional


DEFAULT_SG_DB_URL = "https://raw.githubusercontent.com/whatwg/sg/main/db.json"
DEFAULT_ENTITIES_URL = "https://raw.githubusercontent.com/whatwg/participant-data/main/entities.json"
DEFAULT_INDIVIDUALS_URL = "https://raw.githubusercontent.com/whatwg/participant-data/main/individuals.json"
DEFAULT_GITHUB_REST_URL = "https://api.github.com"
DEFAULT_GITHUB_GRAPHQL_URL = "https://api.github.com/graphql"
DEFAULT_GITHUB_API_VERSION = "2022-11-28"
CACHE_VERSION = 2

GRAPHQL_HISTORY_QUERY = r"""
query($owner: String!, $repo: String!, $branch: String!, $after: String, $pageSize: Int!) {
  repository(owner: $owner, name: $repo) {
    nameWithOwner
    url
    ref(qualifiedName: $branch) {
      name
      target {
        __typename
        ... on Commit {
          history(first: $pageSize, after: $after) {
            totalCount
            pageInfo {
              hasNextPage
              endCursor
            }
            nodes {
              oid
              committedDate
              author {
                name
                email
                user {
                  login
                }
              }
              associatedPullRequests(first: 10) {
                nodes {
                  number
                  url
                  merged
                  mergedAt
                  baseRefName
                  author {
                    __typename
                    login
                  }
                }
              }
            }
          }
        }
      }
    }
  }
  rateLimit {
    cost
    remaining
    resetAt
  }
}
"""


@dataclasses.dataclass(frozen=True)
class Spec:
    workstream_title: str
    workstream_id: str
    name: str
    spec_url: str
    repo_owner: str = ""
    repo_name: str = ""

    @property
    def repo_full_name(self) -> str:
        return f"{self.repo_owner}/{self.repo_name}" if self.repo_owner and self.repo_name else ""

    @property
    def repo_url(self) -> str:
        return f"https://github.com/{self.repo_full_name}" if self.repo_full_name else ""


@dataclasses.dataclass(frozen=True)
class ParticipantWorkstreams:
    all_workstreams: bool
    ids: frozenset[str]

    def applies_to(self, workstream_id: str) -> bool:
        return self.all_workstreams or workstream_id in self.ids


@dataclasses.dataclass(frozen=True)
class Entity:
    entity_id: str
    name: str
    org_login: str
    verified: bool
    workstreams: ParticipantWorkstreams
    contacts: frozenset[str]
    url: str = ""


@dataclasses.dataclass(frozen=True)
class Individual:
    participant_id: str
    name: str
    login: str
    verified: bool
    workstreams: ParticipantWorkstreams


@dataclasses.dataclass
class RunWarning:
    area: str
    message: str


class ReportError(RuntimeError):
    pass


class GitHubClient:
    def __init__(
        self,
        token: str,
        rest_url: str = DEFAULT_GITHUB_REST_URL,
        graphql_url: str = DEFAULT_GITHUB_GRAPHQL_URL,
        api_version: str = DEFAULT_GITHUB_API_VERSION,
        user_agent: str = "whatwg-contrib-report/1.0",
        max_retries: int = 4,
        retry_sleep: float = 2.0,
    ) -> None:
        self.token = token
        self.rest_url = rest_url.rstrip("/")
        self.graphql_url = graphql_url
        self.api_version = api_version
        self.user_agent = user_agent
        self.max_retries = max_retries
        self.retry_sleep = retry_sleep

    def _headers(self, extra: Optional[Mapping[str, str]] = None) -> dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": self.api_version,
            "User-Agent": self.user_agent,
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if extra:
            headers.update(extra)
        return headers

    def request_json(
        self,
        method: str,
        url: str,
        body: Optional[Mapping[str, Any]] = None,
        allow_404: bool = False,
        extra_headers: Optional[Mapping[str, str]] = None,
    ) -> tuple[Any, Mapping[str, str], int]:
        data = None
        headers = self._headers(extra_headers)
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        last_exc: Optional[BaseException] = None
        for attempt in range(self.max_retries + 1):
            try:
                with urllib.request.urlopen(req, timeout=90) as response:
                    raw = response.read()
                    parsed = json.loads(raw.decode("utf-8")) if raw else None
                    return parsed, response.headers, response.status
            except urllib.error.HTTPError as exc:
                last_exc = exc
                raw = exc.read().decode("utf-8", "replace")
                if allow_404 and exc.code == 404:
                    return None, exc.headers, exc.code
                if exc.code in (403, 429) and self._is_primary_rate_limit(exc.headers):
                    self._sleep_until_reset(exc.headers)
                    continue
                if exc.code >= 500 and attempt < self.max_retries:
                    time.sleep(self.retry_sleep * (2**attempt))
                    continue
                raise ReportError(f"GitHub API {method} {url} failed with HTTP {exc.code}: {raw[:1000]}") from exc
            except (urllib.error.URLError, TimeoutError) as exc:
                last_exc = exc
                if attempt < self.max_retries:
                    time.sleep(self.retry_sleep * (2**attempt))
                    continue
                raise ReportError(f"GitHub API {method} {url} failed: {exc}") from exc
        raise ReportError(f"GitHub API {method} {url} failed: {last_exc}")

    @staticmethod
    def _is_primary_rate_limit(headers: Mapping[str, str]) -> bool:
        return headers.get("x-ratelimit-remaining") == "0" and bool(headers.get("x-ratelimit-reset"))

    @staticmethod
    def _sleep_until_reset(headers: Mapping[str, str]) -> None:
        try:
            reset_epoch = int(headers.get("x-ratelimit-reset", "0"))
        except ValueError:
            reset_epoch = 0
        delay = max(1, reset_epoch - int(time.time()) + 5)
        print(f"GitHub primary rate limit reached; sleeping for {delay} seconds", file=sys.stderr)
        time.sleep(delay)

    def graphql(self, query: str, variables: Mapping[str, Any]) -> Any:
        response, _headers, _status = self.request_json(
            "POST",
            self.graphql_url,
            {"query": query, "variables": variables},
            extra_headers={"Accept": "application/vnd.github+json"},
        )
        if response is None:
            raise ReportError("GitHub GraphQL returned an empty response")
        if response.get("errors"):
            errors = response["errors"]
            if any(err.get("type") == "RATE_LIMITED" for err in errors):
                raise ReportError(f"GitHub GraphQL rate limit exceeded: {errors}")
            raise ReportError(f"GitHub GraphQL errors: {json.dumps(errors, indent=2)}")
        return response.get("data")

    def rest_get_json(self, path_or_url: str, allow_404: bool = False) -> tuple[Any, Mapping[str, str], int]:
        url = path_or_url if path_or_url.startswith("http") else f"{self.rest_url}{path_or_url}"
        return self.request_json("GET", url, None, allow_404=allow_404)

    def paginate(self, path: str, allow_404: bool = False) -> Iterator[Any]:
        separator = "&" if "?" in path else "?"
        url = f"{self.rest_url}{path}{separator}per_page=100"
        while url:
            data, headers, status = self.rest_get_json(url, allow_404=allow_404)
            if status == 404:
                return
            if not isinstance(data, list):
                raise ReportError(f"Expected list from {url}, got {type(data).__name__}")
            for item in data:
                yield item
            url = parse_next_link(headers.get("Link") or headers.get("link") or "")


def parse_next_link(link_header: str) -> Optional[str]:
    if not link_header:
        return None
    for part in link_header.split(","):
        section = part.strip()
        if 'rel="next"' not in section:
            continue
        match = re.search(r"<([^>]+)>", section)
        if match:
            return match.group(1)
    return None


def clean_ws(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def normalize_spec_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    path = parsed.path or "/"
    if path != "/":
        path = path.rstrip("/") + "/"
    return urllib.parse.urlunparse((parsed.scheme or "https", parsed.netloc, path, "", "", ""))


def normalize_login(login: Optional[str]) -> str:
    if not login:
        return ""
    return login.strip().lstrip("@").lower()


def display_login(login: str) -> str:
    return login.strip().lstrip("@")


def normalize_github_org(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    text = value.strip()
    if not text:
        return ""
    match = re.search(r"github\.com/(?:orgs/)?([A-Za-z0-9_.-]+)", text)
    if match:
        return match.group(1)
    if re.fullmatch(r"[A-Za-z0-9_.-]+", text):
        return text
    return ""


def participant_workstreams(raw: Any) -> ParticipantWorkstreams:
    if raw == "all":
        return ParticipantWorkstreams(True, frozenset())
    if isinstance(raw, list):
        return ParticipantWorkstreams(False, frozenset(str(item) for item in raw))
    return ParticipantWorkstreams(False, frozenset())


def participant_visibility(item: Mapping[str, Any]) -> str:
    """Return declared participant visibility, if present.

    Current public participant-data JSON files do not expose a visibility field.
    Missing visibility is treated as public for compatibility with those files,
    but explicit non-public visibility is never reportable and there is no
    override to include it.
    """
    info = item.get("info")
    signature = item.get("signature")
    for container in (item, info if isinstance(info, Mapping) else None, signature if isinstance(signature, Mapping) else None):
        if not isinstance(container, Mapping):
            continue
        for key in ("visibility", "participantVisibility", "profileVisibility"):
            value = container.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        for key in ("public", "isPublic"):
            if key in container:
                return "Public" if bool(container.get(key)) else "Private"
    return ""


def is_public_participant(item: Mapping[str, Any]) -> bool:
    visibility = participant_visibility(item)
    return not visibility or visibility.casefold() == "public"


def entity_contact_logins(item: Mapping[str, Any]) -> set[str]:
    info = item.get("info")
    if not isinstance(info, Mapping):
        return set()
    logins: set[str] = set()
    for value in info.values():
        if isinstance(value, Mapping):
            login = normalize_login(value.get("gitHubID"))
            if login:
                logins.add(login)
    return logins


def now_iso() -> str:
    return _dt.datetime.now(_dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def fetch_bytes_with_metadata(url: str, timeout: int = 90) -> tuple[bytes, dict[str, Any]]:
    req = urllib.request.Request(url, headers={"User-Agent": "whatwg-contrib-report/1.0"})
    fetched_at = now_iso()
    with urllib.request.urlopen(req, timeout=timeout) as response:
        raw = response.read()
        headers = response.headers
    meta = {
        "url": url,
        "fetchedAt": fetched_at,
        "bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
    }
    etag = headers.get("ETag") or headers.get("etag")
    last_modified = headers.get("Last-Modified") or headers.get("last-modified")
    if etag:
        meta["etag"] = etag
    if last_modified:
        meta["lastModified"] = last_modified
    return raw, meta


def json_fetch_with_metadata(url: str, timeout: int = 90) -> tuple[Any, dict[str, Any]]:
    raw, meta = fetch_bytes_with_metadata(url, timeout=timeout)
    return json.loads(raw.decode("utf-8")), meta


def resolve_raw_github_source(client: GitHubClient, url: str, warnings: list[RunWarning]) -> tuple[str, dict[str, Any]]:
    """Resolve a raw.githubusercontent.com branch URL to a commit-pinned URL.

    The returned metadata is merged into the fetch metadata so the report can
    show both the configured input URL and the immutable source URL used for
    this run. Non-GitHub-raw URLs are returned unchanged.
    """
    meta: dict[str, Any] = {"configuredUrl": url, "resolvedUrl": url}
    parsed = urllib.parse.urlparse(url)
    if parsed.netloc != "raw.githubusercontent.com":
        return url, meta

    parts = parsed.path.strip("/").split("/", 3)
    if len(parts) != 4:
        warnings.append(RunWarning("provenance", f"Could not parse raw GitHub URL {url}; using it as-is."))
        return url, meta

    owner, repo, ref, path = parts
    meta.update({"githubRepository": f"{owner}/{repo}", "githubRef": ref, "githubPath": path})
    try:
        quoted_ref = urllib.parse.quote(ref, safe="")
        commit, _headers, status = client.rest_get_json(f"/repos/{owner}/{repo}/commits/{quoted_ref}", allow_404=True)
        if status == 404 or not isinstance(commit, Mapping) or not commit.get("sha"):
            warnings.append(RunWarning("provenance", f"Could not resolve {owner}/{repo}@{ref}; using configured URL."))
            return url, meta
        sha = str(commit.get("sha"))
        resolved_url = f"https://raw.githubusercontent.com/{owner}/{repo}/{sha}/{path}"
        meta.update({"githubCommitSha": sha, "githubCommitUrl": commit.get("html_url") or "", "resolvedUrl": resolved_url})
        return resolved_url, meta
    except Exception as exc:
        warnings.append(RunWarning("provenance", f"Could not resolve {owner}/{repo}@{ref}: {exc}; using configured URL."))
        return url, meta


def load_specs(sg_db_url: str, warnings: list[RunWarning]) -> tuple[list[Spec], dict[str, Any]]:
    data, meta = json_fetch_with_metadata(sg_db_url)
    workstreams = data.get("workstreams") if isinstance(data, dict) else None
    if not isinstance(workstreams, list):
        raise ReportError(f"Expected {sg_db_url} to contain a top-level workstreams array")

    specs: list[Spec] = []
    seen_spec_urls: set[str] = set()
    for workstream in workstreams:
        if not isinstance(workstream, dict):
            continue
        workstream_id = clean_ws(str(workstream.get("id") or ""))
        workstream_title = clean_ws(str(workstream.get("name") or workstream_id))
        standards = workstream.get("standards") or []
        if not workstream_id or not isinstance(standards, list):
            continue
        for standard in standards:
            if not isinstance(standard, dict):
                continue
            href = standard.get("href")
            if not isinstance(href, str) or not href.strip():
                warnings.append(RunWarning("sg-db", f"Skipping standard without href in workstream {workstream_id}."))
                continue
            spec_url = normalize_spec_url(href)
            if spec_url in seen_spec_urls:
                continue
            seen_spec_urls.add(spec_url)
            name = clean_ws(str(standard.get("name") or standard.get("reference") or href))
            specs.append(
                Spec(
                    workstream_title=workstream_title,
                    workstream_id=workstream_id,
                    name=name,
                    spec_url=spec_url,
                )
            )
    if not specs:
        raise ReportError(f"No standards found in {sg_db_url}")
    return specs, meta


def infer_repo_from_spec_url(spec: Spec, warnings: list[RunWarning]) -> Spec:
    parsed = urllib.parse.urlparse(spec.spec_url)
    host = parsed.netloc.lower()
    path_parts = [part for part in parsed.path.split("/") if part]
    owner = "whatwg"
    repo = ""
    if host.endswith(".spec.whatwg.org"):
        repo = host[: -len(".spec.whatwg.org")]
    elif host == "whatwg.github.io" and path_parts:
        repo = path_parts[0]
    elif host.endswith(".idea.whatwg.org"):
        repo = host[: -len(".idea.whatwg.org")]
    elif host.endswith(".whatwg.org"):
        repo = host.split(".", 1)[0]
    if repo:
        repo = repo.removesuffix(".git")
        return dataclasses.replace(spec, repo_owner=owner, repo_name=repo)
    warnings.append(RunWarning(spec.name, f"Could not infer repository from {spec.spec_url}; skipping this standard."))
    return spec


def load_entities(
    url: str,
    include_unverified: bool,
    warnings: list[RunWarning],
) -> tuple[list[Entity], set[str], dict[str, Any]]:
    data, meta = json_fetch_with_metadata(url)
    if not isinstance(data, list):
        raise ReportError(f"Expected {url} to contain an array")
    entities: list[Entity] = []
    non_public_logins: set[str] = set()
    seen_ids: set[str] = set()
    for item in data:
        if not isinstance(item, Mapping):
            continue
        if not is_public_participant(item):
            non_public_logins.update(entity_contact_logins(item))
            continue
        verified = bool(item.get("verified"))
        if not include_unverified and not verified:
            continue
        info = item.get("info") or {}
        if not isinstance(info, Mapping):
            continue
        org = normalize_github_org(info.get("gitHubOrganization"))
        if not org:
            warnings.append(RunWarning("participant-data", f"Skipping entity {info.get('name') or item.get('id')} because gitHubOrganization is missing or invalid."))
            continue
        name = clean_ws(str(info.get("name") or org))
        contacts = entity_contact_logins(item)
        entity = Entity(
            entity_id=str(item.get("id") or org),
            name=name,
            org_login=org,
            verified=verified,
            workstreams=participant_workstreams(item.get("workstreams")),
            contacts=frozenset(contacts),
            url=str(info.get("url") or ""),
        )
        if entity.entity_id in seen_ids:
            warnings.append(RunWarning("participant-data", f"Duplicate entity id {entity.entity_id}; keeping duplicate as separate entity."))
        seen_ids.add(entity.entity_id)
        entities.append(entity)
    return entities, non_public_logins, meta


def load_individuals(
    url: str,
    include_unverified: bool,
) -> tuple[dict[str, Individual], set[str], dict[str, Any]]:
    data, meta = json_fetch_with_metadata(url)
    if not isinstance(data, list):
        raise ReportError(f"Expected {url} to contain an array")
    individuals: dict[str, Individual] = {}
    non_public_logins: set[str] = set()
    for item in data:
        if not isinstance(item, Mapping):
            continue
        info = item.get("info") or {}
        if not isinstance(info, Mapping):
            continue
        login = normalize_login(info.get("gitHubID"))
        if not login:
            continue
        if not is_public_participant(item):
            non_public_logins.add(login)
            continue
        verified = bool(item.get("verified"))
        if not include_unverified and not verified:
            continue
        individuals[login] = Individual(
            participant_id=str(item.get("id") or login),
            name=clean_ws(str(info.get("name") or login)),
            login=display_login(str(info.get("gitHubID") or login)),
            verified=verified,
            workstreams=participant_workstreams(item.get("workstreams")),
        )
    return individuals, non_public_logins, meta


def load_cache(path: str) -> dict[str, Any]:
    if not path or not os.path.exists(path) or os.path.getsize(path) == 0:
        return {"version": CACHE_VERSION, "repositories": {}}
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict) or data.get("version") != CACHE_VERSION:
        return {"version": CACHE_VERSION, "repositories": {}}
    data.setdefault("repositories", {})
    return data


def write_cache(path: str, cache: Mapping[str, Any]) -> None:
    if not path:
        return
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=2, sort_keys=True)
        f.write("\n")


def fetch_repo_commits(
    client: GitHubClient,
    spec: Spec,
    branch: str,
    cache: MutableMapping[str, Any],
    incremental: bool,
    page_size: int,
    warnings: list[RunWarning],
    retry_full_on_mismatch: bool = True,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    repo_key = f"{spec.repo_full_name}@{branch}"
    repositories = cache.setdefault("repositories", {})
    repo_cache = repositories.setdefault(repo_key, {"commits": {}})
    cached_commits: dict[str, dict[str, Any]] = repo_cache.setdefault("commits", {})
    commits: dict[str, dict[str, Any]] = {}
    after: Optional[str] = None
    total_count: Optional[int] = None
    pages = 0
    new_count = 0
    stopped_on_cache = False

    print(f"Fetching commits for {spec.name} ({spec.repo_full_name}@{branch})", file=sys.stderr)
    while True:
        variables = {
            "owner": spec.repo_owner,
            "repo": spec.repo_name,
            "branch": branch,
            "after": after,
            "pageSize": page_size,
        }
        data = client.graphql(GRAPHQL_HISTORY_QUERY, variables)
        repository = data.get("repository") if data else None
        if not repository:
            warnings.append(RunWarning(spec.name, f"Repository {spec.repo_full_name} not found or not visible."))
            break
        ref = repository.get("ref")
        if not ref:
            warnings.append(RunWarning(spec.name, f"Branch/ref {branch!r} not found in {spec.repo_full_name}."))
            break
        target = ref.get("target") or {}
        if target.get("__typename") != "Commit":
            warnings.append(RunWarning(spec.name, f"Ref {branch!r} in {spec.repo_full_name} is not a commit."))
            break
        history = target.get("history") or {}
        total_count = int(history.get("totalCount") or 0)
        page_info = history.get("pageInfo") or {}
        nodes = history.get("nodes") or []
        page_all_cached = bool(nodes)

        for node in nodes:
            oid = str(node.get("oid") or "")
            if not oid:
                continue
            if incremental and oid in cached_commits:
                commit = cached_commits[oid]
            else:
                commit = normalize_commit_node(node, branch)
                if oid not in cached_commits:
                    new_count += 1
                page_all_cached = False
            commits[oid] = commit

        pages += 1
        if incremental and page_all_cached:
            stopped_on_cache = True
            for oid, commit in cached_commits.items():
                commits.setdefault(oid, commit)
            break
        if not page_info.get("hasNextPage"):
            break
        after = page_info.get("endCursor")
        if not after:
            break

    if incremental and retry_full_on_mismatch and total_count is not None and commits and len(commits) != total_count:
        warnings.append(
            RunWarning(
                spec.name,
                f"Incremental cache for {repo_key} had {len(commits)} commits but GitHub reports {total_count}; retrying with a full scan.",
            )
        )
        return fetch_repo_commits(
            client=client,
            spec=spec,
            branch=branch,
            cache=cache,
            incremental=False,
            page_size=page_size,
            warnings=warnings,
            retry_full_on_mismatch=False,
        )

    repo_cache.update(
        {
            "repo": spec.repo_full_name,
            "branch": branch,
            "fetchedAt": now_iso(),
            "totalCount": total_count if total_count is not None else len(commits),
            "commits": commits,
        }
    )
    meta = {
        "totalCount": total_count if total_count is not None else len(commits),
        "pagesFetched": pages,
        "newCommitsFetched": new_count,
        "stoppedOnCache": stopped_on_cache,
    }
    return list(commits.values()), meta


def normalize_commit_node(node: Mapping[str, Any], branch: str) -> dict[str, Any]:
    prs = (((node.get("associatedPullRequests") or {}).get("nodes")) or [])
    merged_prs = [pr for pr in prs if pr and pr.get("merged")]
    branch_prs = [pr for pr in merged_prs if not pr.get("baseRefName") or pr.get("baseRefName") == branch]
    selected_pr = select_pr(branch_prs or merged_prs)
    author_user = ((node.get("author") or {}).get("user") or {})
    author_login = normalize_login(author_user.get("login"))
    pr_author = (selected_pr.get("author") if selected_pr else None) or {}
    pr_author_login = normalize_login(pr_author.get("login"))
    return {
        "oid": str(node.get("oid") or ""),
        "committedDate": str(node.get("committedDate") or ""),
        "authorLogin": author_login,
        "authorName": str((node.get("author") or {}).get("name") or ""),
        "authorEmail": str((node.get("author") or {}).get("email") or ""),
        "prNumber": selected_pr.get("number") if selected_pr else None,
        "prUrl": selected_pr.get("url") if selected_pr else None,
        "prMergedAt": selected_pr.get("mergedAt") if selected_pr else None,
        "prBaseRefName": selected_pr.get("baseRefName") if selected_pr else None,
        "prAuthorLogin": pr_author_login,
        "prAuthorType": pr_author.get("__typename") if pr_author else None,
    }


def select_pr(prs: list[Mapping[str, Any]]) -> Optional[Mapping[str, Any]]:
    if not prs:
        return None
    return sorted(prs, key=lambda pr: str(pr.get("mergedAt") or ""), reverse=True)[0]


def build_affiliations(
    client: GitHubClient,
    contributors: set[str],
    entities: list[Entity],
    source: str,
    include_contacts: bool,
    warnings: list[RunWarning],
) -> dict[str, set[str]]:
    """Return contributor login -> public entity ids."""
    login_to_entity_ids: dict[str, set[str]] = {login: set() for login in contributors}
    org_to_entities: DefaultDict[str, list[Entity]] = collections.defaultdict(list)
    for entity in entities:
        org_to_entities[normalize_login(entity.org_login)].append(entity)

    if include_contacts:
        for entity in entities:
            for contact in entity.contacts:
                if contact in login_to_entity_ids:
                    login_to_entity_ids[contact].add(entity.entity_id)

    if source in {"user-orgs", "both"}:
        for index, login in enumerate(sorted(contributors), start=1):
            if not login:
                continue
            print(f"Fetching public orgs for contributor {login} ({index}/{len(contributors)})", file=sys.stderr)
            try:
                orgs = client.paginate(f"/users/{urllib.parse.quote(login)}/orgs", allow_404=True)
                for org in orgs:
                    org_login = normalize_login(org.get("login"))
                    for entity in org_to_entities.get(org_login, []):
                        login_to_entity_ids[login].add(entity.entity_id)
            except Exception as exc:
                warnings.append(RunWarning("affiliations", f"Could not fetch public orgs for {login}: {exc}"))

    if source in {"org-members", "both"}:
        for index, (org_login, org_entities) in enumerate(sorted(org_to_entities.items()), start=1):
            print(f"Fetching public members for org {org_login} ({index}/{len(org_to_entities)})", file=sys.stderr)
            try:
                members = client.paginate(f"/orgs/{urllib.parse.quote(org_login)}/public_members", allow_404=True)
                for member in members:
                    member_login = normalize_login(member.get("login"))
                    if member_login in login_to_entity_ids:
                        for entity in org_entities:
                            login_to_entity_ids[member_login].add(entity.entity_id)
            except Exception as exc:
                warnings.append(RunWarning("affiliations", f"Could not fetch public members for org {org_login}: {exc}"))

    return login_to_entity_ids


def collect_contributor_logins(
    commits_by_repo: Mapping[str, list[dict[str, Any]]],
    non_public_participant_logins: set[str],
) -> set[str]:
    contributors: set[str] = set()
    for commits in commits_by_repo.values():
        for commit in commits:
            login = normalize_login(commit.get("prAuthorLogin"))
            if not login:
                login = normalize_login(commit.get("authorLogin"))
            if login and login not in non_public_participant_logins:
                contributors.add(login)
    return contributors


def compute_spec_report(
    spec: Spec,
    commits: list[dict[str, Any]],
    repo_meta: Mapping[str, Any],
    branch: str,
    entities_by_id: Mapping[str, Entity],
    individuals_by_login: Mapping[str, Individual],
    login_to_entity_ids: Mapping[str, set[str]],
    non_public_participant_logins: set[str],
    entity_attribution: str,
    ignore_participant_workstreams: bool,
) -> dict[str, Any]:
    total_commits = len(commits)
    pr_commits = 0
    direct_commits = 0
    credited_commits = 0
    uncredited_commits = 0
    no_login_commits = 0
    date_values = [c.get("committedDate") for c in commits if c.get("committedDate")]
    first_commit_date = min(date_values) if date_values else None
    last_commit_date = max(date_values) if date_values else None

    individual_stats: dict[str, dict[str, Any]] = {}
    entity_stats: dict[str, dict[str, Any]] = {}

    def entity_applies(entity: Entity) -> bool:
        return ignore_participant_workstreams or entity.workstreams.applies_to(spec.workstream_id)

    def individual_applies(individual: Individual) -> bool:
        return ignore_participant_workstreams or individual.workstreams.applies_to(spec.workstream_id)

    for commit in commits:
        pr_author = normalize_login(commit.get("prAuthorLogin"))
        author = normalize_login(commit.get("authorLogin"))
        has_pr = bool(pr_author)
        if has_pr:
            pr_commits += 1
            credit_login = pr_author
            credit_source = "merged-pr"
        else:
            direct_commits += 1
            credit_login = author
            credit_source = "direct-commit" if credit_login else "uncredited-direct"

        if not credit_login:
            uncredited_commits += 1
            if not author and not pr_author:
                no_login_commits += 1
            continue
        if credit_login in non_public_participant_logins:
            uncredited_commits += 1
            continue

        credited_commits += 1
        individual_record = individuals_by_login.get(credit_login)
        individual = individual_record if individual_record and individual_applies(individual_record) else None
        individual_is_signed = bool(individual)
        eligible_entity_ids = sorted(
            entity_id
            for entity_id in login_to_entity_ids.get(credit_login, set())
            if entity_id in entities_by_id and entity_applies(entities_by_id[entity_id])
        )
        weight = 1.0
        if eligible_entity_ids and entity_attribution == "fractional":
            weight = 1.0 / len(eligible_entity_ids)

        row = individual_stats.setdefault(
            credit_login,
            {
                "login": credit_login,
                "name": individual.name if individual else "",
                "signedIndividual": individual_is_signed,
                "entityAffiliations": collections.Counter(),
                "entityAffiliationRawMatches": collections.Counter(),
                "commitCount": 0,
                "prCommitCount": 0,
                "directCommitCount": 0,
                "botOrApp": False,
                "sources": collections.Counter(),
            },
        )
        if individual and not row["name"]:
            row["name"] = individual.name
        row["signedIndividual"] = bool(row["signedIndividual"] or individual_is_signed)
        row["commitCount"] += 1
        if credit_source == "merged-pr":
            row["prCommitCount"] += 1
        elif credit_source == "direct-commit":
            row["directCommitCount"] += 1
        row["sources"][credit_source] += 1
        if (commit.get("prAuthorType") or "").lower() in {"bot", "app"}:
            row["botOrApp"] = True

        for entity_id in eligible_entity_ids:
            entity = entities_by_id[entity_id]
            row["entityAffiliations"][entity.name] += weight
            row["entityAffiliationRawMatches"][entity.name] += 1

        if eligible_entity_ids:
            for entity_id in eligible_entity_ids:
                entity = entities_by_id[entity_id]
                est = entity_stats.setdefault(
                    entity_id,
                    {
                        "entityId": entity.entity_id,
                        "name": entity.name,
                        "gitHubOrganization": entity.org_login,
                        "url": entity.url,
                        "commitCredit": 0.0,
                        "contributors": collections.Counter(),
                        "contributorRawMatches": collections.Counter(),
                        "rawCommitMatches": 0,
                    },
                )
                est["commitCredit"] += weight
                est["rawCommitMatches"] += 1
                est["contributors"][credit_login] += weight
                est["contributorRawMatches"][credit_login] += 1

    def pct(value: float) -> float:
        return (value / total_commits * 100.0) if total_commits else 0.0

    entities_out = []
    for row in entity_stats.values():
        contributors = [
            {
                "login": login,
                "commitCredit": round(float(credit), 6),
                "rawCommitMatches": int(row["contributorRawMatches"].get(login, 0)),
            }
            for login, credit in sorted(row["contributors"].items(), key=lambda kv: (-kv[1], kv[0]))
        ]
        entities_out.append(
            {
                "entityId": row["entityId"],
                "name": row["name"],
                "gitHubOrganization": row["gitHubOrganization"],
                "url": row["url"],
                "commitCredit": round(float(row["commitCredit"]), 6),
                "percentageOfAllCommits": round(pct(float(row["commitCredit"])), 6),
                "rawCommitMatches": int(row["rawCommitMatches"]),
                "contributors": contributors,
            }
        )
    entities_out.sort(key=lambda row: (-row["commitCredit"], row["name"].lower()))

    individuals_out = []
    for row in individual_stats.values():
        affiliations_list = [
            {
                "entity": name,
                "commitCredit": round(float(credit), 6),
                "rawCommitMatches": int(row["entityAffiliationRawMatches"].get(name, 0)),
            }
            for name, credit in sorted(row["entityAffiliations"].items(), key=lambda kv: (-kv[1], kv[0].lower()))
        ]
        status_parts = []
        if row["signedIndividual"]:
            status_parts.append("public signed individual")
        if affiliations_list:
            status_parts.append("public entity member/contact")
        if row["botOrApp"]:
            status_parts.append("bot/app")
        if not status_parts:
            status_parts.append("no public entity attribution")
        individuals_out.append(
            {
                "login": row["login"],
                "name": row["name"],
                "status": ", ".join(status_parts),
                "signedIndividual": bool(row["signedIndividual"]),
                "hasPublicEntityAttribution": bool(affiliations_list),
                "commitCount": int(row["commitCount"]),
                "prCommitCount": int(row["prCommitCount"]),
                "directCommitCount": int(row["directCommitCount"]),
                "percentageOfAllCommits": round(pct(int(row["commitCount"])), 6),
                "entityAffiliations": affiliations_list,
            }
        )
    individuals_out.sort(key=lambda row: (-row["commitCount"], row["login"]))

    return {
        "workstreamTitle": spec.workstream_title,
        "workstreamId": spec.workstream_id,
        "name": spec.name,
        "specUrl": spec.spec_url,
        "repository": spec.repo_full_name,
        "repositoryUrl": spec.repo_url,
        "branch": branch,
        "totalCommits": total_commits,
        "mergedPrAssociatedCommits": pr_commits,
        "directOrNoPrCommits": direct_commits,
        "creditedCommits": credited_commits,
        "uncreditedCommits": uncredited_commits,
        "noLoginCommits": no_login_commits,
        "creditedPercentageOfAllCommits": round(pct(credited_commits), 6),
        "uncreditedPercentageOfAllCommits": round(pct(uncredited_commits), 6),
        "firstCommitDate": first_commit_date,
        "lastCommitDate": last_commit_date,
        "repoFetch": dict(repo_meta),
        "entities": entities_out,
        "individuals": individuals_out,
    }


def build_totals(spec_reports: list[Mapping[str, Any]]) -> dict[str, int]:
    return {
        "specCount": len(spec_reports),
        "totalCommits": sum(int(s.get("totalCommits") or 0) for s in spec_reports),
        "mergedPrAssociatedCommits": sum(int(s.get("mergedPrAssociatedCommits") or 0) for s in spec_reports),
        "directOrNoPrCommits": sum(int(s.get("directOrNoPrCommits") or 0) for s in spec_reports),
        "creditedCommits": sum(int(s.get("creditedCommits") or 0) for s in spec_reports),
        "uncreditedCommits": sum(int(s.get("uncreditedCommits") or 0) for s in spec_reports),
    }


def build_aggregate(spec_reports: list[Mapping[str, Any]], totals: Mapping[str, Any]) -> dict[str, Any]:
    total_commits = int(totals.get("totalCommits") or 0)

    entity_rows: dict[str, dict[str, Any]] = {}
    individual_rows: dict[str, dict[str, Any]] = {}

    for spec in spec_reports:
        spec_name = str(spec.get("name") or "")
        for entity in spec.get("entities") or []:
            entity_id = str(entity.get("entityId") or entity.get("name") or "")
            if not entity_id:
                continue
            row = entity_rows.setdefault(
                entity_id,
                {
                    "entityId": entity_id,
                    "name": entity.get("name") or "",
                    "gitHubOrganization": entity.get("gitHubOrganization") or "",
                    "url": entity.get("url") or "",
                    "commitCredit": 0.0,
                    "rawCommitMatches": 0,
                    "specs": [],
                },
            )
            credit = float(entity.get("commitCredit") or 0.0)
            row["commitCredit"] += credit
            row["rawCommitMatches"] += int(entity.get("rawCommitMatches") or 0)
            row["specs"].append({"name": spec_name, "commitCredit": round(credit, 6)})

        for individual in spec.get("individuals") or []:
            login = str(individual.get("login") or "")
            if not login:
                continue
            row = individual_rows.setdefault(
                login,
                {
                    "login": login,
                    "name": individual.get("name") or "",
                    "statuses": set(),
                    "commitCount": 0,
                    "prCommitCount": 0,
                    "directCommitCount": 0,
                    "specs": [],
                },
            )
            if individual.get("name") and not row["name"]:
                row["name"] = individual.get("name")
            if individual.get("status"):
                row["statuses"].add(str(individual.get("status")))
            count = int(individual.get("commitCount") or 0)
            row["commitCount"] += count
            row["prCommitCount"] += int(individual.get("prCommitCount") or 0)
            row["directCommitCount"] += int(individual.get("directCommitCount") or 0)
            row["specs"].append({"name": spec_name, "commitCount": count})

    def pct(value: float) -> float:
        return (value / total_commits * 100.0) if total_commits else 0.0

    entities_out = []
    for row in entity_rows.values():
        specs = sorted(row["specs"], key=lambda item: (-float(item.get("commitCredit") or 0), str(item.get("name") or "")))
        entities_out.append(
            {
                "entityId": row["entityId"],
                "name": row["name"],
                "gitHubOrganization": row["gitHubOrganization"],
                "url": row["url"],
                "commitCredit": round(float(row["commitCredit"]), 6),
                "percentageOfAllCommits": round(pct(float(row["commitCredit"])), 6),
                "rawCommitMatches": int(row["rawCommitMatches"]),
                "specCount": len(specs),
                "topSpecs": specs[:8],
            }
        )
    entities_out.sort(key=lambda row: (-float(row["commitCredit"]), str(row["name"]).lower()))

    individuals_out = []
    for row in individual_rows.values():
        specs = sorted(row["specs"], key=lambda item: (-int(item.get("commitCount") or 0), str(item.get("name") or "")))
        statuses = sorted(str(status) for status in row["statuses"] if status)
        individuals_out.append(
            {
                "login": row["login"],
                "name": row["name"],
                "status": "; ".join(statuses),
                "commitCount": int(row["commitCount"]),
                "prCommitCount": int(row["prCommitCount"]),
                "directCommitCount": int(row["directCommitCount"]),
                "percentageOfAllCommits": round(pct(int(row["commitCount"])), 6),
                "specCount": len(specs),
                "topSpecs": specs[:8],
            }
        )
    individuals_out.sort(key=lambda row: (-int(row["commitCount"]), str(row["login"])))

    return {"entities": entities_out, "individuals": individuals_out}


def build_html_report(report: Mapping[str, Any]) -> str:
    generated = escape(report["generatedAt"])
    specs = report["specs"]
    totals = report["totals"]
    aggregate = report.get("aggregate") or {"entities": [], "individuals": []}
    warnings = report.get("warnings") or []
    css = """
:root { color-scheme: light dark; --border: #d0d7de; --muted: #57606a; --bg: #ffffff; --soft: #f6f8fa; --fg: #24292f; --accent: #0969da; }
@media (prefers-color-scheme: dark) { :root { --border: #30363d; --muted: #8b949e; --bg: #0d1117; --soft: #161b22; --fg: #c9d1d9; --accent: #58a6ff; } }
* { box-sizing: border-box; }
body { margin: 0; font: 16px/1.5 system-ui, -apple-system, Segoe UI, sans-serif; color: var(--fg); background: var(--bg); }
a { color: var(--accent); }
header { padding: 2rem; border-bottom: 1px solid var(--border); background: var(--soft); }
main { padding: 1.5rem 2rem 3rem; max-width: 1500px; margin: 0 auto; }
h1 { margin: 0 0 .5rem; }
h2 { margin-top: 2.5rem; border-bottom: 1px solid var(--border); padding-bottom: .25rem; }
h3 { margin-top: 1.5rem; }
code { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
.muted { color: var(--muted); }
.cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(12rem, 1fr)); gap: 1rem; margin: 1rem 0; }
.card { border: 1px solid var(--border); border-radius: .75rem; padding: 1rem; background: var(--bg); }
.card strong { display: block; font-size: 1.6rem; }
table { border-collapse: collapse; width: 100%; margin: .75rem 0 1.5rem; font-size: .95rem; }
th, td { border: 1px solid var(--border); padding: .45rem .6rem; vertical-align: top; }
th { text-align: left; background: var(--soft); position: sticky; top: 0; }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
.badge { display: inline-block; padding: .12rem .45rem; border: 1px solid var(--border); border-radius: 999px; background: var(--soft); margin: .05rem; font-size: .85rem; }
.warning { border-left: .35rem solid #bf8700; padding: .75rem 1rem; background: var(--soft); margin: .75rem 0; }
details { margin: .75rem 0 1.5rem; }
summary { cursor: pointer; color: var(--accent); }
.toc { columns: 2 18rem; }
.small { font-size: .9rem; }
.provenance dt { font-weight: 600; margin-top: .5rem; }
.provenance dd { margin-left: 1rem; }
"""
    parts = [
        "<!doctype html>",
        '<html lang="en">',
        "<head>",
        '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        "<title>WHATWG contribution attribution report</title>",
        f"<style>{css}</style>",
        "</head>",
        "<body>",
        "<header>",
        "<h1>WHATWG contribution attribution report</h1>",
        f'<p class="muted">Generated {generated}. Branch: <code>{escape(report["branch"])}</code>. Data: <a href="report.json">report.json</a>.</p>',
        "<p>This report credits commits reachable from each specification repository branch. Commits associated by GitHub with a merged pull request are credited to the PR author. Direct/no-PR commits are credited to the resolved GitHub commit author. Percentages use all commits in the branch as the denominator.</p>",
        "<p><strong>Public data note:</strong> Participant-data entries explicitly marked non-Public are ignored. Entity attribution uses public GitHub organization memberships and public participant-data contacts only; private or concealed memberships are intentionally ignored, even if the token used for the run could see them.</p>",
        "<p><strong>Uncredited commits:</strong> These are commits with no reportable credited GitHub login, such as commits for which GitHub has no associated PR author and no resolved GitHub commit author.</p>",
        "<p><strong>Affiliation disclaimer:</strong> Historical employer changes are not reconstructed. Past contributions are credited to the contributor's current public entity affiliation at generation time.</p>",
        "</header>",
        "<main>",
        "<section>",
        "<h2>Summary</h2>",
        '<div class="cards">',
        card("Specs", totals["specCount"]),
        card("Commits", totals["totalCommits"]),
        card("Merged-PR commits", totals["mergedPrAssociatedCommits"]),
        card("Direct/no-PR commits", totals["directOrNoPrCommits"]),
        card("Credited commits", totals["creditedCommits"]),
        card("Uncredited commits", totals["uncreditedCommits"]),
        "</div>",
        render_provenance_details(report),
    ]
    if warnings:
        parts.append("<details open><summary>Warnings</summary>")
        for warning in warnings:
            parts.append(f'<div class="warning"><strong>{escape(warning.get("area", "warning"))}</strong>: {escape(warning.get("message", ""))}</div>')
        parts.append("</details>")

    parts.extend([
        "<h3>Specifications overview</h3>",
        render_specs_summary_table(specs),
        "<h3>Aggregate rankings</h3>",
        '<p class="muted small">Aggregated percentages use all commits across all listed spec repositories as the denominator.</p>',
        "<h4>Top entities</h4>",
        render_aggregate_entities_table(aggregate.get("entities") or [], limit=25),
        "<h4>Top individuals</h4>",
        render_aggregate_individuals_table(aggregate.get("individuals") or [], limit=25),
        "<h3>Specifications</h3>",
        '<ul class="toc">',
    ])
    for spec in specs:
        anchor = spec_anchor(spec)
        parts.append(f'<li><a href="#{anchor}">{escape(spec["name"])}</a> <span class="muted">({escape(spec["repository"])})</span></li>')
    parts.extend(["</ul>", "</section>"])

    for spec in specs:
        parts.append(render_spec_section(spec))

    parts.append("</main></body></html>")
    return "\n".join(parts)


def render_provenance_details(report: Mapping[str, Any]) -> str:
    provenance = report.get("provenance") or {}
    inputs = provenance.get("inputs") or {}
    run = provenance.get("run") or {}
    parts = [
        '<details><summary>Data provenance</summary>',
        '<dl class="provenance small">',
    ]
    if run:
        mode = "incremental/cache-assisted" if run.get("incremental") else "full scan"
        parts.append("<dt>Run</dt>")
        bits = [f"mode: {escape(mode)}"]
        if run.get("githubRepository"):
            bits.append(f"repository: <code>{escape(run.get('githubRepository'))}</code>")
        if run.get("githubSha"):
            bits.append(f"script commit: <code>{escape(run.get('githubSha'))}</code>")
        if run.get("githubRunId"):
            bits.append(f"run id: <code>{escape(run.get('githubRunId'))}</code>")
        parts.append(f"<dd>{' · '.join(bits)}</dd>")
    for label, meta in inputs.items():
        if not isinstance(meta, Mapping):
            continue
        parts.append(f"<dt>{escape(label)}</dt>")
        configured_url = meta.get("configuredUrl") or meta.get("url") or ""
        resolved_url = meta.get("resolvedUrl") or meta.get("url") or ""
        bits = []
        if configured_url:
            bits.append(f'<a href="{escape_attr(configured_url)}">configured source</a>')
        if resolved_url and resolved_url != configured_url:
            bits.append(f'<a href="{escape_attr(resolved_url)}">commit-pinned source used</a>')
        elif resolved_url:
            bits.append(f'<a href="{escape_attr(resolved_url)}">source used</a>')
        if meta.get("githubRepository"):
            bits.append(f"repository <code>{escape(meta.get('githubRepository'))}</code>")
        if meta.get("githubCommitSha"):
            commit_text = f"commit <code>{escape(str(meta.get('githubCommitSha'))[:12])}</code>"
            if meta.get("githubCommitUrl"):
                commit_text = f'<a href="{escape_attr(meta.get("githubCommitUrl"))}">{commit_text}</a>'
            bits.append(commit_text)
        if meta.get("etag"):
            bits.append(f"ETag <code>{escape(meta.get('etag'))}</code>")
        if meta.get("lastModified"):
            bits.append(f"Last-Modified <code>{escape(meta.get('lastModified'))}</code>")
        if meta.get("sha256"):
            bits.append(f"SHA-256 <code>{escape(meta.get('sha256'))}</code>")
        if meta.get("fetchedAt"):
            bits.append(f"fetched <code>{escape(meta.get('fetchedAt'))}</code>")
        parts.append(f"<dd>{' · '.join(bits)}</dd>")
    parts.extend(["</dl>", "</details>"])
    return "\n".join(parts)


def card(label: str, value: Any) -> str:
    return f'<div class="card"><span class="muted">{escape(label)}</span><strong>{escape(format_number(value))}</strong></div>'


def render_specs_summary_table(specs: list[Mapping[str, Any]]) -> str:
    if not specs:
        return '<p class="muted">No specs.</p>'
    parts = [
        "<table>",
        '<thead><tr><th>Spec</th><th>Workstream</th><th>Repo</th><th class="num">Commits</th><th class="num">Credited</th><th class="num">Uncredited</th><th>Top entity</th><th>Top individual</th></tr></thead>',
        "<tbody>",
    ]
    for spec in specs:
        top_entity = (spec.get("entities") or [{}])[0] if spec.get("entities") else {}
        top_individual = (spec.get("individuals") or [{}])[0] if spec.get("individuals") else {}
        top_entity_html = ""
        if top_entity:
            top_entity_html = f'{escape(top_entity.get("name", ""))} <span class="muted">({format_pct(top_entity.get("percentageOfAllCommits", 0))})</span>'
        top_individual_html = ""
        if top_individual:
            login = top_individual.get("login") or ""
            top_individual_html = f'<a href="https://github.com/{escape_attr(login)}">{escape(login)}</a> <span class="muted">({format_pct(top_individual.get("percentageOfAllCommits", 0))})</span>'
        parts.append(
            "<tr>"
            f'<td><a href="#{spec_anchor(spec)}">{escape(spec.get("name", ""))}</a></td>'
            f'<td>{escape(spec.get("workstreamTitle", ""))}</td>'
            f'<td><a href="{escape_attr(spec.get("repositoryUrl", ""))}">{escape(spec.get("repository", ""))}</a></td>'
            f'<td class="num">{format_number(spec.get("totalCommits", 0))}</td>'
            f'<td class="num">{format_pct(spec.get("creditedPercentageOfAllCommits", 0))}</td>'
            f'<td class="num">{format_pct(spec.get("uncreditedPercentageOfAllCommits", 0))}</td>'
            f"<td>{top_entity_html}</td>"
            f"<td>{top_individual_html}</td>"
            "</tr>"
        )
    parts.extend(["</tbody></table>"])
    return "\n".join(parts)


def render_aggregate_entities_table(rows: list[Mapping[str, Any]], limit: int = 25) -> str:
    if not rows:
        return '<p class="muted">No entity-attributed commits.</p>'
    shown = rows[:limit]
    parts = [
        "<table>",
        '<thead><tr><th class="num">#</th><th>Entity</th><th>GitHub org</th><th class="num">Commit credit</th><th class="num">% of all commits</th><th class="num">Specs</th><th>Top specs</th></tr></thead>',
        "<tbody>",
    ]
    for idx, row in enumerate(shown, start=1):
        org = row.get("gitHubOrganization") or ""
        org_html = f'<a href="https://github.com/{escape_attr(org)}">{escape(org)}</a>' if org else ""
        top_specs = " ".join(
            f'<span class="badge">{escape(item.get("name", ""))}: {format_credit(item.get("commitCredit", 0))}</span>'
            for item in row.get("topSpecs", [])
        )
        parts.append(
            "<tr>"
            f'<td class="num">{idx}</td>'
            f'<td>{escape(row.get("name", ""))}</td>'
            f"<td>{org_html}</td>"
            f'<td class="num">{format_credit(row.get("commitCredit", 0))}</td>'
            f'<td class="num">{format_pct(row.get("percentageOfAllCommits", 0))}</td>'
            f'<td class="num">{format_number(row.get("specCount", 0))}</td>'
            f"<td>{top_specs}</td>"
            "</tr>"
        )
    parts.extend(["</tbody></table>"])
    if len(rows) > limit:
        parts.append(f'<p class="muted small">Showing top {limit} of {len(rows)} entities. Full data is in <a href="report.json">report.json</a>.</p>')
    return "\n".join(parts)


def render_aggregate_individuals_table(rows: list[Mapping[str, Any]], limit: int = 25) -> str:
    if not rows:
        return '<p class="muted">No individual-attributed commits.</p>'
    shown = rows[:limit]
    parts = [
        "<table>",
        '<thead><tr><th class="num">#</th><th>GitHub login</th><th>Name</th><th class="num">Commits</th><th class="num">PR commits</th><th class="num">Direct commits</th><th class="num">% of all commits</th><th class="num">Specs</th><th>Top specs</th></tr></thead>',
        "<tbody>",
    ]
    for idx, row in enumerate(shown, start=1):
        login = row.get("login") or ""
        top_specs = " ".join(
            f'<span class="badge">{escape(item.get("name", ""))}: {format_number(item.get("commitCount", 0))}</span>'
            for item in row.get("topSpecs", [])
        )
        parts.append(
            "<tr>"
            f'<td class="num">{idx}</td>'
            f'<td><a href="https://github.com/{escape_attr(login)}">{escape(login)}</a></td>'
            f'<td>{escape(row.get("name", ""))}</td>'
            f'<td class="num">{format_number(row.get("commitCount", 0))}</td>'
            f'<td class="num">{format_number(row.get("prCommitCount", 0))}</td>'
            f'<td class="num">{format_number(row.get("directCommitCount", 0))}</td>'
            f'<td class="num">{format_pct(row.get("percentageOfAllCommits", 0))}</td>'
            f'<td class="num">{format_number(row.get("specCount", 0))}</td>'
            f"<td>{top_specs}</td>"
            "</tr>"
        )
    parts.extend(["</tbody></table>"])
    if len(rows) > limit:
        parts.append(f'<p class="muted small">Showing top {limit} of {len(rows)} individuals. Full data is in <a href="report.json">report.json</a>.</p>')
    return "\n".join(parts)


def render_spec_section(spec: Mapping[str, Any]) -> str:
    total = int(spec.get("totalCommits") or 0)
    anchor = spec_anchor(spec)
    individual_rows = spec.get("individuals") or []
    parts = [
        f'<section id="{anchor}">',
        f'<h2>{escape(spec["name"])} <span class="muted">{escape(spec["workstreamTitle"])} workstream</span></h2>',
        '<p class="small">'
        f'<a href="{escape_attr(spec["specUrl"])}">Standard</a> · '
        f'<a href="{escape_attr(spec["repositoryUrl"])}">{escape(spec["repository"])}</a> · '
        f'Branch <code>{escape(spec["branch"])}</code> · '
        f'{format_number(total)} total commits · '
        f'{format_number(spec["mergedPrAssociatedCommits"])} merged-PR-associated commits · '
        f'{format_number(spec["directOrNoPrCommits"])} direct/no-PR commits · '
        f'{format_number(spec["uncreditedCommits"])} uncredited commits'
        "</p>",
    ]
    if spec.get("firstCommitDate") and spec.get("lastCommitDate"):
        parts.append(f'<p class="muted small">Commit date range: {escape(spec["firstCommitDate"])} to {escape(spec["lastCommitDate"])}.</p>')

    parts.append("<h3>Entities</h3>")
    parts.append(render_entities_table(spec.get("entities") or [], total))
    parts.append(f'<details><summary>Individuals ({len(individual_rows)})</summary>')
    parts.append(render_individuals_table(individual_rows, total))
    parts.append("</details>")
    parts.append("</section>")
    return "\n".join(parts)


def render_entities_table(rows: list[Mapping[str, Any]], total: int) -> str:
    if not rows:
        return '<p class="muted">No entity-attributed commits.</p>'
    parts = [
        "<table>",
        '<thead><tr><th class="num">#</th><th>Entity</th><th>GitHub org</th><th class="num">Commit credit</th><th class="num">% of all commits</th><th>Contributor credit</th></tr></thead>',
        "<tbody>",
    ]
    for idx, row in enumerate(rows, start=1):
        contributors = " ".join(
            contributor_badge(c.get("login", ""), c.get("commitCredit", 0), c.get("rawCommitMatches", 0))
            for c in row.get("contributors", [])[:12]
        )
        if len(row.get("contributors", [])) > 12:
            contributors += f' <span class="muted">+{len(row.get("contributors", [])) - 12} more</span>'
        org = row.get("gitHubOrganization") or ""
        org_html = f'<a href="https://github.com/{escape_attr(org)}">{escape(org)}</a>' if org else ""
        parts.append(
            "<tr>"
            f'<td class="num">{idx}</td>'
            f'<td>{escape(row.get("name", ""))}</td>'
            f"<td>{org_html}</td>"
            f'<td class="num">{format_credit(row.get("commitCredit", 0))}</td>'
            f'<td class="num">{format_pct(row.get("percentageOfAllCommits", 0))}</td>'
            f"<td>{contributors}</td>"
            "</tr>"
        )
    parts.extend(["</tbody></table>"])
    return "\n".join(parts)


def render_individuals_table(rows: list[Mapping[str, Any]], total: int) -> str:
    if not rows:
        return '<p class="muted">No individual-attributed commits.</p>'
    parts = [
        "<table>",
        '<thead><tr><th class="num">#</th><th>GitHub login</th><th>Name</th><th>Status</th><th class="num">Commits</th><th class="num">PR commits</th><th class="num">Direct commits</th><th class="num">% of all commits</th><th>Entity affiliations</th></tr></thead>',
        "<tbody>",
    ]
    for idx, row in enumerate(rows, start=1):
        login = row.get("login") or ""
        affiliations = " ".join(
            affiliation_badge(a.get("entity", ""), a.get("commitCredit", 0), a.get("rawCommitMatches", 0))
            for a in row.get("entityAffiliations", [])[:8]
        )
        if len(row.get("entityAffiliations", [])) > 8:
            affiliations += f' <span class="muted">+{len(row.get("entityAffiliations", [])) - 8} more</span>'
        parts.append(
            "<tr>"
            f'<td class="num">{idx}</td>'
            f'<td><a href="https://github.com/{escape_attr(login)}">{escape(login)}</a></td>'
            f'<td>{escape(row.get("name", ""))}</td>'
            f'<td>{escape(row.get("status", ""))}</td>'
            f'<td class="num">{format_number(row.get("commitCount", 0))}</td>'
            f'<td class="num">{format_number(row.get("prCommitCount", 0))}</td>'
            f'<td class="num">{format_number(row.get("directCommitCount", 0))}</td>'
            f'<td class="num">{format_pct(row.get("percentageOfAllCommits", 0))}</td>'
            f"<td>{affiliations}</td>"
            "</tr>"
        )
    parts.extend(["</tbody></table>"])
    return "\n".join(parts)


def contributor_badge(login: str, credit: Any, raw_matches: Any) -> str:
    title = ""
    try:
        credit_f = float(credit)
        raw_i = int(raw_matches)
    except (TypeError, ValueError):
        credit_f = 0.0
        raw_i = 0
    if raw_i and not math.isclose(credit_f, raw_i):
        title = f' title="{raw_i} raw matched commits before fractional attribution"'
    return f'<span class="badge"{title}>{escape(login)}: {format_credit(credit)}</span>'


def affiliation_badge(name: str, credit: Any, raw_matches: Any) -> str:
    title = ""
    try:
        credit_f = float(credit)
        raw_i = int(raw_matches)
    except (TypeError, ValueError):
        credit_f = 0.0
        raw_i = 0
    if raw_i and not math.isclose(credit_f, raw_i):
        title = f' title="{raw_i} raw matched commits before fractional attribution"'
    return f'<span class="badge"{title}>{escape(name)}: {format_credit(credit)}</span>'


def spec_anchor(spec: Mapping[str, Any]) -> str:
    raw = f"{spec.get('workstreamId', '')}-{spec.get('name', '')}-{spec.get('repository', '')}"
    anchor = re.sub(r"[^a-zA-Z0-9_-]+", "-", raw).strip("-").lower()
    return anchor or "spec"


def escape(value: Any) -> str:
    return html_lib.escape(str(value), quote=False)


def escape_attr(value: Any) -> str:
    return html_lib.escape(str(value), quote=True)


def format_pct(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = 0.0
    return f"{number:.2f}%"


def format_credit(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "0"
    if math.isclose(number, round(number)):
        return format_number(int(round(number)))
    return f"{number:,.2f}"


def format_number(value: Any) -> str:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return str(value)
    return f"{number:,}"


def write_report_files(output_dir: str, report: Mapping[str, Any]) -> None:
    os.makedirs(output_dir, exist_ok=True)
    html = build_html_report(report)
    with open(os.path.join(output_dir, "index.html"), "w", encoding="utf-8") as f:
        f.write(html)
        f.write("\n")
    with open(os.path.join(output_dir, "report.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, sort_keys=True)
        f.write("\n")
    with open(os.path.join(output_dir, ".nojekyll"), "w", encoding="utf-8") as f:
        f.write("# Generated by whatwg_contrib_report.py\n")


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate WHATWG per-spec contribution attribution report.")
    parser.add_argument("--output", default="public", help="Output directory for index.html and report.json")
    parser.add_argument("--branch", default="main", help="Branch/ref to analyze in every spec repository")
    parser.add_argument("--sg-db-url", default=DEFAULT_SG_DB_URL, help="URL of whatwg/sg db.json")
    parser.add_argument("--entities-url", default=DEFAULT_ENTITIES_URL)
    parser.add_argument("--individuals-url", default=DEFAULT_INDIVIDUALS_URL)
    parser.add_argument("--github-rest-url", default=DEFAULT_GITHUB_REST_URL)
    parser.add_argument("--github-graphql-url", default=DEFAULT_GITHUB_GRAPHQL_URL)
    parser.add_argument("--github-api-version", default=DEFAULT_GITHUB_API_VERSION)
    parser.add_argument("--cache-file", default="", help="Existing cache JSON path, usually restored from gh-pages")
    parser.add_argument("--write-cache", default="", help="Where to write the updated cache JSON")
    scan = parser.add_mutually_exclusive_group()
    scan.add_argument("--incremental", action="store_true", help="Stop scanning a repo once an already-cached page is reached")
    scan.add_argument("--full-scan", action="store_true", help="Ignore incremental stopping and scan full history")
    parser.add_argument("--page-size", type=int, default=100, choices=range(1, 101), metavar="1-100")
    parser.add_argument("--include-unverified", action="store_true", help="Include unverified public participant-data entries")
    parser.add_argument("--ignore-participant-workstreams", action="store_true", help="Do not filter participant entries by workstream")
    parser.add_argument(
        "--entity-attribution",
        choices=["fractional", "duplicate"],
        default="fractional",
        help="How to handle a contributor who maps to multiple public entities for the same spec",
    )
    parser.add_argument(
        "--affiliation-source",
        choices=["user-orgs", "org-members", "both"],
        default="user-orgs",
        help=(
            "How to map GitHub users to public entity GitHub organizations. user-orgs uses public user org memberships; "
            "org-members crawls every entity org's public_members endpoint. Private memberships are never used."
        ),
    )
    parser.add_argument("--no-entity-contacts", action="store_true", help="Do not use public participant-data contact GitHub IDs as entity affiliations")
    parser.add_argument("--limit-specs", type=int, default=0, help="Debugging: limit number of specs processed")
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    warnings: list[RunWarning] = []
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not token:
        raise ReportError("GITHUB_TOKEN is required. The GitHub GraphQL API does not support anonymous requests.")

    incremental = bool(args.incremental and not args.full_scan)
    client = GitHubClient(
        token=token,
        rest_url=args.github_rest_url,
        graphql_url=args.github_graphql_url,
        api_version=args.github_api_version,
    )

    print("Resolving input source snapshots", file=sys.stderr)
    sg_db_fetch_url, sg_db_source_meta = resolve_raw_github_source(client, args.sg_db_url, warnings)
    entities_fetch_url, entities_source_meta = resolve_raw_github_source(client, args.entities_url, warnings)
    individuals_fetch_url, individuals_source_meta = resolve_raw_github_source(client, args.individuals_url, warnings)

    print("Loading WHATWG standards and participant data", file=sys.stderr)
    specs, sg_db_meta = load_specs(sg_db_fetch_url, warnings)
    sg_db_meta = {**sg_db_source_meta, **sg_db_meta}
    if args.limit_specs:
        specs = specs[: args.limit_specs]
    specs = [infer_repo_from_spec_url(spec, warnings) for spec in specs]

    entities, non_public_entity_logins, entities_meta = load_entities(entities_fetch_url, args.include_unverified, warnings)
    entities_meta = {**entities_source_meta, **entities_meta}
    individuals, non_public_individual_logins, individuals_meta = load_individuals(individuals_fetch_url, args.include_unverified)
    individuals_meta = {**individuals_source_meta, **individuals_meta}
    non_public_participant_logins = non_public_entity_logins | non_public_individual_logins
    entities_by_id = {entity.entity_id: entity for entity in entities}

    cache = load_cache(args.cache_file)
    commits_by_repo: dict[str, list[dict[str, Any]]] = {}
    repo_meta_by_repo: dict[str, dict[str, Any]] = {}
    for spec in specs:
        if not spec.repo_full_name:
            warnings.append(RunWarning(spec.name, "No repository inferred; skipping."))
            continue
        commits, repo_meta = fetch_repo_commits(
            client=client,
            spec=spec,
            branch=args.branch,
            cache=cache,
            incremental=incremental,
            page_size=args.page_size,
            warnings=warnings,
        )
        commits_by_repo[spec.repo_full_name] = commits
        repo_meta_by_repo[spec.repo_full_name] = repo_meta

    contributors = collect_contributor_logins(commits_by_repo, non_public_participant_logins)
    print(f"Collected {len(contributors)} unique reportable contributor logins", file=sys.stderr)
    login_to_entity_ids = build_affiliations(
        client=client,
        contributors=contributors,
        entities=entities,
        source=args.affiliation_source,
        include_contacts=not args.no_entity_contacts,
        warnings=warnings,
    )

    spec_reports: list[dict[str, Any]] = []
    for spec in specs:
        if spec.repo_full_name not in commits_by_repo:
            continue
        spec_report = compute_spec_report(
            spec=spec,
            commits=commits_by_repo[spec.repo_full_name],
            repo_meta=repo_meta_by_repo.get(spec.repo_full_name, {}),
            branch=args.branch,
            entities_by_id=entities_by_id,
            individuals_by_login=individuals,
            login_to_entity_ids=login_to_entity_ids,
            non_public_participant_logins=non_public_participant_logins,
            entity_attribution=args.entity_attribution,
            ignore_participant_workstreams=args.ignore_participant_workstreams,
        )
        spec_reports.append(spec_report)

    totals = build_totals(spec_reports)
    aggregate = build_aggregate(spec_reports, totals)
    report = {
        "generatedAt": now_iso(),
        "branch": args.branch,
        "inputs": {
            "sgDbUrl": args.sg_db_url,
            "entitiesUrl": args.entities_url,
            "individualsUrl": args.individuals_url,
            "affiliationSource": args.affiliation_source,
            "includeEntityContacts": not args.no_entity_contacts,
            "includeUnverifiedParticipants": bool(args.include_unverified),
            "creditDirectCommits": True,
            "entityAttribution": args.entity_attribution,
            "ignoreParticipantWorkstreams": bool(args.ignore_participant_workstreams),
            "incremental": incremental,
        },
        "provenance": {
            "inputs": {
                "whatwg/sg db.json": sg_db_meta,
                "participant-data entities.json": entities_meta,
                "participant-data individuals.json": individuals_meta,
            },
            "run": {
                "incremental": incremental,
                "githubRepository": os.environ.get("GITHUB_REPOSITORY", ""),
                "githubSha": os.environ.get("GITHUB_SHA", ""),
                "githubRunId": os.environ.get("GITHUB_RUN_ID", ""),
                "githubRunAttempt": os.environ.get("GITHUB_RUN_ATTEMPT", ""),
            },
        },
        "disclaimer": (
            "Participant-data entries explicitly marked non-Public are ignored. Entity attribution uses public GitHub organization memberships "
            "and public participant-data contacts only; private or concealed memberships are intentionally ignored, even if the token used for "
            "the run could see them. Historical employer changes are not reconstructed, so past contributions are credited to the contributor's "
            "current public entity affiliation at generation time."
        ),
        "totals": totals,
        "aggregate": aggregate,
        "specs": spec_reports,
        "warnings": [dataclasses.asdict(warning) for warning in warnings],
    }

    write_report_files(args.output, report)
    if args.write_cache:
        write_cache(args.write_cache, cache)
    elif args.cache_file:
        write_cache(args.cache_file, cache)

    print(f"Wrote report to {args.output}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
    except ReportError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
