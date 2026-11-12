"""`open_app`'s language, region and text size (docs/AGENT-TOOLS.md §16.3): checking the values and building the launch
arguments WebDriverAgent hands the app. They are Apple's own launch arguments, so they hold for that launch of that app
only and nothing in Settings changes."""
from __future__ import annotations

import re

from .control import Refused

# text_size -> UIContentSizeCategory name (the value of -UIPreferredContentSizeCategoryName). `l` is the iOS default.
TEXT_SIZES = {
    "xs": "UICTContentSizeCategoryXS", "s": "UICTContentSizeCategoryS", "m": "UICTContentSizeCategoryM",
    "l": "UICTContentSizeCategoryL", "xl": "UICTContentSizeCategoryXL", "xxl": "UICTContentSizeCategoryXXL",
    "xxxl": "UICTContentSizeCategoryXXXL", "ax1": "UICTContentSizeCategoryAccessibilityM",
    "ax2": "UICTContentSizeCategoryAccessibilityL", "ax3": "UICTContentSizeCategoryAccessibilityXL",
    "ax4": "UICTContentSizeCategoryAccessibilityXXL", "ax5": "UICTContentSizeCategoryAccessibilityXXXL",
}
LANGUAGE = re.compile(r"[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})*")  # BCP 47, loosely: ar, en-GB, zh-Hans-CN
REGION = re.compile(r"[A-Za-z]{2}")  # ISO 3166-1 alpha-2: QA
DEFAULT_LANGUAGE = "en"  # the language a region alone is paired with (the phone's own isn't asked for)
KEYS = ("language", "region", "text_size")
SYSTEM = "system"  # text_size: the phone's own setting (shared with Tapline, AGENT-TOOLS §16.3)


def check(args: dict) -> dict:
    """The language, region and text size in `args`, cleaned (region upper case, text size lower case); the keys
    that are null or missing are left out. `bad_params` for a value that isn't one, with what is allowed."""
    out: dict = {}
    language, region, size = args.get("language"), args.get("region"), args.get("text_size")
    if language is not None:
        if not isinstance(language, str) or not LANGUAGE.fullmatch(language.strip()):
            raise Refused("bad_params", f"language must be a BCP 47 code like ar, en or en-GB, not {language!r}")
        out["language"] = language.strip()
    if region is not None:
        if not isinstance(region, str) or not REGION.fullmatch(region.strip()):
            raise Refused("bad_params", f"region must be an ISO 3166 country code like QA or GB, not {region!r}")
        out["region"] = region.strip().upper()
    if size is not None:
        if not isinstance(size, str) or size.strip().lower() not in (*TEXT_SIZES, SYSTEM):
            raise Refused("bad_params", f"text_size must be one of {', '.join(TEXT_SIZES)}, or {SYSTEM} (the phone's "
                                        f"own), not {size!r}")
        if size.strip().lower() != SYSTEM:  # `system` is the phone's own setting: as with no text_size (§16.3)
            out["text_size"] = size.strip().lower()
    return out


def arguments(args: dict) -> list[str]:
    """The launch arguments for the (checked) language, region and text size, as WebDriverAgent's `arguments`."""
    language, region, size = args.get("language"), args.get("region"), args.get("text_size")
    out: list[str] = []
    if language:
        out += ["-AppleLanguages", f"({language})"]
    if region:
        base = (language or DEFAULT_LANGUAGE).split("-")[0].lower()
        out += ["-AppleLocale", f"{base}_{region}"]
    if size:
        out += ["-UIPreferredContentSizeCategoryName", TEXT_SIZES[size]]
    return out


def said(args: dict) -> str:
    """` in ar, QA, text size xl` (only the parts given), or an empty string: the end of `open_app`'s first line."""
    parts = [args[k] for k in ("language", "region") if args.get(k)]
    if args.get("text_size"):
        parts.append(f"text size {args['text_size']}")
    return f" in {', '.join(parts)}" if parts else ""
