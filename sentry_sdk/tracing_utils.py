import re
import contextlib
import json
import math

from numbers import Real

import sentry_sdk

from sentry_sdk.utils import (
    capture_internal_exceptions,
    Dsn,
    logger,
    to_base64,
    to_string,
)
from sentry_sdk._compat import PY2
from sentry_sdk._types import MYPY

if PY2:
    from collections import Mapping
else:
    from collections.abc import Mapping

if MYPY:
    import typing

    from typing import Generator
    from typing import Optional
    from typing import Any
    from typing import Union

    from sentry_sdk.tracing import Span, Transaction


SENTRY_TRACE_REGEX = re.compile(
    "^[ \t]*"  # whitespace
    "([0-9a-f]{32})?"  # trace_id
    "-?([0-9a-f]{16})?"  # span_id
    "-?([01])?"  # sampled
    "[ \t]*$"  # whitespace
)

# This is a normal base64 regex, modified to reflect that fact that we strip the
# trailing = or == off
base64_stripped = (
    # any of the characters in the base64 "alphabet", in multiples of 4
    "([a-zA-Z0-9+/]{4})*"
    # either nothing or 2 or 3 base64-alphabet characters (see
    # https://en.wikipedia.org/wiki/Base64#Decoding_Base64_without_padding for
    # why there's never only 1 extra character)
    "([a-zA-Z0-9+/]{2,3})?"
)

# comma-delimited list of entries of the form `xxx=yyy`
tracestate_entry = "[^=]+=[^=]+"
TRACESTATE_ENTRIES_REGEX = re.compile(
    # one or more xxxxx=yyyy entries
    "^({te})+"
    # each entry except the last must be followed by a comma
    "(,|$)".format(te=tracestate_entry)
)

# this doesn't check that the value is valid, just that there's something there
# of the form `sentry=xxxx`
SENTRY_TRACESTATE_ENTRY_REGEX = re.compile(
    # either sentry is the first entry or there's stuff immediately before it,
    # ending in a commma (this prevents matching something like `coolsentry=xxx`)
    "(?:^|.+,)"
    # sentry's part
    "(sentry=[^,]*)"
    # either there's another vendor's entry or we end
    "(?:,|$)"
)


class EnvironHeaders(Mapping):  # type: ignore
    def __init__(
        self,
        environ,  # type: typing.Mapping[str, str]
        prefix="HTTP_",  # type: str
    ):
        # type: (...) -> None
        self.environ = environ
        self.prefix = prefix

    def __getitem__(self, key):
        # type: (str) -> Optional[Any]
        return self.environ[self.prefix + key.replace("-", "_").upper()]

    def __len__(self):
        # type: () -> int
        return sum(1 for _ in iter(self))

    def __iter__(self):
        # type: () -> Generator[str, None, None]
        for k in self.environ:
            if not isinstance(k, str):
                continue

            k = k.replace("-", "_").upper()
            if not k.startswith(self.prefix):
                continue

            yield k[len(self.prefix) :]


def is_valid_sample_rate(rate):
    # type: (Any) -> bool
    """
    Checks the given sample rate to make sure it is valid type and value (a
    boolean or a number between 0 and 1, inclusive).
    """

    # both booleans and NaN are instances of Real, so a) checking for Real
    # checks for the possibility of a boolean also, and b) we have to check
    # separately for NaN
    if not isinstance(rate, Real) or math.isnan(rate):
        logger.warning(
            "[Tracing] Given sample rate is invalid. Sample rate must be a boolean or a number between 0 and 1. Got {rate} of type {type}.".format(
                rate=rate, type=type(rate)
            )
        )
        return False

    # in case rate is a boolean, it will get cast to 1 if it's True and 0 if it's False
    rate = float(rate)
    if rate < 0 or rate > 1:
        logger.warning(
            "[Tracing] Given sample rate is invalid. Sample rate must be between 0 and 1. Got {rate}.".format(
                rate=rate
            )
        )
        return False

    return True


@contextlib.contextmanager
def record_sql_queries(
    hub,  # type: sentry_sdk.Hub
    cursor,  # type: Any
    query,  # type: Any
    params_list,  # type:  Any
    paramstyle,  # type: Optional[str]
    executemany,  # type: bool
):
    # type: (...) -> Generator[Span, None, None]

    # TODO: Bring back capturing of params by default
    if hub.client and hub.client.options["_experiments"].get(
        "record_sql_params", False
    ):
        if not params_list or params_list == [None]:
            params_list = None

        if paramstyle == "pyformat":
            paramstyle = "format"
    else:
        params_list = None
        paramstyle = None

    query = _format_sql(cursor, query)

    data = {}
    if params_list is not None:
        data["db.params"] = params_list
    if paramstyle is not None:
        data["db.paramstyle"] = paramstyle
    if executemany:
        data["db.executemany"] = True

    with capture_internal_exceptions():
        hub.add_breadcrumb(message=query, category="query", data=data)

    with hub.start_span(op="db", description=query) as span:
        for k, v in data.items():
            span.set_data(k, v)
        yield span


def maybe_create_breadcrumbs_from_span(hub, span):
    # type: (sentry_sdk.Hub, Span) -> None
    if span.op == "redis":
        hub.add_breadcrumb(
            message=span.description, type="redis", category="redis", data=span._tags
        )
    elif span.op == "http":
        hub.add_breadcrumb(type="http", category="httplib", data=span._data)
    elif span.op == "subprocess":
        hub.add_breadcrumb(
            type="subprocess",
            category="subprocess",
            message=span.description,
            data=span._data,
        )


def extract_sentrytrace_data(header):
    # type: (Optional[str]) -> typing.Mapping[str, Union[Optional[str], Optional[bool]]]

    """
    Given a `sentry-trace` header string, return a dictionary of data.
    """
    trace_id = parent_span_id = parent_sampled = None

    if header:
        if header.startswith("00-") and header.endswith("-00"):
            header = header[3:-3]

        match = SENTRY_TRACE_REGEX.match(header)

        if match:
            trace_id, parent_span_id, sampled_str = match.groups()

            if trace_id:
                trace_id = "{:032x}".format(int(trace_id, 16))
            if parent_span_id:
                parent_span_id = "{:016x}".format(int(parent_span_id, 16))
            if sampled_str:
                parent_sampled = sampled_str != "0"

    return {
        "trace_id": trace_id,
        "parent_span_id": parent_span_id,
        "parent_sampled": parent_sampled,
    }


def extract_tracestate_data(header):
    # type: (Optional[str]) -> typing.Mapping[str, Optional[str]]
    """
    Extracts the sentry tracestate value and any third-party data from the given
    tracestate header, returning a dictionary of data.
    """
    sentry_value = third_party = None
    before = after = ""

    if header:
        # find sentry's entry, if any
        sentry_match = SENTRY_TRACESTATE_ENTRY_REGEX.search(header)

        if sentry_match:
            sentry_entry = sentry_match.group(1)

            # we have to strip them after the split so we don't end up with
            # `xxx=yyy,,zzz=qqq` (double commas) when we put them back together
            before, after = map(lambda s: s.strip(","), header.split(sentry_entry))

            # extract sentry's value from its entry and test to make sure it's
            # valid; if it isn't, discard it so that a new one will be created
            sentry_value = sentry_entry.replace("sentry=", "")
            if not re.search("^{b64}$".format(b64=base64_stripped), sentry_value):
                sentry_value = None
        else:
            after = header

        # if either part is invalid or empty, remove it before gluing them together
        third_party = (
            ",".join(filter(TRACESTATE_ENTRIES_REGEX.search, [before, after])) or None
        )

    return {"sentry_tracestate": sentry_value, "third_party_tracestate": third_party}


def compute_new_tracestate(transaction):
    # type: (Transaction) -> str
    """
    Computes a new tracestate value for the transaction.
    """
    data = {}

    client = (transaction.hub or sentry_sdk.Hub.current).client

    # if there's no client and/or no DSN, we're not sending anything anywhere,
    # so it's fine to not have any tracestate data
    if client and client.options.get("dsn"):
        options = client.options
        data = {
            "trace_id": transaction.trace_id,
            "environment": options["environment"],
            "release": options.get("release"),
            "public_key": Dsn(options["dsn"]).public_key,
        }

    tracestate_json = json.dumps(data)

    # Base64-encoded strings always come out with a length which is a multiple
    # of 4. In order to achieve this, the end is padded with one or more `=`
    # signs. Because the tracestate standard calls for using `=` signs between
    # vendor name and value (`sentry=xxx,dogsaregreat=yyy`), to avoid confusion
    # we strip the `=`
    return (to_base64(tracestate_json) or "").rstrip("=")


def _format_sql(cursor, sql):
    # type: (Any, str) -> Optional[str]

    real_sql = None

    # If we're using psycopg2, it could be that we're
    # looking at a query that uses Composed objects. Use psycopg2's mogrify
    # function to format the query. We lose per-parameter trimming but gain
    # accuracy in formatting.
    try:
        if hasattr(cursor, "mogrify"):
            real_sql = cursor.mogrify(sql)
            if isinstance(real_sql, bytes):
                real_sql = real_sql.decode(cursor.connection.encoding)
    except Exception:
        real_sql = None

    return real_sql or to_string(sql)