"""Generate deterministic Playwright spec skeletons from structured Scenarios."""

from __future__ import annotations

import argparse
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from webapp_debug_skill.cli import CliResult, Issue, emit_result
from webapp_debug_skill.errors import (
    EXIT_ARGUMENT_OR_SCHEMA,
    EXIT_EXTERNAL_FAILURE,
    EXIT_OK,
    EXIT_POLICY_BLOCKED,
    EXIT_UNEXPECTED,
)
from webapp_debug_skill.inventory_model import dumps_snapshot
from webapp_debug_skill.locator_model import (
    LocatorCandidate,
    LocatorChoice,
    LocatorKind,
    LocatorModelError,
    choose_locator,
    parse_locator_candidates,
)
from webapp_debug_skill.playwright_project import (
    DEFAULT_PROJECT_DIR,
    GENERATED_MARKER,
    MANIFEST_NAME,
    PlannedFile,
    atomic_write,
    ensure_no_symlink_path,
    ensure_within_root,
    manifest_entry,
    read_bytes,
    read_existing_manifest,
    relative_file,
    resolve_project_dir,
    resolve_root,
    safe_relative_path,
    sha256_bytes,
    validate_target_file,
)
from webapp_debug_skill.redaction import secret_findings
from webapp_debug_skill.scenario_model import (
    DataRequirementKind,
    ScenarioAction,
    ScenarioActionKind,
    ScenarioAssertion,
    ScenarioAssertionKind,
    ScenarioContract,
    ScenarioLifecycleStatus,
    ScenarioModelError,
    ScenarioTestScope,
)

SCENARIOS_TAB = "Scenarios"
GENERATION_MANIFEST_NAME = "webapp-debug.generation-manifest.json"
SUPPORTED_ACTIONS = {
    ScenarioActionKind.NAVIGATE,
    ScenarioActionKind.CLICK,
    ScenarioActionKind.FILL,
    ScenarioActionKind.SELECT,
    ScenarioActionKind.SUBMIT,
    ScenarioActionKind.WAIT,
}
SUPPORTED_ASSERTIONS = {
    ScenarioAssertionKind.VISIBLE,
    ScenarioAssertionKind.TEXT,
    ScenarioAssertionKind.URL,
}
SAFE_DATA_REQUIREMENTS = {DataRequirementKind.NONE}
BLOCKING_DATA_REQUIREMENTS = {
    DataRequirementKind.SEEDED_ACCOUNT,
    DataRequirementKind.TEST_RECORD,
    DataRequirementKind.UPLOAD_FILE,
    DataRequirementKind.MAILBOX,
    DataRequirementKind.PERMISSION,
}


class PlaywrightGeneratorError(RuntimeError):
    """Safe Playwright generator error."""

    def __init__(
        self,
        code: str,
        path: str = "playwright_generator",
        reason: str = "FAILED",
        *,
        exit_code: int = EXIT_POLICY_BLOCKED,
    ) -> None:
        safe_code = "PLAYWRIGHT_GENERATOR_FAILED" if secret_findings(code) else code
        super().__init__(safe_code)
        self.code = safe_code
        self.path = "playwright_generator" if secret_findings(path) else path
        self.reason = "FAILED" if secret_findings(reason) else reason
        self.exit_code = exit_code


@dataclass(frozen=True)
class ScenarioGenerationDecision:
    """Generation decision for one Scenario."""

    scenario_id: str
    scenario_version: int
    status: str
    reason_code: str
    test_file: str = ""
    test_name: str = ""
    locator_stability: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Return safe JSON data."""

        return {
            "scenario_id": self.scenario_id,
            "scenario_version": self.scenario_version,
            "status": self.status,
            "reason_code": self.reason_code,
            "test_file": self.test_file,
            "test_name": self.test_name,
            "locator_stability": self.locator_stability,
        }


@dataclass(frozen=True)
class GeneratedSpec:
    """Generated spec file and its source Scenario."""

    scenario: ScenarioContract
    planned_file: PlannedFile
    test_name: str


@dataclass(frozen=True)
class GeneratedPageObject:
    """Generated page object file for one Scenario."""

    scenario: ScenarioContract
    planned_file: PlannedFile


@dataclass(frozen=True)
class LocatorBinding:
    """One generated page object locator binding."""

    method_name: str
    choice: LocatorChoice

    def to_dict(self) -> dict[str, Any]:
        """Return safe JSON data."""

        return {
            "method_name": self.method_name,
            "locator_stability": self.choice.locator_stability,
            "requires_review": self.choice.requires_review,
            "choice": self.choice.to_payload(),
        }


@dataclass(frozen=True)
class GenerationFileDecision:
    """Safe file decision for generated files."""

    path: str
    action: str
    checksum: str
    size: int

    def to_dict(self) -> dict[str, Any]:
        """Return safe JSON details."""

        return {
            "path": self.path,
            "action": self.action,
            "checksum": self.checksum,
            "size": self.size,
        }


@dataclass(frozen=True)
class PlaywrightGenerationPlan:
    """Validated generation plan."""

    root: Path
    project_dir: Path
    scenario_count: int
    decisions: tuple[ScenarioGenerationDecision, ...]
    files: tuple[PlannedFile, ...]
    file_decisions: tuple[GenerationFileDecision, ...]

    def to_safe_dict(self, *, dry_run: bool) -> dict[str, Any]:
        """Return safe CLI data."""

        return {
            "dry_run": dry_run,
            "project_dir": safe_relative_path(self.root, self.project_dir),
            "scenario_count": self.scenario_count,
            "generated_count": sum(1 for item in self.decisions if item.status == "GENERATED"),
            "blocked_count": sum(1 for item in self.decisions if item.status == "BLOCKED"),
            "planned_file_count": len(self.files),
            "create_count": sum(1 for item in self.file_decisions if item.action == "CREATE"),
            "update_count": sum(1 for item in self.file_decisions if item.action == "UPDATE"),
            "unchanged_count": sum(1 for item in self.file_decisions if item.action == "UNCHANGED"),
            "scenarios": [item.to_dict() for item in self.decisions],
            "files": [item.to_dict() for item in self.file_decisions],
        }


def build_parser() -> argparse.ArgumentParser:
    """Build generate_playwright_tests parser."""

    parser = argparse.ArgumentParser(
        description="Generate static Playwright spec skeletons from structured Scenario rows."
    )
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--scenario-json", type=Path, required=True)
    parser.add_argument("--project-dir", type=Path, default=DEFAULT_PROJECT_DIR)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run generate_playwright_tests CLI."""

    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = run(args)
    except PlaywrightGeneratorError as exc:
        result = CliResult(
            ok=False,
            code=exc.code,
            message="Playwright test generation failed.",
            details=[Issue(exc.path, exc.reason)],
        )
        emit_result(result, args.format)
        return exc.exit_code
    except Exception:
        result = CliResult(
            ok=False,
            code="PLAYWRIGHT_GENERATOR_UNEXPECTED",
            message="Unexpected Playwright generator failure.",
            details=[Issue("playwright_generator", "UNEXPECTED")],
        )
        emit_result(result, args.format)
        return EXIT_UNEXPECTED
    emit_result(result, args.format)
    return EXIT_OK if result.ok else EXIT_POLICY_BLOCKED


def run(args: argparse.Namespace) -> CliResult:
    """Build and optionally write a Playwright generation plan."""

    root = resolve_root(args.root)
    project_dir = resolve_project_dir(root, args.project_dir)
    scenario_path = resolve_input_path(args.scenario_json)
    scenarios = read_scenarios(scenario_path)
    plan = build_generation_plan(root, project_dir, scenarios)
    if not args.dry_run:
        write_generation_plan(plan)
    return CliResult(
        ok=True,
        code="PLAYWRIGHT_GENERATION_PLAN" if args.dry_run else "PLAYWRIGHT_GENERATION_OK",
        message=(
            "Playwright generation plan generated."
            if args.dry_run
            else "Playwright test skeletons written."
        ),
        details=[],
        data=plan.to_safe_dict(dry_run=args.dry_run),
    )


def resolve_input_path(path: Path) -> Path:
    """Resolve Scenario JSON input."""

    try:
        resolved = path.expanduser().resolve(strict=True)
    except OSError:
        raise PlaywrightGeneratorError(
            "PLAYWRIGHT_GENERATOR_INPUT_INVALID",
            "scenario_json",
            "MISSING",
            exit_code=EXIT_ARGUMENT_OR_SCHEMA,
        ) from None
    if not resolved.is_file() or resolved.is_symlink():
        raise PlaywrightGeneratorError(
            "PLAYWRIGHT_GENERATOR_INPUT_INVALID",
            "scenario_json",
            "NOT_REGULAR_FILE",
            exit_code=EXIT_ARGUMENT_OR_SCHEMA,
        )
    return resolved


def read_scenarios(path: Path) -> tuple[ScenarioContract, ...]:
    """Read Scenario rows from JSON object/list without leaking raw values."""

    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        raise PlaywrightGeneratorError(
            "PLAYWRIGHT_GENERATOR_JSON_INVALID",
            "scenario_json",
            "JSON_INVALID",
            exit_code=EXIT_ARGUMENT_OR_SCHEMA,
        ) from None
    except OSError:
        raise PlaywrightGeneratorError(
            "PLAYWRIGHT_GENERATOR_READ_FAILED",
            "scenario_json",
            "READ_FAILED",
            exit_code=EXIT_EXTERNAL_FAILURE,
        ) from None
    rows = extract_scenario_rows(loaded)
    scenarios: list[ScenarioContract] = []
    for index, row in enumerate(rows):
        try:
            scenarios.append(ScenarioContract.from_sheet_row(row))
        except ScenarioModelError as exc:
            raise PlaywrightGeneratorError(
                exc.code,
                f"scenarios.[{index}].{exc.path}",
                exc.reason,
                exit_code=EXIT_ARGUMENT_OR_SCHEMA,
            ) from None
    return tuple(scenarios)


def extract_scenario_rows(value: Any) -> list[Mapping[str, Any]]:
    """Extract Scenario rows from supported JSON payload shapes."""

    if isinstance(value, list):
        rows = value
    elif isinstance(value, Mapping):
        tabs = value.get("tabs")
        if isinstance(tabs, Mapping) and isinstance(tabs.get(SCENARIOS_TAB), list):
            rows = tabs[SCENARIOS_TAB]
        elif isinstance(value.get(SCENARIOS_TAB), list):
            rows = value[SCENARIOS_TAB]
        elif isinstance(value.get("scenarios"), list):
            rows = value["scenarios"]
        else:
            raise PlaywrightGeneratorError(
                "PLAYWRIGHT_GENERATOR_JSON_INVALID",
                "scenario_json",
                "SCENARIOS_MISSING",
                exit_code=EXIT_ARGUMENT_OR_SCHEMA,
            )
    else:
        raise PlaywrightGeneratorError(
            "PLAYWRIGHT_GENERATOR_JSON_INVALID",
            "scenario_json",
            "OBJECT_OR_LIST_REQUIRED",
            exit_code=EXIT_ARGUMENT_OR_SCHEMA,
        )
    if not rows:
        raise PlaywrightGeneratorError(
            "PLAYWRIGHT_GENERATOR_JSON_INVALID",
            "scenario_json",
            "SCENARIOS_EMPTY",
            exit_code=EXIT_ARGUMENT_OR_SCHEMA,
        )
    if not all(isinstance(row, Mapping) for row in rows):
        raise PlaywrightGeneratorError(
            "PLAYWRIGHT_GENERATOR_JSON_INVALID",
            "scenario_json",
            "SCENARIO_OBJECT_REQUIRED",
            exit_code=EXIT_ARGUMENT_OR_SCHEMA,
        )
    return list(rows)


def build_generation_plan(
    root: Path,
    project_dir: Path,
    scenarios: Sequence[ScenarioContract],
) -> PlaywrightGenerationPlan:
    """Build and validate generated specs and manifest updates."""

    existing_manifest = require_bootstrap_manifest(project_dir)
    specs: list[GeneratedSpec] = []
    page_objects: list[GeneratedPageObject] = []
    scenario_decisions: list[ScenarioGenerationDecision] = []
    for scenario in sorted(scenarios, key=lambda item: (item.scenario_id, item.scenario_version)):
        decision = classify_scenario(root, project_dir, scenario)
        scenario_decisions.append(decision)
        if decision.status == "GENERATED":
            specs.append(build_spec(root, project_dir, scenario, decision.test_name))
            page_object = build_page_object(root, project_dir, scenario)
            if page_object is not None:
                page_objects.append(page_object)

    status_manifest = generation_status_manifest(
        root,
        project_dir,
        scenario_decisions,
    )
    planned_files = (
        tuple(spec.planned_file for spec in specs)
        + tuple(page_object.planned_file for page_object in page_objects)
        + (generation_manifest_file(root, project_dir, status_manifest),)
    )
    updated_manifest = merged_project_manifest(existing_manifest, planned_files)
    all_files = planned_files + (project_manifest_file(root, project_dir, updated_manifest),)
    file_decisions = validate_generated_files(
        root,
        project_dir,
        all_files,
        existing_manifest=existing_manifest,
    )
    return PlaywrightGenerationPlan(
        root=root,
        project_dir=project_dir,
        scenario_count=len(scenarios),
        decisions=tuple(scenario_decisions),
        files=all_files,
        file_decisions=tuple(file_decisions),
    )


def require_bootstrap_manifest(project_dir: Path) -> Mapping[str, Any]:
    """Require an existing Phase 8A manifest before generating tests."""

    manifest_path = project_dir / MANIFEST_NAME
    try:
        manifest = read_existing_manifest(manifest_path)
    except Exception:
        raise PlaywrightGeneratorError(
            "PLAYWRIGHT_GENERATOR_BOOTSTRAP_REQUIRED",
            MANIFEST_NAME,
            "MANIFEST_INVALID",
        ) from None
    if manifest is None:
        raise PlaywrightGeneratorError(
            "PLAYWRIGHT_GENERATOR_BOOTSTRAP_REQUIRED",
            MANIFEST_NAME,
            "MISSING",
        )
    if manifest.get("generator") != "webapp-debug":
        raise PlaywrightGeneratorError(
            "PLAYWRIGHT_GENERATOR_BOOTSTRAP_REQUIRED",
            MANIFEST_NAME,
            "GENERATOR_INVALID",
        )
    return manifest


def classify_scenario(
    root: Path,
    project_dir: Path,
    scenario: ScenarioContract,
) -> ScenarioGenerationDecision:
    """Return GENERATED/BLOCKED decision for one Scenario."""

    reason = blocking_reason(scenario)
    test_file = scenario_spec_relative_path(root, project_dir, scenario)
    test_name = scenario_test_name(scenario)
    locator_stability = scenario_locator_stability(scenario)
    if reason:
        return ScenarioGenerationDecision(
            scenario_id=scenario.scenario_id,
            scenario_version=scenario.scenario_version,
            status="BLOCKED",
            reason_code=reason,
            locator_stability=locator_stability,
        )
    return ScenarioGenerationDecision(
        scenario_id=scenario.scenario_id,
        scenario_version=scenario.scenario_version,
        status="GENERATED",
        reason_code="READY",
        test_file=test_file,
        test_name=test_name,
        locator_stability=locator_stability,
    )


def blocking_reason(scenario: ScenarioContract) -> str:
    """Return a safe reason code if a Scenario must not be generated."""

    if scenario.lifecycle_status != ScenarioLifecycleStatus.ACTIVE:
        return "INACTIVE_SCENARIO"
    if scenario.test_scope != ScenarioTestScope.E2E_PLAYWRIGHT:
        return "NON_E2E_SCOPE"
    if scenario.conflict_detected:
        return "EXPECTATION_CONFLICT"
    for requirement in scenario.data_requirements:
        if requirement.kind in BLOCKING_DATA_REQUIREMENTS:
            return "UNSAFE_DATA_REQUIREMENT"
        if requirement.kind not in SAFE_DATA_REQUIREMENTS:
            return "UNSUPPORTED_DATA_REQUIREMENT"
    for action in scenario.actions:
        if action.kind not in SUPPORTED_ACTIONS:
            return "UNSUPPORTED_ACTION"
        reason = action_blocking_reason(action)
        if reason:
            return reason
    for assertion in scenario.assertions:
        if assertion.kind not in SUPPORTED_ASSERTIONS:
            return "UNSUPPORTED_ASSERTION"
        reason = assertion_blocking_reason(assertion)
        if reason:
            return reason
    return ""


def action_blocking_reason(action: ScenarioAction) -> str:
    """Return whether one action lacks safe static generation data."""

    if action.kind == ScenarioActionKind.NAVIGATE:
        return "" if safe_relative_url(action.target) else "NAVIGATION_TARGET_UNSAFE"
    if action.kind in {
        ScenarioActionKind.CLICK,
        ScenarioActionKind.FILL,
        ScenarioActionKind.SELECT,
        ScenarioActionKind.SUBMIT,
    }:
        choice, reason = locator_choice_for_value(action.target, path="structured_actions.target")
        if reason:
            return reason
        if choice is None:
            return "LOCATOR_SOURCE_MISSING"
        if choice.requires_review:
            return "LOCATOR_REVIEW_REQUIRED"
        if action.kind in {ScenarioActionKind.FILL, ScenarioActionKind.SELECT} and not action.value:
            return "ACTION_VALUE_MISSING"
    return ""


def assertion_blocking_reason(assertion: ScenarioAssertion) -> str:
    """Return whether one assertion lacks safe static generation data."""

    if assertion.kind == ScenarioAssertionKind.URL:
        value = assertion.expected or assertion.target
        return "" if safe_relative_url(value) else "ASSERTION_TARGET_UNSAFE"
    if assertion.kind in {ScenarioAssertionKind.VISIBLE, ScenarioAssertionKind.TEXT}:
        choice, reason = locator_choice_for_value(
            assertion.target,
            path="structured_assertions.target",
        )
        if reason:
            return reason
        if choice is None:
            return "LOCATOR_SOURCE_MISSING"
        if choice.requires_review:
            return "LOCATOR_REVIEW_REQUIRED"
        if assertion.kind == ScenarioAssertionKind.TEXT and not assertion.expected:
            return "ASSERTION_EXPECTED_MISSING"
    return ""


def build_spec(
    root: Path,
    project_dir: Path,
    scenario: ScenarioContract,
    test_name: str,
) -> GeneratedSpec:
    """Build a generated Playwright spec file."""

    relative_path = scenario_spec_relative_path(root, project_dir, scenario)
    content = render_spec(scenario, test_name).encode("utf-8")
    if secret_findings(content.decode("utf-8", errors="replace")):
        raise PlaywrightGeneratorError(
            "PLAYWRIGHT_GENERATOR_CONTENT_UNSAFE",
            relative_path,
            "SECRET_DETECTED",
        )
    validate_static_typescript(content.decode("utf-8", errors="replace"), relative_path)
    return GeneratedSpec(
        scenario=scenario,
        planned_file=PlannedFile(
            relative_path=relative_path,
            content=content,
            metadata={
                "scenario_id": scenario.scenario_id,
                "scenario_version": scenario.scenario_version,
                "source_fingerprint": scenario.source_fingerprint,
            },
        ),
        test_name=test_name,
    )


def render_spec(scenario: ScenarioContract, test_name: str) -> str:
    """Render one Playwright spec skeleton."""

    bindings = locator_bindings(scenario)
    binding_index = 0
    class_name = page_object_class_name(scenario)
    page_object_file = page_object_import_path(scenario)
    lines = [
        f"// {GENERATED_MARKER}",
        f"// scenario_id: {scenario.scenario_id}",
        f"// scenario_version: {scenario.scenario_version}",
        "import { expect, test } from '@playwright/test';",
    ]
    if bindings:
        lines.append(f"import {{ {class_name} }} from {ts_string(page_object_file)};")
    lines.extend(
        [
            "",
            f"test({ts_string(test_name)}, async ({{ page }}) => {{",
        ]
    )
    if bindings:
        lines.append(f"  const scenarioPage = new {class_name}(page);")
    lines.extend(
        [
            "  test.info().annotations.push(",
            f"    {{ type: 'scenario_id', description: {ts_string(scenario.scenario_id)} }},",
            "  );",
            f"  // locator_stability: {scenario_locator_stability(scenario) or 'NONE'}",
        ]
    )
    for precondition in scenario.preconditions:
        lines.append(f"  // precondition: {ts_comment(precondition)}")
    for action in scenario.actions:
        binding = None
        if action_needs_locator(action):
            binding = bindings[binding_index]
            binding_index += 1
        lines.extend(render_action(action, binding))
    for assertion in scenario.assertions:
        binding = None
        if assertion_needs_locator(assertion):
            binding = bindings[binding_index]
            binding_index += 1
        lines.extend(render_assertion(assertion, binding))
    lines.extend(["});", ""])
    return "\n".join(lines)


def render_action(action: ScenarioAction, binding: LocatorBinding | None) -> list[str]:
    """Render a supported structured action."""

    lines = [f"  // action: {ts_comment(action.description)}"]
    if action.kind == ScenarioActionKind.NAVIGATE:
        lines.append(f"  await page.goto({ts_string(action.target)});")
    elif action.kind == ScenarioActionKind.CLICK and binding is not None:
        lines.append(f"  await scenarioPage.{binding.method_name}().click();")
    elif action.kind == ScenarioActionKind.FILL and binding is not None:
        lines.append(
            f"  await scenarioPage.{binding.method_name}().fill({ts_string(action.value)});"
        )
    elif action.kind == ScenarioActionKind.SELECT and binding is not None:
        lines.append(
            f"  await scenarioPage.{binding.method_name}().selectOption({ts_string(action.value)});"
        )
    elif action.kind == ScenarioActionKind.SUBMIT and binding is not None:
        lines.append(f"  await scenarioPage.{binding.method_name}().click();")
    elif action.kind == ScenarioActionKind.WAIT:
        lines.append("  await page.waitForLoadState('domcontentloaded');")
    return lines


def render_assertion(assertion: ScenarioAssertion, binding: LocatorBinding | None) -> list[str]:
    """Render a supported structured assertion."""

    lines = [f"  // assertion: {ts_comment(assertion.description)}"]
    if assertion.kind == ScenarioAssertionKind.VISIBLE and binding is not None:
        lines.append(f"  await expect(scenarioPage.{binding.method_name}()).toBeVisible();")
    elif assertion.kind == ScenarioAssertionKind.TEXT and binding is not None:
        lines.append(
            f"  await expect(scenarioPage.{binding.method_name}()).toContainText({ts_string(assertion.expected)});"
        )
    elif assertion.kind == ScenarioAssertionKind.URL:
        expected = assertion.expected or assertion.target
        lines.append(
            f"  await expect(page).toHaveURL(new RegExp({ts_string(regex_escape(expected))}));"
        )
    return lines


def build_page_object(
    root: Path,
    project_dir: Path,
    scenario: ScenarioContract,
) -> GeneratedPageObject | None:
    """Build a generated page object file when Scenario has locators."""

    bindings = locator_bindings(scenario)
    if not bindings:
        return None
    relative_path = page_object_relative_path(root, project_dir, scenario)
    content = render_page_object(scenario, bindings).encode("utf-8")
    if secret_findings(content.decode("utf-8", errors="replace")):
        raise PlaywrightGeneratorError(
            "PLAYWRIGHT_GENERATOR_CONTENT_UNSAFE",
            relative_path,
            "SECRET_DETECTED",
        )
    validate_static_typescript(content.decode("utf-8", errors="replace"), relative_path)
    return GeneratedPageObject(
        scenario=scenario,
        planned_file=PlannedFile(
            relative_path=relative_path,
            content=content,
            metadata={
                "scenario_id": scenario.scenario_id,
                "scenario_version": scenario.scenario_version,
                "source_fingerprint": scenario.source_fingerprint,
                "artifact": "page_object",
            },
        ),
    )


def render_page_object(
    scenario: ScenarioContract,
    bindings: Sequence[LocatorBinding],
) -> str:
    """Render a minimal page object with locator confidence comments."""

    lines = [
        f"// {GENERATED_MARKER}",
        f"// scenario_id: {scenario.scenario_id}",
        f"// scenario_version: {scenario.scenario_version}",
        "import type { Locator, Page } from '@playwright/test';",
        "",
        f"export class {page_object_class_name(scenario)} {{",
        "  constructor(private readonly page: Page) {}",
    ]
    for binding in bindings:
        expression = locator_expression(binding.choice.candidate, page_var="this.page")
        lines.extend(
            [
                "",
                f"  // locator_stability: {binding.choice.locator_stability}",
                f"  // locator_kind: {binding.choice.candidate.kind.value}",
                f"  {binding.method_name}(): Locator {{",
                f"    return {expression};",
                "  }",
            ]
        )
    lines.extend(["}", ""])
    return "\n".join(lines)


def locator_bindings(scenario: ScenarioContract) -> tuple[LocatorBinding, ...]:
    """Return selected locator bindings in execution order."""

    bindings: list[LocatorBinding] = []
    for action in scenario.actions:
        if action_needs_locator(action):
            bindings.append(
                LocatorBinding(
                    method_name=f"locator{len(bindings) + 1}",
                    choice=required_locator_choice(action.target, "structured_actions.target"),
                )
            )
    for assertion in scenario.assertions:
        if assertion_needs_locator(assertion):
            bindings.append(
                LocatorBinding(
                    method_name=f"locator{len(bindings) + 1}",
                    choice=required_locator_choice(
                        assertion.target,
                        "structured_assertions.target",
                    ),
                )
            )
    return tuple(bindings)


def scenario_locator_stability(scenario: ScenarioContract) -> str:
    """Return aggregate locator stability for generated status integration."""

    choices = [binding.choice for binding in locator_bindings_if_valid(scenario)]
    if not choices:
        return ""
    levels = {choice.locator_stability for choice in choices}
    if "LOW" in levels:
        return "LOW"
    if "MEDIUM" in levels:
        return "MEDIUM"
    return "HIGH"


def locator_bindings_if_valid(scenario: ScenarioContract) -> tuple[LocatorBinding, ...]:
    """Return locator bindings, or empty tuple when locator target is invalid."""

    try:
        return locator_bindings(scenario)
    except PlaywrightGeneratorError:
        return ()


def required_locator_choice(value: str, path: str) -> LocatorChoice:
    """Return a locator choice or raise an internal-safe generator error."""

    choice, reason = locator_choice_for_value(value, path=path)
    if reason:
        raise PlaywrightGeneratorError("PLAYWRIGHT_GENERATOR_LOCATOR_INVALID", path, reason)
    if choice is None:
        raise PlaywrightGeneratorError(
            "PLAYWRIGHT_GENERATOR_LOCATOR_INVALID",
            path,
            "LOCATOR_SOURCE_MISSING",
        )
    return choice


def locator_choice_for_value(value: str, *, path: str) -> tuple[LocatorChoice | None, str]:
    """Return a locator choice and a safe blocking reason."""

    try:
        candidates = parse_locator_candidates(value, path=path)
    except LocatorModelError:
        return None, "LOCATOR_SOURCE_INVALID"
    choice = choose_locator(candidates)
    return choice, ""


def action_needs_locator(action: ScenarioAction) -> bool:
    """Return whether an action needs a selected locator."""

    return action.kind in {
        ScenarioActionKind.CLICK,
        ScenarioActionKind.FILL,
        ScenarioActionKind.SELECT,
        ScenarioActionKind.SUBMIT,
    }


def assertion_needs_locator(assertion: ScenarioAssertion) -> bool:
    """Return whether an assertion needs a selected locator."""

    return assertion.kind in {ScenarioAssertionKind.VISIBLE, ScenarioAssertionKind.TEXT}


def locator_expression(candidate: LocatorCandidate, *, page_var: str) -> str:
    """Return a Playwright locator expression for a selected candidate."""

    if candidate.kind == LocatorKind.ROLE:
        return (
            f"{page_var}.getByRole({ts_string(candidate.role)}, "
            f"{{ name: {ts_string(candidate.name or candidate.value)} }})"
        )
    if candidate.kind == LocatorKind.LABEL:
        return f"{page_var}.getByLabel({ts_string(candidate.value)})"
    if candidate.kind == LocatorKind.PLACEHOLDER:
        return f"{page_var}.getByPlaceholder({ts_string(candidate.value)})"
    if candidate.kind == LocatorKind.TEXT:
        return f"{page_var}.getByText({ts_string(candidate.value)}, {{ exact: true }})"
    if candidate.kind == LocatorKind.TEST_ID:
        return f"{page_var}.getByTestId({ts_string(candidate.value)})"
    if candidate.kind == LocatorKind.ID:
        return f"{page_var}.locator({ts_string(css_attr_selector('id', candidate.value))})"
    if candidate.kind == LocatorKind.NAME:
        return f"{page_var}.locator({ts_string(css_attr_selector('name', candidate.value))})"
    if candidate.kind in {LocatorKind.CSS, LocatorKind.XPATH}:
        return f"{page_var}.locator({ts_string(candidate.value)})"
    raise PlaywrightGeneratorError(
        "PLAYWRIGHT_GENERATOR_LOCATOR_INVALID",
        "locator.kind",
        "UNKNOWN",
    )


def css_attr_selector(attribute: str, value: str) -> str:
    """Return a simple quoted CSS attribute selector."""

    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'[{attribute}="{escaped}"]'


def page_object_class_name(scenario: ScenarioContract) -> str:
    """Return deterministic page object class name."""

    stem = scenario.scenario_id.replace("-", "").title()
    return f"Scenario{stem}V{scenario.scenario_version}Page"


def page_object_import_path(scenario: ScenarioContract) -> str:
    """Return spec-to-page object import path."""

    return f"../pages/{scenario.scenario_id.lower()}-v{scenario.scenario_version}.page"


def page_object_relative_path(
    root: Path,
    project_dir: Path,
    scenario: ScenarioContract,
) -> str:
    """Return page object path relative to repo root."""

    file_name = f"{scenario.scenario_id.lower()}-v{scenario.scenario_version}.page.ts"
    return relative_file(root, project_dir / "pages" / file_name)


def validate_static_typescript(content: str, relative_path: str) -> None:
    """Run deterministic static checks without invoking TypeScript or Playwright."""

    forbidden = (
        "storageState",
        ".auth",
        "Cookie" + ":",
        "Authorization" + ":",
        "test.only",
        "describe.only",
        "page.pause",
        "child_process",
        "exec(",
        "spawn(",
        "process.env",
    )
    for value in forbidden:
        if value in content:
            raise PlaywrightGeneratorError(
                "PLAYWRIGHT_GENERATOR_STATIC_INVALID",
                relative_path,
                "FORBIDDEN_TOKEN",
            )
    if GENERATED_MARKER not in content:
        raise PlaywrightGeneratorError(
            "PLAYWRIGHT_GENERATOR_STATIC_INVALID",
            relative_path,
            "GENERATED_MARKER_MISSING",
        )
    if content.count("{") != content.count("}"):
        raise PlaywrightGeneratorError(
            "PLAYWRIGHT_GENERATOR_STATIC_INVALID",
            relative_path,
            "BRACE_MISMATCH",
        )


def generation_status_manifest(
    root: Path,
    project_dir: Path,
    decisions: Sequence[ScenarioGenerationDecision],
) -> dict[str, Any]:
    """Build a safe local status plan for generated/blocked Scenarios."""

    payload = {
        "schema_version": 1,
        "generator": "webapp-debug",
        "project_dir": safe_relative_path(root, project_dir),
        "scenarios": [decision.to_dict() for decision in decisions],
    }
    if secret_findings(payload):
        raise PlaywrightGeneratorError(
            "PLAYWRIGHT_GENERATOR_STATUS_UNSAFE",
            GENERATION_MANIFEST_NAME,
            "SECRET_DETECTED",
        )
    return payload


def generation_manifest_file(
    root: Path,
    project_dir: Path,
    payload: Mapping[str, Any],
) -> PlannedFile:
    """Return the generation status manifest as a planned file."""

    return PlannedFile(
        relative_path=relative_file(root, project_dir / "generated" / GENERATION_MANIFEST_NAME),
        content=dumps_snapshot(payload),
        marker_required=False,
    )


def project_manifest_file(
    root: Path,
    project_dir: Path,
    payload: Mapping[str, Any],
) -> PlannedFile:
    """Return the project generated-file manifest as a planned file."""

    return PlannedFile(
        relative_path=relative_file(root, project_dir / MANIFEST_NAME),
        content=dumps_snapshot(payload),
        marker_required=False,
    )


def merged_project_manifest(
    existing_manifest: Mapping[str, Any],
    generated_files: Sequence[PlannedFile],
) -> dict[str, Any]:
    """Merge generated spec entries into the Phase 8A manifest."""

    existing_files = existing_manifest.get("files")
    if not isinstance(existing_files, list):
        raise PlaywrightGeneratorError(
            "PLAYWRIGHT_GENERATOR_BOOTSTRAP_REQUIRED",
            MANIFEST_NAME,
            "FILES_INVALID",
        )
    entries: dict[str, dict[str, Any]] = {}
    for item in existing_files:
        if not isinstance(item, Mapping) or not isinstance(item.get("path"), str):
            raise PlaywrightGeneratorError(
                "PLAYWRIGHT_GENERATOR_BOOTSTRAP_REQUIRED",
                MANIFEST_NAME,
                "ENTRY_INVALID",
            )
        entries[str(item["path"])] = dict(item)
    for planned in generated_files:
        entries[planned.relative_path] = {
            "path": planned.relative_path,
            "sha256": planned.checksum,
            "size": planned.size,
            "generated_marker_required": planned.marker_required,
        } | ({"metadata": dict(planned.metadata)} if planned.metadata else {})
    manifest = dict(existing_manifest)
    manifest["files"] = [entries[path] for path in sorted(entries)]
    return manifest


def validate_generated_files(
    root: Path,
    project_dir: Path,
    files: Sequence[PlannedFile],
    *,
    existing_manifest: Mapping[str, Any],
) -> tuple[GenerationFileDecision, ...]:
    """Validate generated files and ownership before any write."""

    decisions: list[GenerationFileDecision] = []
    manifest_path = project_dir / MANIFEST_NAME
    for planned in files:
        if secret_findings(planned.content.decode("utf-8", errors="replace")):
            raise PlaywrightGeneratorError(
                "PLAYWRIGHT_GENERATOR_CONTENT_UNSAFE",
                planned.relative_path,
                "SECRET_DETECTED",
            )
        target = root / planned.relative_path
        try:
            ensure_within_root(root, target, planned.relative_path)
            ensure_no_symlink_path(root, target)
            action = validate_target_file(
                target,
                planned,
                manifest=existing_manifest,
                manifest_path=manifest_path,
            )
            validate_current_manifest_entry(target, planned, existing_manifest)
        except Exception as exc:
            if isinstance(exc, PlaywrightGeneratorError):
                raise
            raise PlaywrightGeneratorError(
                "PLAYWRIGHT_GENERATOR_FILE_CONFLICT",
                planned.relative_path,
                "OWNERSHIP_CHECK_FAILED",
            ) from None
        decisions.append(
            GenerationFileDecision(
                path=planned.relative_path,
                action=action,
                checksum=planned.checksum,
                size=planned.size,
            )
        )
    return tuple(decisions)


def validate_current_manifest_entry(
    target: Path,
    planned: PlannedFile,
    existing_manifest: Mapping[str, Any],
) -> None:
    """Reject changed generated files before update."""

    if not target.exists() or read_bytes(target, planned.relative_path) == planned.content:
        return
    entry = manifest_entry(existing_manifest, planned.relative_path)
    if entry is None:
        return
    current = read_bytes(target, planned.relative_path)
    if entry.get("sha256") != sha256_bytes(current):
        raise PlaywrightGeneratorError(
            "PLAYWRIGHT_GENERATOR_FILE_CONFLICT",
            planned.relative_path,
            "CHECKSUM_MISMATCH",
        )


def write_generation_plan(plan: PlaywrightGenerationPlan) -> None:
    """Atomically write all generated files."""

    for planned in plan.files:
        try:
            atomic_write(plan.root / planned.relative_path, planned.content)
        except Exception:
            raise PlaywrightGeneratorError(
                "PLAYWRIGHT_GENERATOR_WRITE_FAILED",
                planned.relative_path,
                "WRITE_FAILED",
                exit_code=EXIT_EXTERNAL_FAILURE,
            ) from None


def scenario_spec_relative_path(
    root: Path,
    project_dir: Path,
    scenario: ScenarioContract,
) -> str:
    """Return a deterministic generated spec relative path."""

    file_name = f"{scenario.scenario_id.lower()}-v{scenario.scenario_version}.spec.ts"
    return relative_file(root, project_dir / "generated" / file_name)


def scenario_test_name(scenario: ScenarioContract) -> str:
    """Return deterministic Playwright test name."""

    return f"[{scenario.scenario_id} v{scenario.scenario_version}] {scenario.scenario_title}"


def safe_relative_url(value: str) -> bool:
    """Return whether a target is a safe relative app URL."""

    if not isinstance(value, str):
        return False
    if secret_findings(value):
        return False
    stripped = value.strip()
    return (
        stripped.startswith("/")
        and not stripped.startswith("//")
        and "\n" not in stripped
        and "\r" not in stripped
        and not re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", stripped)
    )


def ts_string(value: str) -> str:
    """Return a JavaScript string literal."""

    return json.dumps(value, ensure_ascii=False)


def ts_comment(value: str) -> str:
    """Return safe one-line comment text."""

    return value.replace("\r", " ").replace("\n", " ").replace("*/", "* /").strip()


def regex_escape(value: str) -> str:
    """Escape a literal string for a JavaScript RegExp constructor."""

    return re.escape(value)
