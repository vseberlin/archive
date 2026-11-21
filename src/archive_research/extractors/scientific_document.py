"""R&D block routing and conservative checks; literal evidence remains separate."""
from copy import deepcopy
from html import escape
from html.parser import HTMLParser
import io
import re

from ..contracts import Region, require


def block_kind(label):
    if label in ('isolate_formula', 'formula', 'inline_formula'): return 'formula'
    if label == 'table': return 'table'
    if label in ('figure', 'illustration'): return 'illustration'
    if label in ('plain text', 'title', 'figure_caption', 'table_caption', 'table_footnote', 'formula_caption'):
        return 'text'
    return 'unresolved'


def route_blocks(blocks):
    """Retain all detector proposals, including unresolved/overlapping regions."""
    return [{'index':b['index'], 'region':b['region'], 'detector_label':b['label'],
             'kind':block_kind(b['label']), 'review_required':True,
             'handwriting':'not_assessed',
             'inline_formula_coverage':'not_assessed' if block_kind(b['label'])=='text' else 'not_applicable'}
            for b in blocks]


def intersects(a, b):
    return (min(a['x']+a['width'],b['x']+b['width'])>max(a['x'],b['x']) and
            min(a['y']+a['height'],b['y']+b['height'])>max(a['y'],b['y']))


def correction_proposal(words, replacements, protected_regions):
    """Apply explicit candidate edits to a copy; never mutate original OCR/ALTO.

    Formula regions protect all overlapping words even when OCR dropped digits.
    The caller must supply additional unit/name/identifier regions when needed.
    """
    for region in protected_regions: Region(**region)
    out=deepcopy(words); blocked=[]; applied=[]
    for edit in replacements:
        require(set(edit)=={'word','before','after'}, 'Explicit word edit required')
        i=edit['word']
        require(type(i) is int and 0<=i<len(words), 'Unknown OCR word')
        require(words[i]['text']==edit['before'] and isinstance(edit['after'],str) and edit['after'].strip(),
                'Correction does not match literal OCR')
        require(i not in [e['word'] for e in applied+blocked], 'Duplicate word correction')
        word=words[i]
        if (any(intersects(word['region'],b) for b in protected_regions) or
                any(c.isnumeric() for c in word['text']+edit['after']) or
                re.search(r'[\dµμ°=<>+−]',word['text']) or
                re.search(r'[\dµμ°=<>+−]',edit['after'])):
            blocked.append({**edit,'reason':'protected region or technical token'})
        else:
            out[i]['text']=edit['after'];applied.append(edit)
    return {'words':out,'applied':applied,'blocked':blocked,'metadata_adopted':False,
            'status':'unreviewed correction proposal; literal OCR unchanged'}


class TableParser(HTMLParser):
    """Accept only inert table markup; retain structure separately from cell text."""
    allowed={'table','thead','tbody','tfoot','tr','td','th','br'}
    def __init__(self):
        super().__init__(convert_charrefs=True);self.stack=[];self.output=[];self.cells=[];self.rows=0;self.tables=0
    def handle_starttag(self,tag,attrs):
        require(tag in self.allowed,'Unsupported table markup')
        require(all(k in ('rowspan','colspan') and tag in ('td','th') for k,v in attrs),'Unsupported table attribute')
        parent=self.stack[-1] if self.stack else None
        require((tag=='table' and parent is None and self.tables==0) or
                (tag in ('thead','tbody','tfoot') and parent=='table') or
                (tag=='tr' and parent in ('table','thead','tbody','tfoot')) or
                (tag in ('td','th') and parent=='tr') or
                (tag=='br' and parent in ('td','th')), 'Invalid table structure')
        values={}
        for k,v in attrs:
            require(k not in values and v is not None and v.isdecimal() and 1<=int(v)<=100,'Invalid cell span')
            values[k]=int(v)
        self.output.append('<'+tag+''.join(f' {k}="{v}"' for k,v in values.items())+'>')
        if tag=='table': self.tables+=1
        if tag=='tr':self.rows+=1
        if tag in ('td','th'):self.cells.append({'row':self.rows-1,'rowspan':values.get('rowspan',1),
                                               'colspan':values.get('colspan',1),'text':''})
        if tag!='br':self.stack.append(tag)
    def handle_endtag(self,tag):
        require(self.stack and self.stack[-1]==tag,'Unbalanced table markup')
        self.stack.pop();self.output.append('</'+tag+'>')
    def handle_data(self,data):
        require(not data.strip() or self.stack and self.stack[-1] in ('td','th'), 'Text outside table cell')
        self.output.append(escape(data))
        if self.stack and self.stack[-1] in ('td','th'):self.cells[-1]['text']+=data
    def handle_comment(self,data):raise ValueError('Unsupported table comment')


def table_proposal(raw):
    parser=TableParser()
    try:
        parser.feed(raw.strip());parser.close()
        require(not parser.stack and parser.tables==1 and parser.cells,'Incomplete table')
    except ValueError as error:
        return {'html':None,'structure_valid':False,'review_required':True,'reason':str(error)}
    return {'html':''.join(parser.output),'cells':parser.cells,'rows':parser.rows,
            'structure_valid':True,'review_required':True,
            'limitation':'Syntactic structure only; cells, spans and text are unverified.'}


def structured_table_proposal(raw):
    """Convert an explicit OTSL grid to inert HTML without repairing its structure.

    OTSL cell semantics follow the Docling/IBM dataset specification. The model's
    empty cells and rectangular spans are retained, including incorrect proposals.
    """
    if raw.lstrip().startswith('<table'):return table_proposal(raw)
    try:
        parts=re.split(r'(<(?:fcel|ecel|lcel|ucel|xcel|nl)>)',raw.strip())
        require(not parts[0].strip() and len(parts)>1,'Expected HTML table or OTSL')
        rows=[];row=[]
        for i in range(1,len(parts),2):
            kind=parts[i][1:-1];content=parts[i+1].strip()
            if kind=='nl':
                require(row and not content,'Invalid OTSL row delimiter');rows.append(row);row=[]
            else:
                require(kind=='fcel' or not content,'Text attached to an empty/merged cell')
                row.append((kind,content))
        require(not row and rows and len(rows)<=100,'Incomplete or oversized OTSL table')
        width=len(rows[0]);require(width<=100 and all(len(r)==width for r in rows),'Ragged OTSL grid')
        owners={};cells={}
        for y,row in enumerate(rows):
            for x,(kind,content) in enumerate(row):
                if kind in ('fcel','ecel'):
                    origin=(y,x);cells[origin]={'text':content,'positions':[]}
                elif kind=='lcel':
                    require(x>0,'Left merge without a cell');origin=owners[(y,x-1)]
                elif kind=='ucel':
                    require(y>0,'Up merge without a cell');origin=owners[(y-1,x)]
                else:
                    require(y>0 and x>0 and owners[(y-1,x)]==owners[(y,x-1)],'Invalid cross merge')
                    origin=owners[(y-1,x)]
                owners[(y,x)]=origin;cells[origin]['positions'].append((y,x))
        for (y,x),cell in cells.items():
            yy=max(a for a,b in cell['positions']);xx=max(b for a,b in cell['positions'])
            require(set(cell['positions'])=={(a,b) for a in range(y,yy+1) for b in range(x,xx+1)},
                    'Nonrectangular OTSL merge')
            cell['rowspan']=yy-y+1;cell['colspan']=xx-x+1
        html=['<table>']
        for y in range(len(rows)):
            html.append('<tr>')
            for x in range(width):
                if (y,x) not in cells:continue
                cell=cells[(y,x)];attrs=''.join(f' {k}="{cell[k]}"' for k in ('rowspan','colspan') if cell[k]>1)
                html.append('<td'+attrs+'>'+escape(cell['text'])+'</td>')
            html.append('</tr>')
        html.append('</table>');result=table_proposal(''.join(html));result['input_format']='OTSL'
        result['grid_columns']=width
        return result
    except (ValueError,KeyError) as error:
        return {'html':None,'structure_valid':False,'review_required':True,'reason':str(error)}


def render_formula(latex):
    """Render the supported MathText subset without running TeX or shell commands."""
    from matplotlib.mathtext import math_to_image
    from matplotlib.font_manager import FontProperties
    require(isinstance(latex,str) and 0<len(latex)<=2048,'Invalid formula length')
    expr=latex.strip()
    for left,right in [('$$','$$'),('\\[','\\]'),('\\(','\\)'),('$','$')]:
        if expr.startswith(left) and expr.endswith(right):expr=expr[len(left):-len(right)].strip();break
    require('$' not in expr and not re.search(r'\\(?:input|include|write|read|openout|def|newcommand|href|url|ce)\b',expr),
            'Unsupported formula command; retain raw LaTeX for review')
    out=io.BytesIO()
    math_to_image('$'+expr+'$',out,prop=FontProperties(size=24),dpi=160,format='png',color='black')
    return out.getvalue()


def formula_similarity(crop, rendered):
    """Deterministic foreground comparison; an uncalibrated review signal only."""
    import cv2
    import numpy as np
    from PIL import Image
    def mask(value):
        with Image.open(value) as image:
            a=np.array(image.convert('L'))
        _,ink=cv2.threshold(a,0,255,cv2.THRESH_BINARY_INV|cv2.THRESH_OTSU)
        y,x=np.where(ink>0)
        require(len(x)>0,'No foreground for formula comparison')
        ink=ink[y.min():y.max()+1,x.min():x.max()+1]
        scale=min(240/ink.shape[1],112/ink.shape[0])
        small=cv2.resize(ink,(max(1,round(ink.shape[1]*scale)),max(1,round(ink.shape[0]*scale))),interpolation=cv2.INTER_AREA)>127
        canvas=np.zeros((128,256),np.uint8);yy=(128-small.shape[0])//2;xx=(256-small.shape[1])//2
        canvas[yy:yy+small.shape[0],xx:xx+small.shape[1]]=small
        return canvas
    a=mask(crop);b=mask(io.BytesIO(rendered));best=0.
    for dy in range(-2,3):
        for dx in range(-2,3):
            moved=np.roll(b,(dy,dx),axis=(0,1));union=np.count_nonzero(a|moved)
            best=max(best,float(np.count_nonzero(a&moved)/union))
    return {'foreground_iou':best,'mismatch_flag':best<.5,'threshold':.5,
            'threshold_calibrated':False,'review_required':True,
            'limitation':'Font, alignment and paper noise affect this score; similar wrong subscripts can pass.'}
