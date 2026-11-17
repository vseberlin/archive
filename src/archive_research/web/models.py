from django.db import models
from django.urls import reverse


class ResearchCase(models.Model):
    """Disposable browse projection; evidence.json and assertions.jsonl are authority."""
    key = models.CharField(max_length=100, primary_key=True)
    title = models.CharField(max_length=200)
    revision = models.CharField(max_length=64)
    payload = models.JSONField()

    def __str__(self):
        return self.title

    def get_absolute_url(self):
        return reverse('case', args=[self.key])
