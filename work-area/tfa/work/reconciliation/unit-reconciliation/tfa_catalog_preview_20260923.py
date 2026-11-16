"""Render a small real-cell workbook excerpt when Artifact Tool import is unavailable."""
import sys
import textwrap
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
from openpyxl import load_workbook
from openpyxl.utils import get_column_letter
from PIL import Image, ImageDraw, ImageFont

path, sheet, output = sys.argv[1:4]
wb = load_workbook(path, read_only=True)
ws = wb[sheet]
indices = [1] + (list(range(2, 6)) if len(sys.argv) == 4 else list(range(ws.max_row - 6, ws.max_row + 1)))
with zipfile.ZipFile(path) as z:
    tree = ET.fromstring(z.read('xl/worksheets/sheet' + str(wb.sheetnames.index(sheet) + 1) + '.xml'))
ns = {'m': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
columns = tree.findall('m:cols/m:col', ns)
row_sizes = {int(r.get('r')): float(r.get('ht', '15')) for r in tree.findall('m:sheetData/m:row', ns) if int(r.get('r')) in indices}
widths = [round(next((float(c.get('width')) for c in columns if int(c.get('min')) <= col <= int(c.get('max'))), 13) * 7 + 5) for col in range(1, 5)]
heights = [round(row_sizes[r] * 4 / 3) for r in indices]
samples = {row[0].row: row for row in ws.iter_rows(max_col=4) if row[0].row in indices}
canvas = Image.new('RGB', (sum(widths), sum(heights)), 'white')
y = 0
for index, height in zip(indices, heights):
    x = 0
    for col, width in enumerate(widths, 1):
        cell = samples[index][col - 1]
        fill = '#24445B' if index == 1 and cell.fill.patternType else ('#F0F5F9' if index % 2 == 0 else 'white')
        tile = Image.new('RGB', (width, height), fill)
        draw = ImageDraw.Draw(tile)
        font_path = '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf' if cell.font.bold else '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'
        font = ImageFont.truetype(font_path, round((cell.font.sz or 11) * 4 / 3))
        value = str(cell.value or '')
        if cell.alignment.wrap_text:
            value = '\n'.join(textwrap.wrap(value, max(1, int((width - 10) / (font.size * .57)))))
        draw.multiline_text((5, 4), value, fill='white' if index == 1 and cell.fill.patternType else '#202124', font=font, spacing=2)
        canvas.paste(tile, (x, y))
        x += width
    y += height
canvas.save(output)
print(output)
wb.close()
