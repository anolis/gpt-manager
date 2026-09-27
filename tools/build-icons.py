"""Regenerate checked-in icon exports. Requires ImageMagick and Pillow."""
import shutil
import subprocess
from pathlib import Path
from PIL import Image

root = Path(__file__).resolve().parents[1]
assets = root / 'ui/assets'
magick = shutil.which('magick') or shutil.which('convert')
if not magick: raise SystemExit('Install ImageMagick to regenerate the icon exports.')
subprocess.run([magick, '-background', 'none', '-density', '192', str(assets/'icon.svg'), '-resize', '1024x1024', str(assets/'icon.png')], check=True)
with Image.open(assets/'icon.png') as image:
    image.save(assets/'icon.ico', sizes=[(n,n) for n in (16,24,32,48,64,128,256)])
    image.resize((180,180), Image.Resampling.LANCZOS).save(root/'docs/apple-touch-icon.png')
    image.resize((32,32), Image.Resampling.LANCZOS).save(root/'docs/favicon.png')
shutil.copyfile(assets/'icon.svg', root/'docs/favicon.svg')
shutil.copyfile(assets/'icon.ico', root/'docs/favicon.ico')
