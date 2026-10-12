#!/usr/bin/env python3
"""AndroidForge — Self-Healing System.

Runs AFTER setup_environment.py and BEFORE build_project.py. Scans the
project for common, well-known issues and applies non-destructive fixes
in the workspace (never touches the source ZIP).

Issues it auto-fixes:
  1. AGP version requires a newer Gradle than the project's wrapper pins.
     → If a wrapper exists, bump the wrapper's distributionUrl to the
       required Gradle version. If no wrapper exists, this is a no-op
       (build_project.py already falls back to system gradle).
  2. gradlew missing or wrapper jar missing.
     → Run `gradle wrapper` to regenerate (best-effort).
  3. Missing local.properties (sdk.dir).
     → Already handled by setup_environment.py; self_heal double-checks.
  4. Duplicate top-level Kotlin/Java class names across source dirs.
     → Detect + warn (DO NOT auto-delete — that's destructive; surface
       a clear actionable error instead).
  5. Settings file uses RepositoriesMode.FAIL_ON_PROJECT_REPOS but
     build.gradle declares project-level repos.
     → setup_environment.py already handles this; self_heal warns if
       it sees the conflict still present.
  6. AndroidManifest.xml missing package attribute (very old projects).
     → Add `package="..."` based on the namespace from build.gradle.
  7. Kotlin/Java JVM target validation fails on modern AGP/Kotlin defaults.
     → Set `kotlin.jvm.target.validation.mode=warning` in gradle.properties
      for Kotlin + AGP 8+ projects unless already defined.

The script writes a JSON report to stdout (and to $GITHUB_OUTPUT for
the workflow). It NEVER fails the build — fixes are best-effort, and
unfixed issues are reported so the diagnosis step can pick them up.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def read_file(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""


def parse_agp_version(content: str) -> str | None:
    """Extract AGP version from a Kotlin/Groovy build file."""
    # Modern Kotlin DSL: id("com.android.application") version "8.5.2"
    m = re.search(r'id\s*\(\s*["\']com\.android\.application["\']\s*\)\s*version\s*["\']([^"\']+)["\']', content)
    if m:
        return m.group(1).strip()
    # Modern Kotlin DSL (no parens): id "com.android.application" version "8.5.2"
    m = re.search(r'id\s+["\']com\.android\.application["\'].*?version\s*["\']([^"\']+)["\']', content)
    if m:
        return m.group(1).strip()
    # Old Groovy: classpath 'com.android.tools.build:gradle:8.5.2'
    m = re.search(r"com\.android\.tools\.build:gradle:([^'\"\s]+)", content)
    if m:
        return m.group(1).strip()
    return None


def parse_gradle_wrapper_version(wrapper_props: str) -> str | None:
    m = re.search(r"gradle-([\d.]+)-\w+\.zip", wrapper_props)
    return m.group(1) if m else None


def version_tuple(v: str) -> tuple[int, ...]:
    """Convert '8.5.2' → (8, 5, 2). Returns (0,) on parse failure."""
    try:
        return tuple(int(p) for p in v.split("."))
    except ValueError:
        return (0,)


# Reference: https://developer.android.com/build/releases/gradle-plugin
AGP_TO_MIN_GRADLE = {
    "8.7": "8.9",
    "8.6": "8.7",
    "8.5": "8.7",
    "8.4": "8.6",
    "8.3": "8.4",
    "8.2": "8.2",
    "8.1": "8.0",
    "8.0": "8.0",
    "7.4": "7.5",
    "7.3": "7.4",
    "7.2": "7.3.3",
    "7.1": "7.2",
    "7.0": "7.0.2",
    "4.2": "6.7.1",
    "4.1": "6.5",
    "4.0": "6.1.1",
    "3.6": "5.6.4",
    "3.5": "5.4.1",
    "3.4": "5.1.1",
    "3.3": "4.10.1",
    "3.2": "4.6",
    "3.1": "4.4",
    "3.0": "4.1",
}


def min_gradle_for_agp(agp: str) -> str | None:
    """Look up the minimum Gradle version required for a given AGP version."""
    if not agp:
        return None
    parts = agp.split(".")
    # Try exact major.minor match first
    if len(parts) >= 2:
        key = f"{parts[0]}.{parts[1]}"
        if key in AGP_TO_MIN_GRADLE:
            return AGP_TO_MIN_GRADLE[key]
    # Fall back to major version
    major = parts[0]
    for k, v in AGP_TO_MIN_GRADLE.items():
        if k.split(".")[0] == major:
            return v
    return None


# ---------------------------------------------------------------------------
# Self-heal actions
# ---------------------------------------------------------------------------
def heal_gradle_wrapper(root: Path, agp_version: str | None, fixes: list[dict]) -> None:
    """If the project's gradle-wrapper pins a version too old for the AGP,
    bump the wrapper to the minimum required version. Also regenerate the
    wrapper jar if it's missing.
    """
    if not agp_version:
        return
    min_gradle = min_gradle_for_agp(agp_version)
    if not min_gradle:
        return

    wrapper_props = root / "gradle" / "wrapper" / "gradle-wrapper.properties"
    if not wrapper_props.exists():
        # No wrapper to bump — build_project.py will fall back to system
        # gradle (which is 8.9 in the Docker image, modern enough for AGP 8.x).
        fixes.append({
            "name": "no_wrapper_present",
            "action": "none",
            "reason": "No gradle-wrapper.properties — build will use system gradle (Docker image has 8.9).",
        })
        return

    content = read_file(wrapper_props)
    wrapper_version = parse_gradle_wrapper_version(content)
    if not wrapper_version:
        return

    if version_tuple(wrapper_version) >= version_tuple(min_gradle):
        # Wrapper is already new enough — nothing to do.
        return

    # Bump the wrapper.
    new_url = f"https\\://services.gradle.org/distributions/gradle-{min_gradle}-bin.zip"
    new_content = re.sub(
        r"distributionUrl=[^\s]+",
        f"distributionUrl={new_url}",
        content,
    )
    if new_content != content:
        wrapper_props.write_text(new_content)
        fixes.append({
            "name": "bump_gradle_wrapper",
            "action": "rewrote distributionUrl",
            "reason": f"Project pins gradle-{wrapper_version} but AGP {agp_version} requires >= {min_gradle}. Bumped wrapper.",
            "from": wrapper_version,
            "to": min_gradle,
        })


def heal_missing_gradlew(root: Path, fixes: list[dict]) -> None:
    """If gradlew is missing but gradle-wrapper.properties exists, regenerate
    the wrapper by running `gradle wrapper` (best-effort, never fails).
    """
    gradlew = root / "gradlew"
    wrapper_jar = root / "gradle" / "wrapper" / "gradle-wrapper.jar"
    wrapper_props = root / "gradle" / "wrapper" / "gradle-wrapper.properties"

    if gradlew.exists() and wrapper_jar.exists():
        return  # nothing to do

    if not wrapper_props.exists():
        return  # no wrapper to regenerate

    # Try to run `gradle wrapper` (uses system gradle)
    try:
        result = subprocess.run(
            ["gradle", "wrapper", "--distribution-type", "bin"],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode == 0 and gradlew.exists() and wrapper_jar.exists():
            fixes.append({
                "name": "regenerate_gradlew",
                "action": "ran `gradle wrapper`",
                "reason": "gradlew or gradle-wrapper.jar was missing — regenerated via system gradle.",
            })
        else:
            fixes.append({
                "name": "regenerate_gradlew_failed",
                "action": "none",
                "reason": f"`gradle wrapper` exited {result.returncode}: {result.stderr[:200]}",
            })
    except Exception as e:
        fixes.append({
            "name": "regenerate_gradlew_failed",
            "action": "none",
            "reason": f"Exception while running `gradle wrapper`: {e}",
        })


def heal_missing_local_properties(root: Path, android_sdk: str, fixes: list[dict]) -> None:
    """Ensure local.properties exists with sdk.dir pointing to the Android SDK.
    (setup_environment.py already does this, but self_heal double-checks.)
    """
    local_props = root / "local.properties"
    if local_props.exists():
        content = read_file(local_props)
        if "sdk.dir" in content:
            return
    local_props.write_text(f"sdk.dir={android_sdk}\n")
    fixes.append({
        "name": "create_local_properties",
        "action": "wrote local.properties",
        "reason": f"local.properties missing or had no sdk.dir — set sdk.dir={android_sdk}",
    })


def heal_kotlin_jvm_target_validation(root: Path, agp_version: str | None, fixes: list[dict]) -> None:
    """Avoid hard failures from Kotlin/Java target mismatch validation.

    Newer Kotlin/AGP combinations can fail with:
      "Inconsistent JVM-target compatibility ... (1.8) and ... (17)"
    when Java compile defaults differ from Kotlin defaults. We keep the build
    moving by downgrading this specific check to a warning via gradle.properties.
    """
    if not agp_version:
        return
    try:
        agp_major = int(str(agp_version).split(".", 1)[0])
    except Exception:
        return
    if agp_major < 8:
        return

    gradle_files = [
        root / "build.gradle.kts",
        root / "build.gradle",
        root / "app" / "build.gradle.kts",
        root / "app" / "build.gradle",
    ]
    has_kotlin = False
    for gf in gradle_files:
        if not gf.exists():
            continue
        content = read_file(gf)
        if "org.jetbrains.kotlin" in content or "kotlin(" in content:
            has_kotlin = True
            break
    if not has_kotlin:
        return

    props = root / "gradle.properties"
    content = read_file(props) if props.exists() else ""
    if re.search(r"(?im)^\s*kotlin\.jvm\.target\.validation\.mode\s*=", content):
        return

    line = "kotlin.jvm.target.validation.mode=warning"
    if content and not content.endswith("\n"):
        content += "\n"
    content += f"{line}\n"
    props.write_text(content)
    fixes.append({
        "name": "set_kotlin_jvm_target_validation_mode",
        "action": f"added `{line}` to {props.relative_to(root) if props.is_relative_to(root) else props}",
        "reason": "Prevents hard failure when Gradle detects Kotlin/Java JVM target mismatch (for example 17 vs 1.8).",
    })


def detect_duplicate_classes(root: Path, warnings: list[dict]) -> None:
    """Scan all .kt / .java files for top-level class declarations, warn if
    the same fully-qualified class name appears in 2+ files (Kotlin refuses
    to compile 'Redeclaration: class X').
    """
    class_to_files: dict[str, list[Path]] = {}
    for src_ext in ("*.kt", "*.java"):
        for f in root.rglob(src_ext):
            # Skip build/ output dirs
            if "/build/" in str(f) or "\\build\\" in str(f):
                continue
            try:
                content = f.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            # Find package
            pkg_match = re.search(r"^package\s+([\w.]+)", content, re.MULTILINE)
            pkg = pkg_match.group(1) if pkg_match else ""
            # Find top-level class/object names
            for m in re.finditer(r"^\s*(?:public\s+|internal\s+|private\s+)?(?:final\s+)?(?:class|object|interface|enum class)\s+(\w+)", content, re.MULTILINE):
                name = m.group(1)
                fq = f"{pkg}.{name}" if pkg else name
                class_to_files.setdefault(fq, []).append(f)
    for fq, files in class_to_files.items():
        if len(files) > 1:
            warnings.append({
                "kind": "duplicate_class",
                "class": fq,
                "files": [str(f.relative_to(root) if f.is_relative_to(root) else f) for f in files],
                "hint": "Kotlin refuses to compile 'Redeclaration: class X'. Delete one of the duplicates.",
            })


def detect_repositories_mode_conflict(root: Path, warnings: list[dict]) -> None:
    """If settings.gradle(.kts) declares FAIL_ON_PROJECT_REPOS but a
    build.gradle file still has `repositories { }` (other than inside
    pluginManagement or buildscript), warn.
    """
    settings_files = [
        root / "settings.gradle",
        root / "settings.gradle.kts",
    ]
    has_forbid = False
    for sf in settings_files:
        if not sf.exists():
            continue
        if "FAIL_ON_PROJECT_REPOS" in read_file(sf):
            has_forbid = True
            break
    if not has_forbid:
        return

    for bf in [root / "build.gradle", root / "build.gradle.kts",
               root / "app" / "build.gradle", root / "app" / "build.gradle.kts"]:
        if not bf.exists():
            continue
        content = read_file(bf)
        # Look for a `repositories {` block NOT inside `buildscript {}` or
        # `pluginManagement {}` (those are allowed even with FAIL_ON_PROJECT_REPOS).
        # Simple heuristic: find `repositories {` and check the surrounding context.
        for m in re.finditer(r"^\s*repositories\s*\{", content, re.MULTILINE):
            # Look backwards for buildscript / pluginManagement
            before = content[:m.start()].rstrip()
            if before.endswith("buildscript") or "pluginManagement" in before.rsplit("\n", 5)[-1:]:
                continue
            warnings.append({
                "kind": "repositories_mode_conflict",
                "file": str(bf.relative_to(root) if bf.is_relative_to(root) else bf),
                "hint": "settings.gradle declares FAIL_ON_PROJECT_REPOS but this build.gradle has a project-level `repositories {}` block — Gradle will fail. Either remove the block from build.gradle or relax the RepositoriesMode in settings.gradle.",
            })
            break  # one warning per file is enough


def heal_android_manifest_package(root: Path, fixes: list[dict], warnings: list[dict]) -> None:
    """For very old projects that don't have `namespace` in build.gradle
    and have an AndroidManifest.xml without a `package` attribute, add one
    based on the applicationId.
    """
    # Find AndroidManifest.xml
    manifests = list(root.rglob("AndroidManifest.xml"))
    if not manifests:
        return

    # Find applicationId from app/build.gradle(.kts)
    app_gradle = None
    for cand in [root / "app" / "build.gradle", root / "app" / "build.gradle.kts"]:
        if cand.exists():
            app_gradle = read_file(cand)
            break
    if not app_gradle:
        return

    # Look for namespace= first (modern AGP)
    ns_match = re.search(r'namespace\s*=\s*["\']([^"\']+)["\']', app_gradle)
    if ns_match:
        return  # namespace is set, no need to patch manifest

    # Look for applicationId
    appid_match = re.search(r'applicationId\s*["\']?\s*=?\s*["\']([^"\']+)["\']', app_gradle)
    if not appid_match:
        return
    application_id = appid_match.group(1)

    for manifest in manifests:
        content = read_file(manifest)
        if "package=" in content or 'package =' in content:
            continue  # already has package attribute
        # Insert package attribute into the <manifest> opening tag
        new_content = re.sub(
            r"(<manifest[^>]*?)(/?>)",
            rf'\1 package="{application_id}"\2',
            content,
            count=1,
        )
        if new_content != content:
            manifest.write_text(new_content)
            fixes.append({
                "name": "add_manifest_package",
                "action": f"added package=\"{application_id}\" to {manifest.relative_to(root) if manifest.is_relative_to(root) else manifest}",
                "reason": "build.gradle has no `namespace` and AndroidManifest.xml had no `package` attribute — added one based on applicationId.",
            })


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description="AndroidForge self-healing system")
    parser.add_argument("--root", required=True, help="Project root path")
    parser.add_argument("--detect", required=True, help="Detection JSON (string or path)")
    parser.add_argument("--toolchain", required=True, help="Toolchain JSON (string or path)")
    parser.add_argument("--android-sdk", default=os.environ.get("ANDROID_HOME", "/opt/android-sdk"))
    parser.add_argument("--output", default=None, help="Write JSON report to this file")
    args = parser.parse_args()

    def load(s: str) -> Any:
        if Path(s).exists():
            return json.loads(Path(s).read_text())
        return json.loads(s)

    detect = load(args.detect)
    toolchain = load(args.toolchain)
    root = Path(args.root).resolve()

    fixes: list[dict] = []
    warnings: list[dict] = []

    # 1. Gradle wrapper version vs AGP
    agp_version = (detect.get("versions") or {}).get("agp_version")
    if not agp_version:
        # Fall back to scanning build files directly
        for grad_file in [root / "build.gradle.kts", root / "build.gradle",
                          root / "app" / "build.gradle.kts", root / "app" / "build.gradle"]:
            if grad_file.exists():
                agp_version = parse_agp_version(read_file(grad_file))
                if agp_version:
                    break
    heal_gradle_wrapper(root, agp_version, fixes)

    # 2. Missing gradlew / wrapper jar
    heal_missing_gradlew(root, fixes)

    # 3. Missing local.properties
    heal_missing_local_properties(root, args.android_sdk, fixes)

    # 4. Kotlin JVM target validation mismatch (Kotlin + AGP 8+)
    heal_kotlin_jvm_target_validation(root, agp_version, fixes)

    # 5. AndroidManifest package attribute (for old projects)
    heal_android_manifest_package(root, fixes, warnings)

    # 6. Detect duplicate top-level classes (warn-only, do NOT auto-delete)
    detect_duplicate_classes(root, warnings)

    # 7. Detect RepositoriesMode conflict (warn-only)
    detect_repositories_mode_conflict(root, warnings)

    report = {
        "project_root": str(root),
        "agp_version": agp_version,
        "fixes_applied": fixes,
        "warnings": warnings,
        "summary": f"{len(fixes)} fix(es) applied, {len(warnings)} warning(s)",
    }

    output = json.dumps(report, indent=2)
    if args.output:
        Path(args.output).write_text(output)
    else:
        print(output)

    # Also surface key fields to GITHUB_OUTPUT
    gh_output = os.environ.get("GITHUB_OUTPUT")
    if gh_output:
        with open(gh_output, "a", encoding="utf-8") as f:
            f.write(f"self_heal_fixes={len(fixes)}\n")
            f.write(f"self_heal_warnings={len(warnings)}\n")
            f.write(f"json<<EOF\n{json.dumps(report)}\nEOF\n")

    # Also emit a step summary
    summary_file = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_file:
        with open(summary_file, "a", encoding="utf-8") as f:
            f.write("\n## 🩹 Self-healing report\n\n")
            f.write(f"{len(fixes)} fix(es) applied, {len(warnings)} warning(s)\n\n")
            if fixes:
                f.write("### Fixes applied\n")
                for fix in fixes:
                    f.write(f"- **{fix['name']}**: {fix.get('action','')} — {fix.get('reason','')}\n")
                f.write("\n")
            if warnings:
                f.write("### Warnings (not auto-fixable)\n")
                for w in warnings:
                    f.write(f"- ⚠️ **{w.get('kind','?')}**: {w.get('hint','')}\n")
                    if "file" in w:
                        f.write(f"  - File: `{w['file']}`\n")
                    if "files" in w:
                        f.write(f"  - Files: {', '.join(w['files'])}\n")
                f.write("\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
