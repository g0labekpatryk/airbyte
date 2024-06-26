#
# Copyright (c) 2023 Airbyte, Inc., all rights reserved.
#

from abc import ABC
from datetime import datetime
from typing import Any, Iterable, List, Mapping, MutableMapping, Optional

import pendulum
import requests
from airbyte_cdk.models import SyncMode
from airbyte_cdk.sources.streams import Stream
from airbyte_cdk.sources.streams.http import HttpStream, HttpSubStream
from airbyte_cdk.sources.utils.transform import TransformConfig, TypeTransformer

from .utils import get_analytics_columns, to_datetime_str

# For Pinterest analytics streams rate limit is 300 calls per day / per user.
# once hit - response would contain `code` property with int.
MAX_RATE_LIMIT_CODE = 8


class NonJSONResponse(Exception):
    pass


class RateLimitExceeded(Exception):
    pass


class PinterestStream(HttpStream, ABC):
    url_base = "https://api.pinterest.com/v5/"
    primary_key = "id"
    data_fields = ["items"]
    raise_on_http_errors = True
    max_rate_limit_exceeded = False
    transformer = TypeTransformer(TransformConfig.DefaultSchemaNormalization)

    def __init__(self, config: Mapping[str, Any]) -> None:
        super().__init__(authenticator=config["authenticator"])
        self.config = config

    @property
    def start_date(self) -> str:
        return self.config["start_date"]

    @property
    def window_in_days(self) -> int:
        return 30  # Set window_in_days to 30 days date range

    def next_page_token(self, response: requests.Response) -> Optional[Mapping[str, Any]]:
        next_page = response.json().get("bookmark", {}) if self.data_fields else {}

        if next_page:
            return {"bookmark": next_page}

    def request_params(
        self, stream_state: Mapping[str, Any], stream_slice: Mapping[str, any] = None, next_page_token: Mapping[str, Any] = None
    ) -> MutableMapping[str, Any]:
        return next_page_token or {}

    def parse_response(self, response: requests.Response, stream_state: Mapping[str, Any], **kwargs) -> Iterable[Mapping]:
        """
        Parsing response data with respect to Rate Limits.
        """
        data = response.json()

        if not self.max_rate_limit_exceeded:
            for data_field in self.data_fields:
                data = data.get(data_field, [])

            for record in data:
                yield record

    def should_retry(self, response: requests.Response) -> bool:
        try:
            resp = response.json()
        except requests.exceptions.JSONDecodeError:
            raise NonJSONResponse(f"Received unexpected response in non json format: '{response.text}'")

        if isinstance(resp, dict):
            self.max_rate_limit_exceeded = resp.get("code", 0) == MAX_RATE_LIMIT_CODE
        # when max rate limit exceeded, we should skip the stream.
        if response.status_code == requests.codes.too_many_requests and self.max_rate_limit_exceeded:
            self.logger.error(f"For stream {self.name} Max Rate Limit exceeded.")
            setattr(self, "raise_on_http_errors", False)
        return 500 <= response.status_code < 600

    def backoff_time(self, response: requests.Response) -> Optional[float]:
        if response.status_code == requests.codes.too_many_requests:
            self.logger.error(f"For stream {self.name} rate limit exceeded.")
            sleep_time = float(response.headers.get("X-RateLimit-Reset", 0))
            if sleep_time > 600:
                raise RateLimitExceeded(
                    f"Rate limit exceeded for stream {self.name}. Waiting time is longer than 10 minutes: {sleep_time}s."
                )
            return sleep_time


class PinterestSubStream(HttpSubStream):
    def stream_slices(
        self, sync_mode: SyncMode, cursor_field: List[str] = None, stream_state: Mapping[str, Any] = None
    ) -> Iterable[Optional[Mapping[str, Any]]]:
        parent_stream_slices = self.parent.stream_slices(
            sync_mode=SyncMode.full_refresh, cursor_field=cursor_field, stream_state=stream_state
        )
        # iterate over all parent stream_slices
        for stream_slice in parent_stream_slices:
            parent_records = self.parent.read_records(sync_mode=SyncMode.full_refresh, stream_slice=stream_slice)

            # iterate over all parent records with current stream_slice
            for record in parent_records:
                yield {"parent": record, "sub_parent": stream_slice}


class IncrementalPinterestStream(PinterestStream, ABC):
    def get_updated_state(self, current_stream_state: MutableMapping[str, Any], latest_record: Mapping[str, Any]) -> Mapping[str, Any]:
        default_value = self.start_date.format("YYYY-MM-DD")
        latest_state = latest_record.get(self.cursor_field, default_value)
        current_state = current_stream_state.get(self.cursor_field, default_value)
        latest_state_is_numeric = isinstance(latest_state, int) or isinstance(latest_state, float)

        if latest_state_is_numeric and isinstance(current_state, str):
            current_state = datetime.strptime(current_state, "%Y-%m-%d").timestamp()

        return {self.cursor_field: max(latest_state, current_state)}

    def stream_slices(
        self, sync_mode: SyncMode, cursor_field: List[str] = None, stream_state: Mapping[str, Any] = None
    ) -> Iterable[Optional[Mapping[str, any]]]:
        """
        Override default stream_slices CDK method to provide date_slices as page chunks for data fetch.
        Returns list of dict, example: [{
            "start_date": "2020-01-01",
            "end_date": "2021-01-02"
            },
            {
            "start_date": "2020-01-03",
            "end_date": "2021-01-04"
            },
            ...]
        """

        start_date = self.start_date
        end_date = pendulum.now()

        # determine stream_state, if no stream_state we use start_date
        if stream_state:
            state = stream_state.get(self.cursor_field)

            state_is_timestamp = isinstance(state, int) or isinstance(state, float)
            if state_is_timestamp:
                state = str(datetime.fromtimestamp(state).date())

            start_date = pendulum.parse(state)

        # use the lowest date between start_date and self.end_date, otherwise API fails if start_date is in future
        start_date = min(start_date, end_date)
        date_slices = []

        while start_date < end_date:
            # the amount of days for each data-chunk beginning from start_date
            end_date_slice = (
                end_date if end_date.subtract(days=self.window_in_days) < start_date else start_date.add(days=self.window_in_days)
            )
            date_slices.append({"start_date": to_datetime_str(start_date), "end_date": to_datetime_str(end_date_slice)})

            # add 1 day for start next slice from next day and not duplicate data from previous slice end date.
            start_date = end_date_slice.add(days=1)

        return date_slices


class IncrementalPinterestSubStream(IncrementalPinterestStream):
    cursor_field = "updated_time"

    def __init__(self, parent: Stream, with_data_slices: bool = True, **kwargs) -> None:
        super().__init__(**kwargs)
        self.parent = parent
        self.with_data_slices = with_data_slices

    def stream_slices(
        self, sync_mode: SyncMode, cursor_field: List[str] = None, stream_state: Mapping[str, Any] = None
    ) -> Iterable[Optional[Mapping[str, Any]]]:
        date_slices = super().stream_slices(sync_mode, cursor_field, stream_state) if self.with_data_slices else [{}]
        parents_slices = PinterestSubStream.stream_slices(self, sync_mode, cursor_field, stream_state) if self.parent else [{}]

        for parents_slice in parents_slices:
            for date_slice in date_slices:
                parents_slice.update(date_slice)

                yield parents_slice


class PinterestAnalyticsStream(IncrementalPinterestSubStream):
    primary_key = None
    cursor_field = "DATE"
    data_fields = []
    granularity = "DAY"
    analytics_target_ids = None

    @staticmethod
    def lookback_date_limit_reached(response: requests.Response) -> bool:
        """
        After few consecutive requests analytics API return bad request error
        with 'You can only get data from the last 90 days' error message.
        But with next request all working good. So, we wait 1 sec and
        request again if we get this issue.
        """

        if isinstance(response.json(), dict):
            return response.json().get("code", 0) and response.status_code == 400
        return False

    def should_retry(self, response: requests.Response) -> bool:
        return super().should_retry(response) or self.lookback_date_limit_reached(response)

    def backoff_time(self, response: requests.Response) -> Optional[float]:
        if self.lookback_date_limit_reached(response):
            return 1
        return super().backoff_time(response)

    def request_params(
        self, stream_state: Mapping[str, Any], stream_slice: Mapping[str, any] = None, next_page_token: Mapping[str, Any] = None
    ) -> MutableMapping[str, Any]:
        params = super().request_params(stream_state, stream_slice, next_page_token)
        params.update(
            {
                "start_date": stream_slice["start_date"],
                "end_date": stream_slice["end_date"],
                "granularity": self.granularity,
                "columns": get_analytics_columns(),
            }
        )

        if self.analytics_target_ids:
            params.update({self.analytics_target_ids: stream_slice["parent"]["id"]})

        return params
