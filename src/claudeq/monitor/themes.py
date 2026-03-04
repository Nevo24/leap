"""Visual theme definitions and manager for ClaudeQ Monitor.

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

    # Status indicator colors (QColor args as hex)
    status_running: str = '#4caf50'
    status_permission: str = '#ff9800'
    status_question: str = '#64b5f6'
    status_interrupted: str = '#ffd54f'
    status_idle: str = '#ffffff'  # idle / default text


# ---------------------------------------------------------------------------
#  Built-in themes
# ---------------------------------------------------------------------------

_MIDNIGHT = Theme(
    name='Midnight',
    is_dark=True,
    window_bg='#1e1e1e',
    cell_bg='#1e1e1e',
    cell_bg_alt='#232323',
    hover_bg='rgba(255, 255, 255, 20)',
    text_primary='#e0e0e0',
    text_secondary='#999999',
    text_muted='#999999',
    border_solid='#ffffff',
    border_subtle='rgba(255, 255, 255, 50)',
    accent_green='#00ff00',
    accent_red='#ff4444',
    accent_orange='#ffa500',
    accent_blue='#5B9BD5',
    accent_yellow='#ffd54f',
    popup_bg='#2b2b2b',
    popup_border='#555555',
    input_bg='#2a2a3a',
    input_border='#555555',
    input_focus_border='#5B9BD5',
    icon_color='#aaaaaa',
    header_bg='transparent',
    status_idle='#ffffff',
)

_OCEAN = Theme(
    name='Ocean',
    is_dark=True,
    window_bg='#1a2332',
    cell_bg='#1e2a3a',
    cell_bg_alt='#1a2636',
    hover_bg='rgba(100, 180, 255, 25)',
    text_primary='#d4dce8',
    text_secondary='#7a8fa6',
    text_muted='#5a7090',
    border_solid='#5e82a4',
    border_subtle='rgba(74, 106, 138, 80)',
    accent_green='#50fa7b',
    accent_red='#ff5555',
    accent_orange='#ffb86c',
    accent_blue='#8be9fd',
    accent_yellow='#f1fa8c',
    popup_bg='#1e2a3a',
    popup_border='#4a6a8a',
    input_bg='#162030',
    input_border='#4a6a8a',
    input_focus_border='#8be9fd',
    icon_color='#7a8fa6',
    header_bg='transparent',
    status_running='#50fa7b',
    status_permission='#ffb86c',
    status_question='#8be9fd',
    status_interrupted='#f1fa8c',
    status_idle='#d4dce8',
)

_MONOKAI = Theme(
    name='Monokai',
    is_dark=True,
    window_bg='#272822',
    cell_bg='#272822',
    cell_bg_alt='#2d2e27',
    hover_bg='rgba(255, 255, 255, 18)',
    text_primary='#f8f8f2',
    text_secondary='#a6a68a',
    text_muted='#75715e',
    border_solid='#928e78',
    border_subtle='rgba(117, 113, 94, 80)',
    accent_green='#a6e22e',
    accent_red='#f92672',
    accent_orange='#fd971f',
    accent_blue='#66d9ef',
    accent_yellow='#e6db74',
    popup_bg='#3e3d32',
    popup_border='#75715e',
    input_bg='#3e3d32',
    input_border='#75715e',
    input_focus_border='#66d9ef',
    icon_color='#a6a68a',
    header_bg='transparent',
    status_running='#a6e22e',
    status_permission='#fd971f',
    status_question='#66d9ef',
    status_interrupted='#e6db74',
    status_idle='#f8f8f2',
)

_NORD = Theme(
    name='Nord',
    is_dark=True,
    window_bg='#2e3440',
    cell_bg='#2e3440',
    cell_bg_alt='#333a47',
    hover_bg='rgba(136, 192, 208, 20)',
    text_primary='#d8dee9',
    text_secondary='#81a1c1',
    text_muted='#616e88',
    border_solid='#616e84',
    border_subtle='rgba(76, 86, 106, 80)',
    accent_green='#a3be8c',
    accent_red='#bf616a',
    accent_orange='#d08770',
    accent_blue='#88c0d0',
    accent_yellow='#ebcb8b',
    popup_bg='#3b4252',
    popup_border='#4c566a',
    input_bg='#3b4252',
    input_border='#4c566a',
    input_focus_border='#88c0d0',
    icon_color='#81a1c1',
    header_bg='transparent',
    status_running='#a3be8c',
    status_permission='#d08770',
    status_question='#88c0d0',
    status_interrupted='#ebcb8b',
    status_idle='#d8dee9',
)

_SOLARIZED_DARK = Theme(
    name='Solarized Dark',
    is_dark=True,
    window_bg='#002b36',
    cell_bg='#002b36',
    cell_bg_alt='#073642',
    hover_bg='rgba(131, 148, 150, 20)',
    text_primary='#839496',
    text_secondary='#657b83',
    text_muted='#586e75',
    border_solid='#72909a',
    border_subtle='rgba(88, 110, 117, 80)',
    accent_green='#859900',
    accent_red='#dc322f',
    accent_orange='#cb4b16',
    accent_blue='#268bd2',
    accent_yellow='#b58900',
    popup_bg='#073642',
    popup_border='#586e75',
    input_bg='#073642',
    input_border='#586e75',
    input_focus_border='#268bd2',
    icon_color='#657b83',
    header_bg='transparent',
    status_running='#859900',
    status_permission='#cb4b16',
    status_question='#268bd2',
    status_interrupted='#b58900',
    status_idle='#839496',
)

_DAWN = Theme(
    name='Dawn',
    is_dark=False,
    window_bg='#f5f5f5',
    cell_bg='#ffffff',
    cell_bg_alt='#f0f0f0',
    hover_bg='rgba(0, 0, 0, 15)',
    text_primary='#1e1e1e',
    text_secondary='#555555',
    text_muted='#888888',
    border_solid='#777777',
    border_subtle='rgba(0, 0, 0, 30)',
    accent_green='#2e7d32',
    accent_red='#c62828',
    accent_orange='#e65100',
    accent_blue='#1565c0',
    accent_yellow='#f57f17',
    popup_bg='#ffffff',
    popup_border='#cccccc',
    input_bg='#ffffff',
    input_border='#cccccc',
    input_focus_border='#1565c0',
    icon_color='#555555',
    header_bg='transparent',
    status_running='#2e7d32',
    status_permission='#e65100',
    status_question='#1565c0',
    status_interrupted='#f57f17',
    status_idle='#1e1e1e',
)

# Ordered dict preserving insertion order for combo box display
THEMES: dict[str, Theme] = {
    'Midnight': _MIDNIGHT,
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
