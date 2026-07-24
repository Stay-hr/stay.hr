from __future__ import annotations

from django import forms

from apps.properties.guest_info import (
    merge_parking_into_guest_info,
    merge_wifi_into_guest_info,
    parking_facts_from_guest_info,
    wifi_facts_from_guest_info,
)
from apps.properties.models import Property


class PropertyAdminForm(forms.ModelForm):
    wifi_ssid = forms.CharField(
        label="WiFi mreža (SSID)",
        required=False,
        max_length=128,
        help_text="Prikazuje se gostu u porukama nakon check-in-a.",
    )
    wifi_password = forms.CharField(
        label="WiFi lozinka",
        required=False,
        max_length=128,
        help_text="Prikazuje se gostu u porukama nakon check-in-a.",
    )
    parking_has_private = forms.BooleanField(
        label="Privatni parking na objektu",
        required=False,
        initial=False,
    )
    parking_zone_label = forms.CharField(
        label="Parking zona",
        required=False,
        max_length=255,
        help_text="Općinska / lokalna zona parkiranja (slobodan tekst).",
    )
    parking_price_per_day = forms.DecimalField(
        label="Cijena parkiranja / dan",
        required=False,
        max_digits=10,
        decimal_places=2,
        help_text="0 = besplatno.",
    )
    parking_currency = forms.CharField(
        label="Valuta parkinga",
        required=False,
        max_length=3,
        initial="EUR",
    )
    parking_price_notes = forms.CharField(
        label="Napomena cijene",
        required=False,
        max_length=255,
        help_text="Npr. ovisno o sezoni.",
    )
    parking_reservation_required = forms.BooleanField(
        label="Rezervacija parkinga potrebna",
        required=False,
        initial=False,
    )
    parking_ev_charging = forms.BooleanField(
        label="EV punjenje dostupno",
        required=False,
        initial=False,
    )
    parking_large_vehicles_allowed = forms.BooleanField(
        label="Velika vozila / kombiji dozvoljeni",
        required=False,
        initial=True,
    )
    parking_custom_hr = forms.CharField(
        label="Dodatni opis parkinga (HR)",
        required=False,
        widget=forms.Textarea(attrs={"rows": 3}),
    )
    parking_custom_en = forms.CharField(
        label="Dodatni opis parkinga (EN)",
        required=False,
        widget=forms.Textarea(attrs={"rows": 3}),
    )

    class Meta:
        model = Property
        exclude = ("settings_version",)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance and self.instance.pk:
            ssid, password = wifi_facts_from_guest_info(self.instance.guest_info)
            self.fields["wifi_ssid"].initial = ssid
            self.fields["wifi_password"].initial = password
            parking = parking_facts_from_guest_info(self.instance.guest_info)
            self.fields["parking_has_private"].initial = bool(parking.get("has_private"))
            self.fields["parking_zone_label"].initial = parking.get("zone_label") or ""
            if parking.get("price_per_day") is not None:
                self.fields["parking_price_per_day"].initial = parking.get("price_per_day")
            self.fields["parking_currency"].initial = parking.get("currency") or "EUR"
            self.fields["parking_price_notes"].initial = parking.get("price_notes") or ""
            self.fields["parking_reservation_required"].initial = bool(
                parking.get("reservation_required")
            )
            self.fields["parking_ev_charging"].initial = bool(parking.get("ev_charging"))
            if parking.get("large_vehicles_allowed") is not None:
                self.fields["parking_large_vehicles_allowed"].initial = bool(
                    parking.get("large_vehicles_allowed")
                )
            custom = parking.get("custom") if isinstance(parking.get("custom"), dict) else {}
            self.fields["parking_custom_hr"].initial = custom.get("hr") or ""
            self.fields["parking_custom_en"].initial = custom.get("en") or ""

    def save(self, commit=True):
        instance = super().save(commit=False)
        instance.guest_info = merge_wifi_into_guest_info(
            instance.guest_info,
            ssid=self.cleaned_data.get("wifi_ssid") or "",
            password=self.cleaned_data.get("wifi_password") or "",
        )
        price = self.cleaned_data.get("parking_price_per_day")
        instance.guest_info = merge_parking_into_guest_info(
            instance.guest_info,
            has_private=bool(self.cleaned_data.get("parking_has_private")),
            zone_label=self.cleaned_data.get("parking_zone_label") or "",
            price_per_day=price,
            currency=self.cleaned_data.get("parking_currency") or "EUR",
            price_notes=self.cleaned_data.get("parking_price_notes") or "",
            reservation_required=bool(self.cleaned_data.get("parking_reservation_required")),
            ev_charging=bool(self.cleaned_data.get("parking_ev_charging")),
            large_vehicles_allowed=bool(self.cleaned_data.get("parking_large_vehicles_allowed")),
            custom_hr=self.cleaned_data.get("parking_custom_hr") or "",
            custom_en=self.cleaned_data.get("parking_custom_en") or "",
        )
        if commit:
            instance.save()
            self.save_m2m()
        return instance
