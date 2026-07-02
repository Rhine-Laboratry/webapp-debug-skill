"""Read-only CakePHP static discovery."""

from __future__ import annotations

import json
import re
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from webapp_debug_skill.inventory_model import (
    DiscoveryGap,
    InventoryCandidate,
    InventorySnapshotBuilder,
    rfc3339_utc,
)
from webapp_debug_skill.php_static import (
    PhpMethod,
    contains_any,
    extract_php_classes,
    safe_read_text,
    string_literals,
)

EXCLUDED_DIRS = {
    ".git",
    ".webapp-debug",
    "vendor",
    "node_modules",
    "tmp",
    "logs",
    "log",
    "cache",
    "coverage",
    "build",
    "dist",
}
LIFECYCLE_METHODS = {
    "initialize",
    "beforeFilter",
    "beforeRender",
    "afterFilter",
    "afterRender",
    "beforeRedirect",
}
HTTP_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"}
ROUTE_CONNECT_RE = re.compile(
    r"->connect\s*\(\s*(?P<path>['\"][^'\"]+['\"]|[^,]+)\s*,?\s*(?P<target>\[[^\)]*\])?",
    re.DOTALL,
)
TARGET_PAIR_RE = re.compile(
    r"['\"](?P<key>controller|action|prefix|plugin)['\"]\s*=>\s*['\"](?P<value>[^'\"]+)['\"]"
)
CONTEXT_RE = re.compile(r"->(?P<kind>scope|prefix|plugin)\s*\(\s*['\"](?P<value>[^'\"]+)['\"]")


@dataclass(frozen=True)
class CakePHPVersion:
    """Detected CakePHP version mode."""

    mode: str
    source: str
    warning: str | None = None


@dataclass(frozen=True)
class RouteCandidate:
    """A discovered route candidate."""

    path: str
    controller: str
    action: str
    plugin: str
    prefix: str
    source_path: str
    line: int
    dynamic: bool = False


@dataclass(frozen=True)
class ControllerAction:
    """A discovered controller action."""

    controller: str
    action: str
    plugin: str
    prefix: str
    source_path: str
    line: int
    http_methods: tuple[str, ...]
    hints: tuple[str, ...]


@dataclass(frozen=True)
class TemplateHint:
    """Template hints for one controller/action."""

    controller: str
    action: str
    plugin: str
    prefix: str
    source_path: str
    line: int
    hints: tuple[str, ...]


@dataclass(frozen=True)
class DiscoveryWarning:
    """Safe discovery warning."""

    path: str
    reason: str


@dataclass
class CakePHPDiscoveryResult:
    """CakePHP discovery result."""

    root: Path
    version: CakePHPVersion
    files_scanned: int
    controllers: list[ControllerAction] = field(default_factory=list)
    routes: list[RouteCandidate] = field(default_factory=list)
    templates: list[TemplateHint] = field(default_factory=list)
    gaps: list[DiscoveryGap] = field(default_factory=list)
    warnings: list[DiscoveryWarning] = field(default_factory=list)
    source_paths: tuple[str, ...] = ()

    def to_payload(
        self, *, clock: Callable[[], datetime] = lambda: datetime.now(UTC)
    ) -> dict[str, Any]:
        """Build Inventory snapshot payload."""

        generated_at = rfc3339_utc(clock())
        source = {
            "kind": "cakephp_static_discovery",
            "root": ".",
            "cakephp_version": self.version.mode,
            "generated_at": generated_at,
        }
        builder = InventorySnapshotBuilder(generated_at=generated_at, source=source)
        for candidate in build_inventory_candidates(self):
            builder.add_candidate(candidate)
        for gap in self.gaps:
            builder.add_gap(gap)
        summary = {
            "controllers_scanned": len({item.source_path for item in self.controllers}),
            "routes_scanned": len(self.routes),
            "templates_scanned": len(self.templates),
            "files_scanned": self.files_scanned,
            "warnings": [warning.__dict__ for warning in self.warnings],
        }
        return builder.payload(summary=summary)


def discover_cakephp(
    root: Path,
    *,
    include_plugins: bool = False,
    cakephp_version: str = "auto",
    max_files: int = 5000,
) -> CakePHPDiscoveryResult:
    """Discover CakePHP Inventory candidates without executing project code."""

    version = detect_cakephp_version(root, override=cakephp_version)
    files = collect_source_files(root, include_plugins=include_plugins)
    if len(files) > max_files:
        raise CakePHPDiscoveryError("DISCOVERY_TOO_MANY_FILES", "root", "TOO_MANY_FILES")
    if version.mode == "generic" and not looks_like_cakephp(root):
        raise CakePHPDiscoveryError(
            "DISCOVERY_NO_CAKEPHP_APP",
            "root",
            "UNSUPPORTED_STRUCTURE",
        )
    result = CakePHPDiscoveryResult(
        root=root,
        version=version,
        files_scanned=len(files),
        source_paths=tuple(safe_relative(root, path) for path in files),
    )
    if version.warning:
        result.warnings.append(DiscoveryWarning("composer.json", version.warning))
    for file_path in files:
        relative = safe_relative(root, file_path)
        read_result = safe_read_text(file_path)
        if read_result.warning:
            result.warnings.append(DiscoveryWarning(relative, read_result.warning))
        if read_result.text is None:
            result.gaps.append(
                DiscoveryGap(
                    "DISCOVERY_FILE_READ_FAILED", relative, 1, read_result.warning or "READ_FAILED"
                )
            )
            continue
        text = read_result.text
        if is_route_file(relative):
            routes, gaps = parse_routes_file(relative, text)
            result.routes.extend(routes)
            result.gaps.extend(gaps)
        if is_controller_file(relative):
            actions, gaps = parse_controller_file(relative, text)
            result.controllers.extend(actions)
            result.gaps.extend(gaps)
        if is_template_file(relative):
            result.templates.append(parse_template_file(relative, text))
        if is_table_file(relative) and "validationDefault" in text:
            result.warnings.append(DiscoveryWarning(relative, "MODEL_VALIDATION_HINT"))
    if not result.controllers and not result.routes:
        result.gaps.append(
            DiscoveryGap(
                "DISCOVERY_UNSUPPORTED_STRUCTURE", ".", 1, "No routes or controllers found"
            )
        )
    return result


class CakePHPDiscoveryError(RuntimeError):
    """Safe discovery error."""

    def __init__(self, code: str, path: str = "discovery", reason: str = "FAILED") -> None:
        super().__init__(code)
        self.code = code
        self.path = path
        self.reason = reason


def detect_cakephp_version(root: Path, *, override: str = "auto") -> CakePHPVersion:
    """Detect CakePHP major version from composer and structure."""

    if override != "auto":
        return CakePHPVersion(override, "override")
    composer = root / "composer.json"
    if composer.is_file():
        try:
            data = json.loads(composer.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return CakePHPVersion("generic", "composer.json", "COMPOSER_JSON_INVALID")
        require = data.get("require", {})
        require_dev = data.get("require-dev", {})
        constraint = ""
        if isinstance(require, dict):
            constraint = str(require.get("cakephp/cakephp", ""))
        if constraint == "" and isinstance(require_dev, dict):
            constraint = str(require_dev.get("cakephp/cakephp", ""))
        major = cakephp_major_from_constraint(constraint)
        if major:
            return CakePHPVersion(major, "composer.json")
    if (root / "app/Config/routes.php").is_file() or (root / "app/Controller").is_dir():
        return CakePHPVersion("2", "app")
    if (root / "src/Application.php").is_file() or (root / "config/routes.php").is_file():
        return CakePHPVersion("generic", "structure")
    return CakePHPVersion("generic", "unknown")


def cakephp_major_from_constraint(constraint: str) -> str | None:
    """Extract supported CakePHP major from a composer constraint."""

    match = re.search(r"(?:\^|~|>=|<=|>|<)?\s*(?P<major>[2-5])(?:\.|\b)", constraint)
    if match:
        return match.group("major")
    return None


def looks_like_cakephp(root: Path) -> bool:
    """Return whether the root resembles a CakePHP app."""

    return any(
        path.exists()
        for path in (
            root / "composer.json",
            root / "config/routes.php",
            root / "src/Controller",
            root / "app/Config/routes.php",
            root / "app/Controller",
        )
    )


def collect_source_files(root: Path, *, include_plugins: bool) -> list[Path]:
    """Collect source files under known CakePHP surfaces."""

    candidates: list[Path] = []
    patterns = [
        "composer.json",
        "config/routes.php",
        "src/Application.php",
        "src/Controller/**/*.php",
        "templates/**/*.php",
        "src/Template/**/*.ctp",
        "src/Model/Table/**/*.php",
        "src/Policy/**/*.php",
        "src/Middleware/**/*.php",
        "app/Config/routes.php",
        "app/Controller/**/*.php",
        "app/View/**/*.ctp",
    ]
    if include_plugins:
        patterns.extend(
            [
                "plugins/*/config/routes.php",
                "plugins/*/src/Controller/**/*.php",
                "plugins/*/templates/**/*.php",
                "plugins/*/src/Template/**/*.ctp",
            ]
        )
    for pattern in patterns:
        for path in root.glob(pattern):
            if is_safe_source_file(root, path) and not is_excluded_path(root, path):
                candidates.append(path)
    return sorted(set(candidates))


def is_safe_source_file(root: Path, path: Path) -> bool:
    """Return whether a candidate is a regular in-root, non-symlink file."""

    try:
        if path.is_symlink() or not path.is_file():
            return False
        path.resolve(strict=True).relative_to(root.resolve(strict=True))
    except (OSError, ValueError):
        return False
    return True


def is_excluded_path(root: Path, path: Path) -> bool:
    """Return whether path is in an excluded directory or secret config."""

    relative_parts = path.relative_to(root).parts
    lowered = {part.lower() for part in relative_parts}
    if lowered & EXCLUDED_DIRS:
        return True
    name = path.name.lower()
    return name in {"local.php", ".env", ".env.local", "app_local.php", "database.php"}


def parse_routes_file(relative: str, text: str) -> tuple[list[RouteCandidate], list[DiscoveryGap]]:
    """Parse route declarations conservatively."""

    routes: list[RouteCandidate] = []
    gaps: list[DiscoveryGap] = []
    context = route_contexts(text)
    for match in ROUTE_CONNECT_RE.finditer(text):
        line = text.count("\n", 0, match.start()) + 1
        raw_path = match.group("path").strip()
        current = context_for_line(context, line)
        if not quoted(raw_path):
            gaps.append(
                DiscoveryGap("DISCOVERY_DYNAMIC_ROUTE", relative, line, "Dynamic route path")
            )
            continue
        path = strip_quotes(raw_path)
        target = parse_target(match.group("target") or "")
        controller = target.get("controller", "")
        action = target.get("action", "index")
        plugin = target.get("plugin", current.get("plugin", ""))
        prefix = target.get("prefix", current.get("prefix", ""))
        scoped_path = combine_paths(current.get("scope", ""), path)
        if controller == "":
            gaps.append(
                DiscoveryGap(
                    "DISCOVERY_ROUTE_PARSE_GAP", relative, line, "Route target missing controller"
                )
            )
        routes.append(
            RouteCandidate(
                path=scoped_path,
                controller=controller,
                action=action,
                plugin=plugin,
                prefix=prefix,
                source_path=relative,
                line=line,
            )
        )
    for index, line_text in enumerate(text.splitlines(), start=1):
        if "fallbacks" in line_text:
            gaps.append(DiscoveryGap("DISCOVERY_DYNAMIC_ROUTE", relative, index, "Fallback route"))
    return routes, gaps


def route_contexts(text: str) -> list[tuple[int, dict[str, str]]]:
    """Collect simple line-based route contexts."""

    contexts: list[tuple[int, dict[str, str]]] = []
    active: dict[str, str] = {}
    for line_number, line in enumerate(text.splitlines(), start=1):
        for match in CONTEXT_RE.finditer(line):
            kind = match.group("kind")
            value = match.group("value")
            if kind == "scope":
                active["scope"] = value
            else:
                active[kind] = value
        if active:
            contexts.append((line_number, dict(active)))
        if "});" in line or "});" in line.replace(" ", ""):
            active = {}
    return contexts


def context_for_line(contexts: list[tuple[int, dict[str, str]]], line: int) -> dict[str, str]:
    """Return latest context for a route line."""

    selected: dict[str, str] = {}
    for context_line, context in contexts:
        if context_line <= line:
            selected = context
        else:
            break
    return selected


def parse_controller_file(
    relative: str, text: str
) -> tuple[list[ControllerAction], list[DiscoveryGap]]:
    """Parse CakePHP controller action candidates."""

    actions: list[ControllerAction] = []
    gaps: list[DiscoveryGap] = []
    classes = extract_php_classes(text)
    if not classes:
        gaps.append(
            DiscoveryGap(
                "DISCOVERY_CONTROLLER_PARSE_GAP", relative, 1, "Controller class not found"
            )
        )
        return actions, gaps
    plugin, prefix = plugin_prefix_from_path(relative)
    for php_class in classes:
        if not php_class.name.endswith("Controller"):
            continue
        controller = php_class.name[: -len("Controller")]
        for method in php_class.methods:
            if is_action_method(method):
                methods = extract_http_methods(method)
                actions.append(
                    ControllerAction(
                        controller=controller,
                        action=method.name,
                        plugin=plugin,
                        prefix=prefix,
                        source_path=relative,
                        line=method.line,
                        http_methods=methods,
                        hints=extract_action_hints(method.body),
                    )
                )
    return actions, gaps


def is_action_method(method: PhpMethod) -> bool:
    """Return whether method is a public action candidate."""

    return (
        method.visibility == "public"
        and not method.name.startswith("_")
        and method.name not in LIFECYCLE_METHODS
    )


def extract_http_methods(method: PhpMethod) -> tuple[str, ...]:
    """Extract HTTP method constraints from an action body."""

    found: set[str] = set()
    body = method.body
    if "allowMethod" in body:
        for literal in string_literals(body):
            normalized = literal.upper()
            if normalized in HTTP_METHODS:
                found.add(normalized)
    for match in re.finditer(r"->is\s*\(\s*['\"]([^'\"]+)['\"]", body):
        normalized = match.group(1).upper()
        if normalized in HTTP_METHODS:
            found.add(normalized)
    return tuple(sorted(found)) if found else ("GET",)


def extract_action_hints(body: str) -> tuple[str, ...]:
    """Extract safe bounded action hints."""

    hints: list[str] = []
    for needle, hint in (
        ("redirect(", "redirect"),
        ("render(", "explicit_render"),
        ("set(", "sets_view_vars"),
        ("paginate(", "pagination"),
        ("Flash->", "flash_message"),
        ("Mailer", "mailer"),
    ):
        if needle in body:
            hints.append(hint)
    return tuple(hints)


def parse_template_file(relative: str, text: str) -> TemplateHint:
    """Parse template path and feature hints."""

    plugin, prefix, controller, action = template_identity(relative)
    hints: list[str] = []
    if contains_any(text, ["Form->create", "Form->control", "Form->input"]):
        hints.append("form")
    if contains_any(text, ["Html->link", "Url->build"]):
        hints.append("link")
    if "postLink" in text:
        hints.append("post_link")
    if contains_any(text, ["type' => 'file", 'type" => "file', 'enctype="multipart']):
        hints.append("file_upload")
    if contains_any(text, ["download", "csv", "pdf"]):
        hints.append("download")
    return TemplateHint(
        controller=controller,
        action=action,
        plugin=plugin,
        prefix=prefix,
        source_path=relative,
        line=1,
        hints=tuple(dict.fromkeys(hints)),
    )


def build_inventory_candidates(result: CakePHPDiscoveryResult) -> list[InventoryCandidate]:
    """Merge route, controller and template discoveries into Inventory candidates."""

    routes_by_key: dict[tuple[str, str, str, str], RouteCandidate] = {}
    for route in result.routes:
        if route.controller:
            routes_by_key[(route.plugin, route.prefix, route.controller, route.action)] = route
    templates_by_key: dict[tuple[str, str, str, str], list[TemplateHint]] = defaultdict(list)
    for template in result.templates:
        templates_by_key[
            (template.plugin, template.prefix, template.controller, template.action)
        ].append(template)
    candidates: list[InventoryCandidate] = []
    seen_keys: set[tuple[str, str, str, str]] = set()
    for action in result.controllers:
        key = (action.plugin, action.prefix, action.controller, action.action)
        seen_keys.add(key)
        route = routes_by_key.get(key)
        templates = templates_by_key.get(key, [])
        hints = [*action.hints]
        for template in templates:
            hints.extend(template.hints)
        route_or_url = route.path if route else fallback_route(action)
        candidates.append(
            InventoryCandidate(
                source_key="|".join(("controller", *key)),
                feature_area=feature_area(action.controller, action.prefix, action.plugin),
                name=f"{qualified_controller(action)}::{action.action}",
                item_type=item_type_from_hints(hints, action.http_methods),
                actor=actor_from_prefix(action.prefix, hints),
                route_or_url=route_or_url,
                source_path=action.source_path,
                source_symbol=f"{qualified_controller(action)}::{action.action}",
                source_line=action.line,
                discovery_source="cakephp_controller",
                confidence="HIGH" if route or templates else "MEDIUM",
                risk=risk_from_action(action, hints),
                http_methods=action.http_methods,
                notes=route_notes(route),
                hints=tuple(dict.fromkeys(hints)),
            )
        )
    for route in result.routes:
        key = (route.plugin, route.prefix, route.controller, route.action)
        if key in seen_keys or route.controller == "":
            continue
        candidates.append(
            InventoryCandidate(
                source_key="|".join(("route", *key, route.path)),
                feature_area=feature_area(route.controller, route.prefix, route.plugin),
                name=f"{qualified_name(route.plugin, route.prefix, route.controller)}::{route.action}",
                item_type="UI_PAGE",
                actor=actor_from_prefix(route.prefix, ()),
                route_or_url=route.path,
                source_path=route.source_path,
                source_symbol=f"{route.controller}::{route.action}",
                source_line=route.line,
                discovery_source="cakephp_route",
                confidence="MEDIUM",
                risk="MEDIUM",
                http_methods=("GET",),
                notes=("route without local controller action",),
            )
        )
    return candidates


def route_notes(route: RouteCandidate | None) -> tuple[str, ...]:
    """Return safe route notes."""

    if route is None:
        return ("route inferred from controller fallback",)
    return ("explicit route",)


def item_type_from_hints(hints: Sequence[str], methods: Sequence[str]) -> str:
    """Infer Inventory item type."""

    if "file_upload" in hints:
        return "UPLOAD"
    if "download" in hints:
        return "DOWNLOAD"
    if any(method in {"POST", "PUT", "PATCH", "DELETE"} for method in methods):
        return "UI_ACTION"
    return "UI_PAGE"


def risk_from_action(action: ControllerAction, hints: Sequence[str]) -> str:
    """Infer simple risk level."""

    lowered = f"{action.controller} {action.action} {' '.join(hints)}".lower()
    if "admin" in action.prefix.lower() or action.action.lower() in {"delete", "remove"}:
        return "HIGH"
    if any(word in lowered for word in ("upload", "download", "csv", "pdf", "mailer")):
        return "HIGH"
    if any(word in lowered for word in ("add", "edit", "save", "update")):
        return "MEDIUM"
    return "LOW"


def actor_from_prefix(prefix: str, hints: Sequence[str]) -> str:
    """Infer actor with conservative confidence."""

    if prefix.lower() == "admin":
        return "admin"
    if "allow_unauthenticated" in hints:
        return "unauthenticated"
    return "user"


def feature_area(controller: str, prefix: str, plugin: str) -> str:
    """Return a compact feature area."""

    parts = [part for part in (plugin, prefix, controller) if part]
    return " / ".join(parts) if parts else "Application"


def qualified_controller(action: ControllerAction) -> str:
    """Return plugin/prefix qualified controller name."""

    return qualified_name(action.plugin, action.prefix, action.controller)


def qualified_name(plugin: str, prefix: str, controller: str) -> str:
    """Return a qualified controller display name."""

    parts = [part for part in (plugin, prefix, controller) if part]
    return ".".join(parts) if parts else controller


def fallback_route(action: ControllerAction) -> str:
    """Infer fallback route path from controller/action."""

    segments = []
    if action.prefix:
        segments.append(dasherize(action.prefix))
    if action.plugin:
        segments.append(dasherize(action.plugin))
    segments.append(dasherize(action.controller))
    if action.action != "index":
        segments.append(dasherize(action.action))
    return "/" + "/".join(segment for segment in segments if segment)


def plugin_prefix_from_path(relative: str) -> tuple[str, str]:
    """Infer plugin and prefix from a controller path."""

    parts = Path(relative).parts
    plugin = ""
    prefix = ""
    if len(parts) >= 2 and parts[0] == "plugins":
        plugin = parts[1]
    if "Controller" in parts:
        controller_index = parts.index("Controller")
        prefix_parts = parts[controller_index + 1 : -1]
        if prefix_parts:
            prefix = "/".join(prefix_parts)
    return plugin, prefix


def template_identity(relative: str) -> tuple[str, str, str, str]:
    """Infer plugin, prefix, controller and action from template path."""

    parts = Path(relative).parts
    plugin = ""
    prefix = ""
    controller = ""
    action = Path(relative).stem
    if len(parts) >= 2 and parts[0] == "plugins":
        plugin = parts[1]
    for marker in ("templates", "Template", "View"):
        if marker in parts:
            index = parts.index(marker)
            remaining = parts[index + 1 :]
            if len(remaining) >= 2:
                controller = remaining[-2]
                prefix_parts = remaining[:-2]
                prefix = "/".join(prefix_parts)
            break
    return plugin, prefix, controller, action


def parse_target(raw: str) -> dict[str, str]:
    """Parse a PHP array target for route controller/action."""

    return {match.group("key"): match.group("value") for match in TARGET_PAIR_RE.finditer(raw)}


def quoted(value: str) -> bool:
    """Return whether a route path expression is quoted."""

    return len(value) >= 2 and value[0] in {"'", '"'} and value[-1] == value[0]


def strip_quotes(value: str) -> str:
    """Strip surrounding quotes."""

    return value[1:-1]


def combine_paths(scope: str, path: str) -> str:
    """Combine route scope and path."""

    if not scope:
        return path
    return "/" + "/".join(part.strip("/") for part in (scope, path) if part.strip("/"))


def dasherize(value: str) -> str:
    """Convert a CamelCase symbol to a URL-ish segment."""

    return re.sub(r"(?<!^)([A-Z])", r"-\1", value).replace("_", "-").lower()


def is_route_file(relative: str) -> bool:
    """Return whether a file is a route config."""

    return relative.endswith("config/routes.php") or relative.endswith("Config/routes.php")


def is_controller_file(relative: str) -> bool:
    """Return whether a file is a controller."""

    return relative.endswith("Controller.php") and "/Controller/" in f"/{relative}"


def is_template_file(relative: str) -> bool:
    """Return whether a file is a template."""

    return (
        ("/templates/" in f"/{relative}" and relative.endswith(".php"))
        or ("/Template/" in f"/{relative}" and relative.endswith(".ctp"))
        or ("/View/" in f"/{relative}" and relative.endswith(".ctp"))
    )


def is_table_file(relative: str) -> bool:
    """Return whether a file is a Table class."""

    return "/Model/Table/" in f"/{relative}" and relative.endswith("Table.php")


def safe_relative(root: Path, path: Path) -> str:
    """Return repo-relative path string."""

    return path.relative_to(root).as_posix()
