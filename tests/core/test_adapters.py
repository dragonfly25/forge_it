"""Tests for the adapters (spec §13, ADR 0014): project-level rendering, drift checks and
machine-level subagent rendering against real temporary repositories."""

from __future__ import annotations

import tomllib
from dataclasses import replace
from pathlib import Path

import pytest
import yaml
from packaging.specifiers import SpecifierSet

from forge.commands.docs import describe_cli
from forge.core import adapters as A
from forge.core.adapters import claude, codex, generic
from forge.core.adapters._common import GENERATED_MARK, Project, project_from_texts
from forge.core.config import ForgeConfig, GitHubConfig, ModelsConfig, ProjectConfig
from forge.core.errors import ExitCode, PreconditionError
from forge.core.git import Git
from forge.core.init import InitValues, init_project, render_templates
from forge.core.managed import BEGIN, END, region_inner
from forge.core.presets import load_presets
from forge.core.profile import AgentProfile, Profile
from forge.core.prompts import parse_agents
from forge.core.tiers import Effort, Tier

VALUES = InitValues(
    project_name="shop",
    repo="acme/shop",
    default_branch="main",
    forge_version=">=0.1,<0.2",
    forge_install_ref="v0.1.0",
    github_login="alice",
)


def shipped_project(adapters: tuple[str, ...] = ("generic", "claude", "codex")) -> Project:
    rendered = render_templates(VALUES, adapters)
    return project_from_texts(
        name="shop",
        default_branch="main",
        adapters=adapters,
        rules=rendered[Path(".forge/rules.md")],
        prompts={p: t for p, t in rendered.items() if p.parent == Path(".forge/prompts")},
        agents={p: t for p, t in rendered.items() if p.parent == Path(".forge/agents")},
    )


def config(min_tier_for: dict[str, Tier] | None = None) -> ForgeConfig:
    return ForgeConfig(
        forge_version=SpecifierSet(">=0.1,<0.2"),
        project=ProjectConfig(name="shop"),
        github=GitHubConfig(repo="acme/shop"),
        models=ModelsConfig(min_tier_for=min_tier_for or {}),
    )


CLAUDE_AGENT = AgentProfile(
    id="claude-pro-1",
    tool="claude-code",
    providers=("anthropic",),
    tiers={Tier.STRONG: "opus", Tier.STANDARD: "sonnet", Tier.LIGHT: "haiku"},
    effort={Effort.LOW: "low", Effort.MEDIUM: "medium", Effort.HIGH: "high"},
)
CODEX_AGENT = AgentProfile(
    id="openai-team",
    tool="codex",
    providers=("openai",),
    tiers={Tier.STRONG: "gpt-6-astra", Tier.LIGHT: "gpt-6-luna"},
    effort={Effort.LOW: "low", Effort.MEDIUM: "medium", Effort.HIGH: "xhigh"},
)
PROFILE = Profile(github="alice", agents=(CLAUDE_AGENT, CODEX_AGENT), default_agent="claude-pro-1")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """An initialised Forge project in a fresh git repository (nothing committed)."""
    root = tmp_path / "shop"
    root.mkdir()
    Git(root).run("init", "--quiet", "--initial-branch=main")
    init_project(root, VALUES, adapters=["claude", "codex"], cli=describe_cli())
    return root


def meta_and_body(text: str) -> tuple[dict[str, object], str]:
    assert text.startswith("---\n")
    _, front, body = text.split("---\n", 2)
    return yaml.safe_load(front), body


# -- generic ----------------------------------------------------------------------------------


def test_agents_md_region_content() -> None:
    project = shipped_project()
    [file] = generic.render(project)
    assert file.path == Path("AGENTS.md") and file.region
    text = file.content
    assert GENERATED_MARK in text.splitlines()[0]
    assert (
        "When the user invokes `/<cmd>`, `/forge-<cmd>` or asks for it in words, read and "
        "follow `.forge/prompts/<cmd>.md`." in text
    )
    for prompt in project.prompts:
        assert f"| `/{prompt.name}` | `.forge/prompts/{prompt.name}.md` |" in text
    for agent in (
        "forge-explorer",
        "forge-test-writer",
        "forge-reviewer",
        "forge-implementer",
        "forge-planner",
        "forge-arch-writer",
    ):
        assert f"`{agent}`" in text
    # Rules are inlined (headings shifted under "## Rules"), prompt bodies are not.
    rules_line = next(
        line for line in project.rules.splitlines() if line.strip() and not line.startswith("#")
    )
    assert rules_line in text
    assert project.prompts[0].body.strip() not in text
    assert "shop" in text


def test_agents_md_stays_well_under_codex_limit() -> None:
    [file] = generic.render(shipped_project())
    full = A.expected_content(Path("/nonexistent"), file)
    assert len(full.encode("utf-8")) < 32 * 1024


def test_shift_headings_skips_code_fences() -> None:
    md = "# A\n## B\n```\n# not a heading\n```\n###### deep\n"
    assert generic.shift_headings(md, 2) == (
        "### A\n#### B\n```\n# not a heading\n```\n###### deep\n"
    )


def test_table_cells_escape_pipes() -> None:
    assert generic.cell("a | b\nc") == "a \\| b c"


# -- claude -----------------------------------------------------------------------------------


def test_claude_skills_frontmatter_and_arguments() -> None:
    project = shipped_project()
    files = {f.path: f for f in claude.render(project)}
    nxt = files[Path(".claude/skills/forge-next/SKILL.md")]
    meta, body = meta_and_body(nxt.content)
    prompt = next(p for p in project.prompts if p.name == "next")
    assert meta == {
        "name": "forge-next",
        "description": prompt.description,
        "argument-hint": prompt.args,
    }
    assert GENERATED_MARK in body.splitlines()[0]
    assert ".forge/prompts/next.md" in body.splitlines()[0]
    assert body.rstrip("\n").endswith("Arguments: $ARGUMENTS")
    assert prompt.body.strip() in body
    assert len([p for p in files if p.name == "SKILL.md"]) == len(project.prompts)


def test_claude_skill_without_args_has_no_argument_hint() -> None:
    project = shipped_project()
    prompt = replace(project.prompts[0], args="")
    meta, _ = meta_and_body(claude.render_skill(prompt).content)
    assert "argument-hint" not in meta


def test_claude_md_region_imports_agents_md() -> None:
    files = {f.path: f for f in claude.render(shipped_project())}
    region = files[Path("CLAUDE.md")]
    assert region.region
    assert "@AGENTS.md" in region.content.splitlines()
    assert "/forge-next" in region.content


# -- codex ------------------------------------------------------------------------------------


def test_codex_skills() -> None:
    project = shipped_project()
    files = {f.path: f for f in codex.render(project)}
    assert set(files) == {Path(f".agents/skills/forge-{p.name}/SKILL.md") for p in project.prompts}
    meta, body = meta_and_body(files[Path(".agents/skills/forge-next/SKILL.md")].content)
    assert set(meta) == {"name", "description"}
    assert meta["name"] == "forge-next"
    assert "user's message" in body
    assert "$ARGUMENTS" not in body


def test_toml_strings_round_trip() -> None:
    tricky = 'quote " triple """ back \\ tab\t ctrl \x01 del \x7f end "'
    doc = f"a = {codex.toml_string(tricky)}\nb = {codex.toml_multiline(tricky + chr(10))}\n"
    parsed = tomllib.loads(doc)
    assert parsed["a"] == tricky
    assert parsed["b"] == tricky + "\n"


# -- driver: render, check, generate ------------------------------------------------------------


def test_render_project_respects_adapters() -> None:
    paths = {f.path for f in A.render_project(shipped_project(("generic",)))}
    assert paths == {Path("AGENTS.md")}
    paths = {f.path for f in A.render_project(shipped_project(("generic", "codex")))}
    assert Path("CLAUDE.md") not in paths
    assert Path(".agents/skills/forge-next/SKILL.md") in paths


def test_check_clean_after_init(repo: Path) -> None:
    assert A.check(repo, shipped_project()) == []


def test_check_detects_missing_different_and_extra(repo: Path) -> None:
    project = shipped_project()
    (repo / ".claude/skills/forge-next/SKILL.md").unlink()
    (repo / ".agents/skills/forge-ship/SKILL.md").write_text("edited\n")
    stale = repo / ".claude/skills/forge-old/SKILL.md"
    stale.parent.mkdir(parents=True)
    stale.write_text(f"---\nname: forge-old\n---\n<!-- {GENERATED_MARK}` from x -->\n")
    hand = repo / ".claude/skills/forge-mine/SKILL.md"
    hand.parent.mkdir(parents=True)
    hand.write_text("---\nname: forge-mine\n---\nhand written\n")
    agents_md = repo / "AGENTS.md"
    agents_md.write_text(agents_md.read_text().replace("Forge", "Frog", 1))

    drift = {(d.path.as_posix(), d.kind) for d in A.check(repo, project)}
    assert drift == {
        (".claude/skills/forge-next/SKILL.md", "missing"),
        (".agents/skills/forge-ship/SKILL.md", "different"),
        (".claude/skills/forge-old/SKILL.md", "extra"),
        ("AGENTS.md", "different"),
    }

    report = A.generate(repo, project)
    assert Path(".claude/skills/forge-old/SKILL.md") in report.removed
    assert not stale.parent.exists()
    assert hand.is_file()
    assert A.check(repo, project) == []
    again = A.generate(repo, project)
    assert again.written == [] and again.removed == []


def test_human_text_outside_region_is_never_flagged(repo: Path) -> None:
    agents_md = repo / "AGENTS.md"
    agents_md.write_bytes(b"# Our team notes\r\n\r\nKeep this.\r\n\r\n" + agents_md.read_bytes())
    project = shipped_project()
    A.generate(repo, project)
    data = agents_md.read_bytes()
    assert data.startswith(b"# Our team notes\r\n\r\nKeep this.\r\n\r\n")
    assert A.check(repo, project) == []


def test_disabling_an_adapter_makes_its_skills_extra(repo: Path) -> None:
    project = shipped_project(("generic", "claude"))
    kinds = {d.kind for d in A.check(repo, project) if d.path.parts[0] == ".agents"}
    assert kinds == {"extra"}
    A.generate(repo, project)
    assert not (repo / ".agents/skills").exists() or not any((repo / ".agents/skills").iterdir())


def test_disabling_claude_makes_its_skills_extra(repo: Path) -> None:
    project = shipped_project(("generic", "codex"))
    drift = {(d.path.parts[0], d.kind) for d in A.check(repo, project)}
    assert (".claude", "extra") in drift
    A.generate(repo, project)
    assert not (repo / ".claude/skills").exists() or not any((repo / ".claude/skills").iterdir())


def test_disabling_claude_reports_and_removes_the_claude_md_region(repo: Path) -> None:
    claude_md = repo / "CLAUDE.md"
    region = claude_md.read_text()
    claude_md.write_text("# Team notes\n\nKeep me.\n\n" + region)
    project = shipped_project(("generic", "codex"))

    drift = {(d.path.as_posix(), d.kind) for d in A.check(repo, project)}
    assert ("CLAUDE.md", "extra") in drift

    report = A.generate(repo, project)
    assert Path("CLAUDE.md") in report.written
    assert claude_md.read_text() == "# Team notes\n\nKeep me.\n"
    assert A.check(repo, project) == []
    assert A.generate(repo, project).written == []


def test_disabling_claude_deletes_a_claude_md_holding_only_the_region(repo: Path) -> None:
    project = shipped_project(("generic", "codex"))
    report = A.generate(repo, project)
    assert Path("CLAUDE.md") in report.removed
    assert not (repo / "CLAUDE.md").exists()
    assert A.check(repo, project) == []


def test_claude_md_without_region_is_left_alone_when_claude_is_disabled(repo: Path) -> None:
    (repo / "CLAUDE.md").write_text("# Hand written\n")
    project = shipped_project(("generic", "codex"))
    assert all(d.path != Path("CLAUDE.md") for d in A.check(repo, project))
    A.generate(repo, project)
    assert (repo / "CLAUDE.md").read_text() == "# Hand written\n"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (f"{BEGIN}\nx\n{END}\n", ""),
        (f"# a\n\n{BEGIN}\nx\n{END}\n", "# a\n"),
        (f"# a\r\n\r\n{BEGIN}\r\nx\r\n{END}\r\n", "# a\r\n"),
        (f"top\n{BEGIN}\nx\n{END}\nbottom\n", "top\nbottom\n"),
        ("no region\n", "no region\n"),
    ],
)
def test_remove_region(text: str, expected: str) -> None:
    assert A.remove_region(text) == expected


def test_region_inner_matches_render(repo: Path) -> None:
    text = (repo / "CLAUDE.md").read_text()
    assert text.startswith(BEGIN) and text.rstrip().endswith(END)
    inner = region_inner(text)
    assert inner is not None and "@AGENTS.md" in inner


# -- machine level ----------------------------------------------------------------------------


def local(cfg: ForgeConfig, profile: Profile = PROFILE) -> A.LocalRender:
    defs = shipped_project().agents
    return A.render_local_from(defs, profile, cfg, load_presets())


def test_local_render_claude_concrete_models() -> None:
    render = local(config())
    files = {f.path.as_posix(): f.content for f in render.files}
    meta, body = meta_and_body(files[".claude/agents/forge-explorer.md"])
    assert meta == {
        "name": "forge-explorer",
        "description": meta["description"],
        "tools": "Read, Grep, Glob",
        "model": "haiku",
        "effort": "low",
    }
    assert "--local" in body.splitlines()[0] and "claude-pro-1" in body.splitlines()[0]
    meta, _ = meta_and_body(files[".claude/agents/forge-test-writer.md"])
    assert meta["model"] == "inherit"
    assert meta["tools"] == "Read, Grep, Glob, Edit, Write, Bash"
    meta, _ = meta_and_body(files[".claude/agents/forge-reviewer.md"])
    assert (meta["model"], meta["effort"]) == ("opus", "high")


def test_local_render_codex_toml() -> None:
    render = local(config())
    files = {f.path.as_posix(): f.content for f in render.files}
    explorer = files[".codex/agents/forge-explorer.toml"]
    assert explorer.startswith("# ") and GENERATED_MARK in explorer.splitlines()[0]
    data = tomllib.loads(explorer)
    assert data["name"] == "forge-explorer"
    assert data["model"] == "gpt-6-luna"
    assert data["model_reasoning_effort"] == "low"
    assert data["developer_instructions"].strip()
    assert "tools" not in data
    writer = tomllib.loads(files[".codex/agents/forge-test-writer.toml"])
    assert "model" not in writer
    reviewer = tomllib.loads(files[".codex/agents/forge-reviewer.toml"])
    assert (reviewer["model"], reviewer["model_reasoning_effort"]) == ("gpt-6-astra", "xhigh")


def test_local_render_implementer_for_claude_and_codex() -> None:
    files = {f.path.as_posix(): f.content for f in local(config()).files}
    meta, body = meta_and_body(files[".claude/agents/forge-implementer.md"])
    assert meta == {
        "name": "forge-implementer",
        "description": meta["description"],
        "tools": "Read, Grep, Glob, Edit, Write, Bash",
        "model": "sonnet",
        "effort": "medium",
    }
    assert "implementer.md" in body.splitlines()[0]
    data = tomllib.loads(files[".codex/agents/forge-implementer.toml"])
    assert data["name"] == "forge-implementer"
    # The Codex agent serves no standard tier, so the next tier up is used.
    assert (data["model"], data["model_reasoning_effort"]) == ("gpt-6-astra", "medium")
    assert data["developer_instructions"].strip()
    assert "tools" not in data


def test_local_render_planner_and_arch_writer_for_claude_and_codex() -> None:
    files = {f.path.as_posix(): f.content for f in local(config()).files}
    meta, body = meta_and_body(files[".claude/agents/forge-planner.md"])
    assert meta == {
        "name": "forge-planner",
        "description": meta["description"],
        "tools": "Read, Grep, Glob, Edit, Write",
        "model": "sonnet",
        "effort": "medium",
    }
    assert "planner.md" in body.splitlines()[0]
    meta, body = meta_and_body(files[".claude/agents/forge-arch-writer.md"])
    assert meta == {
        "name": "forge-arch-writer",
        "description": meta["description"],
        "tools": "Read, Grep, Glob, Edit, Write",
        "model": "opus",
        "effort": "high",
    }
    assert "arch-writer.md" in body.splitlines()[0]
    planner = tomllib.loads(files[".codex/agents/forge-planner.toml"])
    assert planner["name"] == "forge-planner"
    # The Codex agent serves no standard tier, so the next tier up is used.
    assert (planner["model"], planner["model_reasoning_effort"]) == ("gpt-6-astra", "medium")
    assert planner["developer_instructions"].strip() and "tools" not in planner
    arch = tomllib.loads(files[".codex/agents/forge-arch-writer.toml"])
    assert arch["name"] == "forge-arch-writer"
    assert (arch["model"], arch["model_reasoning_effort"]) == ("gpt-6-astra", "xhigh")
    assert arch["developer_instructions"].strip() and "tools" not in arch


def test_min_tier_for_raises_the_planner() -> None:
    files = {f.path.as_posix(): f.content for f in local(config({"planner": Tier.STRONG})).files}
    assert meta_and_body(files[".claude/agents/forge-planner.md"])[0]["model"] == "opus"


def test_min_tier_for_raises_the_implementer() -> None:
    cfg = config({"implementer": Tier.STRONG})
    files = {f.path.as_posix(): f.content for f in local(cfg).files}
    meta, _ = meta_and_body(files[".claude/agents/forge-implementer.md"])
    assert (meta["model"], meta["effort"]) == ("opus", "medium")
    assert tomllib.loads(files[".codex/agents/forge-implementer.toml"])["model"] == "gpt-6-astra"


def test_min_tier_for_raises_the_subagent() -> None:
    files = {f.path.as_posix(): f.content for f in local(config({"explorer": Tier.STRONG})).files}
    meta, _ = meta_and_body(files[".claude/agents/forge-explorer.md"])
    assert meta["model"] == "opus"
    assert tomllib.loads(files[".codex/agents/forge-explorer.toml"])["model"] == "gpt-6-astra"


def test_profile_change_changes_rendered_models() -> None:
    changed = replace(CLAUDE_AGENT, tiers={**CLAUDE_AGENT.tiers, Tier.STRONG: "fable"})
    profile = replace(PROFILE, agents=(changed, CODEX_AGENT))
    before = {f.path.as_posix(): f.content for f in local(config()).files}
    after = {f.path.as_posix(): f.content for f in local(config(), profile).files}
    assert meta_and_body(after[".claude/agents/forge-reviewer.md"])[0]["model"] == "fable"
    assert before[".claude/agents/forge-reviewer.md"] != after[".claude/agents/forge-reviewer.md"]


def test_unserved_tier_inherits_with_warning() -> None:
    weak = replace(CLAUDE_AGENT, tiers={Tier.LIGHT: "haiku"})
    render = local(config(), Profile(github="alice", agents=(weak,), default_agent=weak.id))
    files = {f.path.as_posix(): f.content for f in render.files}
    assert meta_and_body(files[".claude/agents/forge-reviewer.md"])[0]["model"] == "inherit"
    assert any("reviewer" in w and "cannot serve" in w for w in render.warnings)
    assert not any(p.startswith(".codex") for p in files)


def test_non_claude_model_renders_inherit_with_warning() -> None:
    hybrid = replace(CLAUDE_AGENT, tiers={**CLAUDE_AGENT.tiers, Tier.LIGHT: "llama-local"})
    render = local(config(), Profile(github="alice", agents=(hybrid,), default_agent=hybrid.id))
    files = {f.path.as_posix(): f.content for f in render.files}
    assert meta_and_body(files[".claude/agents/forge-explorer.md"])[0]["model"] == "inherit"
    assert meta_and_body(files[".claude/agents/forge-reviewer.md"])[0]["model"] == "opus"
    assert any("explorer" in w and "'llama-local'" in w for w in render.warnings)


@pytest.mark.parametrize(
    ("model", "ok"),
    [
        ("haiku", True),
        ("sonnet", True),
        ("opus", True),
        ("fable", True),
        ("claude-opus-5-5", True),
        ("llama-local", False),
        ("gpt-6-astra", False),
        ("Opus", False),
    ],
)
def test_is_claude_model(model: str, ok: bool) -> None:
    assert claude.is_claude_model(model) is ok


def test_full_claude_model_id_is_kept() -> None:
    full = replace(CLAUDE_AGENT, tiers={**CLAUDE_AGENT.tiers, Tier.STRONG: "claude-opus-5-5"})
    render = local(config(), Profile(github="alice", agents=(full,), default_agent=full.id))
    files = {f.path.as_posix(): f.content for f in render.files}
    meta = meta_and_body(files[".claude/agents/forge-reviewer.md"])[0]
    assert meta["model"] == "claude-opus-5-5"
    assert not any("not a Claude model" in w for w in render.warnings)


def test_effort_values_outside_tool_range_are_dropped() -> None:
    odd = replace(CODEX_AGENT, effort={Effort.HIGH: "ultra", Effort.LOW: "low"})
    render = local(config(), Profile(github="alice", agents=(odd,), default_agent=odd.id))
    files = {f.path.as_posix(): f.content for f in render.files}
    reviewer = tomllib.loads(files[".codex/agents/forge-reviewer.toml"])
    assert "model_reasoning_effort" not in reviewer
    assert any("ultra" in w for w in render.warnings)


def test_agent_for_tool_prefers_default() -> None:
    second = replace(CLAUDE_AGENT, id="claude-2")
    profile = Profile(github="a", agents=(CLAUDE_AGENT, second), default_agent="claude-2")
    assert A.agent_for_tool(profile, "claude-code") is second
    profile = replace(profile, default_agent="claude-pro-1")
    assert A.agent_for_tool(profile, "claude-code") is CLAUDE_AGENT
    assert A.agent_for_tool(profile, "codex") is None


def test_write_local_files_are_gitignored(repo: Path) -> None:
    git = Git(repo)
    cfg = config()
    render = A.render_local(repo, PROFILE, cfg)
    assert A.local_status(repo, PROFILE, cfg, git)  # nothing written yet
    written = A.write_local(repo, render, git)
    assert {p.as_posix() for p in written} == {f.path.as_posix() for f in render.files}
    assert A.local_status(repo, PROFILE, cfg, git) == []
    assert A.write_local(repo, render, git) == []
    untracked = git.run("status", "--porcelain", "--untracked-files=all").stdout
    assert ".claude/agents" not in untracked and ".codex/agents" not in untracked

    # A profile change makes the files outdated until re-rendered.
    changed = replace(CLAUDE_AGENT, tiers={**CLAUDE_AGENT.tiers, Tier.STRONG: "fable"})
    profile = replace(PROFILE, agents=(changed, CODEX_AGENT))
    # Both strong-tier agents (arch-writer and reviewer) change model.
    assert A.local_status(repo, profile, cfg, git) == [
        "outdated: .claude/agents/forge-arch-writer.md",
        "outdated: .claude/agents/forge-reviewer.md",
    ]
    A.write_local(repo, A.render_local(repo, profile, cfg), git)
    assert A.local_status(repo, profile, cfg, git) == []


def test_write_local_removes_stale_generated_files(repo: Path) -> None:
    git = Git(repo)
    A.write_local(repo, A.render_local(repo, PROFILE, config()), git)
    only_claude = Profile(github="alice", agents=(CLAUDE_AGENT,), default_agent=CLAUDE_AGENT.id)
    status = A.local_status(repo, only_claude, config(), git)
    assert "stale: .codex/agents/forge-explorer.toml" in status
    removed = A.write_local(repo, A.render_local(repo, only_claude, config()), git)
    assert Path(".codex/agents/forge-explorer.toml") in removed
    assert not (repo / ".codex/agents/forge-explorer.toml").exists()


def test_write_local_refuses_files_git_would_not_ignore(repo: Path) -> None:
    gitignore = repo / ".gitignore"
    gitignore.write_text(gitignore.read_text().replace(".codex/agents/forge-*.toml\n", ""))
    git = Git(repo)
    render = A.render_local(repo, PROFILE, config())
    with pytest.raises(PreconditionError) as info:
        A.write_local(repo, render, git)
    assert info.value.code == ExitCode.PRECONDITION
    assert ".codex/agents/forge-*.toml" in str(info.value.hint)
    assert not (repo / ".claude/agents").exists()  # nothing written
    status = A.local_status(repo, PROFILE, config(), git)
    assert any(m.startswith("not ignored by git: .codex/agents/forge-") for m in status)


def test_render_local_without_agents_dir(tmp_path: Path) -> None:
    render = A.render_local(tmp_path, PROFILE, config())
    assert render.files == ()


def test_parse_agents_used_by_local_render() -> None:
    defs = parse_agents(
        {
            Path(".forge/agents/solo.md"): (
                "---\nname: solo\ndescription: d\ntier: standard\neffort: medium\ntools: []\n"
                "---\nDo.\n"
            )
        }
    )
    render = A.render_local_from(defs, PROFILE, config(), load_presets())
    meta, _ = meta_and_body(render.files[0].content)
    assert "tools" not in meta and meta["model"] == "sonnet"


def test_write_local_best_effort_writes_or_warns(repo: Path) -> None:
    written, warnings = A.write_local_best_effort(repo, PROFILE, config())
    assert repo / ".claude/agents/forge-reviewer.md" in written
    assert warnings == []
    (repo / ".gitignore").write_text(".forge/local/\n")
    (repo / ".claude/agents/forge-reviewer.md").unlink()
    written, warnings = A.write_local_best_effort(repo, PROFILE, config())
    assert written == []
    assert len(warnings) == 1 and str(repo) in warnings[0] and "ignore" in warnings[0]
    assert not (repo / ".claude/agents/forge-reviewer.md").exists()
