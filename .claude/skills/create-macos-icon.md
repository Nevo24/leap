# Create macOS App Icon

Convert a PNG image to a proper macOS .icns icon file for Leap Monitor.

## Prerequisites

- ImageMagick installed (`brew install imagemagick`)
- Source PNG image

## Recommended Method: macOS Photos "Copy Subject"

This is the **best method** to get a clean icon without white background issues in the Dock.

### 1. Extract Subject Using Photos App

1. Open the source image in **Photos** app (double-click or drag into Photos)
2. Right-click on the image
3. Select **"Copy Subject"** - this uses macOS AI to extract the main content with proper transparency
4. Open **Preview** app
5. Press **Cmd+N** (New from Clipboard)
6. **Cmd+S** to save as PNG to `~/Downloads/` or directly to `assets/`

This method ensures:
- Clean edges with proper anti-aliasing
- True transparency (no hidden white/gray pixels)
- Works perfectly in macOS Dock

### 2. Create Icon Set

```bash
cd /Users/Nevo.Mashiach/workspace/leap/assets

# Copy your extracted image
cp ~/Downloads/your-extracted-image.png leap-icon.png

# Create iconset directory
rm -rf leap-icon.iconset
mkdir leap-icon.iconset

# Generate all required sizes
for size in "16 16x16" "32 16x16@2x" "32 32x32" "64 32x32@2x" "128 128x128" "256 128x128@2x" "256 256x256" "512 256x256@2x" "512 512x512" "1024 512x512@2x"; do
  pixels=$(echo $size | cut -d' ' -f1)
  name=$(echo $size | cut -d' ' -f2)
  magick leap-icon.png -resize ${pixels}x${pixels} "leap-icon.iconset/icon_${name}.png"
done

# Convert to ICNS
iconutil -c icns leap-icon.iconset

# Cleanup
rm -rf leap-icon.iconset
```

### 3. Install and Clear Cache

```bash
cd /Users/Nevo.Mashiach/workspace/leap

# Rebuild app
make install-monitor

# Clear macOS icon cache (REQUIRED!)
sudo rm -rf /Library/Caches/com.apple.iconservices.store
rm -rf ~/Library/Caches/com.apple.iconservices
killall Dock
```

## Alternative: Full-Bleed Icon (No Transparency Needed)

If your icon fills the entire canvas edge-to-edge (like the current Leap space icon), you don't need transparency - macOS will apply its own rounded mask.

Just ensure the image is square (1024x1024 recommended) and follow steps 2-3 above.

## Alternative: ImageMagick Background Removal

If you can't use Photos app, try ImageMagick flood fill:

```bash
# Check corner pixel color
magick input.png -format "%[pixel:p{0,0}]" info:

# Remove background from corners only (preserves center)
magick input.png -fuzz 5% -fill none -floodfill +0+0 "#CORNER_COLOR" output.png
```

**Warning**: This method often leaves subtle edge artifacts that cause white backgrounds in Dock.

## Troubleshooting

### White background in Dock

1. **Best fix**: Re-extract using Photos app "Copy Subject" method
2. Clear icon cache (step 3 above)
3. Restart Finder: `killall Finder`

### Icon not updating

```bash
sudo rm -rf /Library/Caches/com.apple.iconservices.store
rm -rf ~/Library/Caches/com.apple.iconservices
killall Dock
killall Finder
```

### Image not square

```bash
# Force to square (may distort)
magick input.png -resize 1024x1024! output.png

# Or resize with padding (preserves aspect)
magick input.png -resize 1024x1024 -gravity center -background none -extent 1024x1024 output.png
```

## Icon Requirements

macOS .icns format requires these sizes:
- 16x16, 16x16@2x (32px)
- 32x32, 32x32@2x (64px)
- 128x128, 128x128@2x (256px)
- 256x256, 256x256@2x (512px)
- 512x512, 512x512@2x (1024px)

## Quick Reference

```bash
# Full workflow after extracting via Photos "Copy Subject"
cd assets
cp ~/Downloads/extracted-icon.png leap-icon.png
rm -rf leap-icon.iconset && mkdir leap-icon.iconset
for size in "16 16x16" "32 16x16@2x" "32 32x32" "64 32x32@2x" "128 128x128" "256 128x128@2x" "256 256x256" "512 256x256@2x" "512 512x512" "1024 512x512@2x"; do
  pixels=$(echo $size | cut -d' ' -f1); name=$(echo $size | cut -d' ' -f2)
  magick leap-icon.png -resize ${pixels}x${pixels} "leap-icon.iconset/icon_${name}.png"
done
iconutil -c icns leap-icon.iconset && rm -rf leap-icon.iconset

# Install and clear cache
cd .. && make install-monitor
sudo rm -rf /Library/Caches/com.apple.iconservices.store
rm -rf ~/Library/Caches/com.apple.iconservices
killall Dock
```
