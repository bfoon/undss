from django.db import models
from django.contrib.auth import get_user_model

User = get_user_model()


class ParkingCard(models.Model):
    card_number = models.CharField(max_length=20, unique=True)
    owner_name = models.CharField(max_length=100)
    owner_id = models.CharField(max_length=50)
    phone = models.CharField(max_length=20)
    department = models.CharField(max_length=100)
    vehicle_make = models.CharField(max_length=50)
    vehicle_model = models.CharField(max_length=50)
    vehicle_plate = models.CharField(max_length=20)
    vehicle_color = models.CharField(max_length=30)
    issued_date = models.DateField(auto_now_add=True)
    expiry_date = models.DateField()
    is_active = models.BooleanField(default=True)
    created_by = models.ForeignKey(User, on_delete=models.CASCADE)

    def __str__(self):
        return f"{self.card_number} - {self.owner_name}"


class Vehicle(models.Model):
    VEHICLE_TYPES = [
        ('un_agency', 'UN Agency Vehicle'),
        ('staff', 'Staff Vehicle'),
        ('visitor', 'Visitor Vehicle'),
    ]

    plate_number = models.CharField(max_length=20, unique=True)
    vehicle_type = models.CharField(max_length=10, choices=VEHICLE_TYPES)
    make = models.CharField(max_length=50)
    model = models.CharField(max_length=50)
    color = models.CharField(max_length=30)
    un_agency = models.CharField(max_length=100, blank=True)  # For UN vehicles
    parking_card = models.ForeignKey(ParkingCard, on_delete=models.SET_NULL, null=True, blank=True)

    def __str__(self):
        return f"{self.plate_number} ({self.get_vehicle_type_display()})"


class VehicleMovement(models.Model):
    MOVEMENT_TYPES = [
        ('entry', 'Entry'),
        ('exit', 'Exit'),
    ]

    GATE_CHOICES = [
        ('front', 'Front Gate'),
        ('back', 'Back Gate'),
    ]

    vehicle = models.ForeignKey(Vehicle, on_delete=models.CASCADE)
    movement_type = models.CharField(max_length=5, choices=MOVEMENT_TYPES)
    gate = models.CharField(max_length=5, choices=GATE_CHOICES)
    timestamp = models.DateTimeField(auto_now_add=True)
    recorded_by = models.ForeignKey(User, on_delete=models.CASCADE)
    driver_name = models.CharField(max_length=100, blank=True)
    purpose = models.CharField(max_length=200, blank=True)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ['-timestamp']

    def __str__(self):
        return f"{self.vehicle.plate_number} - {self.movement_type} at {self.timestamp}"