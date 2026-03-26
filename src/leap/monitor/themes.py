"""Visual theme definitions and manager for Leap Monitor.

Provides named themes with full color palettes, a current/get/set API,
and a WCAG contrast safety-net utility.
"""

import colorsys
from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class Theme:
    """Complete color palette for a monitor theme."""

    name: str
    is_dark: bool

    # Window / table backgrounds
    window_bg: str
    cell_bg: str
    cell_bg_alt: str
    hover_bg: str  # rgba() string for row hover overlay

    # Text
    text_primary: str
    text_secondary: str  # muted hints / disabled
    text_muted: str  # very dim (#999 equivalent)

    # Borders
    border_solid: str  # group separator
    border_subtle: str  # intra-group separator (rgba)

    # Accent colors
    accent_green: str
    accent_red: str
    accent_orange: str
    accent_blue: str
    accent_yellow: str

    # Popup / tooltip
    popup_bg: str
    popup_border: str

    # Input fields
    input_bg: str
    input_border: str
    input_focus_border: str

    # Misc
    icon_color: str  # SVG icon default color
    header_bg: str  # table header background
    font_family: str = ''  # empty = system default

    # Typography (px)
    font_size_base: int = 13
    font_size_small: int = 11
    font_size_large: int = 15

    # Buttons
    button_bg: str = ''          # filled in per-theme below
    button_hover_bg: str = ''
    button_border: str = ''

    # Scrollbar
    scrollbar_bg: str = ''
    scrollbar_handle: str = ''
    scrollbar_handle_hover: str = ''

    # UI geometry
    border_radius: int = 6       # default corner radius for buttons, popups, inputs

    # Status indicator colors (QColor args as hex)
    status_running: str = '#4caf50'
    status_permission: str = '#ff9800'
    status_input: str = '#64b5f6'
    status_interrupted: str = '#ffd54f'
    status_idle: str = '#ffffff'  # idle / default text


# ---------------------------------------------------------------------------
#  Built-in themes
# ---------------------------------------------------------------------------

_MIDNIGHT = Theme(
    name='Midnight',
    is_dark=True,
    window_bg='#1a1b2e',
    cell_bg='#1d1e32',
    cell_bg_alt='#24263c',
    hover_bg='rgba(99, 102, 241, 20)',
    text_primary='#e2e4f0',
    text_secondary='#8b8fa3',
    text_muted='#5c6078',
    border_solid='#3a3d56',
    border_subtle='rgba(99, 102, 241, 22)',
    accent_green='#4ade80',
    accent_red='#f87171',
    accent_orange='#fb923c',
    accent_blue='#60a5fa',
    accent_yellow='#fbbf24',
    popup_bg='#252840',
    popup_border='#3a3d56',
    input_bg='#1d1e32',
    input_border='#3a3d56',
    input_focus_border='#818cf8',
    icon_color='#8b8fa3',
    header_bg='#151628',
    button_bg='#282b44',
    button_hover_bg='#343758',
    button_border='#3a3d56',
    scrollbar_bg='#1a1b2e',
    scrollbar_handle='#3a3d56',
    scrollbar_handle_hover='#4a4d66',
    status_running='#4ade80',
    status_permission='#fb923c',
    status_input='#60a5fa',
    status_interrupted='#fbbf24',
    status_idle='#e2e4f0',
)

_OCEAN = Theme(
    name='Ocean',
    is_dark=True,
    window_bg='#0f1923',
    cell_bg='#152030',
    cell_bg_alt='#1a2636',
    hover_bg='rgba(56, 189, 248, 18)',
    text_primary='#d4dce8',
    text_secondary='#7a8fa6',
    text_muted='#4e6580',
    border_solid='#2a4a6a',
    border_subtle='rgba(56, 189, 248, 18)',
    accent_green='#34d399',
    accent_red='#fb7185',
    accent_orange='#fdba74',
    accent_blue='#38bdf8',
    accent_yellow='#fde68a',
    popup_bg='#1a2a3e',
    popup_border='#2a4a6a',
    input_bg='#152030',
    input_border='#2a4a6a',
    input_focus_border='#38bdf8',
    icon_color='#7a8fa6',
    header_bg='#0c1520',
    button_bg='#1a2a3e',
    button_hover_bg='#243850',
    button_border='#2a4a6a',
    scrollbar_bg='#0f1923',
    scrollbar_handle='#2a4a6a',
    scrollbar_handle_hover='#3a5a7a',
    status_running='#34d399',
    status_permission='#fdba74',
    status_input='#38bdf8',
    status_interrupted='#fde68a',
    status_idle='#d4dce8',
)

_MONOKAI = Theme(
    name='Monokai',
    is_dark=True,
    window_bg='#272822',
    cell_bg='#2c2d26',
    cell_bg_alt='#31322b',
    hover_bg='rgba(166, 226, 46, 14)',
    text_primary='#f8f8f2',
    text_secondary='#a6a68a',
    text_muted='#75715e',
    border_solid='#49483e',
    border_subtle='rgba(117, 113, 94, 40)',
    accent_green='#a6e22e',
    accent_red='#f92672',
    accent_orange='#fd971f',
    accent_blue='#66d9ef',
    accent_yellow='#e6db74',
    popup_bg='#3e3d32',
    popup_border='#49483e',
    input_bg='#3e3d32',
    input_border='#49483e',
    input_focus_border='#66d9ef',
    icon_color='#a6a68a',
    header_bg='#22231c',
    button_bg='#3e3d32',
    button_hover_bg='#49483e',
    button_border='#49483e',
    scrollbar_bg='#272822',
    scrollbar_handle='#49483e',
    scrollbar_handle_hover='#5a594e',
    status_running='#a6e22e',
    status_permission='#fd971f',
    status_input='#66d9ef',
    status_interrupted='#e6db74',
    status_idle='#f8f8f2',
)

_NORD = Theme(
    name='Nord',
    is_dark=True,
    window_bg='#2e3440',
    cell_bg='#323845',
    cell_bg_alt='#373e4c',
    hover_bg='rgba(136, 192, 208, 16)',
    text_primary='#d8dee9',
    text_secondary='#81a1c1',
    text_muted='#616e88',
    border_solid='#434c5e',
    border_subtle='rgba(76, 86, 106, 40)',
    accent_green='#a3be8c',
    accent_red='#bf616a',
    accent_orange='#d08770',
    accent_blue='#88c0d0',
    accent_yellow='#ebcb8b',
    popup_bg='#3b4252',
    popup_border='#434c5e',
    input_bg='#3b4252',
    input_border='#434c5e',
    input_focus_border='#88c0d0',
    icon_color='#81a1c1',
    header_bg='#282d38',
    button_bg='#3b4252',
    button_hover_bg='#434c5e',
    button_border='#434c5e',
    scrollbar_bg='#2e3440',
    scrollbar_handle='#434c5e',
    scrollbar_handle_hover='#4c566a',
    status_running='#a3be8c',
    status_permission='#d08770',
    status_input='#88c0d0',
    status_interrupted='#ebcb8b',
    status_idle='#d8dee9',
)

_SOLARIZED_DARK = Theme(
    name='Solarized Dark',
    is_dark=True,
    window_bg='#002b36',
    cell_bg='#03313d',
    cell_bg_alt='#073642',
    hover_bg='rgba(38, 139, 210, 14)',
    text_primary='#93a1a1',
    text_secondary='#657b83',
    text_muted='#586e75',
    border_solid='#2a5460',
    border_subtle='rgba(88, 110, 117, 35)',
    accent_green='#859900',
    accent_red='#dc322f',
    accent_orange='#cb4b16',
    accent_blue='#268bd2',
    accent_yellow='#b58900',
    popup_bg='#073642',
    popup_border='#2a5460',
    input_bg='#073642',
    input_border='#2a5460',
    input_focus_border='#268bd2',
    icon_color='#657b83',
    header_bg='#002028',
    button_bg='#073642',
    button_hover_bg='#0a4050',
    button_border='#2a5460',
    scrollbar_bg='#002b36',
    scrollbar_handle='#2a5460',
    scrollbar_handle_hover='#3a6470',
    status_running='#859900',
    status_permission='#cb4b16',
    status_input='#268bd2',
    status_interrupted='#b58900',
    status_idle='#93a1a1',
)

_DAWN = Theme(
    name='Dawn',
    is_dark=False,
    window_bg='#f8f9fc',
    cell_bg='#ffffff',
    cell_bg_alt='#f5f6fa',
    hover_bg='rgba(99, 102, 241, 10)',
    text_primary='#1e1e2e',
    text_secondary='#64748b',
    text_muted='#94a3b8',
    border_solid='#d1d5e0',
    border_subtle='rgba(0, 0, 0, 12)',
    accent_green='#16a34a',
    accent_red='#dc2626',
    accent_orange='#ea580c',
    accent_blue='#2563eb',
    accent_yellow='#ca8a04',
    popup_bg='#ffffff',
    popup_border='#d1d5e0',
    input_bg='#ffffff',
    input_border='#d1d5e0',
    input_focus_border='#6366f1',
    icon_color='#64748b',
    header_bg='#eef0f5',
    button_bg='#e8eaf2',
    button_hover_bg='#dcdfe8',
    button_border='#d1d5e0',
    scrollbar_bg='#f8f9fc',
    scrollbar_handle='#d1d5e0',
    scrollbar_handle_hover='#b8bcc8',
    status_running='#16a34a',
    status_permission='#ea580c',
    status_input='#2563eb',
    status_interrupted='#ca8a04',
    status_idle='#1e1e2e',
)

_COSMOS = Theme(
    name='Cosmos',
    is_dark=True,
    window_bg='#13111c',
    cell_bg='#17152a',
    cell_bg_alt='#1c1a30',
    hover_bg='rgba(167, 139, 250, 14)',
    text_primary='#e4e0f0',
    text_secondary='#9086ad',
    text_muted='#5e5580',
    border_solid='#2e2a48',
    border_subtle='rgba(139, 92, 246, 18)',
    accent_green='#34d399',
    accent_red='#fb7185',
    accent_orange='#f59e0b',
    accent_blue='#a78bfa',
    accent_yellow='#fbbf24',
    popup_bg='#1c1a30',
    popup_border='#2e2a48',
    input_bg='#17152a',
    input_border='#2e2a48',
    input_focus_border='#8b5cf6',
    icon_color='#9086ad',
    header_bg='#0f0d18',
    button_bg='#221f3a',
    button_hover_bg='#2e2a48',
    button_border='#2e2a48',
    scrollbar_bg='#13111c',
    scrollbar_handle='#2e2a48',
    scrollbar_handle_hover='#3e3a58',
    status_running='#34d399',
    status_permission='#f59e0b',
    status_input='#a78bfa',
    status_interrupted='#fbbf24',
    status_idle='#e4e0f0',
)

# Ordered dict preserving insertion order for combo box display
THEMES: dict[str, Theme] = {
    'Midnight': _MIDNIGHT,
    'Cosmos': _COSMOS,
    'Ocean': _OCEAN,
    'Monokai': _MONOKAI,
    'Nord': _NORD,
    'Solarized Dark': _SOLARIZED_DARK,
    'Dawn': _DAWN,
}

_current_theme_name: str = 'Midnight'


def current_theme() -> Theme:
    """Return the currently active theme."""
    return THEMES.get(_current_theme_name, _MIDNIGHT)


def set_theme(name: str) -> None:
    """Set the active theme by name. Ignores unknown names."""
    global _current_theme_name
    if name in THEMES:
        _current_theme_name = name


def get_theme(name: str) -> Optional[Theme]:
    """Return a theme by name, or None if not found."""
    return THEMES.get(name)


# ---------------------------------------------------------------------------
#  Contrast utility
# ---------------------------------------------------------------------------

def _relative_luminance(hex_color: str) -> float:
    """Compute WCAG 2.0 relative luminance from a hex color string."""
    h = hex_color.lstrip('#')
    r, g, b = int(h[0:2], 16) / 255.0, int(h[2:4], 16) / 255.0, int(h[4:6], 16) / 255.0

    def linearize(c: float) -> float:
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    return 0.2126 * linearize(r) + 0.7152 * linearize(g) + 0.0722 * linearize(b)


def _contrast_ratio(lum1: float, lum2: float) -> float:
    """Return the WCAG contrast ratio between two relative luminances."""
    lighter = max(lum1, lum2)
    darker = min(lum1, lum2)
    return (lighter + 0.05) / (darker + 0.05)


def _adjust_lightness(fg_hex: str, bg_hex: str, min_ratio: float) -> str:
    """Shift the foreground color's lightness (HLS) to meet *min_ratio*.

    Preserves hue.  Boosts saturation as lightness shifts to keep the color
    vivid.  Tries both lighter and darker directions and picks the one closest
    to the original.  Falls back to black/white only when no lightness value
    can satisfy the ratio.
    """
    h_str = fg_hex.lstrip('#')
    r, g, b = int(h_str[0:2], 16) / 255.0, int(h_str[2:4], 16) / 255.0, int(h_str[4:6], 16) / 255.0
    hue, lig, sat = colorsys.rgb_to_hls(r, g, b)
    lum_bg = _relative_luminance(bg_hex)

    # Achromatic colors (no saturation) → fall back to black/white
    if sat < 0.05:
        return '#000000' if lum_bg > 0.5 else '#ffffff'

    def _search(target: float, ratio: float) -> Optional[str]:
        """Binary-search lightness from *lig* toward *target*, return best."""
        # Always keep lo < hi so midpoint math is consistent
        lo, hi = min(lig, target), max(lig, target)
        toward_higher = target > lig  # are we searching toward higher L?
        found: Optional[str] = None
        for _ in range(30):
            mid = (lo + hi) / 2.0
            # Boost saturation proportionally to lightness shift
            shift = abs(mid - lig)
            boosted_sat = min(1.0, sat + shift * (1.0 - sat))
            cr, cg, cb = colorsys.hls_to_rgb(hue, mid, boosted_sat)
            candidate = '#{:02x}{:02x}{:02x}'.format(
                min(255, max(0, round(cr * 255))),
                min(255, max(0, round(cg * 255))),
                min(255, max(0, round(cb * 255))),
            )
            lum_cand = _relative_luminance(candidate)
            if _contrast_ratio(lum_cand, lum_bg) >= ratio:
                found = candidate
                # Narrow toward original lightness (minimize shift)
                if toward_higher:
                    hi = mid
                else:
                    lo = mid
            else:
                # Need more shift
                if toward_higher:
                    lo = mid
                else:
                    hi = mid
        return found

    def _best_of(ratio: float) -> Optional[str]:
        """Search both directions at the given ratio, return closest match."""
        lighter = _search(1.0, ratio) if lig < 1.0 else None
        darker = _search(0.0, ratio) if lig > 0.0 else None
        if lighter and darker:
            l_lum = _relative_luminance(lighter)
            d_lum = _relative_luminance(darker)
            fg_lum = _relative_luminance(fg_hex)
            return lighter if abs(l_lum - fg_lum) <= abs(d_lum - fg_lum) else darker
        return lighter or darker

    strict = _best_of(min_ratio)
    if strict is not None:
        # Check if the strict result looks visibly colored (not near-black/white)
        s_h = strict.lstrip('#')
        s_r, s_g, s_b = int(s_h[0:2], 16) / 255.0, int(s_h[2:4], 16) / 255.0, int(s_h[4:6], 16) / 255.0
        _, s_lig, s_sat = colorsys.rgb_to_hls(s_r, s_g, s_b)
        if s_sat >= 0.3 and 0.12 <= s_lig <= 0.88:
            return strict
        # Strict result lost its color identity — try a relaxed ratio (WCAG AA
        # large-text = 3:1) for a more vivid, recognizable alternative.
        relaxed = _best_of(min(min_ratio, 3.0))
        if relaxed is not None:
            return relaxed
        return strict
    # Hue cannot reach contrast — fall back to black/white
    return '#000000' if lum_bg > 0.5 else '#ffffff'


def ensure_contrast(fg_hex: str, bg_hex: str, min_ratio: float = 4.5) -> str:
    """Return *fg_hex* if it has sufficient contrast against *bg_hex*.

    If the contrast ratio is below *min_ratio* (WCAG AA = 4.5:1), adjusts the
    foreground color's lightness while preserving its hue and saturation.  Only
    falls back to black/white if no lightness adjustment can satisfy the ratio.
    """
    lum_fg = _relative_luminance(fg_hex)
    lum_bg = _relative_luminance(bg_hex)
    if _contrast_ratio(lum_fg, lum_bg) >= min_ratio:
        return fg_hex
    return _adjust_lightness(fg_hex, bg_hex, min_ratio)
