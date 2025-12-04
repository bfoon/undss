# Django management command or admin action to create room amenities
# Save this as: management/commands/create_room_amenities.py

from django.core.management.base import BaseCommand
from accounts.models import RoomAmenity

class Command(BaseCommand):
    help = 'Creates default room amenities'

    def handle(self, *args, **options):
        amenities = [
            # Presentation & Display
            {
                'code': 'projector',
                'name': 'Projector',
                'icon_class': 'bi-projector',
                'description': 'HD Projector available'
            },
            {
                'code': 'screen',
                'name': 'Projection Screen',
                'icon_class': 'bi-display',
                'description': 'Large projection screen'
            },
            {
                'code': 'tv',
                'name': 'TV Display',
                'icon_class': 'bi-tv',
                'description': 'Large TV/Monitor'
            },
            {
                'code': 'smartboard',
                'name': 'Smart Board',
                'icon_class': 'bi-card-text',
                'description': 'Interactive smart board'
            },
            {
                'code': 'whiteboard',
                'name': 'Whiteboard',
                'icon_class': 'bi-easel',
                'description': 'Dry-erase whiteboard'
            },
            {
                'code': 'flipchart',
                'name': 'Flip Chart',
                'icon_class': 'bi-journal-text',
                'description': 'Flip chart with markers'
            },

            # Communication & Connectivity
            {
                'code': 'video_conf',
                'name': 'Video Conferencing',
                'icon_class': 'bi-camera-video',
                'description': 'Video conferencing equipment (Zoom/Teams ready)'
            },
            {
                'code': 'conference_phone',
                'name': 'Conference Phone',
                'icon_class': 'bi-telephone',
                'description': 'Speakerphone for calls'
            },
            {
                'code': 'microphone',
                'name': 'Microphone',
                'icon_class': 'bi-mic',
                'description': 'Audio microphone system'
            },
            {
                'code': 'wifi',
                'name': 'WiFi',
                'icon_class': 'bi-wifi',
                'description': 'High-speed WiFi available'
            },
            {
                'code': 'ethernet',
                'name': 'Ethernet',
                'icon_class': 'bi-ethernet',
                'description': 'Wired network connection'
            },
            {
                'code': 'hdmi',
                'name': 'HDMI Ports',
                'icon_class': 'bi-plugin',
                'description': 'HDMI connectivity'
            },

            # Furniture & Comfort
            {
                'code': 'standing_desk',
                'name': 'Standing Desk',
                'icon_class': 'bi-layout-text-window',
                'description': 'Adjustable standing desk'
            },
            {
                'code': 'ergonomic_chairs',
                'name': 'Ergonomic Chairs',
                'icon_class': 'bi-person-workspace',
                'description': 'Comfortable ergonomic seating'
            },
            {
                'code': 'round_table',
                'name': 'Round Table',
                'icon_class': 'bi-circle',
                'description': 'Round table setup'
            },
            {
                'code': 'conference_table',
                'name': 'Conference Table',
                'icon_class': 'bi-table',
                'description': 'Large conference table'
            },

            # Environment & Amenities
            {
                'code': 'air_conditioning',
                'name': 'Air Conditioning',
                'icon_class': 'bi-snow2',
                'description': 'Climate controlled room'
            },
            {
                'code': 'natural_light',
                'name': 'Natural Light',
                'icon_class': 'bi-brightness-high',
                'description': 'Windows with natural lighting'
            },
            {
                'code': 'blinds',
                'name': 'Blinds/Curtains',
                'icon_class': 'bi-window',
                'description': 'Window blinds for privacy'
            },
            {
                'code': 'soundproof',
                'name': 'Soundproof',
                'icon_class': 'bi-volume-mute',
                'description': 'Sound-isolated room'
            },

            # Accessibility & Security
            {
                'code': 'wheelchair_access',
                'name': 'Wheelchair Accessible',
                'icon_class': 'bi-person-wheelchair',
                'description': 'Wheelchair accessible'
            },
            {
                'code': 'secure_lock',
                'name': 'Secure Lock',
                'icon_class': 'bi-lock',
                'description': 'Lockable door for privacy'
            },
            {
                'code': 'key_card',
                'name': 'Key Card Access',
                'icon_class': 'bi-credit-card-2-front',
                'description': 'Key card entry system'
            },

            # Food & Beverage
            {
                'code': 'coffee',
                'name': 'Coffee/Tea',
                'icon_class': 'bi-cup-hot',
                'description': 'Coffee and tea available'
            },
            {
                'code': 'water',
                'name': 'Water',
                'icon_class': 'bi-droplet',
                'description': 'Water dispenser/bottles'
            },
            {
                'code': 'catering',
                'name': 'Catering Available',
                'icon_class': 'bi-egg-fried',
                'description': 'Catering can be arranged'
            },
            {
                'code': 'refrigerator',
                'name': 'Refrigerator',
                'icon_class': 'bi-box',
                'description': 'Mini refrigerator available'
            },

            # Technology & Equipment
            {
                'code': 'laptop',
                'name': 'Laptop Available',
                'icon_class': 'bi-laptop',
                'description': 'Laptop provided'
            },
            {
                'code': 'printer',
                'name': 'Printer',
                'icon_class': 'bi-printer',
                'description': 'Printer/Scanner available'
            },
            {
                'code': 'power_outlets',
                'name': 'Power Outlets',
                'icon_class': 'bi-lightning-charge',
                'description': 'Multiple power outlets'
            },
            {
                'code': 'usb_charging',
                'name': 'USB Charging',
                'icon_class': 'bi-battery-charging',
                'description': 'USB charging ports'
            },
            {
                'code': 'recording',
                'name': 'Recording Equipment',
                'icon_class': 'bi-record-circle',
                'description': 'Audio/video recording available'
            },

            # Special Purpose
            {
                'code': 'library',
                'name': 'Library Resources',
                'icon_class': 'bi-book',
                'description': 'Books and reference materials'
            },
            {
                'code': 'study_booths',
                'name': 'Study Booths',
                'icon_class': 'bi-archive',
                'description': 'Individual study booths'
            },
            {
                'code': 'collaboration',
                'name': 'Collaboration Space',
                'icon_class': 'bi-people',
                'description': 'Designed for teamwork'
            },
            {
                'code': 'quiet_zone',
                'name': 'Quiet Zone',
                'icon_class': 'bi-volume-off',
                'description': 'Quiet working environment'
            },
            {
                'code': 'parking',
                'name': 'Parking Available',
                'icon_class': 'bi-car-front',
                'description': 'Parking nearby'
            },
        ]

        created_count = 0
        updated_count = 0

        for amenity_data in amenities:
            amenity, created = RoomAmenity.objects.update_or_create(
                code=amenity_data['code'],
                defaults={
                    'name': amenity_data['name'],
                    'icon_class': amenity_data['icon_class'],
                    'description': amenity_data['description'],
                    'is_active': True,
                }
            )
            if created:
                created_count += 1
                self.stdout.write(
                    self.style.SUCCESS(f'✓ Created: {amenity.name}')
                )
            else:
                updated_count += 1
                self.stdout.write(
                    self.style.WARNING(f'↻ Updated: {amenity.name}')
                )

        self.stdout.write(
            self.style.SUCCESS(
                f'\n✓ Done! Created {created_count} and updated {updated_count} amenities.'
            )
        )