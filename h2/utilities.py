# -*- coding: utf-8 -*-
"""
h2/utilities
~~~~~~~~~~~~

Utility functions that do not belong in a separate module.
"""
import collections
import re

from hpack import NeverIndexedHeaderTuple

from .exceptions import ProtocolError, FlowControlError

UPPER_RE = re.compile(b"[A-Z]")

# A set of headers that are hop-by-hop or connection-specific and thus
# forbidden in HTTP/2. This list comes from RFC 7540 § 8.1.2.2.
CONNECTION_HEADERS = {
    b'connection',
    b'proxy-connection',
    b'keep-alive',
    b'transfer-encoding',
    b'upgrade',
}


_ALLOWED_PSEUDO_HEADER_FIELDS = {
    b':method',
    b':scheme',
    b':authority',
    b':path',
    b':status',
}


_SECURE_HEADERS = frozenset([
    # May have basic credentials which are vulnerable to dictionary attacks.
    b'authorization', u'authorization',
    b'proxy-authorization', u'proxy-authorization',
])


def secure_headers(headers):
    """
    Certain headers are at risk of being attacked during the header compression
    phase, and so need to be kept out of header compression contexts. This
    function automatically transforms certain specific headers into HPACK
    never-indexed fields to ensure they don't get added to header compression
    contexts.

    This function currently implements two rules:

    - 'authorization' and 'proxy-authorization' fields are automatically made
      never-indexed.
    - Any 'cookie' header field shorter than 20 bytes long is made
      never-indexed.

    These fields are the most at-risk. These rules are inspired by Firefox
    and nghttp2.
    """
    for header in headers:
        if header[0] in _SECURE_HEADERS:
            yield NeverIndexedHeaderTuple(*header)
        elif header[0] in (b'cookie', u'cookie') and len(header[1]) < 20:
            yield NeverIndexedHeaderTuple(*header)
        else:
            yield header


def is_informational_response(headers):
    """
    Searches a header block for a :status header to confirm that a given
    collection of headers are an informational response. Assumes the header
    block is well formed: that is, that the HTTP/2 special headers are first
    in the block, and so that it can stop looking when it finds the first
    header field whose name does not begin with a colon.

    :param headers: The HTTP/2 header block.
    :returns: A boolean indicating if this is an informational response.
    """
    for n, v in headers:
        if isinstance(n, bytes):
            sigil = b':'
            status = b':status'
            informational_start = b'1'
        else:
            sigil = u':'
            status = u':status'
            informational_start = u'1'

        # If we find a non-special header, we're done here: stop looping.
        if not n.startswith(sigil):
            return False

        # This isn't the status header, bail.
        if n != status:
            continue

        # If the first digit is a 1, we've got informational headers.
        return v.startswith(informational_start)


def guard_increment_window(current, increment):
    """
    Increments a flow control window, guarding against that window becoming too
    large.

    :param current: The current value of the flow control window.
    :param increment: The increment to apply to that window.
    :returns: The new value of the window.
    :raises: ``FlowControlError``
    """
    # The largest value the flow control window may take.
    LARGEST_FLOW_CONTROL_WINDOW = 2**31 - 1

    new_size = current + increment

    if new_size > LARGEST_FLOW_CONTROL_WINDOW:
        raise FlowControlError(
            "May not increment flow control window past %d" %
            LARGEST_FLOW_CONTROL_WINDOW
        )

    return new_size


def authority_from_headers(headers):
    """
    Given a header set, searches for the authority header and returns the
    value.

    Note that this doesn't terminate early, so should only be called if the
    headers are for a client request. Otherwise, will loop over the entire
    header set, which is potentially unwise.

    :param headers: The HTTP header set.
    :returns: The value of the authority header, or ``None``.
    :rtype: ``bytes`` or ``None``.
    """
    for n, v in headers:
        # This gets run against headers that come both from HPACK and from the
        # user, so we may have unicode floating around in here. We only want
        # bytes.
        if n in (b':authority', u':authority'):
            return v.encode('utf-8') if not isinstance(v, bytes) else v

    return None


# Flags used by the validate_headers pipeline to determine which checks
# should be applied to a given set of headers.
HeaderValidationFlags = collections.namedtuple(
    'HeaderValidationFlags',
    ['is_client', 'is_trailer']
)


def validate_headers(headers, hdr_validation_flags):
    """
    Validates a header sequence against a set of constraints from RFC 7540.

    :param hdr_validation_flags: An instance of HeaderValidationFlags.
    """
    # This validation logic is built on a sequence of generators that are
    # iterated over to provide the final header list. This reduces some of the
    # overhead of doing this checking. However, it's worth noting that this
    # checking remains somewhat expensive, and attempts should be made wherever
    # possible to reduce the time spent doing them.
    #
    # For example, we avoid tuple upacking in loops because it represents a
    # fixed cost that we don't want to spend, instead indexing into the header
    # tuples.
    headers = _reject_uppercase_header_fields(
        headers, hdr_validation_flags
    )
    headers = _reject_te(
        headers, hdr_validation_flags
    )
    headers = _reject_connection_header(
        headers, hdr_validation_flags
    )
    headers = _reject_pseudo_header_fields(
        headers, hdr_validation_flags
    )
    headers = _check_host_authority_header(
        headers, hdr_validation_flags
    )

    return list(headers)


def _reject_uppercase_header_fields(headers, hdr_validation_flags):
    """
    Raises a ProtocolError if any uppercase character is found in a header
    block.
    """
    for header in headers:
        if UPPER_RE.search(header[0]):
            raise ProtocolError(
                "Received uppercase header name %s." % header[0])
        yield header


def _reject_te(headers, hdr_validation_flags):
    """
    Raises a ProtocolError if the TE header is present in a header block and
    its value is anything other than "trailers".
    """
    for header in headers:
        if header[0] == b'te':
            if header[1].lower().strip() != b'trailers':
                raise ProtocolError(
                    "Invalid value for Transfer-Encoding header: %s" %
                    header[1]
                )

        yield header


def _reject_connection_header(headers, hdr_validation_flags):
    """
    Raises a ProtocolError if the Connection header is present in a header
    block.
    """
    for header in headers:
        if header[0] in CONNECTION_HEADERS:
            raise ProtocolError(
                "Connection-specific header field present: %s." % header[0]
            )

        yield header


def _reject_pseudo_header_fields(headers, hdr_validation_flags):
    """
    Raises a ProtocolError if duplicate pseudo-header fields are found in a
    header block or if a pseudo-header field arrives in a block after an
    ordinary header field.
    """
    seen_pseudo_header_fields = set()
    seen_regular_header = False

    for header in headers:
        if header[0].startswith(b':'):
            if header[0] in seen_pseudo_header_fields:
                raise ProtocolError(
                    "Received duplicate pseudo-header field %s" % header[0]
                )

            seen_pseudo_header_fields.add(header[0])

            if seen_regular_header:
                raise ProtocolError(
                    "Received pseudo-header field out of sequence: %s" %
                    header[0]
                )

            if header[0] not in _ALLOWED_PSEUDO_HEADER_FIELDS:
                raise ProtocolError(
                    "Received custom pseudo-header field %s" % header[0]
                )

        else:
            seen_regular_header = True

        yield header


def _check_host_authority_header(headers, hdr_validation_flags):
    """
    Raises a ProtocolError if a header block arrives that does not contain
    :authority or a Host header, or if a header block contains both fields,
    but their values do not match.
    """
    # We only expect to see :authority and Host headers on request header
    # blocks that aren't trailers, so skip this validation if we're on the
    # server side or looking at trailer blocks.
    if hdr_validation_flags.is_client or hdr_validation_flags.is_trailer:
        for header in headers:
            yield header
        return

    # We use None as a sentinel value.  Iterate over the list of headers,
    # and record the value of these headers (if present).  We don't need
    # to worry about receiving duplicate :authority headers, as this is
    # enforced by the _reject_pseudo_header_fields() pipeline.
    #
    # TODO: We should also guard against receiving duplicate Host headers.
    authority_header_val = None
    host_header_val = None

    for header in headers:
        if header[0] == b':authority':
            authority_header_val = header[1]
        elif header[0] == b'host':
            host_header_val = header[1]

        yield header

    # If we have not-None values for these variables, then we know we saw
    # the corresponding header.
    authority_present = (authority_header_val is not None)
    host_present = (host_header_val is not None)

    # It is an error for a request header block to contain neither
    # an :authority header nor a Host header.
    if not authority_present and not host_present:
        raise ProtocolError(
            "Did not receive an :authority or Host header."
        )

    # If we receive both headers, they should definitely match.
    if authority_present and host_present:
        if authority_header_val != host_header_val:
            raise ProtocolError(
                "Received mismatched :authority and Host headers: %r / %r" %
                (authority_header_val, host_header_val)
            )
