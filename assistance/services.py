import math
from django.db import transaction
from django.utils import timezone
from django.conf import settings

from .models import AssistanceRequest, Provider, ServiceAssignment
from .tasks import notify_insurance_company_task


class AssistanceService:
    @classmethod
    def create_request(cls, data: dict) -> AssistanceRequest:
        """
        Create a new assistance request from validated input data.
        """
        return AssistanceRequest.objects.create(**data)


    # Distance / nearest logic
    @staticmethod
    def _haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        """
        Compute distance between two coordinates in kilometers using Haversine formula.
        """
        R = 6371  # Earth radius in km

        d_lat = math.radians(lat2 - lat1)
        d_lon = math.radians(lon2 - lon1)

        a = (
            math.sin(d_lat / 2) ** 2
            + math.cos(math.radians(lat1))
            * math.cos(math.radians(lat2))
            * math.sin(d_lon / 2) ** 2
        )
        c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
        return R * c



    @classmethod
    def find_nearest_available_provider(
        cls,
        lat: float,
        lon: float,
        for_update: bool = False,
        limit: int = 100,
    ) -> Provider:
        """
        Return the nearest *available* provider to the given coordinates.

        - Uses a simple bounding box + limit to reduce the candidate set.
        - When `for_update=True`, applies SELECT ... FOR UPDATE.
        """
        # 1) Base queryset: only providers that are currently available
        qs = Provider.objects.filter(is_available=True)

        # 2) Apply a coarse bounding box to discard very distant providers early (e.g. 0.2 ≈ ~22 km range)
        LAT_DELTA = getattr(settings, "ASSISTANCE_PROVIDER_LAT_DELTA", 0.2) 
        LON_DELTA = getattr(settings, "ASSISTANCE_PROVIDER_LON_DELTA", 0.2)

        qs = qs.filter(
            lat__gte=lat - LAT_DELTA,
            lat__lte=lat + LAT_DELTA,
            lon__gte=lon - LON_DELTA,
            lon__lte=lon + LON_DELTA,
        )

        # 3) Acquire row-level locks when running inside an atomic transaction
        if for_update:
            qs = qs.select_for_update(skip_locked=True)

        # 4) Limit the result set to avoid loading too many rows into memory
        providers = list(qs[:limit])

        # 5) Fallback: if the bounding box contains no candidates, look at the global pool (still limited)
        if not providers:
            fallback_qs = Provider.objects.filter(is_available=True)
            if for_update:
                fallback_qs = fallback_qs.select_for_update(skip_locked=True)

            providers = list(fallback_qs[:limit])

            if not providers:
                raise ValueError("No available provider found")

        # 6) Compute the Haversine distance in Python and pick the closest candidate
        def distance(p: Provider) -> float:
            return cls._haversine(lat, lon, p.lat, p.lon)

        return min(providers, key=distance)


    # Assignment with locking
    @classmethod
    def assign_provider_atomic(cls, request_id: int, provider_id: int | None = None) -> ServiceAssignment:
        """
        Atomically assign a provider to a request.

        - Uses SELECT ... FOR UPDATE to prevent race conditions.
        - Marks provider as busy and request as DISPATCHED in one transaction.
        - Triggers insurance notification only AFTER successful commit.
        """
        with transaction.atomic():
            # Lock request row to prevent double assignment on same request
            req = AssistanceRequest.objects.select_for_update().get(id=request_id)

            # Defensive check: request already assigned?
            if hasattr(req, "assignment"):
                raise ValueError("Request already has an assignment")

            # Select provider with row-level lock
            if provider_id is not None:
                provider_qs = Provider.objects.select_for_update().filter(
                    id=provider_id,
                    is_available=True,
                )
                provider = provider_qs.first()
                if provider is None:
                    raise ValueError("Provider is busy or does not exist")
            else:
                # Find nearest available provider with FOR UPDATE locking
                provider = cls.find_nearest_available_provider(
                    lat=req.lat,
                    lon=req.lon,
                    for_update=True,
                )

            # Mark provider as busy
            provider.is_available = False
            provider.save(update_fields=["is_available"])

            # Update request status
            req.status = "DISPATCHED"
            req.save(update_fields=["status"])

            # Create assignment record
            assignment = ServiceAssignment.objects.create(
                request=req,
                provider=provider,
            )

            # IMPORTANT:
            # Signal the external system *after* the transaction is committed
            transaction.on_commit(
                lambda: notify_insurance_company_task.delay(req.id)
            )

            return assignment

    # Request lifecycle
    @classmethod
    def complete_request(cls, request_id: int) -> None:
        """
        Mark a dispatched request as completed and free the provider.

        Only DISPATCHED requests can be completed.
        """
        with transaction.atomic():
            req = AssistanceRequest.objects.select_for_update().get(id=request_id)

            if req.status != "DISPATCHED":
                raise ValueError("Only dispatched requests can be completed")

            try:
                assignment = req.assignment
            except ServiceAssignment.DoesNotExist:
                raise ValueError("No provider assignment found for this request")

            provider = assignment.provider

            # Mark provider as available again
            provider.is_available = True
            provider.save(update_fields=["is_available"])

            # Update request status
            req.status = "COMPLETED"
            req.save(update_fields=["status"])

    @classmethod
    def cancel_request(cls, request_id: int) -> None:
        """
        Cancel a request.

        - PENDING or DISPATCHED requests may be cancelled.
        - If DISPATCHED, the provider is freed.
        """
        with transaction.atomic():
            req = AssistanceRequest.objects.select_for_update().get(id=request_id)

            if req.status == "COMPLETED":
                raise ValueError("Completed requests cannot be cancelled")

            if req.status == "CANCELLED":
                # Idempotent behaviour: already cancelled, nothing to do
                return

            if req.status == "DISPATCHED":
                try:
                    assignment = req.assignment
                except ServiceAssignment.DoesNotExist:
                    assignment = None

                if assignment is not None:
                    provider = assignment.provider
                    provider.is_available = True
                    provider.save(update_fields=["is_available"])

            req.status = "CANCELLED"
            req.save(update_fields=["status"])
