#!/usr/bin/env python3
"""Literal archive CSV to XLSX fallback; the supplied Artifact Tool cannot export
on this host (WASM SIMD failure with both installed and bundled Node runtimes).
Uses already bundled openpyxl; no installation or new directory.
"""
import csv
import io
import sys
from pathlib import Path

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.formatting.rule import FormulaRule
from openpyxl.utils import get_column_letter
from PIL import Image, ImageDraw, ImageFont

source, output, preview = sys.argv[1:]
with Path(source).open(newline='') as f:
    matrix=list(csv.reader(f))
fields=matrix[0]
wb=Workbook()
ws=wb.active
ws.title='CATALOG'
numeric={'size_bytes','source_size_bytes','sequence','pages','width','height'}
for ri, values in enumerate(matrix,1):
    for ci, value in enumerate(values,1):
        number=ri>1 and fields[ci-1] in numeric and value!=''
        cell=ws.cell(ri,ci,int(value) if number else value)
        if not number: cell.data_type='s'
        cell.number_format='#,##0' if number else '@'
        cell.font=Font(name='Arial',size=10,bold=ri==1,color='FFFFFF' if ri==1 else '202020')
        cell.alignment=Alignment(vertical='top',wrap_text=True)
        if ri==1:cell.fill=PatternFill('solid',fgColor='344454')
    ws.row_dimensions[ri].height=32 if ri==1 else 78
widths={'relative_path':45,'object_key':13,'file_role':23,'title':48,'content_note':70,
        'description':75,'source_path':100,'source_url':100,'source_sha256':72,'final_sha256':72,
        'metadata_path':42,'master_relative_path':45,'object_url':58,'source_mtime_ns':25}
for ci,name in enumerate(fields,1):ws.column_dimensions[get_column_letter(ci)].width=widths.get(name,26)
ws.freeze_panes='C2'
ws.auto_filter.ref=ws.dimensions
ws.sheet_view.showGridLines=False
ws.conditional_formatting.add(f'E2:E{len(matrix)}',FormulaRule(
    formula=['ISNUMBER(SEARCH("Known mismatch",E2))'],
    fill=PatternFill('solid',fgColor='FFF1CF'),font=Font(color='753A00',bold=True)))
buffer=io.BytesIO()
wb.save(buffer)
buffer.seek(0)
restored=load_workbook(buffer,data_only=False)
actual=[[str(c.value) if c.value is not None else '' for c in r] for r in restored.active]
assert actual==matrix,'Saved Excel values differ from CSV'
assert all(c.data_type!='f' for r in restored.active for c in r),'Unexpected Excel formula'
if output!='--check':
    with Path(output).open('xb') as f:f.write(buffer.getvalue())

# Render the saved literal opening cells and their explicit widths/heights for
# visual inspection. This is a static catalogue view, not an Excel-engine test.
sheet=restored.active
font_path='/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf'
bold_path='/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf'
font=ImageFont.truetype(font_path,14)
bold=ImageFont.truetype(bold_path,14)
col_widths=[int(sheet.column_dimensions[get_column_letter(c)].width*7+5) for c in range(1,6)]
row_heights=[int(sheet.row_dimensions[r].height*4/3) for r in range(1,6)]
canvas=Image.new('RGB',(sum(col_widths),sum(row_heights)), 'white')
draw=ImageDraw.Draw(canvas)
y=0
for ri,rheight in enumerate(row_heights,1):
    x=0
    for ci,width in enumerate(col_widths,1):
        text=str(sheet.cell(ri,ci).value or '')
        warning=ci==5 and 'Known mismatch' in text
        fill='#344454' if ri==1 else '#FFF1CF' if warning else 'white'
        ink='white' if ri==1 else '#753A00' if warning else '#202020'
        chosen=bold if ri==1 or warning else font
        draw.rectangle((x,y,x+width,y+rheight),fill=fill)
        remaining=text
        lines=[]
        while remaining:
            cut=len(remaining)
            while cut>1 and draw.textlength(remaining[:cut],font=chosen)>width-12:cut-=1
            if cut<len(remaining) and ' ' in remaining[:cut]:cut=remaining.rfind(' ',0,cut)+1
            lines.append(remaining[:cut].strip())
            remaining=remaining[cut:].lstrip()
        assert len(lines)*17<=rheight-8,'Opening cell text would be clipped'
        for n,line in enumerate(lines):draw.text((x+6,y+4+n*17),line,font=chosen,fill=ink)
        x+=width
    draw.line((0,y+rheight-1,canvas.width,y+rheight-1),fill='#D5DADF')
    y+=rheight
canvas.save(preview)
print(f'CSV/XLSX values agree: {len(matrix)-1} rows, {len(fields)} columns; opening cells rendered.')
