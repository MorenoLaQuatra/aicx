class AicxError(Exception):
    """An expected, user-facing aicx error."""


class RpcError(AicxError):
    """An error returned by a provider RPC endpoint."""

