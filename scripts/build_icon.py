from pathlib import Path

from PIL import Image

project_root = Path(__file__).resolve().parents[1]
source = project_root / "assets" / "yunqi-ai-live-icon-v2.png"
target = project_root / "assets" / "yunqi-ai-live-icon-v2.ico"

with Image.open(source) as image:
    icon = image.convert("RGBA")
    icon.save(
        target,
        format="ICO",
        sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
    )

print(target)
