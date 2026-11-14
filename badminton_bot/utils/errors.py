"""Describe exceptions without leaking what they carry."""


def describe_error(error: BaseException) -> str:
    """Describe an error for logs and the results table.

    Never format errors with repr() for this: an aiohttp response error's repr includes
    the full request headers, login cookie included, and log files are sent to
    the author when something goes wrong.

    Args:
        error (BaseException): the error to describe.

    Returns:
        str: the error's type name, followed by its message when it has one.
    """
    message = str(error)
    name = type(error).__name__
    return f"{name}: {message}" if message else name
