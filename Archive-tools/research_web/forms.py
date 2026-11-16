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


class ReviewForm(forms.Form):
    revision = forms.CharField(widget=forms.HiddenInput, max_length=64)
    value = forms.CharField(label='Assertion', max_length=TEXT_LIMITS['value'])
    scope = forms.CharField(max_length=TEXT_LIMITS['scope'], widget=forms.Textarea(attrs={'rows': 2}))
    uncertainty = forms.CharField(max_length=TEXT_LIMITS['uncertainty'], widget=forms.Textarea(attrs={'rows': 2}))
    review = forms.ChoiceField(label='Review outcome', choices=[('', 'Choose an outcome')] + [(s, s.title()) for s in REVIEW_STATES])
    citation_keys = forms.MultipleChoiceField(label='Supporting citations', widget=forms.CheckboxSelectMultiple)
    reason = forms.CharField(label='Reason for this review or correction', max_length=TEXT_LIMITS['reason'],
                             widget=forms.Textarea(attrs={'rows': 3}),
                             help_text='Explain what the evidence supports and why you are recording this revision.')

    def __init__(self, *args, views, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['citation_keys'].choices = [(v['key'], v['label']) for v in views]
