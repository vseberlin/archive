import unittest
from research_routes import classify, routing_for, guidance, validate_override, source_preferences


class RoutingTests(unittest.TestCase):
    def test_source_preferences_are_owned_by_json(self):
        registry={'sources':[{'id':'manuals'},{'id':'buildings'}], 'selection_policy':{
            'subject_routes':{'machinery_industry':['manuals'],'buildings_places':['buildings']}}}
        route={'primary':'machinery_industry','secondary':['buildings_places']}
        self.assertEqual(source_preferences(registry,route),['manuals','buildings'])
        registry['selection_policy']['subject_routes']['machinery_industry']=['buildings']
        self.assertEqual(source_preferences(registry,route),['buildings'])
        registry['selection_policy']['subject_routes']['machinery_industry']=['missing']
        with self.assertRaisesRegex(ValueError,'Unknown source'):source_preferences(registry,route)

    def test_mixed_subjects_choose_diagnostic_anchor(self):
        cases=[('Fabrikhof mit Schornstein','buildings_places'),
               ('Beschäftigte zwischen Maschinenreihen','machinery_industry'),
               ('Personengruppe an einer Bärenskulptur','objects_art'),
               ('Menschen bei einem Festumzug','events_celebrations'),
               ('Kinder beim gemeinsamen Essen','people_activity'),
               ('Frau mit Ehrenurkunde','documents_signs'),('Unbekannt','unresolved')]
        for title,want in cases:
            with self.subTest(title=title): self.assertEqual(classify(title)['primary'],want)
        self.assertIn('people_activity',classify(cases[1][0])['secondary'])

    def test_override_is_separate_from_identity_and_can_be_cleared(self):
        record={'result':{'title':{'value':'Gebäude mit Plakat'}}}
        decision={'metadata_proposal':{'research_routing':{'primary':'events_celebrations','secondary':['documents_signs']}}}
        chosen=routing_for(record,decision)
        self.assertEqual(chosen['primary'],'events_celebrations');self.assertTrue(chosen['reviewer_override'])
        self.assertEqual(routing_for(record)['primary'],'buildings_places')
        self.assertIn('newspapers',guidance(chosen))

    def test_invalid_overrides_rejected(self):
        for value in [{'primary':'guess','secondary':[]},{'primary':'people_activity','secondary':['people_activity']},
                      {'primary':'people_activity','secondary':['objects_art']*2}]:
            with self.assertRaises(ValueError): validate_override(value)

    def test_low_confidence_remains_revisable(self):
        route=classify('Gebäude und Skulptur')
        self.assertEqual(route['confidence'],'low')
        self.assertIn('select a better route',guidance(route))

    def test_visual_regrouping_used_unless_reviewer_overrode(self):
        record={'result':{'title':{'value':'Gebäude'}},'reference_check':{'research_routing':
            {'primary':'documents_signs','secondary':[],'confidence':'high','reason':'Legible poster'}}}
        self.assertEqual(routing_for(record)['primary'],'documents_signs')
