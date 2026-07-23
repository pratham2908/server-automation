"""Domain exceptions shared across services."""


class ChannelNotConnectedError(Exception):
    """A channel has no usable platform credentials.

    This is operational state, not a fault: the channel simply needs to be
    reconnected in the UI. Cron jobs catch it and skip the channel instead of
    filing an error-queue entry, which otherwise repeats on every run — a
    single token-less channel accumulated 72 identical entries before this
    existed.
    """
