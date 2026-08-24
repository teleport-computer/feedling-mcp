"""Shared APNs sound contract for regular IO notifications."""

# The iOS app owns this file in Library/Sounds. Devices without it fall back to
# the system default sound, so the payload stays compatible with older clients.
NOTIFICATION_SOUND_NAME = "io-selected.wav"
