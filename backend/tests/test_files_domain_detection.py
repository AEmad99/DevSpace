"""Pin the broadened 'files' domain detection in _classify_agent_request.

The intent classifier seeds a list of domain names from the latest user
message; each domain name in turn seeds a fixed toolset into the
agent's RAG-retrieval pool. The 'files' domain is the one that surfaces
write_file / edit_file / run_tests / bash / python, so a coding ask
that misses the seed silently drops the file tools from the agent's
toolset — the model then has to guess from prose descriptions, and
small models in particular fall back to bash heredocs (which the
harness explicitly forbids).

The original regex only fired on file/folder/repo/git/bash/python. The
broadened detection adds coding-verb patterns (fix/repair/refactor/
implement/add ... function|module|endpoint|...) so plain coding asks
like "refactor the user service" or "add a new endpoint" still reach
the files domain. Pin the key examples here so a future tightening of
the regex can't silently regress the seed.
"""
from src.agent_loop import _classify_agent_request, _DOMAIN_TOOL_MAP


def _domains_for(text: str) -> set:
    """Run the classifier on a single user message and return the seeded domains."""
    out = _classify_agent_request(messages=[{"role": "user", "content": text}],
                                  last_user=text)
    return set(out.get("domains") or set())


def test_explicit_file_keyword_still_seeds_files_domain():
    # Baseline: the original keyword regex must still work. README/doc
    # queries correctly route to the `documents` domain instead (which
    # also exposes edit tooling for editor documents), so we focus the
    # baseline on unambiguous file/folder/shell requests.
    for q in (
        "show me the file",
        "list the folder",
        "run a grep for TODO",
        "open the bash terminal",
        "edit the file at src/api/users.py",
    ):
        assert "files" in _domains_for(q), f"expected 'files' in domains for {q!r}"


def test_coding_verb_only_requests_seed_files_domain():
    # Coding asks that don't name a file/folder should still seed files.
    cases = [
        "fix the bug in the auth flow",
        "refactor the user service to use a dataclass",
        "implement a new endpoint for /v2/search",
        "add a function that returns the user's full name",
        "rewrite the parser to handle nested quotes",
        "extract the validation logic into its own module",
        "patch the race condition in the worker pool",
        "rename the function to something clearer",
        "add a method on the User class to export to JSON",
        "clean up the imports in src/api/users.py",
    ]
    for q in cases:
        domains = _domains_for(q)
        assert "files" in domains, (
            f"coding-verb-only request {q!r} must seed the files domain; "
            f"got {sorted(domains)}"
        )


def test_non_coding_smalltalk_does_not_seed_files_domain():
    # Negative: casual smalltalk must NOT pull file tools into context.
    for q in ("hi there", "thanks!", "what can you do?"):
        domains = _domains_for(q)
        assert "files" not in domains, (
            f"smalltalk {q!r} must not seed files; got {sorted(domains)}"
        )


def test_files_domain_toolset_includes_write_edit_and_tests():
    # The seed only matters because it pulls the right tools. Pin the
    # full toolset the files domain is supposed to contribute.
    files_tools = _DOMAIN_TOOL_MAP["files"]
    for must_have in ("write_file", "edit_file", "read_file",
                      "run_tests", "lint", "format",
                      "bash", "python", "grep", "glob", "ls"):
        assert must_have in files_tools, (
            f"files domain toolset missing {must_have!r} — would break "
            f"the coding-agent harness"
        )
