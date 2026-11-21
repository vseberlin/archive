from django import forms

from .records import PHYSICAL_LIMITS, PHYSICAL_RELATIONSHIPS, REVIEW_STATES, TEXT_LIMITS


class PhysicalObjectForm(forms.Form):
    revision = forms.CharField(widget=forms.HiddenInput, max_length=64)
    inventory_reference = forms.CharField(label='Inventory number / object reference', required=False,
                                         max_length=PHYSICAL_LIMITS['inventory_reference'])
    catalogue_url = forms.URLField(label='Museum catalogue link', required=False,
                                  max_length=PHYSICAL_LIMITS['catalogue_url'])
    location = forms.CharField(label='Last recorded museum location', required=False,
                              max_length=PHYSICAL_LIMITS['location'],
                              help_text='For example: museum, room, display case, store or shelf.')
    location_source = forms.CharField(label='Source / supplied or checked by', required=False,
                                     max_length=PHYSICAL_LIMITS['location_source'],
                                     widget=forms.Textarea(attrs={'rows': 3}),
                                     help_text='Identify the inventory record or person; explain any claimed link to the pictured object.')
    location_date = forms.DateField(label='Location recorded or checked on', required=False,
                                   input_formats=['%Y-%m-%d'], widget=forms.DateInput(attrs={'type': 'date'}))
    relationship = forms.ChoiceField(label='Relationship to the depicted object', required=False,
                                     choices=PHYSICAL_RELATIONSHIPS)
    reason = forms.CharField(label='Reason for this change', max_length=TEXT_LIMITS['reason'],
                             widget=forms.Textarea(attrs={'rows': 2}))

    def clean_location_date(self):
        value = self.cleaned_data['location_date']
        return value.isoformat() if value else ''


class ComparisonDecisionForm(forms.Form):
    revision = forms.CharField(widget=forms.HiddenInput, max_length=64)
    review = forms.ChoiceField(choices=[(s, s.title()) for s in REVIEW_STATES])
    reason = forms.CharField(required=False, max_length=TEXT_LIMITS['reason'],
                             widget=forms.Textarea(attrs={'rows': 2}), label='Note (optional)')


class ReviewForm(forms.Form):
    revision = forms.CharField(widget=forms.HiddenInput, max_length=64)
    value = forms.CharField(label='Assertion', max_length=TEXT_LIMITS['value'],
                            widget=forms.Textarea(attrs={'rows': 3}))
    scope = forms.CharField(max_length=TEXT_LIMITS['scope'], widget=forms.Textarea(attrs={'rows': 2}))
    uncertainty = forms.CharField(max_length=TEXT_LIMITS['uncertainty'], widget=forms.Textarea(attrs={'rows': 2}))
    review = forms.ChoiceField(label='Review outcome', choices=[('', 'Choose an outcome')] + [(s, s.title()) for s in REVIEW_STATES])
    citation_keys = forms.MultipleChoiceField(label='Supporting citations', widget=forms.CheckboxSelectMultiple)
    reason = forms.CharField(label='Reviewer note (optional)', required=False, max_length=TEXT_LIMITS['reason'],
                             widget=forms.Textarea(attrs={'rows': 3}),
                             help_text='Add context only when it helps. Your decision is recorded without a note.')

    def __init__(self, *args, views, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['citation_keys'].choices = [(v['key'], v['label']) for v in views]


class PersonAttributionForm(forms.Form):
    revision = forms.CharField(widget=forms.HiddenInput, max_length=64)
    attribution_key = forms.CharField(widget=forms.HiddenInput, max_length=100, required=False)
    target_view_key = forms.ChoiceField(widget=forms.HiddenInput)
    position = forms.CharField(label='Person’s position in the displayed photograph', max_length=200,
                              help_text='For example: second from the left in the front row. Use the displayed orientation.')
    candidate_key = forms.ChoiceField(label='Your proposed name', required=False)
    reason = forms.CharField(label='Why you make this attribution', max_length=3000,
                             widget=forms.Textarea(attrs={'rows': 3}),
                             help_text='Record your own evidence and reasoning separately from the documentary candidate suggestion.')
    uncertainty = forms.CharField(label='Uncertainty or conflicting evidence', max_length=1000,
                                  widget=forms.Textarea(attrs={'rows': 2}))
    citation_keys = forms.MultipleChoiceField(label='Additional supporting sources', widget=forms.CheckboxSelectMultiple)
    review = forms.ChoiceField(label='Attribution outcome', choices=[('', 'Choose an outcome'),
                              ('accepted', 'Attribute this person'), ('rejected', 'Reject this attribution'),
                              ('unresolved', 'Leave unresolved')])

    def __init__(self, *args, case, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['target_view_key'].choices = [(v['key'], v['label']) for v in case['views']]
        self.fields['citation_keys'].choices = self.fields['target_view_key'].choices
        self.fields['candidate_key'].choices = [('', 'Unnamed / unresolved')] + [
            (p['key'], p['name']) for p in sorted(case.get('documentary_people', []), key=lambda p: p['name'])]

    def clean(self):
        data = super().clean()
        if data.get('review') in ('accepted', 'rejected') and not data.get('candidate_key'):
            self.add_error('candidate_key', 'Select a documented candidate, or leave the person unresolved.')
        return data
