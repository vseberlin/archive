"""Shared research guidance and editable routing; never catalogue identification."""
import re
from .common import require

# Material presets for extraction, distinct from the research-subject guidance
# below. No preset is a claim of independently measured recognition accuracy.
# Increment the version whenever its fixed settings or scope change.
OCR_PROFILES = {
    'brigadebuch': {
        'version': 1, 'label': 'Brigadebuch and mixed album pages',
        'extractors': ['ocr', 'document-layout', 'document-text', 'handwriting'],
        'workflow': ['inspect mixed page regions', 'retain photographs and caption links',
                     'route printed text to OCR and selected handwritten lines to handwriting recognition'],
        'checks': ['region and line coverage', 'different hands', 'names and dates',
                   'caption associations', 'omissions and unsupported symbols'],
        'limits': ['Prepared regions are explicit proposals, not automatic handwriting detection.',
                   'Line recognition does not qualify full-page reading order or formulas.'],
    },
    'clean_print': {
        'version': 1, 'label': 'Clean printed text',
        'extractors': ['pdf-text', 'ocr'],
        'workflow': ['inspect existing PDF text when present', 'whole-page OCR when needed'],
        'checks': ['text-layer correctness', 'missing text', 'paragraph order',
                   'source coordinates', 'names, dates, numbers and identifiers'],
        'limits': ['Complex columns, scene text and handwriting require another profile.'],
    },
    'historical_newspaper': {
        'version': 1, 'label': 'Historical newspaper',
        'extractors': ['pdf-text', 'ocr', 'document-layout', 'document-text'],
        'workflow': ['inspect existing PDF text', 'detect page regions',
                     'recognize text/caption crops', 'assemble explicit reading order'],
        'checks': ['omitted columns and captions', 'article order and continuations',
                   'image texture excluded from text', 'caption associations',
                   'source coordinates', 'names, dates, numbers and identifiers'],
        'limits': ['Whole-page OCR is a baseline, not an assembled article reading.',
                   'Fraktur and other scripts are not qualified by ordinary German language data.'],
    },
    'rd_reports': {
        'version': 1, 'label': 'Typewritten and R&D reports',
        'extractors': ['pdf-text', 'ocr', 'document-layout', 'document-text'],
        'workflow': ['inspect existing PDF text', 'detect page regions',
                     'recognize text/caption crops', 'assemble explicit reading order'],
        'checks': ['faint or broken type', 'captions separated from illustrations',
                   'source coordinates', 'dates, quantities and type identifiers',
                   'forms and tables retained with explicit limitations'],
        'limits': ['Formula recognition and correction are deferred; preserve raw readings.',
                   'No automatic handwriting reading or verified table-cell structure is claimed.'],
    },
}


def ocr_profile(name):
    """Return a detached, versioned candidate preset for explicit task selection."""
    from copy import deepcopy
    require(isinstance(name, str) and name in OCR_PROFILES, 'Unknown OCR material profile')
    return {'name': name, **deepcopy(OCR_PROFILES[name]),
            'accuracy_qualified': False, 'postprocessing': 'raw text; spelling is a separate stage',
            'ocr_settings': {'dpi': 300, 'languages': 'deu+eng', 'image_preparation': 'gray-v1',
                             'page_psm': 3, 'region_psm': 6},
            'layout_preparation': 'local-red-v1' if name == 'rd_reports' else 'original',
            'language_models': 'Exact traineddata fingerprints are bound in each OCR recipe; no download or substitution.'}


def resolve_ocr_task(task):
    """Expand a material preset inside the existing finite extraction task contract.

    Unprofiled tasks retain their existing recipes. Named profiles deliberately
    reject preprocessing experiments and deferred recognizers; explicit model,
    crop and reading-order bindings remain the existing adapters' responsibility.
    """
    from copy import deepcopy
    if 'ocr_profile' not in task:
        return task
    selected = task['ocr_profile']
    require(isinstance(selected, dict) and set(selected) == {'name', 'version'},
            'OCR profile needs an explicit name and version')
    profile = ocr_profile(selected['name'])
    require(type(selected['version']) is int and selected['version'] == profile['version'],
            'Unsupported OCR profile version')
    extractor = task['extractor']
    require(extractor in profile['extractors'], 'Extractor outside OCR profile; specialist work remains separate')
    result = deepcopy(task)
    settings = result.setdefault('settings', {})
    require(isinstance(settings, dict), 'OCR settings must be an object')
    if extractor == 'handwriting':
        require(profile['name']=='brigadebuch', 'Handwriting is a separately bound processor')
        return result
    fixed = {'dpi': 300}
    if extractor == 'ocr':
        fixed.update(psm=6 if task.get('region') is not None else 3, image_preparation='gray-v1')
        settings.setdefault('languages', 'deu+eng')
        require(not set(settings) & {'normalization', 'thresholding_method', 'character_boxes'},
                'OCR profile uses the frozen grayscale reading; experiments require an unprofiled task')
    elif extractor == 'document-layout' and profile['name'] == 'rd_reports':
        fixed['normalization'] = 'local-red-v1'
    else:
        require('normalization' not in settings, 'OCR profile uses the original layout image')
    if extractor == 'document-text':
        require(any(k in settings for k in ('annotation', 'order_origin', 'order_method')),
                'OCR profile assembly requires an explicit reading-order basis')
    for key, value in fixed.items():
        require(key not in settings or settings[key] == value, 'Setting conflicts with OCR profile: '+key)
        settings[key] = value
    return result

ROUTES = {
    'buildings_places': {'label':'Buildings and places',
        'clues':r'fassade|gebäude|gewerbebau|gewerbehof|fabrikhof|kirch|straße|strasse|schornstein|turm|bauwerk|brücke|backstein|architektur',
        'guidance':'Compare facade axes, roofs, ornament, neighbours and street geometry. Seek historic AND modern exterior views, maps and architectural records. Common building style alone does not identify a site.'},
    'machinery_industry': {'label':'Machinery and industrial interiors',
        'clues':r'maschin|walzen|förderband|verpackungsanlage|riemen|drehmaschine|werkzeug|messgerät|mikroskop|elektronenröhre|produktionsanlage|vermittlungsanlage|lagerhalle|lagerraum|lagerplatz',
        'guidance':'Identify function and mechanism before maker/model or factory. Compare housings, controls, feed/discharge, drives and marks. Seek technical museums, manuals, drawings and manufacturer catalogues. For storage scenes without machinery compare layout, containers, goods and inscriptions instead. Building exteriors do not identify interior machinery.'},
    'objects_art': {'label':'Objects and artworks',
        'clues':r'skulptur|plastik|denkmal|statue|kunstwerk|bronze|schnitz|gemälde|büste|buste|schiff|automobil|lieferwagen|pferdewagen|fuhrwerk|werbewagen',
        'guidance':'Compare shape, proportions, material, inscriptions and maker details; for artworks also pose and base, for vehicles bodywork and fittings. Seek collection records, manufacturer catalogues, sculpture inventories and exhibitions. Object identity does not prove photograph location or date.'},
    'events_celebrations': {'label':'Events and celebrations',
        'clues':r'umzug|fest(?:ival|zug|akt|lich)|feier|zeremonie|kundgebung|demonstration|vorführung|vorfuehrung|ehrung|parade|solidarität|überreich|ueberreich',
        'guidance':'Read banners, posters and decorations; inspect activity, venue and sequence. Seek contemporary newspapers, programmes and photographs of the event. Distinguish recurring events and decorations reused in different years. Event date does not automatically date this photograph.'},
    'people_activity': {'label':'People and everyday life',
        'clues':r'person|porträt|portrait|kinder|mann|männer|frau|beschäftigte|arbeiter|belegschaft|publikum|besucher|büroarbeit|schreibtisch|anprobe|essen|verkehrsübung',
        'guidance':'Research activity, tools, clothing, surroundings and written context. Seek documentary scenes and archive captions. Do not identify a person from their face; names require independently bound documentary evidence. Avoid unsupported occupation, affiliation or occasion claims.'},
    'documents_signs': {'label':'Documents, signs and displays',
        'clues':r'urkunde|gedenktafel|plakat|inschrift|werbeschild|ladungsschild|schriftzug|dokument|aushang|ausweis|buchseite|buchumschlag',
        'guidance':'Transcribe only legible words, preserving uncertain characters. Search distinctive exact phrases, logos, typography, titles and dates. Seek scans, poster catalogues and publications. Printed dates and later reprints do not automatically date this photograph.'},
    'unresolved': {'label':'Unclear / mixed subject', 'clues':r'(?!)',
        'guidance':'Inspect the image before choosing a route. Start with its most distinctive visible clue; keep multiple subject interpretations open. Do not search a generic scene description repeatedly.'},
}

# Profiles belong to the existing subject routes. They can be combined on one
# photograph and never assign object identity or replace reviewer routing.
ROUTES['buildings_places']['profiles'] = {
    'buildings': {
        'label': 'Buildings',
        'extract_first': ['signs and street numbers', 'facade and roof details', 'neighbouring structures'],
        'compare_against': ['whole scenes', 'facade/detail crops', 'documented place and period'],
        'limitations': ['Similar architecture and repetitive windows do not establish identity.',
                        'Alterations or viewpoint changes can hide a real match; failed geometry is inconclusive.'],
    },
}
ROUTES['people_activity']['profiles'] = {
    'people': {
        'label': 'People',
        'extract_first': ['person/face regions and group position', 'literal captions and names', 'event/workplace text'],
        'compare_against': ['explicit caption-position mappings', 'published captions and documentary records'],
        'limitations': ['Detection supplies no name; no face-based identity matching.',
                        'Unordered names and nearby text do not identify individual pictured people.'],
    },
}
ROUTES['machinery_industry']['profiles'] = {
    'equipment': {
        'label': 'Equipment',
        'extract_first': ['whole apparatus', 'maker/model/serial/inventory markings separately', 'plates, controls, connectors and assemblies'],
        'compare_against': ['catalogue illustrations', 'manuals', 'technical photographs and reports'],
        'limitations': ['Similar housings may contain different models.',
                        'A component plate may not describe the whole apparatus; model is not specimen identity.'],
    },
    'tubes': {
        'label': 'Tubes',
        'extract_first': ['logo and complete nearby type inscription, including small letters and suffixes',
                          'separate long numeric markings, retaining their positions relative to the logo/type',
                          'base/pins', 'envelope and visible internal structure'],
        'compare_against': ['manufacturer-, period- and tube-family-specific marking rules with source pages',
                            'documented type-code dictionaries', 'datasheets', 'identified reference images'],
        'limitations': ['Preserve uncertain OCR; 0/O and 1/I alternatives are suggestions only.',
                        'Do not force letters into an unresolved inscription or apply a later scheme to an earlier logo.',
                        'Type, type-series, batch/date and specimen numbers are distinct; prefix plus number is not proven unique without an allocation rule.',
                        'Similar appearance does not establish type or specimen; dimensions require a scale.'],
    },
}
ROUTES['documents_signs']['profiles'] = {
    'rd_reports': {
        'label': 'R&D reports',
        'extract_first': ['page layout: text, formula, table and illustration regions',
                          'word-positioned literal text and captions with nested formula regions',
                          'retain uncertain formula crops without further formula recognition or correction',
                          'handwritten additions, displaced subscripts and unresolved regions'],
        'compare_against': ['source crops including small subscripts, units and technical identifiers',
                            'literal OCR before correction'],
        'limitations': ['Never spell-correct protected formula regions, numbers, units or identifiers.',
                        'Formula recognition/correction is deferred by the user.',
                        'Handwriting requires review; table structure does not verify cell text.',
                        'Resampling blurred copies cannot restore missing scan detail.'],
    },
}


def research_profiles(names):
    """Resolve explicit combinable profiles from the sole subject-route owner."""
    from copy import deepcopy
    require(isinstance(names, list) and names and all(isinstance(n, str) for n in names)
            and len(names) == len(set(names)), 'Select distinct research profiles')
    available = {name: {'route': route, **profile}
                 for route, data in ROUTES.items() for name, profile in data.get('profiles', {}).items()}
    require(all(name in available for name in names), 'Unknown research profile')
    return {name: deepcopy(available[name]) for name in names}

ROUTING_SCHEMA = {'type':'object','properties':{
    'primary':{'type':'string','enum':list(ROUTES)},
    'secondary':{'type':'array','items':{'type':'string','enum':list(ROUTES)},'maxItems':3},
    'confidence':{'type':'string','enum':['low','medium','high']},
    'reason':{'type':'string'}},'required':['primary','secondary','confidence','reason'],'additionalProperties':False}


def validate_override(value):
    if value is None: return None
    require(isinstance(value,dict) and set(value)=={'primary','secondary'}, 'Invalid research routing override')
    require(isinstance(value['primary'],str) and value['primary'] in ROUTES, 'Unknown research route')
    secondary=value['secondary']
    require(isinstance(secondary,list) and len(secondary)<=3 and all(isinstance(x,str) and x in ROUTES and x!=value['primary'] for x in secondary)
            and len(set(secondary))==len(secondary), 'Invalid secondary research routes (at most three)')
    return {'primary':value['primary'],'secondary':list(secondary)}


def classify(title='', description='', inscriptions=()):
    scores={}; evidence={}
    for key,route in ROUTES.items():
        title_hits=set(re.findall(route['clues'],title.casefold()))
        body_hits=set(re.findall(route['clues'],description.casefold()))
        scores[key]=len(title_hits)*4+min(len(body_hits),4)
        evidence[key]=sorted(title_hits|body_hits)
    # People often accompany a more diagnostic subject; keep them as a secondary route.
    if any(v>=4 for k,v in scores.items() if k not in ('people_activity','unresolved')):
        scores['people_activity']=min(scores['people_activity'],3)
    if inscriptions: scores['documents_signs']+=2
    ordered=sorted(scores,key=lambda k:-scores[k]);primary=ordered[0] if scores[ordered[0]] else 'unresolved'
    secondary=[k for k in ordered if k!=primary and scores[k]>0][:3]
    gap=scores[primary]-(scores[secondary[0]] if secondary else 0)
    confidence='medium' if scores[primary]>=4 and gap>=2 else 'low'
    return {'primary':primary,'secondary':secondary,'confidence':confidence,
            'reason':'Existing description clues: '+', '.join(evidence[primary]) if evidence[primary] else 'No distinctive category clue in existing descriptions',
            'basis':'description_only','reviewer_override':False}


def routing_for(record, decision=None):
    result=record.get('result',{})
    automatic=classify(result.get('title',{}).get('value') or '',result.get('description',{}).get('value') or '',result.get('inscriptions',[]))
    reviewed=record.get('reference_check',{}).get('research_routing')
    if isinstance(reviewed,dict) and reviewed.get('primary') in ROUTES and all(s in ROUTES for s in reviewed.get('secondary',[])):
        automatic={**automatic,**reviewed,'basis':'visual_discovery','reviewer_override':False}
    override=validate_override((decision or {}).get('metadata_proposal',{}).get('research_routing'))
    if override:
        return {**automatic,**override,'confidence':'high','reason':'Reviewer-selected research route; not an identification',
                'basis':'reviewer_override','reviewer_override':True}
    return automatic


def guidance(routing):
    keys=list(dict.fromkeys([routing['primary']]+routing.get('secondary',[])))
    lines=[ROUTES[k]['label']+': '+ROUTES[k]['guidance'] for k in keys]
    if routing['primary']=='unresolved' or routing.get('confidence')=='low':
        lines.append('If pixels contradict this grouping, select a better route. Available routes: '+', '.join(ROUTES))
    return '\n'.join(lines)


def source_preferences(registry, routing):
    """The JSON registry owns source policy; unknown IDs are configuration errors."""
    configured = registry.get('selection_policy', {}).get('subject_routes', {})
    sources = {s['id'] for s in registry['sources']}
    require(all(k in ROUTES for k in configured), 'Unknown subject route in registry')
    require(all(isinstance(ids, list) and all(s in sources for s in ids) for ids in configured.values()),
            'Unknown source in subject route registry')
    keys = [routing['primary']] + routing.get('secondary', [])
    return list(dict.fromkeys(s for key in keys for s in configured.get(key, [])))
