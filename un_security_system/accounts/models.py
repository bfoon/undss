from django.contrib.auth.models import AbstractUser
from django.db import models

class Agency(models.Model):
    name = models.CharField(max_length=120, unique=True)
    code = models.CharField(max_length=20, unique=True, help_text="Short code e.g. UNDP, UNICEF")
    def __str__(self):
        return self.code or self.name

class User(AbstractUser):
    ROLE_CHOICES = [
        ('requester', 'Requester (Staff)'),  # <-- NEW
        ('data_entry', 'Data Entry (Security Guard)'),
        ('lsa', 'Local Security Associate'),
        ('soc', 'Security Operations Center'),
        ('reception', 'Receptionist'),
        ('registry', 'Registry'),
        ('ict_focal', 'ICT Focal Point'),
    ]

    role = models.CharField(max_length=20, choices=ROLE_CHOICES, default='requester')
    phone = models.CharField(max_length=20, blank=True)
    employee_id = models.CharField(max_length=20, unique=True, blank=True, null=True)
    agency = models.ForeignKey(Agency, on_delete=models.SET_NULL, null=True, blank=True, related_name="users")

    def __str__(self):
        return f"{self.username} ({self.get_role_display()})"


class SecurityIncident(models.Model):
    SEVERITY_CHOICES = [
        ('low', 'Low'),
        ('medium', 'Medium'),
        ('high', 'High'),
        ('critical', 'Critical'),
    ]

    reported_by = models.ForeignKey(User, on_delete=models.CASCADE)
    title = models.CharField(max_length=200)
    description = models.TextField()
    severity = models.CharField(max_length=10, choices=SEVERITY_CHOICES)
    location = models.CharField(max_length=100)
    reported_at = models.DateTimeField(auto_now_add=True)
    resolved = models.BooleanField(default=False)
    resolved_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True,
                                    related_name='resolved_incidents')
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-reported_at']

    def __str__(self):
        return f"{self.title} - {self.severity}"