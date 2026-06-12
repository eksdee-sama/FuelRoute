from django.db import models


class FuelStation(models.Model):
    opis_id = models.IntegerField(db_index=True)
    name = models.CharField(max_length=255)
    address = models.CharField(max_length=255)
    city = models.CharField(max_length=100)
    state = models.CharField(max_length=2, db_index=True)
    rack_id = models.IntegerField()
    retail_price = models.FloatField()
    latitude = models.FloatField(null=True, blank=True)
    longitude = models.FloatField(null=True, blank=True)
    geocoded = models.BooleanField(default=False, db_index=True)

    class Meta:
        ordering = ['state', 'city', 'retail_price']
        indexes = [
            models.Index(fields=['geocoded', 'state']),
            # Composite index for the bounding-box station query (the hot path)
            models.Index(fields=['geocoded', 'latitude', 'longitude']),
        ]

    def __str__(self):
        return f"{self.name} — {self.city}, {self.state} (${self.retail_price:.3f}/gal)"
