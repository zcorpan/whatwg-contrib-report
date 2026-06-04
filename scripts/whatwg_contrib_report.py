#!/usr/bin/env python3
"""Generate a WHATWG contribution attribution report.

The report:

* discovers WHATWG standards from the WHATWG Steering Group db.json file;
* infers each repository from the standard URL, e.g. https://fs.spec.whatwg.org/
  maps to https://github.com/whatwg/fs;
* walks commits reachable from the requested branch, main by default;
* credits commits associated by GitHub with a merged PR to the PR author;
* credits direct/no-PR commits to the resolved GitHub commit author;
* uses all commits on the branch as the denominator;
* attributes entities using participant-data contacts and public GitHub
  organization memberships only.

Historical affiliation changes are not reconstructed.

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
from typing import Any, DefaultDict, Iterator, Mapping, MutableMapping, Optional


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
    repo_owner: str = "whatwg"
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
        return self.all_workstreams or normalize_workstream_id(workstream_id) in self.ids


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


def normalize_login(login: Any) -> str:
    if not isinstance(login, str):
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


def normalize_workstream_id(value: Any) -> str:
    if value is None:
        return ""
    return re.sub(r"[^a-z0-9_-]+", "", str(value).strip().lower())


def slug(value: Any) -> str:
    text = clean_ws(str(value or "")).lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-")


def participant_workstreams(raw: Any) -> ParticipantWorkstreams:
    if raw == "all":
        return ParticipantWorkstreams(True, frozenset())
    if isinstance(raw, list):
        return ParticipantWorkstreams(False, frozenset(normalize_workstream_id(item) for item in raw))
    return ParticipantWorkstreams(False, frozenset())


def fetch_bytes(url: str, timeout: int = 90) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "whatwg-contrib-report/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return response.read()


def fetch_json_with_meta(url: str, client: Optional[GitHubClient] = None) -> tuple[Any, dict[str, Any]]:
    content = fetch_bytes(url)
    meta = {
        "url": url,
        "fetchedAt": now_iso(),
        "sha256": hashlib.sha256(content).hexdigest(),
        "byteLength": len(content),
    }
    pinned = pinned_raw_github_url(url, client)
    if pinned:
        meta["pinnedUrl"] = pinned
    return json.loads(content.decode("utf-8")), meta


def pinned_raw_github_url(url: str, client: Optional[GitHubClient]) -> str:
    if client is None:
        return ""
    parsed = urllib.parse.urlparse(url)
    if parsed.netloc != "raw.githubusercontent.com":
        return ""
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 4:
        return ""
    owner, repo, ref = parts[0], parts[1], parts[2]
    rest_path = "/".join(parts[3:])
    if re.fullmatch(r"[0-9a-fA-F]{40}", ref):
        return url
    if "/" in ref:
        return ""
    try:
        data, _headers, status = client.rest_get_json(
            f"/repos/{urllib.parse.quote(owner)}/{urllib.parse.quote(repo)}/commits/{urllib.parse.quote(ref)}",
            allow_404=True,
        )
    except Exception:
        return ""
    if status != 200 or not isinstance(data, dict):
        return ""
    sha = data.get("sha")
    if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-fA-F]{40}", sha):
        return ""
    return f"https://raw.githubusercontent.com/{owner}/{repo}/{sha}/{rest_path}"


def load_specs(sg_db_url: str, client: GitHubClient, warnings: list[RunWarning]) -> tuple[list[Spec], dict[str, Any]]:
    data, meta = fetch_json_with_meta(sg_db_url, client)
    if isinstance(data, dict):
        workstreams = data.get("workstreams") or []
    elif isinstance(data, list):
        workstreams = data
    else:
        raise ReportError(f"Expected object or array from {sg_db_url}, got {type(data).__name__}")
    if not isinstance(workstreams, list):
        raise ReportError(f"Expected workstreams to be a list in {sg_db_url}")

    specs: list[Spec] = []
    seen_spec_urls: set[str] = set()
    for ws in workstreams:
        if not isinstance(ws, dict):
            continue
        workstream_title = clean_ws(str(ws.get("title") or ws.get("name") or ws.get("id") or ""))
        workstream_id = normalize_workstream_id(ws.get("id") or ws.get("shortname") or ws.get("key") or workstream_title)
        standards = ws.get("standards") or []
        if isinstance(standards, dict):
            standards_iter = list(standards.values())
        elif isinstance(standards, list):
            standards_iter = standards
        else:
            warnings.append(RunWarning("sg-db", f"Skipping standards for workstream {workstream_title or workstream_id}: expected list or object."))
            continue

        for standard in standards_iter:
            if isinstance(standard, str):
                href = standard
                name = infer_standard_name_from_url(standard)
            elif isinstance(standard, dict):
                href = standard.get("href") or standard.get("url") or standard.get("spec") or ""
                name = clean_ws(str(standard.get("name") or standard.get("title") or ""))
            else:
                continue
            if not isinstance(href, str) or not href.strip():
                warnings.append(RunWarning("sg-db", f"Skipping standard without href in {workstream_title or workstream_id}."))
                continue
            spec_url = normalize_spec_url(urllib.parse.urljoin("https://whatwg.org/", href.strip()))
            if not name:
                name = infer_standard_name_from_url(spec_url)
            repo = infer_repo_name_from_spec_url(spec_url)
            if not repo:
                repo = slug(name)
                warnings.append(RunWarning(name or spec_url, f"Could not infer whatwg repository from {spec_url}; using {repo!r}."))
            if spec_url in seen_spec_urls:
                continue
            seen_spec_urls.add(spec_url)
            specs.append(
                Spec(
                    workstream_title=workstream_title or workstream_id,
                    workstream_id=workstream_id,
                    name=name or repo,
                    spec_url=spec_url,
                    repo_owner="whatwg",
                    repo_name=repo,
                )
            )

    if not specs:
        raise ReportError(f"No standards found in {sg_db_url}")
    return specs, meta


def infer_repo_name_from_spec_url(spec_url: str) -> str:
    parsed = urllib.parse.urlparse(spec_url)
    host = parsed.netloc.lower()
    suffix = ".spec.whatwg.org"
    if not host.endswith(suffix):
        return ""
    name = host[: -len(suffix)]
    if "." in name:
        name = name.split(".")[-1]
    return re.sub(r"[^a-z0-9_.-]+", "", name)


def infer_standard_name_from_url(url: str) -> str:
    repo = infer_repo_name_from_spec_url(url)
    if repo:
        return repo.replace("-", " ").replace("_", " ").title()
    parsed = urllib.parse.urlparse(url)
    base = parsed.netloc or parsed.path.rstrip("/").rsplit("/", 1)[-1]
    return clean_ws(base.replace(".", " ").replace("-", " ").title())


def load_entities(data: Any, include_unverified: bool, warnings: list[RunWarning]) -> list[Entity]:
    if not isinstance(data, list):
        raise ReportError(f"Expected entities JSON to be a list, got {type(data).__name__}")
    entities: list[Entity] = []
    seen_ids: set[str] = set()
    for item in data:
        if not isinstance(item, dict):
            continue
        verified = bool(item.get("verified"))
        if not include_unverified and not verified:
            continue
        info = item.get("info") or {}
        if not isinstance(info, dict):
            continue
        org = normalize_github_org(info.get("gitHubOrganization"))
        if not org:
            warnings.append(RunWarning("participant-data", f"Skipping entity {info.get('name') or item.get('id')} because gitHubOrganization is missing or invalid."))
            continue
        name = clean_ws(str(info.get("name") or org))
        contacts = collect_contact_logins(info)
        entity_id = str(item.get("id") or org)
        if entity_id in seen_ids:
            warnings.append(RunWarning("participant-data", f"Duplicate entity id {entity_id}; keeping duplicate as separate entity."))
        seen_ids.add(entity_id)
        entities.append(
            Entity(
                entity_id=entity_id,
                name=name,
                org_login=org,
                verified=verified,
                workstreams=participant_workstreams(item.get("workstreams")),
                contacts=frozenset(contacts),
                url=str(info.get("url") or ""),
            )
        )
    return entities


def collect_contact_logins(info: Mapping[str, Any]) -> set[str]:
    contacts: set[str] = set()
    for key, value in info.items():
        if key.startswith("contact") and isinstance(value, dict):
            login = normalize_login(value.get("gitHubID"))
            if login:
                contacts.add(login)
    raw_contacts = info.get("contacts")
    if isinstance(raw_contacts, list):
        for value in raw_contacts:
            if isinstance(value, dict):
                login = normalize_login(value.get("gitHubID"))
                if login:
                    contacts.add(login)
    return contacts


def load_individuals(data: Any, include_unverified: bool) -> dict[str, Individual]:
    if not isinstance(data, list):
        raise ReportError(f"Expected individuals JSON to be a list, got {type(data).__name__}")
    individuals: dict[str, Individual] = {}
    for item in data:
        if not isinstance(item, dict):
            continue
        verified = bool(item.get("verified"))
        if not include_unverified and not verified:
            continue
        info = item.get("info") or {}
        if not isinstance(info, dict):
            continue
        login = normalize_login(info.get("gitHubID"))
        if not login:
            continue
        individuals[login] = Individual(
            participant_id=str(item.get("id") or login),
            name=clean_ws(str(info.get("name") or login)),
            login=display_login(str(info.get("gitHubID") or login)),
            verified=verified,
            workstreams=participant_workstreams(item.get("workstreams")),
        )
    return individuals


def load_cache(path: str) -> dict[str, Any]:
    if not path or not os.path.exists(path) or os.path.getsize(path) == 0:
        return {"version": CACHE_VERSION, "repositories": {}}
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict) or not isinstance(data.get("repositories"), dict):
        return {"version": CACHE_VERSION, "repositories": {}}
    data["version"] = CACHE_VERSION
    return data


def write_cache(path: str, cache: Mapping[str, Any]) -> None:
    if not path:
        return
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    cache_to_write = dict(cache)
    cache_to_write["version"] = CACHE_VERSION
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cache_to_write, f, indent=2, sort_keys=True)
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
            client,
            spec,
            branch,
            cache,
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


def now_iso() -> str:
    return _dt.datetime.now(_dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def build_affiliations(
    client: GitHubClient,
    contributors: set[str],
    entities: list[Entity],
    source: str,
    include_contacts: bool,
    warnings: list[RunWarning],
) -> dict[str, set[str]]:
    """Return contributor login -> entity ids.

    All GitHub org membership lookups here are public-only. The script never uses
    /orgs/{org}/members, which can expose memberships to privileged tokens.
    """
    if source == "org-members":
        source = "org-public-members"
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

    if source in {"org-public-members", "both"}:
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


def compute_spec_report(
    spec: Spec,
    commits: list[dict[str, Any]],
    repo_meta: Mapping[str, Any],
    branch: str,
    entities_by_id: Mapping[str, Entity],
    individuals_by_login: Mapping[str, Individual],
    login_to_entity_ids: Mapping[str, set[str]],
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
        pr_number = commit.get("prNumber")
        pr_author = normalize_login(commit.get("prAuthorLogin"))
        author = normalize_login(commit.get("authorLogin"))
        if pr_number:
            pr_commits += 1
            credit_login = pr_author
            credit_source = "merged-pr" if pr_author else "uncredited-pr"
        else:
            direct_commits += 1
            credit_login = author
            credit_source = "direct-commit" if author else "uncredited-direct"

        if not credit_login:
            uncredited_commits += 1
            if not author and not pr_author:
                no_login_commits += 1
            continue
        credited_commits += 1

        individual = individuals_by_login.get(credit_login)
        individual_is_signed = bool(individual and individual_applies(individual))
        eligible_entity_ids = sorted(
            entity_id
            for entity_id in login_to_entity_ids.get(credit_login, set())
            if entity_id in entities_by_id and entity_applies(entities_by_id[entity_id])
        )
        if entity_attribution == "fractional" and eligible_entity_ids:
            entity_weight = 1.0 / len(eligible_entity_ids)
        else:
            entity_weight = 1.0

        row = individual_stats.setdefault(
            credit_login,
            {
                "login": credit_login,
                "name": individual.name if individual else "",
                "signedIndividual": individual_is_signed,
                "entityAffiliations": {},
                "commitCount": 0,
                "prCommitCount": 0,
                "directCommitCount": 0,
                "botOrApp": False,
                "sources": collections.Counter(),
            },
        )
        row["commitCount"] += 1
        if credit_source == "merged-pr":
            row["prCommitCount"] += 1
        elif credit_source == "direct-commit":
            row["directCommitCount"] += 1
        row["sources"][credit_source] += 1
        if (commit.get("prAuthorType") or "").lower() in {"bot", "app"}:
            row["botOrApp"] = True

        if eligible_entity_ids:
            for entity_id in eligible_entity_ids:
                entity = entities_by_id[entity_id]
                affiliation = row["entityAffiliations"].setdefault(
                    entity.name,
                    {"commitCredit": 0.0, "rawCommitMatches": 0},
                )
                affiliation["commitCredit"] += entity_weight
                affiliation["rawCommitMatches"] += 1

                est = entity_stats.setdefault(
                    entity_id,
                    {
                        "entityId": entity.entity_id,
                        "name": entity.name,
                        "gitHubOrganization": entity.org_login,
                        "url": entity.url,
                        "commitCredit": 0.0,
                        "contributors": {},
                        "rawCommitMatches": 0,
                    },
                )
                est["commitCredit"] += entity_weight
                est["rawCommitMatches"] += 1
                contributor = est["contributors"].setdefault(
                    credit_login,
                    {"commitCredit": 0.0, "rawCommitMatches": 0},
                )
                contributor["commitCredit"] += entity_weight
                contributor["rawCommitMatches"] += 1

    def pct(value: float) -> float:
        return (value / total_commits * 100.0) if total_commits else 0.0

    entities_out = []
    for row in entity_stats.values():
        contributors = [
            {
                "login": login,
                "commitCredit": round(float(values["commitCredit"]), 6),
                "rawCommitMatches": int(values["rawCommitMatches"]),
            }
            for login, values in sorted(
                row["contributors"].items(),
                key=lambda kv: (-float(kv[1]["commitCredit"]), kv[0]),
            )
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
                "commitCredit": round(float(values["commitCredit"]), 6),
                "rawCommitMatches": int(values["rawCommitMatches"]),
            }
            for name, values in sorted(
                row["entityAffiliations"].items(),
                key=lambda kv: (-float(kv[1]["commitCredit"]), kv[0].lower()),
            )
        ]
        status_parts = []
        if row["signedIndividual"]:
            status_parts.append("signed individual")
        if affiliations_list:
            status_parts.append("entity attributed")
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
        "firstCommitDate": first_commit_date,
        "lastCommitDate": last_commit_date,
        "repoFetch": dict(repo_meta),
        "entities": entities_out,
        "individuals": individuals_out,
    }


def collect_contributor_logins(commits_by_repo: Mapping[str, list[dict[str, Any]]]) -> set[str]:
    contributors: set[str] = set()
    for commits in commits_by_repo.values():
        for commit in commits:
            if commit.get("prNumber"):
                login = normalize_login(commit.get("prAuthorLogin"))
            else:
                login = normalize_login(commit.get("authorLogin"))
            if login:
                contributors.add(login)
    return contributors


def build_totals(spec_reports: list[Mapping[str, Any]]) -> dict[str, int]:
    return {
        "specCount": len(spec_reports),
        "totalCommits": sum(int(s.get("totalCommits") or 0) for s in spec_reports),
        "mergedPrAssociatedCommits": sum(int(s.get("mergedPrAssociatedCommits") or 0) for s in spec_reports),
        "directOrNoPrCommits": sum(int(s.get("directOrNoPrCommits") or 0) for s in spec_reports),
        "creditedCommits": sum(int(s.get("creditedCommits") or 0) for s in spec_reports),
        "uncreditedCommits": sum(int(s.get("uncreditedCommits") or 0) for s in spec_reports),
    }


def build_aggregate_rankings(spec_reports: list[Mapping[str, Any]], total_commits: int) -> dict[str, list[dict[str, Any]]]:
    entity_rows: dict[str, dict[str, Any]] = {}
    individual_rows: dict[str, dict[str, Any]] = {}

    for spec in spec_reports:
        spec_name = str(spec.get("name") or "")
        for entity in spec.get("entities") or []:
            key = str(entity.get("entityId") or entity.get("name") or "")
            if not key:
                continue
            row = entity_rows.setdefault(
                key,
                {
                    "entityId": key,
                    "name": entity.get("name") or key,
                    "gitHubOrganization": entity.get("gitHubOrganization") or "",
                    "commitCredit": 0.0,
                    "rawCommitMatches": 0,
                    "specs": set(),
                },
            )
            row["commitCredit"] += float(entity.get("commitCredit") or 0)
            row["rawCommitMatches"] += int(entity.get("rawCommitMatches") or 0)
            row["specs"].add(spec_name)
        for individual in spec.get("individuals") or []:
            login = str(individual.get("login") or "")
            if not login:
                continue
            row = individual_rows.setdefault(
                login,
                {
                    "login": login,
                    "name": individual.get("name") or "",
                    "commitCount": 0,
                    "prCommitCount": 0,
                    "directCommitCount": 0,
                    "specs": set(),
                },
            )
            row["commitCount"] += int(individual.get("commitCount") or 0)
            row["prCommitCount"] += int(individual.get("prCommitCount") or 0)
            row["directCommitCount"] += int(individual.get("directCommitCount") or 0)
            row["specs"].add(spec_name)

    def pct(value: float) -> float:
        return (value / total_commits * 100.0) if total_commits else 0.0

    entities = []
    for row in entity_rows.values():
        entities.append(
            {
                "entityId": row["entityId"],
                "name": row["name"],
                "gitHubOrganization": row["gitHubOrganization"],
                "commitCredit": round(float(row["commitCredit"]), 6),
                "percentageOfAllCommits": round(pct(float(row["commitCredit"])), 6),
                "rawCommitMatches": int(row["rawCommitMatches"]),
                "specCount": len(row["specs"]),
            }
        )
    entities.sort(key=lambda row: (-row["commitCredit"], row["name"].lower()))

    individuals = []
    for row in individual_rows.values():
        individuals.append(
            {
                "login": row["login"],
                "name": row["name"],
                "commitCount": int(row["commitCount"]),
                "prCommitCount": int(row["prCommitCount"]),
                "directCommitCount": int(row["directCommitCount"]),
                "percentageOfAllCommits": round(pct(int(row["commitCount"])), 6),
                "specCount": len(row["specs"]),
            }
        )
    individuals.sort(key=lambda row: (-row["commitCount"], row["login"]))
    return {"entities": entities, "individuals": individuals}


def build_html_report(report: Mapping[str, Any]) -> str:
    generated = escape(report["generatedAt"])
    specs = report["specs"]
    totals = report["totals"]
    warnings = report.get("warnings") or []
    aggregate = report.get("aggregate") or {"entities": [], "individuals": []}
    css = """
:root { color-scheme: light dark; --border: #d0d7de; --muted: #57606a; --bg: #ffffff; --soft: #f6f8fa; --fg: #24292f; --accent: #0969da; }
@media (prefers-color-scheme: dark) { :root { --border: #30363d; --muted: #8b949e; --bg: #0d1117; --soft: #161b22; --fg: #c9d1d9; --accent: #58a6ff; } }
* { box-sizing: border-box; }
body { margin: 0; font: 16px/1.5 system-ui, -apple-system, Segoe UI, sans-serif; color: var(--fg); background: var(--bg); }
a { color: var(--accent); }
header { padding: 2rem; border-bottom: 1px solid var(--border); background: var(--soft); }
main { padding: 1.5rem 2rem 3rem; max-width: 1500px; margin: 0 auto; }
h1 { margin: 0 0 .5rem; }
h2 { margin-top: 2rem; border-top: 1px solid var(--border); padding-top: 1.5rem; }
h3 { margin-top: 1.5rem; }
table { width: 100%; border-collapse: collapse; margin: .75rem 0 1.5rem; font-size: .94rem; }
th, td { border: 1px solid var(--border); padding: .45rem .55rem; vertical-align: top; }
th { background: var(--soft); text-align: left; }
code { background: var(--soft); padding: .1rem .25rem; border-radius: .25rem; }
.num { text-align: right; white-space: nowrap; }
.muted { color: var(--muted); }
.small { font-size: .92rem; }
.cards { display: flex; flex-wrap: wrap; gap: .75rem; margin: 1rem 0; }
.card { border: 1px solid var(--border); background: var(--soft); border-radius: .5rem; padding: .75rem 1rem; min-width: 11rem; }
.card strong { display: block; font-size: 1.4rem; }
.badge { display: inline-block; border: 1px solid var(--border); border-radius: 999px; padding: .08rem .45rem; margin: .08rem .1rem .08rem 0; background: var(--soft); white-space: nowrap; }
.warning { border-left: .25rem solid #bf8700; padding: .5rem .75rem; background: var(--soft); margin: .5rem 0; }
.toc { columns: 2 18rem; }
details { margin: 1rem 0; }
summary { cursor: pointer; font-weight: 600; }
.overview td, .overview th { font-size: .9rem; }
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
        f'<p class="muted">Generated {generated} for branch <code>{escape(report["branch"])}</code>. <a href="report.json">Download report.json</a>.</p>',
        "<p>This report credits commits reachable from each specification repository branch. Commits associated by GitHub with a merged pull request are credited to the PR author. Direct/no-PR commits are credited to the resolved GitHub commit author. Percentages use all commits in the branch as the denominator.</p>",
        "<p>Uncredited commits are commits with no associated merged-PR author and no GitHub commit author that GitHub can resolve.</p>",
        "<p><strong>Public data note:</strong> Entity attribution uses participant-data and public GitHub organization memberships only. Private GitHub organization memberships are intentionally ignored, even if visible to the token used for the run.</p>",
        "<p><strong>Affiliation disclaimer:</strong> Entity attribution uses the latest public GitHub organization memberships and participant-data contacts at generation time. Historical employer changes are not reconstructed; past contributions are credited to the current visible entity affiliation.</p>",
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
    ]
    if warnings:
        parts.append("<details open><summary>Warnings</summary>")
        for warning in warnings:
            parts.append(f'<div class="warning"><strong>{escape(warning.get("area", "warning"))}</strong>: {escape(warning.get("message", ""))}</div>')
        parts.append("</details>")

    parts.append(render_provenance(report.get("provenance") or {}))
    parts.append("<h3>Specifications overview</h3>")
    parts.append(render_specs_overview(specs))
    parts.append("<h3>Aggregate entity ranking</h3>")
    parts.append(render_aggregate_entities_table(aggregate.get("entities") or [], totals["totalCommits"], limit=30))
    parts.append("<h3>Aggregate individual ranking</h3>")
    parts.append(render_aggregate_individuals_table(aggregate.get("individuals") or [], totals["totalCommits"], limit=30))
    parts.extend([
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


def card(label: str, value: Any) -> str:
    return f'<div class="card"><span class="muted">{escape(label)}</span><strong>{escape(format_number(value))}</strong></div>'


def render_provenance(provenance: Mapping[str, Any]) -> str:
    if not provenance:
        return ""
    sources = provenance.get("sources") or {}
    actions = provenance.get("githubActions") or {}
    parts = ["<details><summary>Data provenance</summary>"]
    if sources:
        parts.append("<table><thead><tr><th>Source</th><th>URL</th><th>Pinned URL</th><th>SHA-256</th><th class=\"num\">Bytes</th></tr></thead><tbody>")
        for key, meta in sources.items():
            url = meta.get("url") or ""
            pinned = meta.get("pinnedUrl") or ""
            parts.append(
                "<tr>"
                f"<td>{escape(key)}</td>"
                f'<td><a href="{escape_attr(url)}">{escape(url)}</a></td>'
                f'<td>{f"<a href=\"{escape_attr(pinned)}\">pinned</a>" if pinned else ""}</td>'
                f"<td><code>{escape(meta.get('sha256', ''))}</code></td>"
                f"<td class=\"num\">{format_number(meta.get('byteLength', 0))}</td>"
                "</tr>"
            )
        parts.append("</tbody></table>")
    if actions:
        parts.append("<p class=\"small muted\">")
        items = []
        for label, value in actions.items():
            if value:
                items.append(f"{escape(label)}: <code>{escape(value)}</code>")
        parts.append("; ".join(items))
        parts.append("</p>")
    parts.append("</details>")
    return "\n".join(parts)


def render_specs_overview(specs: list[Mapping[str, Any]]) -> str:
    parts = [
        '<table class="overview">',
        '<thead><tr><th>Spec</th><th>Workstream</th><th>Repo</th><th class="num">Commits</th><th class="num">Credited %</th><th class="num">Uncredited %</th><th>Top entity</th><th>Top individual</th></tr></thead>',
        "<tbody>",
    ]
    for spec in specs:
        total = int(spec.get("totalCommits") or 0)
        credited_pct = (int(spec.get("creditedCommits") or 0) / total * 100.0) if total else 0.0
        uncredited_pct = (int(spec.get("uncreditedCommits") or 0) / total * 100.0) if total else 0.0
        top_entity = ""
        if spec.get("entities"):
            row = spec["entities"][0]
            top_entity = f'{escape(row.get("name", ""))} ({format_credit(row.get("commitCredit", 0))})'
        top_individual = ""
        if spec.get("individuals"):
            row = spec["individuals"][0]
            top_individual = f'{escape(row.get("login", ""))} ({format_number(row.get("commitCount", 0))})'
        parts.append(
            "<tr>"
            f'<td><a href="#{spec_anchor(spec)}">{escape(spec.get("name", ""))}</a></td>'
            f'<td>{escape(spec.get("workstreamTitle", ""))}</td>'
            f'<td><a href="{escape_attr(spec.get("repositoryUrl", ""))}">{escape(spec.get("repository", ""))}</a></td>'
            f'<td class="num">{format_number(total)}</td>'
            f'<td class="num">{format_pct(credited_pct)}</td>'
            f'<td class="num">{format_pct(uncredited_pct)}</td>'
            f"<td>{top_entity}</td>"
            f"<td>{top_individual}</td>"
            "</tr>"
        )
    parts.extend(["</tbody></table>"])
    return "\n".join(parts)


def render_aggregate_entities_table(rows: list[Mapping[str, Any]], total: int, limit: int = 30) -> str:
    if not rows:
        return '<p class="muted">No entity-attributed commits.</p>'
    parts = [
        "<table>",
        '<thead><tr><th class="num">#</th><th>Entity</th><th>GitHub org</th><th class="num">Commit credit</th><th class="num">% of all commits</th><th class="num">Specs</th></tr></thead>',
        "<tbody>",
    ]
    for idx, row in enumerate(rows[:limit], start=1):
        org = row.get("gitHubOrganization") or ""
        org_html = f'<a href="https://github.com/{escape_attr(org)}">{escape(org)}</a>' if org else ""
        parts.append(
            "<tr>"
            f'<td class="num">{idx}</td>'
            f'<td>{escape(row.get("name", ""))}</td>'
            f"<td>{org_html}</td>"
            f'<td class="num">{format_credit(row.get("commitCredit", 0))}</td>'
            f'<td class="num">{format_pct(row.get("percentageOfAllCommits", 0))}</td>'
            f'<td class="num">{format_number(row.get("specCount", 0))}</td>'
            "</tr>"
        )
    parts.extend(["</tbody></table>"])
    if len(rows) > limit:
        parts.append(f'<p class="muted small">Showing top {limit} of {len(rows)} entities.</p>')
    return "\n".join(parts)


def render_aggregate_individuals_table(rows: list[Mapping[str, Any]], total: int, limit: int = 30) -> str:
    if not rows:
        return '<p class="muted">No individual-attributed commits.</p>'
    parts = [
        "<table>",
        '<thead><tr><th class="num">#</th><th>GitHub login</th><th>Name</th><th class="num">Commits</th><th class="num">PR commits</th><th class="num">Direct/no-PR commits</th><th class="num">% of all commits</th><th class="num">Specs</th></tr></thead>',
        "<tbody>",
    ]
    for idx, row in enumerate(rows[:limit], start=1):
        login = row.get("login") or ""
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
            "</tr>"
        )
    parts.extend(["</tbody></table>"])
    if len(rows) > limit:
        parts.append(f'<p class="muted small">Showing top {limit} of {len(rows)} individuals.</p>')
    return "\n".join(parts)


def render_spec_section(spec: Mapping[str, Any]) -> str:
    total = int(spec.get("totalCommits") or 0)
    anchor = spec_anchor(spec)
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
    individuals = spec.get("individuals") or []
    parts.append(f'<details><summary>Individuals ({len(individuals)})</summary>')
    parts.append(render_individuals_table(individuals, total))
    parts.append("</details>")
    parts.append("</section>")
    return "\n".join(parts)


def render_entities_table(rows: list[Mapping[str, Any]], total: int) -> str:
    if not rows:
        return '<p class="muted">No entity-attributed commits.</p>'
    parts = [
        "<table>",
        '<thead><tr><th class="num">#</th><th>Entity</th><th>GitHub org</th><th class="num">Commit credit</th><th class="num">% of all commits</th><th>Contributors</th></tr></thead>',
        "<tbody>",
    ]
    for idx, row in enumerate(rows, start=1):
        contributors = " ".join(render_credit_badge(c["login"], c.get("commitCredit", 0), c.get("rawCommitMatches", 0)) for c in row.get("contributors", [])[:12])
        if len(row.get("contributors", [])) > 12:
            contributors += f' <span class="muted">+{len(row.get("contributors", [])) - 12} more</span>'
        org = row.get("gitHubOrganization") or ""
        org_html = f'<a href="https://github.com/{escape_attr(org)}">{escape(org)}</a>' if org else ""
        title = ""
        if not math.isclose(float(row.get("commitCredit", 0)), float(row.get("rawCommitMatches", 0))):
            title = f' title="{escape_attr(row.get("rawCommitMatches", 0))} raw matched commits before fractional attribution"'
        parts.append(
            "<tr>"
            f'<td class="num">{idx}</td>'
            f'<td>{escape(row.get("name", ""))}</td>'
            f"<td>{org_html}</td>"
            f'<td class="num"{title}>{format_credit(row.get("commitCredit", 0))}</td>'
            f'<td class="num">{format_pct(row.get("percentageOfAllCommits", 0))}</td>'
            f"<td>{contributors}</td>"
            "</tr>"
        )
    parts.extend(["</tbody></table>"])
    return "\n".join(parts)


def render_credit_badge(label: str, credit: Any, raw: Any) -> str:
    try:
        credit_f = float(credit)
        raw_f = float(raw)
    except (TypeError, ValueError):
        credit_f = 0.0
        raw_f = 0.0
    title = ""
    if raw_f and not math.isclose(credit_f, raw_f):
        title = f' title="{escape_attr(raw)} raw matched commits before fractional attribution"'
    return f'<span class="badge"{title}>{escape(label)}: {format_credit(credit)}</span>'


def render_individuals_table(rows: list[Mapping[str, Any]], total: int) -> str:
    if not rows:
        return '<p class="muted">No individual-attributed commits.</p>'
    parts = [
        "<table>",
        '<thead><tr><th class="num">#</th><th>GitHub login</th><th>Name</th><th>Status</th><th class="num">Commits</th><th class="num">PR commits</th><th class="num">Direct/no-PR commits</th><th class="num">% of all commits</th><th>Entity affiliations</th></tr></thead>',
        "<tbody>",
    ]
    for idx, row in enumerate(rows, start=1):
        login = row.get("login") or ""
        affiliations = " ".join(render_credit_badge(a["entity"], a.get("commitCredit", 0), a.get("rawCommitMatches", 0)) for a in row.get("entityAffiliations", [])[:8])
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
    parser.add_argument("--sg-db-url", default=DEFAULT_SG_DB_URL, help="WHATWG Steering Group db.json URL")
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
    parser.add_argument("--include-unverified", action="store_true", help="Include unverified participant-data entries")
    parser.add_argument("--ignore-participant-workstreams", action="store_true", help="Do not filter participant entries by workstream")
    parser.add_argument(
        "--entity-attribution",
        choices=["fractional", "duplicate"],
        default="fractional",
        help="How to handle a contributor who maps to multiple entities for the same spec",
    )
    parser.add_argument(
        "--affiliation-source",
        choices=["user-orgs", "org-public-members", "org-members", "both"],
        default="user-orgs",
        help=(
            "How to map GitHub users to entity GitHub organizations. user-orgs uses public orgs listed on contributor profiles; "
            "org-public-members crawls each entity org's public_members endpoint; org-members is accepted as a legacy alias for org-public-members."
        ),
    )
    parser.add_argument("--no-entity-contacts", action="store_true", help="Do not use participant-data contact GitHub IDs as entity affiliations")
    parser.add_argument("--limit-specs", type=int, default=0, help="Debugging: limit number of specs processed")
    return parser.parse_args(argv)


def github_actions_metadata() -> dict[str, str]:
    keys = [
        "GITHUB_REPOSITORY",
        "GITHUB_RUN_ID",
        "GITHUB_RUN_NUMBER",
        "GITHUB_RUN_ATTEMPT",
        "GITHUB_SHA",
        "GITHUB_REF_NAME",
        "GITHUB_WORKFLOW",
        "GITHUB_ACTOR",
    ]
    return {key: os.environ.get(key, "") for key in keys if os.environ.get(key, "")}


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

    print("Loading WHATWG standards and participant data", file=sys.stderr)
    specs, sg_meta = load_specs(args.sg_db_url, client, warnings)
    if args.limit_specs:
        specs = specs[: args.limit_specs]

    entities_data, entities_meta = fetch_json_with_meta(args.entities_url, client)
    individuals_data, individuals_meta = fetch_json_with_meta(args.individuals_url, client)
    entities = load_entities(entities_data, args.include_unverified, warnings)
    individuals = load_individuals(individuals_data, args.include_unverified)
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

    contributors = collect_contributor_logins(commits_by_repo)
    print(f"Collected {len(contributors)} unique credited contributor logins", file=sys.stderr)
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
            entity_attribution=args.entity_attribution,
            ignore_participant_workstreams=args.ignore_participant_workstreams,
        )
        spec_reports.append(spec_report)

    totals = build_totals(spec_reports)
    aggregate = build_aggregate_rankings(spec_reports, totals["totalCommits"])
    report = {
        "generatedAt": now_iso(),
        "branch": args.branch,
        "inputs": {
            "sgDbUrl": args.sg_db_url,
            "entitiesUrl": args.entities_url,
            "individualsUrl": args.individuals_url,
            "affiliationSource": "org-public-members" if args.affiliation_source == "org-members" else args.affiliation_source,
            "includeEntityContacts": not args.no_entity_contacts,
            "includeUnverifiedParticipants": bool(args.include_unverified),
            "creditDirectCommits": True,
            "entityAttribution": args.entity_attribution,
            "ignoreParticipantWorkstreams": bool(args.ignore_participant_workstreams),
            "incremental": incremental,
        },
        "publicDataNote": (
            "Entity attribution uses participant-data and public GitHub organization memberships only. "
            "Private GitHub organization memberships are intentionally ignored, even if visible to the token used for the run."
        ),
        "disclaimer": (
            "Entity attribution uses the latest public GitHub organization memberships and participant-data contacts at generation time. "
            "Historical employer changes are not reconstructed; past contributions are credited to the current visible entity affiliation."
        ),
        "provenance": {
            "sources": {
                "sg-db": sg_meta,
                "entities": entities_meta,
                "individuals": individuals_meta,
            },
            "githubActions": github_actions_metadata(),
            "script": {
                "cacheVersion": CACHE_VERSION,
            },
        },
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
    except ReportError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
