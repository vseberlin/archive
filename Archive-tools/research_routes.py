"""Shared research guidance and editable routing; never catalogue identification."""
import re
from archive_common import require

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
