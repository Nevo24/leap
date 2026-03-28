# Add a New Monitor Theme

Guide for adding a new visual theme to the Leap Monitor GUI.

## Overview

Themes are defined as frozen `Theme` dataclasses in `src/leap/monitor/themes.py`. Each theme provides a complete color palette. The monitor applies colors via `QPalette` + minimal QSS. A themed logo variant is loaded automatically based on the theme name.

## Steps

### 1. Design the Color Palette

Every theme needs these color groups:

| Group | Fields | Notes |
|-------|--------|-------|
| **Backgrounds** | `window_bg`, `cell_bg`, `cell_bg_alt`, `hover_bg`, `header_bg`, `popup_bg` | `hover_bg` is an `rgba()` string. `cell_bg_alt` is for alternating rows. `header_bg` is the darkest (logo bar + table header). |
| **Text** | `text_primary`, `text_secondary`, `text_muted` | Primary = main content, secondary = labels/hints, muted = disabled/dim. Ensure WCAG 4.5:1 contrast against `cell_bg`. |
| **Borders** | `border_solid`, `border_subtle` | Solid = group separators (2px). Subtle = intra-group (1px, `rgba()` string). |
| **Accents** | `accent_green`, `accent_red`, `accent_orange`, `accent_blue`, `accent_yellow` | `accent_blue` is used for the "Add Session" button and drop indicator. `accent_green` for running status. `accent_red` for close/delete. |
| **Inputs** | `input_bg`, `input_border`, `input_focus_border` | Focus border = the theme's signature accent color. |
| **Buttons** | `button_bg`, `button_hover_bg`, `button_border` | Used for generic toolbar buttons (Settings, Notes, Presets). |
| **Scrollbar** | `scrollbar_bg`, `scrollbar_handle`, `scrollbar_handle_hover` | Match window_bg for track, border_solid range for handle. |
| **Status** | `status_running`, `status_permission`, `status_input`, `status_interrupted`, `status_idle` | Functional colors — keep green/orange/blue/yellow semantics. `status_idle` matches `text_primary`. |
| **Misc** | `icon_color`, `is_dark` | `icon_color` = SVG icon tint. `is_dark` controls macOS appearance mode. |

**Design tips:**
- For dark themes: backgrounds should have subtle warm/cool undertones (not pure gray) to give character
- The `accent_blue` field is the primary action color (button outlines, focus rings) — name it "blue" but use whatever fits the theme
- `hover_bg` should be a very low-alpha version of the accent color (10-20 alpha)
- `border_subtle` should be a low-alpha version too (12-22 alpha)

### 2. Add the Theme Definition

In `src/leap/monitor/themes.py`, add a new `_THEME_NAME` variable before the `THEMES` dict:

```python
_MYTHEME = Theme(
    name='MyTheme',
    is_dark=True,  # or False for light themes
    window_bg='#...',
    # ... all fields ...
)
```

Then add it to the `THEMES` ordered dict (position determines combo box order):

```python
THEMES: dict[str, Theme] = {
    'Leap': _LEAP,
    'MyTheme': _MYTHEME,  # Add here
    # ... existing themes ...
}
```

### 3. Create the Logo Variant

Each theme needs a logo file at `assets/leap-text-<suffix>.png` where `<suffix>` is the theme name lowercased with spaces replaced by hyphens (e.g., `leap-text-mytheme.png`, `leap-text-solarized-dark.png`).

The logo is a 980x251 RGBA PNG with two colors:
- **Main text** (L, E, P letters): should match or complement `text_primary`
- **Accent** (A letter): should match the theme's signature accent color

To create a new logo, recolor an existing one using PIL/numpy:

```python
from PIL import Image
import numpy as np

# Use any existing theme logo as a base
img = Image.open('assets/leap-text-midnight.png')
data = np.array(img, dtype=np.float64)

# Midnight's colors: main=#e2e4f0, accent=#60a5fa
old_main = np.array([0xe2, 0xe4, 0xf0], dtype=np.float64)
old_accent = np.array([0x60, 0xa5, 0xfa], dtype=np.float64)
new_main = np.array([...], dtype=np.float64)   # your text_primary
new_accent = np.array([...], dtype=np.float64)  # your accent color

rgb = data[:, :, :3]
alpha = data[:, :, 3:]
threshold = 40.0

for old_color, new_color in [(old_main, new_main), (old_accent, new_accent)]:
    dist = np.sqrt(np.sum((rgb - old_color) ** 2, axis=2))
    mask = (dist < threshold) & (data[:, :, 3] > 30)
    for ch in range(3):
        offset = rgb[:, :, ch][mask] - old_color[ch]
        rgb[:, :, ch][mask] = np.clip(new_color[ch] + offset, 0, 255)

result = np.concatenate([rgb, alpha], axis=2).astype(np.uint8)
Image.fromarray(result, 'RGBA').save('assets/leap-text-mytheme.png')
```

**Note:** Logo files are auto-included via `glob('assets/leap-text*.png')` in `setup.py`, so no `setup.py` change is needed.

### 4. Update CLAUDE.md

Update the theme count and list in the Theming bullet point under "Adding Features":

```
Nine built-in themes: Leap, Amber, Midnight, Cosmos, Ocean, Monokai, Nord, Solarized Dark, Dawn.
```

### 5. Set as Default (Optional)

To make the new theme the default:

1. In `themes.py`: change `_current_theme_name: str = 'Leap'` to your theme name
2. In `app.py` (`main()`): change the `prefs.get('theme', 'Leap')` fallback
3. In `settings_dialog.py`: change the `current_theme_name: str = 'Leap'` default parameter
4. In `table_builder_mixin.py`: change the `self._prefs.get('theme', 'Leap')` fallback

### Checklist

- [ ] Theme definition added to `themes.py` with all required fields
- [ ] Theme added to `THEMES` dict in desired position
- [ ] Logo PNG created at `assets/leap-text-<suffix>.png` (980x251 RGBA)
- [ ] CLAUDE.md theme count and list updated
- [ ] (If default) All four default references updated
- [ ] Test: switch to the theme in Settings and verify all UI elements render correctly
