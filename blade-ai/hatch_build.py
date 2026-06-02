"""Hatchling build hook — optionally bake the ChaosBlade binary into a
platform-specific wheel.

Two wheel shapes come out of this project:

  * Universal wheel (``py3-none-any``) — the default. No native binary;
    pip-install users on this wheel fetch ChaosBlade at first injection
    via ``chaos_agent.chaosblade_installer``. Built when
    ``BLADE_AI_WHEEL_PLATFORM`` is unset (e.g. local ``make wheel`` and
    the universal CI build).

  * Platform wheel (``py3-none-<platform>``) — built when CI sets
    ``BLADE_AI_WHEEL_PLATFORM`` (e.g. ``macosx_11_0_arm64``,
    ``manylinux2014_x86_64``). The matching ChaosBlade binary, already
    downloaded into ``vendor/chaosblade/`` by the CI step, is
    force-included at ``chaos_agent/_vendor/chaosblade/``. pip auto-selects
    this over the universal wheel for matching hosts, so those users get
    ChaosBlade unpacked at install time — zero runtime download.

The platform tag is supplied explicitly by CI (one value per matrix
entry) rather than auto-detected: the CI runner already knows its target,
and an explicit tag avoids the manylinux-vs-plain-linux ambiguity that
``sysconfig.get_platform()`` can't resolve on its own.

The TS TUI bundle and the skill pack are bundled separately via the
static ``force-include`` in pyproject.toml (they ship in BOTH wheel
shapes) — this hook only handles the platform-specific native binary.
"""

import os
from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class CustomBuildHook(BuildHookInterface):
    def initialize(self, version, build_data):
        plat = (os.environ.get("BLADE_AI_WHEEL_PLATFORM") or "").strip()
        if not plat:
            # Universal wheel: no native binary, stays py3-none-any.
            return

        vendor = Path(self.root) / "vendor" / "chaosblade"
        if not (vendor / "blade").is_file():
            raise RuntimeError(
                f"BLADE_AI_WHEEL_PLATFORM={plat} requests a platform wheel, "
                f"but {vendor / 'blade'} is missing. Download the matching "
                f"ChaosBlade tarball into vendor/chaosblade/ before building "
                f"(CI does this per matrix target; locally use "
                f"`make wheel-platform`)."
            )

        build_data.setdefault("force_include", {})
        build_data["force_include"][str(vendor)] = "chaos_agent/_vendor/chaosblade"
        # Mark the wheel non-pure so hatchling honours the explicit tag
        # instead of forcing py3-none-any.
        build_data["pure_python"] = False
        build_data["tag"] = f"py3-none-{plat}"
