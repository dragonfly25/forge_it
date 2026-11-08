"""Tests for `forge init` core logic (spec §5.2, ADR 0014), on real temporary repositories."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

import forge.commands.docs  # noqa: F401  (registers the CLI description used by init)
from forge.core import adapters as A
from forge.core import docs as D
from forge.core.config import load_config
from forge.core.errors import ExitCode, PreconditionError
from forge.core.git import Git
from forge.core.init import (
    LOGIN_PLACEHOLDER,
    PLACEHOLDERS,
    REPO_PLACEHOLDER,
    InitValues,
    detect_default_branch,
    detect_values,
    github_repo_from_url,
    init_project,
    install_ref,
    merge_lines,
    set_adapters_line,
    substitute,
    template_texts,
    version_pin,
)
from forge.core.managed import BEGIN, END
from forge.core.plan import load_roadmap
from forge.core.team import load_team
from forge.core.zones import load_zones

TEMPLATE_PATHS = sorted(
    [
        ".forge/config.yml",
        ".forge/team.yml",
        ".forge/zones.yml",
        ".forge/rules.md",
        *(
            f".forge/prompts/{n}.md"
            for n in (
                "setup",
                "plan",
                "next",
                "dev",
                "sync",
                "ship",
                "review",
                "status",
                "handoff",
                "release",
            )
        ),
        *(f".forge/agents/{n}.md" for n in ("explorer", "test-writer", "reviewer", "implementer")),
        "specs/_template.md",
        "plans/roadmap.yml",
        "docs/decisions/_template.md",
        "docs/forge/onboarding.md",
        "docs/forge/roles.md",
        "docs/forge/faq.md",
        ".github/workflows/forge-ci.yml",
        ".github/workflows/forge-plan-apply.yml",
        ".github/workflows/forge-radar.yml",
        ".github/ISSUE_TEMPLATE/forge-proposal.yml",
        ".github/pull_request_template.md",
        ".gitattributes",
        ".gitignore",
        ".env.example",
    ]
)
COMMANDS = (
    "setup",
    "plan",
    "next",
    "dev",
    "sync",
    "ship",
    "review",
    "status",
    "handoff",
    "release",
)
ADAPTER_PATHS = sorted(
    [
        "docs/forge/commands.md",  # the generated command reference (ADR 0015)
        "AGENTS.md",
        "CLAUDE.md",
        *(f".claude/skills/forge-{n}/SKILL.md" for n in COMMANDS),
        *(f".agents/skills/forge-{n}/SKILL.md" for n in COMMANDS),
    ]
)

VALUES = InitValues(
    project_name="shop",
    repo="acme/shop",
    default_branch="main",
    forge_version=">=0.1,<0.2",
    forge_install_ref="v0.1.0",
    github_login="alice",
)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "shop"
    root.mkdir()
    Git(root).run("init", "--quiet", "--initial-branch=main")
    return root


def tree(root: Path) -> list[str]:
    return sorted(
        p.relative_to(root).as_posix()
        for p in root.rglob("*")
        if p.is_file() and ".git" not in p.relative_to(root).parts[:1]
    )


# -- placeholders and values ------------------------------------------------------------------


def test_template_tree_matches_adr() -> None:
    assert sorted(p.as_posix() for p in template_texts()) == TEMPLATE_PATHS


def test_substitute_only_known_placeholders() -> None:
    values = VALUES.as_dict()
    text = "{{project_name}} {{ repo }} ${{ secrets.GITHUB_TOKEN }} ${{repo}} {{other}}"
    assert substitute(text, values) == (
        "shop acme/shop ${{ secrets.GITHUB_TOKEN }} ${{repo}} {{other}}"
    )
    assert set(values) == set(PLACEHOLDERS)


def test_templates_use_only_known_placeholders() -> None:
    names = set()
    for text in template_texts().values():
        names |= set(re.findall(r"(?<!\$)\{\{\s*([^}]*?)\s*\}\}", text))
    assert names <= set(PLACEHOLDERS)


def test_version_pin_and_install_ref() -> None:
    assert version_pin("0.1.0.dev0") == ">=0.1,<0.2"
    assert version_pin("1.4.2") == ">=1.4,<1.5"
    assert install_ref("0.1.0.dev0") == "v0.1.0"
    assert install_ref("1.4.2rc1") == "v1.4.2"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("git@github.com:acme/shop.git", "acme/shop"),
        ("https://github.com/acme/shop.git", "acme/shop"),
        ("https://github.com/acme/shop", "acme/shop"),
        ("https://token@github.com/acme/my.repo.git\n", "acme/my.repo"),
        ("ssh://git@github.com/acme/shop.git", "acme/shop"),
        ("https://gitlab.com/acme/shop.git", None),
        ("/srv/remote.git", None),
    ],
)
def test_github_repo_from_url(url: str, expected: str | None) -> None:
    assert github_repo_from_url(url) == expected


def test_detect_values_without_origin(repo: Path) -> None:
    values = detect_values(repo, Git(repo), version="0.1.0.dev0", github_login=None)
    assert values == InitValues(
        project_name="shop",
        repo=REPO_PLACEHOLDER,
        default_branch="main",
        forge_version=">=0.1,<0.2",
        forge_install_ref="v0.1.0",
        github_login=LOGIN_PLACEHOLDER,
    )


def test_detect_values_from_clone(git_world) -> None:  # type: ignore[no-untyped-def]
    clone = git_world.clone("alice")
    clone.run("remote", "set-url", "origin", "git@github.com:acme/shop.git")
    clone.checkout("feature", create_from="HEAD")
    values = detect_values(clone.root(), clone, version="0.2.1", github_login="alice")
    assert clone.root().name != "shop"
    assert values.project_name == "shop"  # from github.repo, not the clone's directory
    assert values.repo == "acme/shop"
    assert values.default_branch == "main"  # from origin/HEAD, not the current branch
    assert values.github_login == "alice"
    assert values.forge_version == ">=0.2,<0.3"


def test_detect_values_project_name_falls_back_to_directory(tmp_path: Path) -> None:
    root = tmp_path / "my-dir"
    root.mkdir()
    git = Git(root)
    git.run("init", "--quiet")
    git.run("remote", "add", "origin", "https://gitlab.com/acme/other.git")
    values = detect_values(root, git, version="0.1.0", github_login=None)
    assert values.project_name == "my-dir" and values.repo == REPO_PLACEHOLDER


def test_default_branch_falls_back_to_current_branch(tmp_path: Path) -> None:
    root = tmp_path / "r"
    root.mkdir()
    Git(root).run("init", "--quiet", "--initial-branch=trunk")
    assert detect_default_branch(Git(root)) == "trunk"


def test_set_adapters_line() -> None:
    assert set_adapters_line("a: 1\nadapters: [x]\nb: 2\n", ["generic"]) == (
        "a: 1\nadapters: [generic]\nb: 2\n"
    )
    assert set_adapters_line("a: 1", ["generic", "codex"]) == "a: 1\nadapters: [generic, codex]\n"


def test_merge_lines() -> None:
    assert merge_lines("node_modules/\n.env.local\n", "# Forge\n.env.local\n.forge/local/\n") == (
        "node_modules/\n.env.local\n# Forge\n.forge/local/\n"
    )
    assert merge_lines("a", "a\nb\n") == "a\nb\n"
    assert merge_lines("a\r\n", "b\n") == "a\r\nb\r\n"
    assert merge_lines("x\n", "x\n") == "x\n"


# -- init_project -----------------------------------------------------------------------------


def test_init_empty_repo_snapshot(repo: Path) -> None:
    report = init_project(repo, VALUES, adapters=["generic", "claude", "codex"])
    assert tree(repo) == sorted(TEMPLATE_PATHS + ADAPTER_PATHS)
    assert sorted(p.as_posix() for p in report.created) == tree(repo)
    assert report.overwritten == report.merged == report.unchanged == []

    for path in tree(repo):
        text = (repo / path).read_text()
        leftovers = re.findall(r"(?<!\$)\{\{", text)
        assert leftovers == [], f"{path} has an unreplaced placeholder"

    config_text = (repo / ".forge/config.yml").read_text()
    assert 'forge_version: ">=0.1,<0.2"' in config_text
    assert "adapters: [generic, claude, codex]" in config_text
    config = load_config(repo)
    assert config.project.name == "shop"
    assert config.github.repo == "acme/shop"
    assert config.adapters == ("generic", "claude", "codex")
    assert "alice" in (repo / ".forge/team.yml").read_text()
    ci = (repo / ".github/workflows/forge-ci.yml").read_text()
    assert "v0.1.0" in ci and "${{ secrets.GITHUB_TOKEN }}" in ci
    agents_md = (repo / "AGENTS.md").read_text()
    assert agents_md.startswith(BEGIN) and END in agents_md
    assert "@AGENTS.md" in (repo / "CLAUDE.md").read_text().splitlines()

    # What init wrote is exactly what `forge adapters generate --check` expects.
    assert A.check(repo, A.load_project(repo, config)) == []

    # ... and what `forge docs --check` expects.
    reference = D.render_reference(D.registered_cli(), D.load_prompts_dir(repo, D.DEFAULT_PROMPTS))
    assert D.check_reference(repo / D.DEFAULT_OUTPUT, reference) == []
    assert "### `forge next`" in reference and "| `release` |" in reference

    # The other project files load with the real loaders.
    assert_project_files_load(repo, login="alice")


def test_workflows_install_with_optional_forge_install_token(repo: Path) -> None:
    init_project(repo, VALUES, adapters=["generic"])
    public = 'uv tool install "git+https://github.com/dragonfly25/forge_it@v0.1.0"'
    for name in ("forge-ci", "forge-plan-apply", "forge-radar"):
        text = (repo / f".github/workflows/{name}.yml").read_text()
        workflow = yaml.safe_load(text)
        steps = [s for job in workflow["jobs"].values() for s in job["steps"]]
        install = [s for s in steps if s.get("name") == "Install Forge (pinned)"]
        assert len(install) == 1, name
        step = install[0]
        assert step["env"] == {"FORGE_INSTALL_TOKEN": "${{ secrets.FORGE_INSTALL_TOKEN }}"}
        run = step["run"]
        # One install, always from the public URL: the token is never part of a URL or argv.
        installs = [line.strip() for line in run.splitlines() if "uv tool install" in line]
        assert installs == [public], name
        assert "@github.com" not in run and "x-access-token:${" not in run
        # With the secret set, git authenticates through env config (an extra header).
        guard = 'if [ -n "${FORGE_INSTALL_TOKEN:-}" ]; then'
        end = run.index("\nfi\n")
        assert run.index(guard) < end < run.index(public)
        block = run[run.index(guard) : end]
        assert "printf 'x-access-token:%s' \"$FORGE_INSTALL_TOKEN\" | base64" in block
        assert 'echo "::add-mask::${basic}"' in block
        assert "export GIT_CONFIG_COUNT=1" in block
        assert 'export GIT_CONFIG_KEY_0="http.https://github.com/.extraheader"' in block
        assert 'export GIT_CONFIG_VALUE_0="AUTHORIZATION: basic ${basic}"' in block
        assert run.count("FORGE_INSTALL_TOKEN") == 2  # the guard and the printf only
        assert 'echo "$FORGE_INSTALL_TOKEN' not in run and "set -x" not in run
        assert "secrets.FORGE_INSTALL_TOKEN" not in run


def assert_project_files_load(repo: Path, *, login: str) -> None:
    team = load_team(repo)
    assert team.member(login) is not None
    zones = load_zones(repo)
    assert zones.zones  # the template ships example zones
    roadmap = load_roadmap(repo)
    assert roadmap is not None and roadmap.milestones
    assert roadmap.milestones[0].lead == login


def test_init_with_placeholders_loads_once_substituted(repo: Path) -> None:
    from dataclasses import replace

    values = replace(VALUES, repo=REPO_PLACEHOLDER, github_login=LOGIN_PLACEHOLDER)
    init_project(repo, values, adapters=["generic"])
    for rel in (".forge/config.yml", ".forge/team.yml", "plans/roadmap.yml"):
        path = repo / rel
        text = path.read_text()
        text = text.replace(REPO_PLACEHOLDER, "acme/shop").replace(LOGIN_PLACEHOLDER, "bob")
        path.write_text(text)
    assert load_config(repo).github.repo == "acme/shop"
    assert_project_files_load(repo, login="bob")


def test_init_with_generic_only(repo: Path) -> None:
    init_project(repo, VALUES, adapters=[])
    assert tree(repo) == sorted([*TEMPLATE_PATHS, "AGENTS.md", "docs/forge/commands.md"])
    assert load_config(repo).adapters == ("generic",)


def test_init_is_idempotent(repo: Path) -> None:
    init_project(repo, VALUES, adapters=["claude"])
    report = init_project(repo, VALUES, adapters=["claude"])
    assert report.created == report.overwritten == report.merged == []
    assert len(report.unchanged) == len(tree(repo))


def test_init_refuses_existing_files_without_force(repo: Path) -> None:
    (repo / ".forge").mkdir()
    (repo / ".forge/rules.md").write_text("our rules\n")
    skill = repo / ".claude/skills/forge-next/SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("mine\n")
    with pytest.raises(PreconditionError) as info:
        init_project(repo, VALUES, adapters=["claude"])
    assert info.value.code == ExitCode.PRECONDITION
    assert ".forge/rules.md" in info.value.message
    assert ".claude/skills/forge-next/SKILL.md" in info.value.message
    assert "--force" in str(info.value.hint)
    assert tree(repo) == [".claude/skills/forge-next/SKILL.md", ".forge/rules.md"]

    report = init_project(repo, VALUES, adapters=["claude"], force=True)
    assert Path(".forge/rules.md") in report.overwritten
    assert (repo / ".forge/rules.md").read_text() != "our rules\n"


def test_init_merges_gitignore_gitattributes_and_regions(repo: Path) -> None:
    (repo / ".gitignore").write_bytes(b"node_modules/\r\n.env.local\r\n")
    (repo / ".gitattributes").write_text("*.png binary")
    (repo / "AGENTS.md").write_text("# Team notes\n\nKeep me.\n")
    (repo / "CLAUDE.md").write_text("Claude notes\n")
    report = init_project(repo, VALUES, adapters=["claude"])
    assert {p.as_posix() for p in report.merged} == {
        ".gitignore",
        ".gitattributes",
        "AGENTS.md",
        "CLAUDE.md",
    }
    gitignore = (repo / ".gitignore").read_bytes()
    assert gitignore.startswith(b"node_modules/\r\n.env.local\r\n")
    assert gitignore.count(b".env.local") == 1
    assert b".claude/agents/forge-*.md\r\n" in gitignore
    attrs = (repo / ".gitattributes").read_text()
    assert attrs.startswith("*.png binary\n") and "CHANGELOG.md merge=union" in attrs
    assert (repo / "AGENTS.md").read_text().startswith("# Team notes\n\nKeep me.\n\n" + BEGIN)
    assert (repo / "CLAUDE.md").read_text().startswith("Claude notes\n\n" + BEGIN)


def test_init_refuses_directory_in_place_of_file(repo: Path) -> None:
    (repo / ".env.example").mkdir()
    with pytest.raises(PreconditionError):
        init_project(repo, VALUES, adapters=[], force=True)


def test_init_uses_the_given_cli_description(repo: Path) -> None:
    cli = D.CliDoc(root=D.CommandDoc(path=("forge",), usage="forge [OPTIONS]"))
    init_project(repo, VALUES, adapters=[], cli=cli)
    text = (repo / "docs/forge/commands.md").read_text()
    assert text.startswith("# `forge` command reference")
    assert "## Commands" in text and "### `" not in text
    assert "| `next` |" in text


def test_init_refuses_a_changed_command_reference(repo: Path) -> None:
    init_project(repo, VALUES, adapters=[])
    (repo / "docs/forge/commands.md").write_text("edited\n")
    with pytest.raises(PreconditionError, match=r"docs/forge/commands\.md"):
        init_project(repo, VALUES, adapters=[])
