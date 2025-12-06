from celery import shared_task
import logging
import time
import random

logger = logging.getLogger(__name__)


class InsuranceAPIError(Exception):
    """Error raised when the insurance provider API fails."""
    pass


@shared_task(bind=True, max_retries=5)
def notify_insurance_company_task(self, request_id: int):
    """
    Notify the insurance company about a dispatched request.

    Implements exponential backoff retry strategy:
    - First failure: retry after 2^0 = 1 second
    - Second failure: retry after 2^1 = 2 seconds
    - Third failure: 4 seconds, etc.
    """
    try:
        logger.info("Sending insurance notification. request_id=%s", request_id)

        # Mock external HTTP call
        time.sleep(1)

        # Fail with 30% probability
        if random.random() < 0.3:
            raise InsuranceAPIError("Connection timeout to insurance API")

        logger.info("Insurance notification succeeded. request_id=%s", request_id)
        return {"status": "success", "request_id": request_id}

    except InsuranceAPIError as exc:
        # Current retry count (0 for first failure)
        retries = self.request.retries
        countdown = 2 ** retries  # Exponential backoff: 1, 2, 4, 8, ...

        logger.warning(
            "Insurance API error for request_id=%s. Retry %s in %s seconds. Error: %s",
            request_id,
            retries + 1,
            countdown,
            exc,
        )

        try:
            raise self.retry(exc=exc, countdown=countdown)
        except self.MaxRetriesExceededError:
            # All retries failed: log and give up
            logger.error(
                "Max retries exceeded for request_id=%s. Last error: %s",
                request_id,
                exc,
            )
            raise
